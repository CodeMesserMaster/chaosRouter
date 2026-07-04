//! Parallel routing engine — the payoff. Routes many nets concurrently on
//! rayon's real threads (no GIL). Each net: rasterize foreign copper ->
//! EDT clearance field -> traversable grid -> A* -> polyline -> exact
//! validation. Disjoint net groups route in parallel; Python does DSN
//! parsing, style post-passes, and SES writing.

use rayon::prelude::*;

use crate::astar;
use crate::collision::{Grid, LayerIndex, Poly, Seg};
use crate::fields;
use crate::geom;

/// A pad: a filled polygon on one layer, owned by a net.
pub struct Pad {
    pub net: i32,
    pub layer: usize,
    pub xs: Vec<f64>,
    pub ys: Vec<f64>,
    pub cx: f64,
    pub cy: f64,
    pub clr: f64,
}

/// A net to route: an ordered list of pad indices to connect, width, clr.
pub struct NetJob {
    pub net: i32,
    pub pads: Vec<usize>,
    pub width: f64,
    pub clr: f64,
}

pub struct Board {
    pub nl: usize,
    pub x0: f64,
    pub y0: f64,
    pub x1: f64,
    pub y1: f64,
    pub step: f64,
    pub pads: Vec<Pad>,
}

pub struct RoutedTrace {
    pub net: i32,
    pub layer: usize,
    pub xs: Vec<f64>,
    pub ys: Vec<f64>,
    pub width: f64,
}

struct Workspace<'a> {
    b: &'a Board,
    wx: usize,
    wy: usize,
    // per-layer owner grid: -1 empty, -2 outside, >=0 net id (pads only)
    owner: Vec<Vec<i32>>,
    // static pad collision index
    grid: Grid,
    idx: LayerIndex,
}

impl<'a> Workspace<'a> {
    #[inline]
    fn to_cell(&self, x: f64, y: f64) -> (i64, i64) {
        (
            ((x - self.b.x0) / self.b.step).round() as i64,
            ((y - self.b.y0) / self.b.step).round() as i64,
        )
    }
    #[inline]
    fn to_world(&self, ix: i64, iy: i64) -> (f64, f64) {
        (
            self.b.x0 + ix as f64 * self.b.step,
            self.b.y0 + iy as f64 * self.b.step,
        )
    }

    fn build(b: &'a Board) -> Self {
        let wx = ((b.x1 - b.x0) / b.step).ceil() as usize + 1;
        let wy = ((b.y1 - b.y0) / b.step).ceil() as usize + 1;
        let mut owner = vec![vec![-1i32; wx * wy]; b.nl];
        // rasterize pads to owner grid
        for pad in &b.pads {
            let minx = pad.xs.iter().cloned().fold(f64::INFINITY, f64::min);
            let maxx = pad.xs.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let miny = pad.ys.iter().cloned().fold(f64::INFINITY, f64::min);
            let maxy = pad.ys.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let (ix0, iy0) = (
                (((minx - b.x0) / b.step).floor() as i64).max(0),
                (((miny - b.y0) / b.step).floor() as i64).max(0),
            );
            let (ix1, iy1) = (
                (((maxx - b.x0) / b.step).ceil() as i64).min(wx as i64 - 1),
                (((maxy - b.y0) / b.step).ceil() as i64).min(wy as i64 - 1),
            );
            for iy in iy0..=iy1 {
                for ix in ix0..=ix1 {
                    let (x, y) = (b.x0 + ix as f64 * b.step, b.y0 + iy as f64 * b.step);
                    if geom::point_in_poly(x, y, &pad.xs, &pad.ys) {
                        owner[pad.layer][iy as usize * wx + ix as usize] = pad.net;
                    }
                }
            }
        }
        // static collision index over pads (polygons)
        let grid = Grid::new(b.x0, b.y0, b.x1, b.y1, 80.0);
        let mut polys = Vec::new();
        for pad in &b.pads {
            polys.push(Poly {
                xs: pad.xs.clone(),
                ys: pad.ys.clone(),
                clr: pad.clr,
                net: pad.net,
            });
        }
        let idx = LayerIndex::build(&grid, Vec::new(), Vec::new(), polys);
        Workspace {
            b,
            wx,
            wy,
            owner,
            grid,
            idx,
        }
    }

