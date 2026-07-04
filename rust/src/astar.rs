//! Grid A* — direct port of chaosrouter/astar_kernel.py `astar`.
//!
//! State = layer*(wy*wx) + iy*wx + ix. 8-connected moves + via moves across
//! all layer pairs. Octile heuristic to the goal bbox. Goals accepted on
//! ARRIVAL (target copper may sit inside a clearance field). Lazy-deletion
//! binary heap. Pure compute, no Python — routes run in parallel via rayon.

use std::cmp::Ordering;
use std::collections::BinaryHeap;

const SQRT2: f64 = std::f64::consts::SQRT_2;

struct Node {
    key: f64,
    val: usize,
}
impl PartialEq for Node {
    fn eq(&self, o: &Self) -> bool {
        self.key == o.key
    }
}
impl Eq for Node {}
impl Ord for Node {
    fn cmp(&self, o: &Self) -> Ordering {
        // min-heap: invert so the smallest key is "greatest"
        o.key.total_cmp(&self.key)
    }
}
impl PartialOrd for Node {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}

#[inline]
fn octile(ix: i64, iy: i64, gix0: i64, gix1: i64, giy0: i64, giy1: i64, step: f64) -> f64 {
    let dx = (gix0 - ix).max(ix - gix1).max(0);
    let dy = (giy0 - iy).max(iy - giy1).max(0);
    let (a, b) = if dx > dy { (dx, dy) } else { (dy, dx) };
    (a as f64 + (SQRT2 - 1.0) * b as f64) * step
}

/// Returns (found_state, parent). found = -1 if unreachable.
#[allow(clippy::too_many_arguments)]
pub fn astar(
    trav: &[bool],
    goal: &[bool],
    via_ok: &[bool],
    cong: &[f32],
    starts: &[i64],
    nl: usize,
    wy: usize,
    wx: usize,
    step: f64,
    via_cost: f64,
    gix0: i64,
    gix1: i64,
    giy0: i64,
    giy1: i64,
) -> (i64, Vec<i64>) {
    let stride = wy * wx;
    let n_states = nl * stride;
    let mut gcost = vec![f64::INFINITY; n_states];
    let mut parent = vec![-1i64; n_states];
    let mut heap: BinaryHeap<Node> = BinaryHeap::new();
    let diag = step * SQRT2;
    let wxi = wx as i64;
    let wyi = wy as i64;

    for &s in starts {
        let s = s as usize;
        if trav[s] && gcost[s] > 0.0 {
            gcost[s] = 0.0;
            let rem = (s % stride) as i64;
            let iy = rem / wxi;
            let ix = rem % wxi;
            let h = octile(ix, iy, gix0, gix1, giy0, giy1, step);
            heap.push(Node { key: h, val: s });
        }
    }

    // 8-connected move table (dx, dy, cost)
    let moves: [(i64, i64, f64); 8] = [
        (1, 0, step),
        (-1, 0, step),
        (0, 1, step),
        (0, -1, step),
        (1, 1, diag),
        (1, -1, diag),
        (-1, 1, diag),
        (-1, -1, diag),
    ];

    let mut found: i64 = -1;
    while let Some(Node { key: f, val: s }) = heap.pop() {
        let li = s / stride;
        let rem = s % stride;
        let iy = (rem / wx) as i64;
        let ix = (rem % wx) as i64;
        let g = gcost[s];
        let h = octile(ix, iy, gix0, gix1, giy0, giy1, step);
        if f > g + h + 1e-9 {
            continue; // stale
        }
        if goal[s] {
            found = s as i64;
            break;
        }
        for &(dx, dy, c) in moves.iter() {
            let nx = ix + dx;
            let ny = iy + dy;
            if nx < 0 || ny < 0 || nx >= wxi || ny >= wyi {
                continue;
            }
            let ns = (s as i64 + dy * wxi + dx) as usize;
            if goal[ns] {
                parent[ns] = s as i64;
                found = ns as i64;
                break;
            }
            if !trav[ns] {
                continue;
            }
            let ng = g + c + cong[ns] as f64;
            if ng < gcost[ns] - 1e-9 {
                gcost[ns] = ng;
                parent[ns] = s as i64;
                let nh = octile(nx, ny, gix0, gix1, giy0, giy1, step);
                heap.push(Node {
                    key: ng + nh,
                    val: ns,
                });
            }
        }
        if found >= 0 {
            break;
        }
        if via_ok[rem] {
            for lj in 0..nl {
                if lj == li {
                    continue;
                }
                let ns = lj * stride + rem;
                if goal[ns] {
                    parent[ns] = s as i64;
                    found = ns as i64;
                    break;
                }
                if !trav[ns] {
                    continue;
                }
                let ng = g + via_cost + cong[ns] as f64;
                if ng < gcost[ns] - 1e-9 {
                    gcost[ns] = ng;
                    parent[ns] = s as i64;
                    let nh = octile(ix, iy, gix0, gix1, giy0, giy1, step);
                    heap.push(Node {
                        key: ng + nh,
                        val: ns,
                    });
                }
            }
            if found >= 0 {
                break;
            }
        }
    }
    (found, parent)
}
