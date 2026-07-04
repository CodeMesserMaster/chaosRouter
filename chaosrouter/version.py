"""chaosRouter version, history and update-check endpoint."""

APP_NAME = "chaosRouter"
__version__ = "0.2.11"

# Update check: the GitHub latest-release API (zero infrastructure).
# The GUI treats a failed lookup as "no update info", never as an error.
UPDATE_URL = (
    "https://api.github.com/repos/CodeMesserMaster/chaosRouter/releases/latest"
)

# (version, date, [notes]) — newest first
HISTORY = [
    (
        "0.2.11",
        "2026-07-04",
        ["Fix live-view stream parsing: buffer partial lines so a split "
         "@CLEAR marker is never missed. This was leaving the raw glowing "
         "route on screen as the 'final result' (glow smearing everywhere) "
         "instead of the clean styled redraw."],
    ),
    (
        "0.2.10",
        "2026-07-04",
        ["Finalize now guarantees all glow is removed within ~10 frames "
         "(hard cutoff) — no residual glow left when routing finishes."],
    ),
    (
        "0.2.9",
        "2026-07-04",
        [
            "Final styled result always draws as clean, glow-free traces "
            "(regardless of density or method) — fixes glow smearing on the "
            "fast Simple route. Glow is now strictly the live-routing effect.",
        ],
    ),
    (
        "0.2.8",
        "2026-07-04",
        [
            "Glow settles: tighter halos (no board-wide bloom), and on the "
            "final pass ALL glow fades out so the finished board is clean, "
            "crisp traces. Smoother 60fps with much less overdraw.",
        ],
    ),
    (
        "0.2.7",
        "2026-07-04",
        ["Board outline drawn in bright neon pink for visibility."],
    ),
    (
        "0.2.6",
        "2026-07-04",
        [
            "Board view now redraws with the FINAL styled geometry after "
            "routing — teardrop pad entries, graded neck-downs and filleted "
            "arcs (previously the live view only showed the raw route, since "
            "style passes stream with the hook off).",
            "Progress bar spans all phases: first pass fills to 40%, then the "
            "rip-up/shake/endgame tail fills to 100% as failures resolve.",
            "Higher-contrast, readable method dropdown.",
        ],
    ),
    (
        "0.2.5",
        "2026-07-04",
        [
            "Smoother, flowing glow: each trace breathes on a spatial phase "
            "offset so the glow ripples across the board instead of a "
            "synchronized blink; eased (smoothstep) fade-in, 45fps.",
            "Progress bar along the bottom, fills as connections route.",
            "Router method selectable: Guided-Chaos, PathFinder, and a new "
            "Simple (fast first-pass) mode — all share the live glow view.",
        ],
    ),
    (
        "0.2.4",
        "2026-07-04",
        [
            "Glowing live board animation: traces render as smooth bezier "
            "curves with layered neon glow that fade in as they route and "
            "breathe with an ambient pulse (old-screensaver aesthetic).",
            "Speed foundation: nogil numba geometry kernels (seg/seg, "
            "seg/poly, point-in-poly) + a fully-nogil CSR collision index "
            "verified 100% against shapely (1000+ tests) — the keystone for "
            "GIL-free parallel routing. nogil string-pull; parallel shaker.",
        ],
    ),
    (
        "0.2.3",
        "2026-07-03",
        [
            "SES import fully correct: stop redefining padstacks the CAD "
            "already knows (our library_out override was what made vias "
            "import oversized in DipTrace) — reference them by name so the "
            "CAD uses its own true sizes. The small 0.4 mm via is allowed "
            "again (no-via-in-pad stays enforced geometrically), restoring "
            "100% completion with a correct round-trip.",
            "New: --via-map OLD=NEW renames padstacks in the SES for CADs "
            "with different naming; --persist-min N keeps re-shaking with "
            "fresh seeds until 100% routed or the time budget expires.",
            "PathFinder v4: cheap negotiation vias (crossings resolve by "
            "layer change) — new PathFinder best, 704/710.",
        ],
    ),
    (
        "0.2.2",
        "2026-07-03",
        [
            "PathFinder v3: pad-escape zones get elastic capacity, "
            "width-aware corridor footprints with shared clearance, "
            "conflicted-only Gauss-Seidel renegotiation (no oscillation), "
            "plateau detection. Now matches Guided-Chaos completion on the "
            "reference board with the lowest via count of any method.",
        ],
    ),
    (
        "0.2.1",
        "2026-07-03",
        [
            "PathFinder routing method (experimental, --method pathfinder / "
            "GUI selector): FPGA-style negotiated congestion on a coarse "
            "grid — nets route through each other, contested cells charge "
            "rising present+history costs until convergence — then exact-"
            "geometry realization attracted to the negotiated corridors, "
            "with the Guided-Chaos repair ladder for residue. Guided-Chaos "
            "remains the default method.",
        ],
    ),
    (
        "0.2.0",
        "2026-07-03",
        [
            "DipTrace SES import fully solved (measured, not guessed): "
            "correct coordinate convention; neck-down preserved via the "
            "class-width import recipe (DipTrace clamps widths UP to class "
            "but keeps everything above); inPadVia padstack avoided by "
            "default (DipTrace imports it oversized); T-junctions converted "
            "to exact endpoint meetings so no phantom ratsnest remains.",
            "Live routing animation: the GUI board view is fully vector and "
            "draws copper as the routing subprocess streams it; instant "
            "unrouted preview on file pick; Cancel button; drag & drop; "
            "settings persistence.",
            "KiCad dialect groundwork: unit scaling (mil/um/mm), lowercase "
            "pcb root, length-rule scaling.",
            "Test suite (pytest, synthetic boards: units, routing, walls, "
            "diff pairs, SES) + CI test workflow on Ubuntu and Windows.",
            "True-scale board rendering (the old renderer drew copper at "
            "roughly half width and misled width judgements).",
            "Update check now queries the GitHub latest-release API.",
        ],
    ),
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
