# Building the chaosRouter native engine (Rust)

Requires a Rust toolchain + a C linker. On this machine we use the GNU
toolchain with a portable MinGW-w64 (no admin, no multi-GB MSVC install).

## One-time setup
1. Install rustup (https://win.rustup.rs) — MSVC or GNU host.
2. `rustup toolchain install stable-x86_64-pc-windows-gnu`
3. `rustup default stable-x86_64-pc-windows-gnu`
4. Portable MinGW-w64 (winlibs UCRT build) extracted somewhere, its
   `bin/` on PATH.
5. `pip install maturin`

## Build
From a shell WITHOUT git-bash's coreutils `link` shadowing MSVC (use
PowerShell), with cargo + mingw64\bin on PATH:

    maturin develop --release

This compiles `chaosrouter_rs` and installs it into the active Python.

## Status
- geometry kernels (seg/seg, seg/poly, point-in-poly) — ported + verified
  exact vs shapely (2000+ cases). 32 real threads via rayon.
- TODO: collision index (CSR), grid/EDT/A*, parallel routing loop.
