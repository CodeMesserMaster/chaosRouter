//! chaosRouter native engine (Rust) — PyO3 module.
//!
//! Ported from the proven Python router. Starts with the geometry kernels
//! (verified against shapely); the collision index, grid/EDT/A*, and the
//! parallel routing loop follow — Rust's real threads (rayon) parallelize
//! the stateful completion tail that CPython's GIL could not.

use pyo3::prelude::*;

mod geom;

/// Distance between two segments (for cross-checking against Python/shapely).
#[pyfunction]
fn seg_seg_dist(
    ax: f64, ay: f64, bx: f64, by: f64, cx: f64, cy: f64, dx: f64, dy: f64,
) -> f64 {
    geom::seg_seg_dist2(ax, ay, bx, by, cx, cy, dx, dy).sqrt()
}

/// Distance from a segment to a point.
#[pyfunction]
fn seg_point_dist(ax: f64, ay: f64, bx: f64, by: f64, px: f64, py: f64) -> f64 {
    geom::seg_point_dist2(ax, ay, bx, by, px, py).sqrt()
}

/// Distance from a segment to a filled polygon.
#[pyfunction]
fn seg_poly_dist(ax: f64, ay: f64, bx: f64, by: f64, px: Vec<f64>, py: Vec<f64>) -> f64 {
    geom::seg_poly_dist(ax, ay, bx, by, &px, &py)
}

/// Point-in-polygon test.
#[pyfunction]
fn point_in_poly(px: f64, py: f64, poly_x: Vec<f64>, poly_y: Vec<f64>) -> bool {
    geom::point_in_poly(px, py, &poly_x, &poly_y)
}

/// Report thread parallelism available to the native engine.
#[pyfunction]
fn num_threads() -> usize {
    rayon::current_num_threads()
}

#[pymodule]
fn chaosrouter_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(seg_seg_dist, m)?)?;
    m.add_function(wrap_pyfunction!(seg_point_dist, m)?)?;
    m.add_function(wrap_pyfunction!(seg_poly_dist, m)?)?;
    m.add_function(wrap_pyfunction!(point_in_poly, m)?)?;
    m.add_function(wrap_pyfunction!(num_threads, m)?)?;
    Ok(())
}
