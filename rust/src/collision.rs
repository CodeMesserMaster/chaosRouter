//! Copper collision index — port of chaosrouter/fastcopper.py.
//! Flat packed arrays + CSR spatial grid; the exact clearance query runs
//! the geom kernels with candidate selection from the grid. Foreign copper
//! only (own net exempt). Matches shapely exact-gap math.

use crate::geom;

#[derive(Clone, Copy)]
pub struct Seg {
    pub x0: f64,
    pub y0: f64,
    pub x1: f64,
    pub y1: f64,
    pub r: f64,
    pub clr: f64,
    pub net: i32,
}

#[derive(Clone, Copy)]
pub struct Cir {
    pub x: f64,
    pub y: f64,
    pub r: f64,
    pub clr: f64,
    pub net: i32,
}

pub struct Poly {
    pub xs: Vec<f64>,
    pub ys: Vec<f64>,
    pub clr: f64,
    pub net: i32,
}

/// CSR spatial grid: cell -> item indices.
pub struct Csr {
    pub start: Vec<usize>,
    pub items: Vec<u32>,
}

pub struct Grid {
    pub gx0: f64,
    pub gy0: f64,
    pub inv: f64,
    pub nx: usize,
    pub ny: usize,
}

impl Grid {
    pub fn new(x0: f64, y0: f64, x1: f64, y1: f64, cell: f64) -> Self {
        Grid {
            gx0: x0 - 40.0,
            gy0: y0 - 40.0,
            inv: 1.0 / cell,
            nx: ((x1 - x0 + 80.0) / cell) as usize + 2,
            ny: ((y1 - y0 + 80.0) / cell) as usize + 2,
        }
    }
    #[inline]
    fn cell_range(&self, minx: f64, miny: f64, maxx: f64, maxy: f64) -> (usize, usize, usize, usize) {
        let cxa = (((minx - self.gx0) * self.inv) as i64).clamp(0, self.nx as i64 - 1) as usize;
        let cxb = (((maxx - self.gx0) * self.inv) as i64).clamp(0, self.nx as i64 - 1) as usize;
        let cya = (((miny - self.gy0) * self.inv) as i64).clamp(0, self.ny as i64 - 1) as usize;
        let cyb = (((maxy - self.gy0) * self.inv) as i64).clamp(0, self.ny as i64 - 1) as usize;
        (cxa, cxb, cya, cyb)
    }

    fn build_csr(&self, bboxes: &[(f64, f64, f64, f64)]) -> Csr {
        let ncells = self.nx * self.ny;
        let mut counts = vec![0usize; ncells + 1];
        let mut per: Vec<Vec<usize>> = Vec::with_capacity(bboxes.len());
        for &(minx, miny, maxx, maxy) in bboxes {
            let (cxa, cxb, cya, cyb) = self.cell_range(minx, miny, maxx, maxy);
            let mut cells = Vec::new();
            for cy in cya..=cyb {
                let base = cy * self.nx;
                for cx in cxa..=cxb {
                    cells.push(base + cx);
                    counts[base + cx + 1] += 1;
                }
            }
            per.push(cells);
        }
        for i in 0..ncells {
            counts[i + 1] += counts[i];
        }
        let total = counts[ncells];
        let mut items = vec![0u32; total];
        let mut cursor = counts.clone();
        for (idx, cells) in per.iter().enumerate() {
            for &c in cells {
                items[cursor[c]] = idx as u32;
                cursor[c] += 1;
            }
        }
        Csr {
            start: counts,
            items,
        }
    }
}

/// A packed, queryable copper index for one layer.
pub struct LayerIndex {
    pub segs: Vec<Seg>,
    pub cirs: Vec<Cir>,
    pub polys: Vec<Poly>,
    pub seg_csr: Csr,
    pub cir_csr: Csr,
    pub poly_csr: Csr,
}

