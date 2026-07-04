//! Exact clearance geometry — direct port of the verified numba kernels
//! (chaosrouter/geom_kernels.py). True-geometry distances: centerline
//! distance minus radii, matching shapely exactly.

#[inline(always)]
fn clampf(v: f64, lo: f64, hi: f64) -> f64 {
    if v < lo {
        lo
    } else if v > hi {
        hi
    } else {
        v
    }
}

/// Squared distance from point (px,py) to segment AB.
pub fn seg_point_dist2(ax: f64, ay: f64, bx: f64, by: f64, px: f64, py: f64) -> f64 {
    let dx = bx - ax;
    let dy = by - ay;
    let d2 = dx * dx + dy * dy;
    if d2 < 1e-12 {
        let ex = px - ax;
        let ey = py - ay;
        return ex * ex + ey * ey;
    }
    let t = clampf(((px - ax) * dx + (py - ay) * dy) / d2, 0.0, 1.0);
    let cx = ax + t * dx;
    let cy = ay + t * dy;
    let ex = px - cx;
    let ey = py - cy;
    ex * ex + ey * ey
}

#[inline]
fn orient(ax: f64, ay: f64, bx: f64, by: f64, cx: f64, cy: f64) -> f64 {
    (by - ay) * (cx - bx) - (bx - ax) * (cy - by)
}

fn segs_intersect(
    ax: f64, ay: f64, bx: f64, by: f64, cx: f64, cy: f64, dx: f64, dy: f64,
) -> bool {
    let d1 = orient(cx, cy, dx, dy, ax, ay);
    let d2 = orient(cx, cy, dx, dy, bx, by);
    let d3 = orient(ax, ay, bx, by, cx, cy);
    let d4 = orient(ax, ay, bx, by, dx, dy);
    ((d1 > 0.0) != (d2 > 0.0)) && ((d3 > 0.0) != (d4 > 0.0))
}

/// Squared minimum distance between segments AB and CD.
pub fn seg_seg_dist2(
    ax: f64, ay: f64, bx: f64, by: f64, cx: f64, cy: f64, dx: f64, dy: f64,
) -> f64 {
    if segs_intersect(ax, ay, bx, by, cx, cy, dx, dy) {
        return 0.0;
    }
    let mut d = seg_point_dist2(ax, ay, bx, by, cx, cy);
    d = d.min(seg_point_dist2(ax, ay, bx, by, dx, dy));
    d = d.min(seg_point_dist2(cx, cy, dx, dy, ax, ay));
    d = d.min(seg_point_dist2(cx, cy, dx, dy, bx, by));
    d
}

/// True if the point is inside the polygon (ray casting).
pub fn point_in_poly(px: f64, py: f64, poly_x: &[f64], poly_y: &[f64]) -> bool {
    let n = poly_x.len();
    let mut inside = false;
    let mut j = n - 1;
    for i in 0..n {
        let yi = poly_y[i];
        let yj = poly_y[j];
        if (yi > py) != (yj > py) {
            let xint = poly_x[i] + (py - yi) / (yj - yi) * (poly_x[j] - poly_x[i]);
            if px < xint {
                inside = !inside;
            }
        }
        j = i;
    }
    inside
}

/// Minimum distance from segment AB to a filled polygon (0 if it enters).
pub fn seg_poly_dist(
    ax: f64, ay: f64, bx: f64, by: f64, poly_x: &[f64], poly_y: &[f64],
) -> f64 {
    let n = poly_x.len();
    if point_in_poly(ax, ay, poly_x, poly_y) || point_in_poly(bx, by, poly_x, poly_y) {
        return 0.0;
    }
    let mut best = 1e30_f64;
    let mut j = n - 1;
    for i in 0..n {
        let d2 = seg_seg_dist2(ax, ay, bx, by, poly_x[j], poly_y[j], poly_x[i], poly_y[i]);
        if d2 < best {
            best = d2;
        }
        j = i;
    }
    best.sqrt()
}
