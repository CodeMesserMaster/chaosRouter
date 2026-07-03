"""chaosRouter launcher — GUI by default, `chaosRouter route <args>` for CLI.

The GUI runs routing as a subprocess of this same executable (the `route`
mode), which gives it live event streaming and a clean Cancel.
"""

import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "route":
        sys.argv.pop(1)
        from chaosrouter.cli import main as cli_main

        sys.exit(cli_main())
    from chaosrouter.gui import main

    main()
