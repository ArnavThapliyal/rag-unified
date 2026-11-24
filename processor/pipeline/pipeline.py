#!/usr/bin/env python3
"""
Pipeline orchestration.

    PipelineRunner().run(root)

`root` is any directory (or a single file). It is crawled recursively with
Path.rglob and every file found is handed to the indexer. There is no required
layout: folder names and depth are ignored, and what a file *is* comes only from
type_router's content sniffing. The one place the tree matters is locating
optional metadata files (see metadata.py), and those are never ingested.

Acquisition (crawling, collectors) is out of scope: the data is purchased and
arrives as static files.

FAILURE MODEL:
    Per-file. The indexer isolates each file, so one bad file never stops the
    rest; failures are reported in the summary (files_failed / failed_files) and
    main.py exits non-zero when there are any.
"""

import logging
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import LANCEDB_PATH, LOG_DIR, SCRATCH_DIR
from pipeline.indexer import Indexer
from pipeline.metadata import is_metadata_file
from pipeline.type_router import HANDLER_UNSUPPORTED, open_routed

logger = logging.getLogger(__name__)

_COUNTER_KEYS = ("files_indexed", "files_failed", "files_skipped", "chunks_written")
# Our own working directories are never input, even when the root is the data dir.
_WORK_DIRS = (SCRATCH_DIR, LOG_DIR, LANCEDB_PATH)


def discover_files(root: Path) -> List[Path]:
    """
    Every candidate file under `root`, sorted for a deterministic run order.

    Skipped: hidden files and anything inside a hidden directory (.DS_Store,
    ._x, .git), symlinks (avoids double-ingesting the same bytes), sidecar/batch
    metadata files, and the pipeline's own working directories.
    """
    root = Path(root)
    if root.is_file():
        return [root]

    work = [d.resolve() for d in _WORK_DIRS]
    files: List[Path] = []
    for path in root.rglob("*"):
        rel_parts = path.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if is_metadata_file(path):
            continue
        resolved = path.resolve()
        if any(d == resolved or d in resolved.parents for d in work):
            continue
        files.append(path)
    return sorted(files)


def dry_run(root: Path) -> Dict[str, Any]:
    """
    Discover and route only: what a run would process, by detected type, and
    what it would skip and why. Nothing is parsed, embedded or written.
    """
    root = Path(root)
    files = discover_files(root)
    by_type: Counter = Counter()
    unsupported: Counter = Counter()
    errors: List[str] = []
    for path in files:
        try:
            with open_routed(path, SCRATCH_DIR) as routed:
                for item in routed:
                    if item.handler == HANDLER_UNSUPPORTED:
                        unsupported[item.reason or "unsupported"] += 1
                    else:
                        by_type[item.source_type] += 1
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    return {"root": str(root), "files_found": len(files), "by_source_type": dict(by_type),
            "unsupported": dict(unsupported), "routing_errors": errors}


class PipelineRunner:

    def __init__(self, workers: Optional[int] = None, indexer: Optional[Any] = None) -> None:
        self._indexer = indexer if indexer is not None else Indexer(workers=workers)

    def run(self, root: Path) -> Dict[str, Any]:
        root = Path(root)
        files = discover_files(root)
        logger.info(f"Starting pipeline: {len(files)} file(s) under {root}")

        base = root if root.is_dir() else root.parent
        result = self._validate_result(self._indexer.index_files(files, base))

        summary = {
            **result,
            "root": str(root),
            "files_found": len(files),
            **{key: int(result.get(key, 0) or 0) for key in _COUNTER_KEYS},
            "failed_files": list(result.get("failed_files", [])),
        }
        logger.info(f"Pipeline run complete: {summary}")
        return summary

    @staticmethod
    def _validate_result(raw: Any) -> Dict[str, Any]:
        """A malformed indexer result is an error, not a silent success."""
        if not isinstance(raw, dict):
            raise ValueError(f"indexer returned {type(raw).__name__}, expected dict")
        if raw.get("status") not in ("success", "skipped", "no_data"):
            raise ValueError(f"indexer returned invalid status {raw.get('status')!r}")
        return raw