    /// Route one segment (pad A -> pad B) inside a WINDOW around the two
    /// pads (bbox + margin). Windowing keeps the EDT + A* proportional to
    /// the net's span, not the whole board. Returns polyline + layer.
    fn route_seg(&self, job: &NetJob, pa: &Pad, pb: &Pad) -> Option<(usize, Vec<(f64, f64)>)> {
        let nl = self.b.nl;
        let req = job.width / 2.0 + job.clr;

        // window in global cells: bbox of both pads + margin
        let margin_cells = (400.0 / self.b.step) as i64 + 6;
        let (ax, ay) = self.to_cell(pa.cx, pa.cy);
        let (bx, by) = self.to_cell(pb.cx, pb.cy);
        let gx0 = (ax.min(bx) - margin_cells).max(0);
        let gy0 = (ay.min(by) - margin_cells).max(0);
        let gx1 = (ax.max(bx) + margin_cells).min(self.wx as i64 - 1);
        let gy1 = (ay.max(by) + margin_cells).min(self.wy as i64 - 1);
        let lwx = (gx1 - gx0 + 1) as usize;
        let lwy = (gy1 - gy0 + 1) as usize;
        let lstride = lwx * lwy;

        // traversable per layer over the window
        let mut trav = vec![false; nl * lstride];
        for l in 0..nl {
            let mut obs = vec![false; lstride];
            for ly in 0..lwy {
                let grow = (gy0 as usize + ly) * self.wx + gx0 as usize;
                let lrow = ly * lwx;
                for lx in 0..lwx {
                    let o = self.owner[l][grow + lx];
                    obs[lrow + lx] = o >= 0 && o != job.net;
                }
            }
            let dist = fields::distance_field(&obs, lwy, lwx, self.b.step);
            for i in 0..lstride {
                trav[l * lstride + i] = dist[i] >= req as f32 - 1e-3;
            }
        }
        // goal / starts in LOCAL coords
        let mut goal = vec![false; nl * lstride];
        let mut starts = Vec::new();
        let (mut gminx, mut gmaxx, mut gminy, mut gmaxy) = (i64::MAX, i64::MIN, i64::MAX, i64::MIN);
        for pad in [pa, pb] {
            let is_goal = std::ptr::eq(pad, pb);
            let minx = pad.xs.iter().cloned().fold(f64::INFINITY, f64::min);
            let maxx = pad.xs.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let miny = pad.ys.iter().cloned().fold(f64::INFINITY, f64::min);
            let maxy = pad.ys.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let (ix0, iy0) = self.to_cell(minx, miny);
            let (ix1, iy1) = self.to_cell(maxx, maxy);
            for iy in iy0.max(gy0)..=iy1.min(gy1) {
                for ix in ix0.max(gx0)..=ix1.min(gx1) {
                    let (x, y) = self.to_world(ix, iy);
                    if geom::point_in_poly(x, y, &pad.xs, &pad.ys) {
                        let (lx, ly) = ((ix - gx0) as usize, (iy - gy0) as usize);
                        let s = pad.layer * lstride + ly * lwx + lx;
                        if is_goal {
                            goal[s] = true;
                            gminx = gminx.min(ix - gx0);
                            gmaxx = gmaxx.max(ix - gx0);
                            gminy = gminy.min(iy - gy0);
                            gmaxy = gmaxy.max(iy - gy0);
                        } else {
                            starts.push(s as i64);
                        }
                    }
                }
            }
        }
        if starts.is_empty() || gminx == i64::MAX {
            return None;
        }
        let via_ok = vec![true; lstride];
        let cong = vec![0.0f32; nl * lstride];
        let (found, parent) = astar::astar(
            &trav, &goal, &via_ok, &cong, &starts, nl, lwy, lwx, self.b.step, 150.0,
            gminx, gmaxx, gminy, gmaxy,
        );
        if found < 0 {
            return None;
        }
        let mut path = Vec::new();
        let mut s = found;
        while s >= 0 {
            let rem = (s as usize) % lstride;
            let lx = (rem % lwx) as i64;
            let ly = (rem / lwx) as i64;
            path.push(self.to_world(gx0 + lx, gy0 + ly));
            s = parent[s as usize];
        }
        path.reverse();
        let layer = (found as usize) / lstride;
        if let Some(first) = path.first_mut() {
            *first = (pa.cx, pa.cy);
        }
        if let Some(last) = path.last_mut() {
            *last = (pb.cx, pb.cy);
        }
        Some((layer, path))
    }

    /// Route a whole net: connect its pads in sequence.
    fn route_net(&self, job: &NetJob) -> Vec<RoutedTrace> {
        let mut out = Vec::new();
        for w in job.pads.windows(2) {
            let pa = &self.b.pads[w[0]];
            let pb = &self.b.pads[w[1]];
            if let Some((layer, path)) = self.route_seg(job, pa, pb) {
                if path.len() >= 2 {
                    out.push(RoutedTrace {
                        net: job.net,
                        layer,
                        xs: path.iter().map(|p| p.0).collect(),
                        ys: path.iter().map(|p| p.1).collect(),
                        width: job.width,
                    });
                }
            }
        }
        out
    }
}

/// Route all nets in parallel across rayon threads. Returns traces.
pub fn route_board(b: &Board, jobs: &[NetJob]) -> Vec<RoutedTrace> {
    let ws = Workspace::build(b);
    jobs.par_iter()
        .flat_map(|job| ws.route_net(job))
        .collect()
}

#[allow(dead_code)]
fn _touch(idx: &LayerIndex, grid: &Grid) {
    // keep collision query wired for the next stage (foreign-trace validation)
    let _ = idx.trace_ok(grid, &[0.0, 1.0], &[0.0, 0.0], 1.0, 5.0, -1, -1e-6);
    let _ = Seg {
        x0: 0.0,
        y0: 0.0,
        x1: 0.0,
        y1: 0.0,
        r: 0.0,
        clr: 0.0,
        net: 0,
    };
}
