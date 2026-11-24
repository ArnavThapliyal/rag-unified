#!/usr/bin/env python3
"""
Entry point for the RAG processor: ingest purchased files into LanceDB.

    main.py [ROOT] [--workers N] [--dry-run]
    main.py --watch [OUTBOX] [--once] [--poll SECONDS]     # continuous: ingest rag-collector's outbox batches

ROOT is any directory (default: config.settings.INPUT_DIR), or a single file.
It is crawled recursively; there is no required folder structure and folder
names are ignored. File types are detected from content by pipeline/type_router.
Optional metadata files (*.meta.json, metadata.json, manifest.json) are read
for vendor metadata and are never ingested themselves.

Parsing and chunking run in worker processes; one separate process owns the
embedding model. Because processes are started with 'spawn', everything that
runs must stay under the `if __name__ == "__main__"` guard at the bottom.
"""

import signal
import sys
import uuid
import logging
import argparse
from pathlib import Path
from typing import Any, Dict

from config.logsetup import setup_logging
from config.settings import COLLECTOR_OUTBOX, INPUT_DIR, NUM_WORKERS, WATCH_POLL_SECONDS, ensure_dirs

logger = logging.getLogger(__name__)

_MAX_FAILED_LISTED = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAG processor: ingest purchased documents into LanceDB")
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=INPUT_DIR,
        help=f"Directory to crawl recursively, or a single file (default: {INPUT_DIR}).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=f"Parse/chunk worker processes (default: NUM_WORKERS = {NUM_WORKERS}). 0 runs everything in one process.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only discover and detect file types; report what would be processed. Nothing is written.",
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        type=Path,
        const=COLLECTOR_OUTBOX,
        metavar="OUTBOX",
        help=f"Continuous mode: ingest every READY batch in rag-collector's outbox (default: {COLLECTOR_OUTBOX}).",
    )
    parser.add_argument("--once", action="store_true", help="With --watch: process what is pending, then exit.")
    parser.add_argument("--poll", type=float, default=WATCH_POLL_SECONDS,
                        help="With --watch: seconds between outbox checks.")
    args = parser.parse_args()
    if args.watch is not None and args.dry_run:
        parser.error("--watch and --dry-run don't go together")
    if args.workers is not None and args.workers < 0:
        parser.error("--workers must be 0 or more")
    return args


def print_summary(summary: Dict[str, Any]) -> None:
    print(
        f"\n{summary['files_found']} file(s) found under {summary['root']}: "
        f"{summary['files_indexed']} indexed, {summary['files_failed']} failed, "
        f"{summary['files_skipped']} skipped | chunks written: {summary['chunks_written']}"
    )
    if summary.get("by_status"):
        print("By status: " + ", ".join(f"{k}: {v}" for k, v in summary["by_status"].items()))
    if summary.get("by_source_type"):
        print("Indexed by type: " + ", ".join(f"{k}: {v}" for k, v in sorted(summary["by_source_type"].items())))
    if summary.get("finalize_error"):
        print(f"Index finalisation failed (rows are stored): {summary['finalize_error']}")
    failed = summary["failed_files"]
    if failed:
        shown = ", ".join(failed[:_MAX_FAILED_LISTED])
        more = f" (+{len(failed) - _MAX_FAILED_LISTED} more)" if len(failed) > _MAX_FAILED_LISTED else ""
        print(f"Failed: {shown}{more}")
    if summary.get("run_id"):
        print(f"Run id: {summary['run_id']}")


def print_dry_run(report: Dict[str, Any]) -> None:
    print(f"\n{report['files_found']} file(s) found under {report['root']} (dry run, nothing written)")
    for source_type, count in sorted(report["by_source_type"].items()):
        print(f"  would process  {source_type}: {count}")
    for reason, count in sorted(report["unsupported"].items()):
        print(f"  would skip     {count} x {reason}")
    for error in report["routing_errors"]:
        print(f"  routing error  {error}")


def _sigterm_as_interrupt(*_: Any) -> None:
    # SIGTERM unwinds like Ctrl-C, so the indexer's cleanup terminates its worker processes (no orphans)
    raise KeyboardInterrupt


def main() -> None:
    args = parse_args()  # first, so --help and bad flags have no filesystem side effects
    signal.signal(signal.SIGTERM, _sigterm_as_interrupt)
    if args.watch is not None:
        from pipeline.watch import Watch

        ensure_dirs()
        setup_logging()
        sys.exit(Watch(args.watch, args.poll, args.once, args.workers).main())
    if not args.root.exists():
        print(f"Error: {args.root} does not exist.")
        sys.exit(1)

    try:
        from pipeline.indexer import Indexer
        from pipeline.pipeline import PipelineRunner, dry_run
    except ImportError as exc:
        hint = " Install the libmagic system library (macOS: brew install libmagic)." if "magic" in str(exc).lower() else ""
        print(f"Error: cannot start the pipeline: {exc}.{hint}")
        sys.exit(1)

    ensure_dirs()
    run_id = uuid.uuid4().hex[:12]
    setup_logging(run_id)

    try:
        if args.dry_run:
            print_dry_run(dry_run(args.root))
            sys.exit(0)
        summary = PipelineRunner(indexer=Indexer(workers=args.workers, run_id=run_id)).run(args.root)
    except KeyboardInterrupt:
        logger.info("Pipeline interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        sys.exit(1)

    print_summary(summary)
    sys.exit(1 if summary["files_failed"] or summary.get("finalize_error") else 0)


if __name__ == "__main__":
    main()
