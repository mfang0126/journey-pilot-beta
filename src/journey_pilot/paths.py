"""Where Journey Pilot keeps its shared store.

``JOURNEY_PILOT_HOME`` overrides the default so several agents can be pointed at
one directory regardless of their working directory.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "JOURNEY_PILOT_HOME"


def store_root() -> Path:
    """Resolve the shared store root without creating it."""
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "journey-pilot").resolve()


def layout(root: Path | None = None) -> dict[str, str]:
    """The directory layout, as absolute paths (nothing is created here)."""
    base = (root or store_root())
    return {
        "root": str(base),
        "journeys": str(base / "journeys"),
        "runs": str(base / "runs"),
        "locks": str(base / "locks"),
        "catalog": str(base / "catalog.json"),
    }
