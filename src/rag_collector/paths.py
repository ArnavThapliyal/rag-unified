"""Repository paths. RAG_COLLECTOR_ROOT overrides the root (tests, or an install outside the checkout);
RAG_COLLECTOR_OUTBOX overrides where export batches go (e.g. a folder rag-processor watches)."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def root() -> Path:
    env = os.environ.get("RAG_COLLECTOR_ROOT")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[2]


def data() -> Path:
    return root() / "data"


def processed() -> Path:
    return data() / "processed"


def metadata() -> Path:
    return data() / "metadata"


def locks() -> Path:
    return metadata() / "locks"


def universe_db() -> Path:
    return data() / "nse_unified.sqlite3"


def docket_db() -> Path:
    return data() / "docket.sqlite"


def outbox() -> Path:
    env = os.environ.get("RAG_COLLECTOR_OUTBOX")
    return Path(env).resolve() if env else data() / "export"


def stop_file() -> Path:
    """Touch this file to make the continuous loop stop after the current step."""
    return metadata() / "STOP"


def python() -> str:
    """The interpreter that runs the pipeline scripts: the repo's .venv when present, else this one."""
    venv = root() / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable
