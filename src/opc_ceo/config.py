from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    workspace: Path


def load_config(workspace: Path | None = None) -> Config:
    selected = workspace or Path(os.environ.get("OPC_WORKSPACE_PATH", "~/OPC_WORKSPACE"))
    return Config(workspace=selected.expanduser().resolve(strict=False))
