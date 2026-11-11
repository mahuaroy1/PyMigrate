"""Allow ``python -m pymigrate`` to use the same entry point as the CLI script."""

from .cli import main

# SystemExit turns main's result into the process exit code used by shell scripts.
raise SystemExit(main())
