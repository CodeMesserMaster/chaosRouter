"""Synthetic DSN board factory for the test suite.

Generates small, fully self-contained Specctra DSN files so tests run
without any proprietary board data.
"""

from __future__ import annotations

UNIT_SCALE = {"mil": 1.0, "um": 25.4, "mm": 0.0254}  # value per mil


def simple_board(
    unit: str = "mil",
    layers: tuple[str, ...] = ("Top", "Bottom"),
    n_pairs: int = 4,
    wall: bool = False,
    diff_pair: bool = False,
) -> str:
    """A rectangle board with resistor pairs left/right to route across.

    wall=True adds solid Top+Bottom obstacles in the middle (forces inner
    layers when they exist). diff_pair=True adds a P/N net pair with class
    rules so the pair machinery engages.
    """
    s = UNIT_SCALE[unit]

    def f(v_mil: float) -> str:
        return f"{v_mil * s:.4f}"

    p = []
    p.append("(pcb synthetic.dsn")
    p.append('  (parser (string_quote ") (space_in_quoted_tokens on) (host_cad test))')
    p.append(f"  (resolution {unit} 1000)")
    p.append(f"  (unit {unit})")
    p.append("  (structure")
    p.append(f"    (boundary(rect pcb {f(-500)} {f(-400)} {f(500)} {f(400)}))")
    p.append("    (via Default)")
    p.append(f"    (rule (clearance {f(6)}))")
    p.append(f"    (rule (width {f(6)}))")
    for ly in layers:
        p.append(f"    (layer {ly}\n      (type signal)\n    )")
    p.append("  )")

    p.append("  (placement")
    for i in range(n_pairs):
        y = 250 - i * 120
        p.append(f"    (component RES\n      (place R{2*i+1} {f(-400)} {f(y)} front 0)\n    )")
        p.append(f"    (component RES\n      (place R{2*i+2} {f(400)} {f(y)} front 0)\n    )")
    if diff_pair:
        p.append(f"    (component DPCONN\n      (place D1 {f(-400)} {f(-330)} front 0)\n    )")
        p.append(f"    (component DPCONN\n      (place D2 {f(400)} {f(-330)} front 0)\n    )")
    if wall:
        p.append(f"    (component WALL\n      (place W1 0 0 front 0)\n    )")
    p.append("  )")

    p.append("  (library")
    p.append(
        f"    (image RES\n"
        f"      (pin smd0 1 {f(-40)} 0)\n"
        f"      (pin smd0 2 {f(40)} 0)\n"
        f"    )"
    )
    if diff_pair:
        # realistic differential pinout: two adjacent pins, 20 mil pitch
        p.append(
            f"    (image DPCONN\n"
            f"      (pin smd1 1 0 {f(10)})\n"
            f"      (pin smd1 2 0 {f(-10)})\n"
            f"    )"
        )
        p.append(
            f"    (padstack smd1\n      (shape(rect Top {f(-8)} {f(-6)} {f(8)} {f(6)}))\n    )"
        )
    if wall:
        p.append(
            "    (image WALL\n"
            "      (pin wallT 1 0 0)\n"
            "      (pin wallB 2 0 0)\n"
            "    )"
        )
        p.append(
            f"    (padstack wallT\n      (shape(rect Top {f(-20)} {f(-380)} {f(20)} {f(380)}))\n    )"
        )
        p.append(
            f"    (padstack wallB\n      (shape(rect Bottom {f(-20)} {f(-380)} {f(20)} {f(380)}))\n    )"
        )
    p.append(
        f"    (padstack smd0\n      (shape(rect Top {f(-15)} {f(-12)} {f(15)} {f(12)}))\n    )"
    )
    p.append(f"    (padstack Default\n      (shape(circle signal {f(19.685)}))\n    )")
    p.append("  )")

    p.append("  (network")
    for i in range(n_pairs):
        p.append(
            f"    (net Net_{i}\n      (pins R{2*i+1}-2 R{2*i+2}-1)\n    )"
        )
    if diff_pair:
        p.append("    (net DP_P\n      (pins D1-1 D2-1)\n    )")
        p.append("    (net DP_N\n      (pins D1-2 D2-2)\n    )")
        p.append(
            "    (class PAIRS DP_P DP_N\n"
            "      (circuit (use_via Default))\n"
            f"      (rule (width {f(6)}))\n"
            f"      (rule (clearance {f(6)}))\n"
            f"      (rule (edge_primary_gap {f(6)}))\n"
            f"      (rule (diffpair_line_width {f(6)}))\n"
            f"      (rule (max_uncoupled_length {f(400)}))\n"
            "    )"
        )
    p.append("  )")
    p.append(")")
    return "\n".join(p)
