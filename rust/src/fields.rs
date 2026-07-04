//! Distance fields — port of chaosrouter/fields.py.
//! Felzenszwalb & Huttenlocher exact separable EDT + disk erosion.

const INF: f64 = 1e12;

/// Squared distance (in cells) from every cell to the nearest `true` cell.
/// `obstacle` is row-major [ny*nx].
pub fn edt_sq(obstacle: &[bool], ny: usize, nx: usize) -> Vec<f32> {
    let mut d = vec![0.0f64; ny * nx];
    // pass 1: 1D along columns
    for x in 0..nx {
        let mut prev: i64 = -1;
        for y in 0..ny {
            if obstacle[y * nx + x] {
                prev = y as i64;
            }
            d[y * nx + x] = if prev >= 0 {
                let dd = y as i64 - prev;
                (dd * dd) as f64
            } else {
                INF
            };
        }
        let mut nxt: i64 = -1;
        for y in (0..ny).rev() {
            if obstacle[y * nx + x] {
                nxt = y as i64;
            }
            if nxt >= 0 {
                let dd = nxt - y as i64;
                let v = (dd * dd) as f64;
                if v < d[y * nx + x] {
                    d[y * nx + x] = v;
                }
            }
        }
    }
    // pass 2: lower envelope of parabolas along rows
    let mut out = vec![0.0f32; ny * nx];
    let mut v = vec![0i64; nx];
    let mut z = vec![0.0f64; nx + 1];
    for y in 0..ny {
        let row = y * nx;
        let mut k: i64 = 0;
        v[0] = 0;
        z[0] = -INF;
        z[1] = INF;
        for q in 1..nx {
            let fq = d[row + q] + (q * q) as f64;
            let mut s = 0.0f64;
            loop {
                let p = v[k as usize];
                s = (fq - (d[row + p as usize] + (p * p) as f64))
                    / (2.0 * q as f64 - 2.0 * p as f64);
                if s <= z[k as usize] {
                    k -= 1;
                    if k < 0 {
                        break;
                    }
                } else {
                    break;
                }
            }
            k += 1;
            v[k as usize] = q as i64;
            z[k as usize] = s;
            z[k as usize + 1] = INF;
        }
        let mut k: usize = 0;
        for q in 0..nx {
            while z[k + 1] < q as f64 {
                k += 1;
            }
            let p = v[k];
            let dd = q as i64 - p;
            out[row + q] = ((dd * dd) as f64 + d[row + p as usize]) as f32;
        }
    }
    out
}

/// Distance in mils to the nearest obstacle cell.
pub fn distance_field(obstacle: &[bool], ny: usize, nx: usize, step: f64) -> Vec<f32> {
    let sq = edt_sq(obstacle, ny, nx);
    sq.iter().map(|&v| (v as f64).sqrt() as f32 * step as f32).collect()
}

/// Disk erosion: keep cells whose nearest `false` cell is farther than r.
pub fn erode_disk(mask: &[bool], ny: usize, nx: usize, r_cells: f64) -> Vec<bool> {
    let inv: Vec<bool> = mask.iter().map(|&b| !b).collect();
    let sq = edt_sq(&inv, ny, nx);
    let r2 = (r_cells * r_cells) as f32;
    sq.iter().map(|&v| v > r2).collect()
}
