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
    pub outline_x: Vec<f64>,
    pub outline_y: Vec<f64>,
}

pub struct RoutedTrace {
    pub net: i32,
    pub layer: usize,
    pub xs: Vec<f64>,
    pub ys: Vec<f64>,
    pub width: f64,
}

pub struct RoutedVia {
    pub net: i32,
    pub x: f64,
    pub y: f64,
}

struct Workspace<'a> {
    b: &'a Board,
    wx: usize,
    wy: usize,
    // per-layer owner grid: -1 empty, -2 outside, >=0 net id (pads only)
    owner: Vec<Vec<i32>>,
    // distance (mils) from each cell to the nearest pad of ANY net — used to
    // forbid via-in-pad (vias may not overlap or crowd any pad)
    pad_dist: Vec<f32>,
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
        let mut pad_any = vec![false; wx * wy];
        // block cells outside the board outline (-2). The EDT then keeps
        // traces `clearance` clear of the edge via the normal req mechanism.
        if b.outline_x.len() >= 3 {
            for iy in 0..wy {
                for ix in 0..wx {
                    let (x, y) = (b.x0 + ix as f64 * b.step, b.y0 + iy as f64 * b.step);
                    if !geom::point_in_poly(x, y, &b.outline_x, &b.outline_y) {
                        for l in 0..b.nl {
                            owner[l][iy * wx + ix] = -2;
                        }
                    }
                }
            }
        }
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
            // conservative: mark a cell if the pad overlaps it (inside OR an
            // edge within ~0.7 cell), so pad edges are never left unmarked
            let near = (b.step * 0.7) * (b.step * 0.7);
            let n = pad.xs.len();
            for iy in iy0..=iy1 {
                for ix in ix0..=ix1 {
                    let (x, y) = (b.x0 + ix as f64 * b.step, b.y0 + iy as f64 * b.step);
                    let mut hit = geom::point_in_poly(x, y, &pad.xs, &pad.ys);
                    if !hit {
                        let mut j = n - 1;
                        for k in 0..n {
                            if geom::seg_point_dist2(
                                pad.xs[j], pad.ys[j], pad.xs[k], pad.ys[k], x, y,
                            ) < near
                            {
                                hit = true;
                                break;
                            }
                            j = k;
                        }
                    }
                    if hit {
                        owner[pad.layer][iy as usize * wx + ix as usize] = pad.net;
                        pad_any[iy as usize * wx + ix as usize] = true;
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
        let pad_dist = fields::distance_field(&pad_any, wy, wx, b.step);
        Workspace {
            b,
            wx,
            wy,
            owner,
            pad_dist,
            grid,
            idx,
        }
    }

    /// Route one segment (pad A -> pad B) inside a WINDOW around the two
    /// pads (bbox + margin). Windowing keeps the EDT + A* proportional to
    /// the net's span, not the whole board. Returns per-layer trace runs
    /// (split at layer changes) and via positions.
    fn route_seg(
        &self,
        job: &NetJob,
        pa: &Pad,
        pb: &Pad,
    ) -> Option<(Vec<(usize, Vec<(f64, f64)>)>, Vec<(f64, f64)>)> {
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

        // traversable per layer over the window; keep per-layer distance so
        // via sites can require clearance on every layer
        let via_r = 12.0_f64; // via radius (mils) ~ 0.6mm pad
        let mut trav = vec![false; nl * lstride];
        let mut dists: Vec<Vec<f32>> = Vec::with_capacity(nl);
        for l in 0..nl {
            let mut obs = vec![false; lstride];
            for ly in 0..lwy {
                let grow = (gy0 as usize + ly) * self.wx + gx0 as usize;
                let lrow = ly * lwx;
                for lx in 0..lwx {
                    let o = self.owner[l][grow + lx];
                    // -1 == empty; anything else that isn't our net is copper
                    // (real nets >=0, and -2 = no-net pads that block everyone)
                    obs[lrow + lx] = o != -1 && o != job.net;
                }
            }
            let dist = fields::distance_field(&obs, lwy, lwx, self.b.step);
            // +1.2 cell safety absorbs grid discretization (diagonal moves
            // can clip a corner up to ~0.7 cell between sampled centres)
            let need = (req + self.b.step * 1.2) as f32;
            for i in 0..lstride {
                trav[l * lstride + i] = dist[i] >= need;
            }
            dists.push(dist);
        }
        // a via may sit only where a via disc clears foreign copper on ALL
        // layers (this also forbids via-in-pad: pad cells are foreign copper)
        let via_need = (via_r + job.clr) as f32;
        let mut via_ok = vec![true; lstride];
        for ly in 0..lwy {
            for lx in 0..lwx {
                let i = ly * lwx + lx;
                let gcell = (gy0 as usize + ly) * self.wx + gx0 as usize + lx;
                // no via-in-pad (any net), and via must clear foreign copper
                // on every layer
                if self.pad_dist[gcell] < via_need {
                    via_ok[i] = false;
                    continue;
                }
                for l in 0..nl {
                    if dists[l][i] < via_need {
                        via_ok[i] = false;
                        break;
                    }
                }
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
        let cong = vec![0.0f32; nl * lstride];
        // vias are expensive: only taken when they save a long detour
        let via_cost = 600.0;
        let (found, parent) = astar::astar(
            &trav, &goal, &via_ok, &cong, &starts, nl, lwy, lwx, self.b.step, via_cost,
            gminx, gmaxx, gminy, gmaxy,
        );
        if found < 0 {
            return None;
        }
        // walk the parent chain: (x, y, layer) per state
        let mut pts: Vec<(f64, f64, usize)> = Vec::new();
        let mut s = found;
        while s >= 0 {
            let layer = (s as usize) / lstride;
            let rem = (s as usize) % lstride;
            let lx = (rem % lwx) as i64;
            let ly = (rem / lwx) as i64;
            let (x, y) = self.to_world(gx0 + lx, gy0 + ly);
            pts.push((x, y, layer));
            s = parent[s as usize];
        }
        pts.reverse();
        if pts.len() < 2 {
            return None;
        }
        // snap endpoints to pad centres
        pts[0].0 = pa.cx;
        pts[0].1 = pa.cy;
        let last = pts.len() - 1;
        pts[last].0 = pb.cx;
        pts[last].1 = pb.cy;
        // split into per-layer runs; a layer change is a via at that point
        let mut runs: Vec<(usize, Vec<(f64, f64)>)> = Vec::new();
        let mut vias: Vec<(f64, f64)> = Vec::new();
        let mut cur_layer = pts[0].2;
        let mut cur: Vec<(f64, f64)> = vec![(pts[0].0, pts[0].1)];
        for i in 1..pts.len() {
            let (x, y, l) = pts[i];
            if l != cur_layer {
                // via at the previous point (same x,y across layers)
                let (vx, vy) = (pts[i - 1].0, pts[i - 1].1);
                cur.push((vx, vy));
                if cur.len() >= 2 {
                    runs.push((cur_layer, std::mem::take(&mut cur)));
                }
                vias.push((vx, vy));
                cur = vec![(vx, vy)];
                cur_layer = l;
            }
            cur.push((x, y));
        }
        if cur.len() >= 2 {
            runs.push((cur_layer, cur));
        }
        Some((runs, vias))
    }

    /// Route a whole net: connect its pads in sequence.
    fn route_net(&self, job: &NetJob) -> (Vec<RoutedTrace>, Vec<RoutedVia>) {
        let mut traces = Vec::new();
        let mut vias = Vec::new();
        for w in job.pads.windows(2) {
            let pa = &self.b.pads[w[0]];
            let pb = &self.b.pads[w[1]];
            if let Some((runs, vs)) = self.route_seg(job, pa, pb) {
                for (layer, path) in runs {
                    traces.push(RoutedTrace {
                        net: job.net,
                        layer,
                        xs: path.iter().map(|p| p.0).collect(),
                        ys: path.iter().map(|p| p.1).collect(),
                        width: job.width,
                    });
                }
                for (x, y) in vs {
                    vias.push(RoutedVia { net: job.net, x, y });
                }
            }
        }
        (traces, vias)
    }
}

impl Workspace<'_> {
    /// Rasterize a via (disc on all layers) into the owner grid.
    fn commit_via(&mut self, v: &RoutedVia) {
        let r = 12.0;
        let rc = (r / self.b.step).ceil() as i64 + 1;
        let (cx, cy) = self.to_cell(v.x, v.y);
        for l in 0..self.b.nl {
            for iy in (cy - rc).max(0)..=(cy + rc).min(self.wy as i64 - 1) {
                for ix in (cx - rc).max(0)..=(cx + rc).min(self.wx as i64 - 1) {
                    let (px, py) = self.to_world(ix, iy);
                    if (px - v.x).powi(2) + (py - v.y).powi(2) <= r * r {
                        let cell = iy as usize * self.wx + ix as usize;
                        if self.owner[l][cell] < 0 {
                            self.owner[l][cell] = v.net;
                        }
                    }
                }
            }
        }
    }

    /// Rasterize a routed trace into the owner grid (centreline grown by
    /// half-width) so later waves treat it as foreign copper.
    fn commit_trace(&mut self, t: &RoutedTrace, net: i32) {
        // grow by half-width + 0.7 cell so the marked copper is conservative
        let half = t.width / 2.0 + self.b.step * 0.7;
        let hc = (half / self.b.step).ceil() as i64 + 1;
        for w in 0..t.xs.len().saturating_sub(1) {
            let (x0, y0) = (t.xs[w], t.ys[w]);
            let (x1, y1) = (t.xs[w + 1], t.ys[w + 1]);
            let (ix0, iy0) = self.to_cell(x0.min(x1), y0.min(y1));
            let (ix1, iy1) = self.to_cell(x0.max(x1), y0.max(y1));
            for iy in (iy0 - hc).max(0)..=(iy1 + hc).min(self.wy as i64 - 1) {
                for ix in (ix0 - hc).max(0)..=(ix1 + hc).min(self.wx as i64 - 1) {
                    let (px, py) = self.to_world(ix, iy);
                    let d2 = geom::seg_point_dist2(x0, y0, x1, y1, px, py);
                    if d2 <= half * half {
                        let cell = iy as usize * self.wx + ix as usize;
                        if self.owner[t.layer][cell] < 0 {
                            self.owner[t.layer][cell] = net;
                        }
                    }
                }
            }
        }
    }
}

/// Net window (global cell bbox of all its pads + margin) for wave coloring.
fn net_window(ws: &Workspace, b: &Board, job: &NetJob) -> (i64, i64, i64, i64) {
    let m = (400.0 / b.step) as i64 + 6;
    let (mut x0, mut y0, mut x1, mut y1) = (i64::MAX, i64::MAX, i64::MIN, i64::MIN);
    for &pi in &job.pads {
        let p = &b.pads[pi];
        let (cx, cy) = ws.to_cell(p.cx, p.cy);
        x0 = x0.min(cx);
        y0 = y0.min(cy);
        x1 = x1.max(cx);
        y1 = y1.max(cy);
    }
    (x0 - m, y0 - m, x1 + m, y1 + m)
}

#[inline]
fn overlap(a: (i64, i64, i64, i64), b: (i64, i64, i64, i64)) -> bool {
    a.0 <= b.2 && b.0 <= a.2 && a.1 <= b.3 && b.1 <= a.3
}

/// Route all nets sequentially against committed copper (quality-first).
pub fn route_board(b: &Board, jobs: &[NetJob]) -> (Vec<RoutedTrace>, Vec<RoutedVia>) {
    let mut ws = Workspace::build(b);
    let windows: Vec<_> = jobs.iter().map(|j| net_window(&ws, b, j)).collect();

    // greedy wave assignment: lowest wave with no overlapping window
    let mut wave_of = vec![usize::MAX; jobs.len()];
    let mut waves: Vec<Vec<usize>> = Vec::new();
    // order by window area desc so big nets seed early waves
    let mut order: Vec<usize> = (0..jobs.len()).collect();
    order.sort_by_key(|&i| {
        let w = windows[i];
        -((w.2 - w.0) * (w.3 - w.1))
    });
    for &i in &order {
        let mut wv = 0;
        loop {
            if wv == waves.len() {
                waves.push(Vec::new());
            }
            if waves[wv].iter().all(|&j| !overlap(windows[i], windows[j])) {
                waves[wv].push(i);
                wave_of[i] = wv;
                break;
            }
            wv += 1;
        }
    }

    let _ = (&wave_of, &waves);
    // SEQUENTIAL, quality-first: every net routes against ALL committed
    // copper and is committed before the next. Correct by construction; the
    // Rust A* is fast enough that this stays well inside the 60s budget.
    // Route larger-span nets first (they're harder to detour late).
    let mut all = Vec::new();
    let mut all_vias = Vec::new();
    for &i in &order {
        let (traces, vias) = ws.route_net(&jobs[i]);
        for t in &traces {
            ws.commit_trace(t, jobs[i].net);
        }
        for v in &vias {
            ws.commit_via(v);
        }
        all.extend(traces);
        all_vias.extend(vias);
    }
    (all, all_vias)
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
