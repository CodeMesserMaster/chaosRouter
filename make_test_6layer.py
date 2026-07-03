"""Generate a synthetic 6-layer DSN that forces inner-layer routing."""

LAYERS = ["Top", "In1", "In2", "In3", "In4", "Bottom"]

parts = []
parts.append("(PCB test_6layer.dsn")
parts.append("  (parser (string_quote \") (space_in_quoted_tokens on) (host_cad test))")
parts.append("  (resolution mil 1000)")
parts.append("  (structure")
parts.append("    (boundary(rect pcb -500 -400 500 400))")
parts.append("    (via Default)")
parts.append("    (rule (clearance 6))")
parts.append("    (rule (width 6))")
for ly in LAYERS:
    parts.append(f"    (layer {ly}\n      (type signal)\n    )")
parts.append("  )")

# placement: 4 resistors left, 4 right (front), 1 back-side pair, 2 walls
parts.append("  (placement")
for i in range(4):
    y = 250 - i * 120
    parts.append(f"    (component RES_R{i+1}\n      (place R{i+1} -400 {y} front 0)\n    )")
    parts.append(f"    (component RES_R{i+5}\n      (place R{i+5} 400 {y} front 0)\n    )")
parts.append("    (component RES_R9\n      (place R9 -400 -330 back 0)\n    )")
parts.append("    (component RES_R10\n      (place R10 400 -330 back 0)\n    )")
parts.append("    (component WALL_W1\n      (place W1 0 0 front 0)\n    )")
parts.append("  )")

parts.append("  (library")
for i in range(10):
    parts.append(
        f"    (image RES_R{i+1}\n"
        f"      (pin smd0 1 -40 0)\n"
        f"      (pin smd0 2 40 0)\n"
        f"    )"
    )
parts.append(
    "    (image WALL_W1\n"
    "      (pin wallT 1 0 0)\n"
    "      (pin wallB 2 0 0)\n"
    "    )"
)
parts.append("    (padstack smd0\n      (shape(rect Top -15 -12 15 12))\n    )")
parts.append("    (padstack wallT\n      (shape(rect Top -20 -400 20 400))\n    )")
parts.append("    (padstack wallB\n      (shape(rect Bottom -20 -400 20 400))\n    )")
parts.append(
    "    (padstack Default\n      (shape(circle signal 19.68503))\n    )"
)
parts.append("  )")

parts.append("  (network")
for i in range(4):
    parts.append(f"    (net N{i+1}\n      (pins R{i+1}-2 R{i+5}-1)\n    )")
parts.append("    (net N5\n      (pins R9-2 R10-1)\n    )")
parts.append("  )")
parts.append("  (wiring\n  )")
parts.append(")")

with open("test_6layer.dsn", "w") as fh:
    fh.write("\n".join(parts) + "\n")
print("wrote test_6layer.dsn")