impl LayerIndex {
    pub fn build(grid: &Grid, segs: Vec<Seg>, cirs: Vec<Cir>, polys: Vec<Poly>) -> Self {
        let sbb: Vec<_> = segs
            .iter()
            .map(|s| {
                let m = s.r + s.clr;
                (
                    s.x0.min(s.x1) - m,
                    s.y0.min(s.y1) - m,
                    s.x0.max(s.x1) + m,
                    s.y0.max(s.y1) + m,
                )
            })
            .collect();
        let cbb: Vec<_> = cirs
            .iter()
            .map(|c| {
                let m = c.r + c.clr;
                (c.x - m, c.y - m, c.x + m, c.y + m)
            })
            .collect();
        let pbb: Vec<_> = polys
            .iter()
            .map(|p| {
                let minx = p.xs.iter().cloned().fold(f64::INFINITY, f64::min) - p.clr;
                let maxx = p.xs.iter().cloned().fold(f64::NEG_INFINITY, f64::max) + p.clr;
                let miny = p.ys.iter().cloned().fold(f64::INFINITY, f64::min) - p.clr;
                let maxy = p.ys.iter().cloned().fold(f64::NEG_INFINITY, f64::max) + p.clr;
                (minx, miny, maxx, maxy)
            })
            .collect();
        let seg_csr = grid.build_csr(&sbb);
        let cir_csr = grid.build_csr(&cbb);
        let poly_csr = grid.build_csr(&pbb);
        LayerIndex {
            segs,
            cirs,
            polys,
            seg_csr,
            cir_csr,
            poly_csr,
        }
    }

    /// True if the trace (polyline `txs`/`tys`, half-width `half`, clearance
    /// `clr`, net `own`) clears all FOREIGN copper. eps=-1e-6 matches shapely.
    pub fn trace_ok(
        &self,
        grid: &Grid,
        txs: &[f64],
        tys: &[f64],
        half: f64,
        clr: f64,
        own: i32,
        eps: f64,
    ) -> bool {
        let n = txs.len();
        if n < 2 {
            return true;
        }
        let (mut minx, mut maxx) = (txs[0], txs[0]);
        let (mut miny, mut maxy) = (tys[0], tys[0]);
        for i in 1..n {
            minx = minx.min(txs[i]);
            maxx = maxx.max(txs[i]);
            miny = miny.min(tys[i]);
            maxy = maxy.max(tys[i]);
        }
        let reach = half + clr + 3.0;
        let (cxa, cxb, cya, cyb) =
            grid.cell_range(minx - reach, miny - reach, maxx + reach, maxy + reach);
        let nt = n - 1;
        for cy in cya..=cyb {
            for cx in cxa..=cxb {
                let cell = cy * grid.nx + cx;
                // segments
                for t in self.seg_csr.start[cell]..self.seg_csr.start[cell + 1] {
                    let s = &self.segs[self.seg_csr.items[t] as usize];
                    if s.net == own {
                        continue;
                    }
                    let need = clr.max(s.clr);
                    let rr = half + s.r;
                    for i in 0..nt {
                        let d2 = geom::seg_seg_dist2(
                            txs[i], tys[i], txs[i + 1], tys[i + 1], s.x0, s.y0, s.x1, s.y1,
                        );
                        if d2.sqrt() - rr < need + eps {
                            return false;
                        }
                    }
                }
                // circles
                for t in self.cir_csr.start[cell]..self.cir_csr.start[cell + 1] {
                    let c = &self.cirs[self.cir_csr.items[t] as usize];
                    if c.net == own {
                        continue;
                    }
                    let need = clr.max(c.clr);
                    let rr = half + c.r;
                    for i in 0..nt {
                        let d2 = geom::seg_point_dist2(
                            txs[i], tys[i], txs[i + 1], tys[i + 1], c.x, c.y,
                        );
                        if d2.sqrt() - rr < need + eps {
                            return false;
                        }
                    }
                }
                // polygons
                for t in self.poly_csr.start[cell]..self.poly_csr.start[cell + 1] {
                    let p = &self.polys[self.poly_csr.items[t] as usize];
                    if p.net == own {
                        continue;
                    }
                    let need = clr.max(p.clr);
                    for i in 0..nt {
                        let d = geom::seg_poly_dist(
                            txs[i], tys[i], txs[i + 1], tys[i + 1], &p.xs, &p.ys,
                        );
                        if d - half < need + eps {
                            return false;
                        }
                    }
                }
            }
        }
        true
    }
}
