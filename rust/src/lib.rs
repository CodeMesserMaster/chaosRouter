//! chaosRouter native engine (Rust) — PyO3 module.
//!
//! Ported from the proven Python router. Starts with the geometry kernels
//! (verified against shapely); the collision index, grid/EDT/A*, and the
//! parallel routing loop follow — Rust's real threads (rayon) parallelize
//! the stateful completion tail that CPython's GIL could not.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::prelude::*;
use rayon::prelude::*;

mod astar;
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

/// Grid A* (port of astar_kernel.astar). Returns (found_state, parent).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn route_astar<'py>(
    py: Python<'py>,
    trav: PyReadonlyArray1<bool>,
    goal: PyReadonlyArray1<bool>,
    via_ok: PyReadonlyArray1<bool>,
    cong: PyReadonlyArray1<f32>,
    starts: PyReadonlyArray1<i64>,
    nl: usize,
    wy: usize,
    wx: usize,
    step: f64,
    via_cost: f64,
    gix0: i64,
    gix1: i64,
    giy0: i64,
    giy1: i64,
) -> (i64, Bound<'py, PyArray1<i64>>) {
    let trav = trav.as_slice().unwrap();
    let goal = goal.as_slice().unwrap();
    let via_ok = via_ok.as_slice().unwrap();
    let cong = cong.as_slice().unwrap();
    let starts = starts.as_slice().unwrap();
    let (found, parent) = py.allow_threads(|| {
        astar::astar(
            trav, goal, via_ok, cong, starts, nl, wy, wx, step, via_cost,
            gix0, gix1, giy0, giy1,
        )
    });
    (found, parent.into_pyarray(py))
}

/// Run the same A* problem `repeats` times, sequentially and then across
/// rayon's real threads (GIL released). Returns (seq_secs, par_secs) —
/// proves the compute genuinely parallelizes on all cores.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn astar_bench(
    py: Python<'_>,
    trav: PyReadonlyArray1<bool>,
    goal: PyReadonlyArray1<bool>,
    via_ok: PyReadonlyArray1<bool>,
    cong: PyReadonlyArray1<f32>,
    starts: PyReadonlyArray1<i64>,
    nl: usize,
    wy: usize,
    wx: usize,
    step: f64,
    via_cost: f64,
    gix0: i64,
    gix1: i64,
    giy0: i64,
    giy1: i64,
    repeats: usize,
) -> (f64, f64) {
    let trav = trav.as_slice().unwrap().to_vec();
    let goal = goal.as_slice().unwrap().to_vec();
    let via_ok = via_ok.as_slice().unwrap().to_vec();
    let cong = cong.as_slice().unwrap().to_vec();
    let starts = starts.as_slice().unwrap().to_vec();
    py.allow_threads(|| {
        let run = || {
            astar::astar(
                &trav, &goal, &via_ok, &cong, &starts, nl, wy, wx, step,
                via_cost, gix0, gix1, giy0, giy1,
            )
        };
        let t = std::time::Instant::now();
        for _ in 0..repeats {
            std::hint::black_box(run());
        }
        let seq = t.elapsed().as_secs_f64();
        let t = std::time::Instant::now();
        (0..repeats).into_par_iter().for_each(|_| {
            std::hint::black_box(run());
        });
        let par = t.elapsed().as_secs_f64();
        (seq, par)
    })
}

#[pymodule]
fn chaosrouter_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(seg_seg_dist, m)?)?;
    m.add_function(wrap_pyfunction!(seg_point_dist, m)?)?;
    m.add_function(wrap_pyfunction!(seg_poly_dist, m)?)?;
    m.add_function(wrap_pyfunction!(point_in_poly, m)?)?;
    m.add_function(wrap_pyfunction!(num_threads, m)?)?;
    m.add_function(wrap_pyfunction!(route_astar, m)?)?;
    m.add_function(wrap_pyfunction!(astar_bench, m)?)?;
    Ok(())
}
