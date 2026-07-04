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
mod collision;
mod engine;
mod fields;
mod geom;

/// Route a board in parallel (rayon, no GIL). Inputs are plain Python lists:
///   pads: [(net_id:int, layer:int, xs:list[float], ys:list[float], clr:float)]
///   jobs: [(net_id:int, pad_indices:list[int], width:float, clr:float)]
/// Returns [(net_id, layer, xs, ys, width)] and (wall_secs, threads).
#[pyfunction]
#[allow(clippy::type_complexity)]
fn route_board(
    py: Python<'_>,
    nl: usize,
    x0: f64,
    y0: f64,
    x1: f64,
    y1: f64,
    step: f64,
    outline_x: Vec<f64>,
    outline_y: Vec<f64>,
    pads: Vec<(i32, usize, Vec<f64>, Vec<f64>, f64)>,
    jobs: Vec<(i32, Vec<usize>, f64, f64)>,
) -> (
    Vec<(i32, usize, Vec<f64>, Vec<f64>, f64)>,
    Vec<(i32, f64, f64)>,
    f64,
    usize,
) {
    let pads: Vec<engine::Pad> = pads
        .into_iter()
        .map(|(net, layer, xs, ys, clr)| {
            let cx = xs.iter().sum::<f64>() / xs.len().max(1) as f64;
            let cy = ys.iter().sum::<f64>() / ys.len().max(1) as f64;
            engine::Pad { net, layer, xs, ys, cx, cy, clr }
        })
        .collect();
    let jobs: Vec<engine::NetJob> = jobs
        .into_iter()
        .map(|(net, pads, width, clr)| engine::NetJob { net, pads, width, clr })
        .collect();
    let board = engine::Board {
        nl, x0, y0, x1, y1, step, pads,
        outline_x, outline_y,
    };
    let (traces, vias, secs, threads) = py.allow_threads(|| {
        let t = std::time::Instant::now();
        let (tr, vs) = engine::route_board(&board, &jobs);
        (tr, vs, t.elapsed().as_secs_f64(), rayon::current_num_threads())
    });
    let out = traces
        .into_iter()
        .map(|t| (t.net, t.layer, t.xs, t.ys, t.width))
        .collect();
    let vout = vias.into_iter().map(|v| (v.net, v.x, v.y)).collect();
    (out, vout, secs, threads)
}

/// EDT distance field (mils) — port of fields.distance_field.
#[pyfunction]
fn distance_field<'py>(
    py: Python<'py>,
    obstacle: PyReadonlyArray1<bool>,
    ny: usize,
    nx: usize,
    step: f64,
) -> Bound<'py, PyArray1<f32>> {
    let obs = obstacle.as_slice().unwrap();
    let out = py.allow_threads(|| fields::distance_field(obs, ny, nx, step));
    out.into_pyarray(py)
}

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
    m.add_function(wrap_pyfunction!(distance_field, m)?)?;
    m.add_function(wrap_pyfunction!(route_board, m)?)?;
    Ok(())
}
