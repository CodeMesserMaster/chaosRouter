"""chaosRouter version, history and update-check endpoint."""

APP_NAME = "chaosRouter"
__version__ = "0.1.1"

# The update server will later live at a real location; the GUI treats a
# failed lookup as "no update information available", never as an error.
UPDATE_URL = "https://chaosrouter.org/latest.json"  # placeholder endpoint

# (version, date, [notes]) — newest first
HISTORY = [
    (
        "0.1.1",
        "2026-07-03",
        [
            "GUI polish: instant unrouted-board preview on file pick, "
            "Save result button (.ses + board image), app icon "
            "(order-out-of-chaos), Windows taskbar identity fix.",
            "Docs: Guided-Chaos Routing technology description in README "
            "and About dialog.",
        ],
    ),
    (
        "0.1.0",
        "2026-07-03",
        [
            "First complete engine: curved-trace-only routing for Specctra DSN "
            "exports (DipTrace and any DSN-capable CAD), SES re-import.",
            "100% completion on the 4-layer reference board (710/710, 0 DRC "
            "violations): grid A* with exact-geometry validation, rip-up & "
            "retry, stochastic shaker, incremental-eviction endgame with "
            "surgical edge stitching.",
            "Differential pairs routed first, coupled envelope + parallel "
            "offset curves, inner-layer preference.",
            "Trace style: neck-down in 0.1 mm steps, never thinner than the "
            "connected pad, teardrop pad/via entries holding full pad width "
            "from pad center, tangent-arc fillets (no sharp corners).",
            "Exact clearance math (true circles, board-edge clearance) "
            "matching DipTrace DRC; independent exact DRC + connectivity, "
            "dangling-end and sharp-corner checks.",
            "Multi-core routing of spatially disjoint nets, numba JIT "
            "kernels; 16-layer capable.",
        ],
    ),
]
