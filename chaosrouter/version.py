"""chaosRouter version, history and update-check endpoint."""

APP_NAME = "chaosRouter"
__version__ = "0.2.30"

# Update check: the GitHub latest-release API (zero infrastructure).
# The GUI treats a failed lookup as "no update info", never as an error.
UPDATE_URL = (
    "https://api.github.com/repos/CodeMesserMaster/chaosRouter/releases/latest"
)

# (version, date, [notes]) — newest first
HISTORY = [
    (
        "0.2.30",
        "2026-07-07",
        [
            "WIDE-NET NECKING FORBIDDEN: a wide current trace can no longer "
            "route the whole hop at neck width (the 'narrow' fallback that let "
            "it squeeze to ~10% through a via/through-hole grid). Wide nets "
            "route full width or FAIL (red ratsnest) — never necked mid-route.",
            "Diff-pair breakout fixed: the coupled tip is often jammed <50mil "
            "from the pad with no room to drop the layer-transition via, which "
            "failed the whole pair. The breakout now walks its join point INWARD "
            "along the segment until there is via room. X500_r4: 3/3 pairs route "
            "(was 2/3 — Net_150 was empty).",
        ],
    ),
    (
        "0.2.29",
        "2026-07-07",
        [
            "Necking fix: neck-down is now gated on the CONNECTING PAD only "
            "(dropped near_pt, which was usually mid-route and re-opened necking "
            "through anything). 4g wide-necking collapsed 800->300mil, longest "
            "single neck 31mil (genuine pad-entry only).",
            "Diff-pair breakout: robust fallback (no snap, full window, cheap "
            "vias) when the primary breakout fails.",
        ],
    ),
    (
        "0.2.28",
        "2026-07-07",
        [
            "Neck-down rule: necking is now allowed ONLY within 20mil of an own "
            "pad (a taper into a fine pad), never as a mid-route shortcut "
            "through congestion. Shared constant -> applies to all four routers.",
            "Wide/current nets route FIRST (net_order) so they get clean full-"
            "width paths before signals fill the space.",
            "Free-space-guided endgame: hard traces that survive the tail are "
            "re-routed biased toward OPEN space (the distance-field map) with "
            "cheap vias, so they dive to whatever layer has room instead of "
            "being guessed at by rip-up. Only rips the failing net (can't hurt).",
        ],
    ),
    (
        "0.2.27",
        "2026-07-07",
        [
            "Plane-aware routing for ALL methods: a declared plane net's pins "
            "are now CONNECTED to the plane with a short stub + a via drop to "
            "the plane layer (through-hole pins already touch it), instead of "
            "being left unrouted. Runs before signal routing so plane vias are "
            "reserved and signals weave around them — this is the fix for fat "
            "power nets necking through congestion (they are planes now).",
            "Parse plane_layer_of (which copper layer each plane occupies).",
        ],
    ),
    (
        "0.2.26",
        "2026-07-06",
        [
            "New experimental method 'Manhattan + Fanout': after diff pairs, "
            "route each fine-pitch IC's pins as a coordinated escape bundle "
            "(straight out of the pad field to aligned breakouts, in pin order) "
            "before Manhattan routing. Helps dual-row parts (U1) but currently "
            "net-negative on QFN-style parts — experimental, needs refinement.",
            "Fix: a previous board's routing overlaid on a new board when the "
            "new route was cancelled. The fixed stats file is now deleted at "
            "route start, a DSN-mismatch guard rejects a stale stats file, and "
            "loading a board clears all stored copper.",
        ],
    ),
    (
        "0.2.25",
        "2026-07-06",
        [
            "Fix board going blank at end of route: _proc_done now reads the "
            "final stats FIRST and only clears the live copper once it has "
            "geometry to replace it with; the rebuild is exception-guarded so a "
            "redraw error can never leave a blank board. Removed the leftover "
            "debug-PNG render from finalize and the redundant double-finalize. "
            "Added a diagnostic log of the final geometry counts.",
        ],
    ),
    (
        "0.2.24",
        "2026-07-06",
        [
            "Trimmed routing methods to the three that earn their place: "
            "Guided-Chaos (default, best for normal boards), PathFinder "
            "(experimental negotiated-congestion), and Manhattan (structured, "
            "for dense boards where the others struggle). Removed Simple and "
            "Balanced from the GUI/CLI.",
        ],
    ),
    (
        "0.2.23",
        "2026-07-05",
        [
            "Neck-down CLEARANCE, not just width: a high-clearance net (e.g. a "
            "15.7mil power/HV rule) physically cannot be honored between "
            "0.65mm-pitch IC pins that are ~9mil apart, so where the trace necks "
            "down at the pins it now uses the class neck_down_gap (or board "
            "default) and returns to full clearance in the open board. Root "
            "cause of the dense-BMS wall: 91.1% -> 94.4%. Full clearance is "
            "preserved everywhere the trace is full width.",
        ],
    ),
    (
        "0.2.22",
        "2026-07-05",
        [
            "Manhattan method rebuilt around the validated direction-on-inner "
            "scheme: East-West and North-South highways on the INNER signal "
            "layers, Top/Bottom kept free for short local escapes, strict grain "
            "so a direction change forces a layer change. On the dense BMS: "
            "84.5% -> 91% with 100% direction discipline and half the vias of "
            "the naive scheme. Per-layer base cost pushes long routes to inner.",
        ],
    ),
    (
        "0.2.21",
        "2026-07-05",
        [
            "New 'Manhattan' method: structured H/V grain routing per signal "
            "layer for dense boards (first thing to beat the free-form baseline "
            "on a 16-layer BMS: 155->122 failures). Curved by the fillet pass.",
            "Plane nets (type-power layers / (plane) directives) are now "
            "excluded from routing — they're copper pours, not signal traces.",
            "Fixed a thread race in the parallel shake that crashed the tail "
            "on dense boards.",
        ],
    ),
    (
        "0.2.20",
        "2026-07-05",
        [
            "Unrouted connections now shown as bright red ratsnest lines in "
            "the final board view, for placement feedback — see where routing "
            "failed and fix placement there.",
            "Fix glow returning at the very end: the final stats-redraw path "
            "drew copper with glow (clear_copper reset final mode); it now "
            "does a single clean final-mode redraw. Zero glow on finish.",
        ],
    ),
    (
        "0.2.19",
        "2026-07-05",
        ["New 'Balanced' method: spreads nets across signal layers by "
         "congestion (shortest paths, no directional lanes). Additional "
         "method for dense multi-layer boards; existing methods unchanged."],
    ),
    (
        "0.2.18",
        "2026-07-04",
        ["Respect DSN layer types: route signals only on signal/mixed layers, "
         "never on (type power) high-current planes. On a 16-layer board with "
         "10 plane layers the router now correctly uses only the 6 signal "
         "layers instead of routing through the planes."],
    ),
    (
        "0.2.17",
        "2026-07-04",
        [
            "QUALITY FIX: routing validation reverted to authoritative shapely "
            "(the experimental nogil FastCopper validator disagreed in rare "
            "dynamic cases, letting the router commit traces the DRC then "
            "flagged — the source of clearance violations). Back to 0 viol.",
            "PathFinder progress bar no longer jumps to the end during its "
            "negotiate phase; end-of-route glow guard on 'routing done'.",
        ],
    ),
    (
        "0.2.16",
        "2026-07-04",
        ["No end-of-route visual garbage: the styled result is collected "
         "silently on @CLEAR (no clear/redraw flash), and drawn in a single "
         "atomic clean redraw at finish. One clean swap, zero glow — no extra "
         "visual event when routing completes."],
    ),
    (
        "0.2.15",
        "2026-07-04",
        ["Glow now TRAILS the routing front: each trace glows briefly when it "
         "appears then fades to a clean trace within ~1.3s, so glow never "
         "accumulates into a board-wide smear. Only the active routing area "
         "glows; older traces are already clean. Final board is clean."],
    ),
    (
        "0.2.14",
        "2026-07-04",
        ["Definitive glow fix: when routing finishes the board view is wiped "
         "and every trace/via is REDRAWN from stored coords as clean glow-free "
         "geometry. A full clean redraw cannot leave any glow — independent of "
         "@CLEAR, stream timing, or glow tracking."],
    ),
    (
        "0.2.13",
        "2026-07-04",
        ["Bulletproof glow removal: finalize scans the whole scene and drops "
         "every semi-transparent item (pads/cores keep transparency in their "
         "brush, item-opacity 1.0, so only glow is removed). Log now shows "
         "when the styled result arrives and how many glow items were removed."],
    ),
    (
        "0.2.12",
        "2026-07-04",
        ["Fix diff-pair false-crossing: the bezier smoothing (added with the "
         "glow animation) overshot on sharp turns and drew coupled pairs as "
         "crossing. The geometry never crossed (shapely-confirmed). Final "
         "result now renders exact geometry; live bezier handles are clamped."],
    ),
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
