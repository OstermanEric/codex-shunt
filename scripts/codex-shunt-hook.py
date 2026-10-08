"""Fail-open hook bridge for native Windows; no shell dependency."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from codex_shunt.cli import main

    main(["hook", *sys.argv[1:]])
except Exception:
    # A missing/corrupt installation must never block ordinary tools.
    pass
