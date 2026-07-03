# chaosRouter

**A curved-trace PCB autorouter for Specctra DSN exports — DipTrace and any
DSN-capable CAD.**

chaosRouter routes with *curves only*: every corner is a tangent arc, every
pad entry is a teardrop, and the result re-imports into your CAD as a
standard Specctra `.ses` session file.

## Guided-Chaos Routing

The technology behind chaosRouter rests on three pillars:

1. **Optimistic grid, exact judge** — the search grid is deliberately
   permissive: a fast guide, never the truth. Every candidate trace, via
   and arc is adjudicated against exact copper geometry (true-circle
   clearance math) before acceptance. Using both worlds is why chaosRouter
   threads 0.5 mm pin fields that conservative grid routers give up on.
2. **Escalating controlled chaos** — completion comes from a ladder of
   increasingly bold perturbation, each rung transactional with exact
   rollback: deterministic rip-up with blocker attribution → Monte-Carlo
   neighborhood shaking → an incremental-eviction *endgame* that identifies
   the physical copper sealing a failing connection, moves exactly that,
   and surgically stitches the missing edge. Deterministic where possible,
   stochastic where useful, never destructive.
3. **Curvilinear copper** — curves are not a cosmetic post-filter: tangent
   arc fillets, teardrop pad/via entries holding full pad width from the
   pad center, 0.1 mm graded neck-downs — every piece exact-clearance
   verified, so the styled board is as legal as the raw one.

## Highlights

- **Curves only** — tangent-arc fillets everywhere; sharp corners are treated
  as defects and reported by the built-in geometry checker.
- **Exact-geometry engine** — the routing grid is only an optimistic guide;
  every trace, via and arc is validated against exact copper geometry
  (true-circle clearance math, board-edge clearance included) before it is
  accepted. Independent DRC + connectivity verification at the end.
- **100% completion machinery** — Steiner-tree net growth, multi-source A*
  with via moves (numba JIT), rip-up & retry with blocker attribution, a
  stochastic shaker, and an incremental-eviction *endgame* that surgically
  stitches the last stubborn connections (verified 710/710, 0 violations on
  a dense 4-layer reference design).
- **Differential pairs first** — coupled envelope routing with parallel
  offset curves, inner-layer preference, coupling verification.
- **Power-net care** — star topologies from a chosen source pin, per-class
  trace widths and via sizes, neck-down in 0.1 mm steps, and a hard rule
  that a trace is never thinner than the pad it connects to (teardrop
  entries hold full pad width from the pad center).
- **Fast** — spatially disjoint nets route in parallel across CPU cores;
  numba-compiled kernels for search and distance fields.
- **Any layer count up to 16**, any DSN-exporting CAD.

## Install

```bash
pip install -e .[gui]
```

## Use

GUI:

```bash
chaosrouter-gui
```

Pick your `.dsn`, hit **ROUTE**, inspect the board preview and statistics,
then import the written `.ses` back into your CAD.

Command line:

```bash
chaosrouter my_board.dsn --out my_board_routed --source GND=U9-2
```

## Building installers

- Windows: `build/build_windows.ps1` (PyInstaller; optional Inno Setup script
  in `build/chaosrouter.iss`)
- macOS: `build/build_macos.sh` (PyInstaller `.app` bundle + zip)

## Status

r0.1 — first public engine. Road map: PathFinder-style negotiated-congestion
routing, copper-pour/plane support, length matching, interactive editing in
the GUI.

## License

MIT — see [LICENSE](LICENSE).
