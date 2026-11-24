#!/usr/bin/env python3
"""Inspect a LanceDB table or input folder and run semantic searches.

Usage:
    python view_db.py                                   # inspect default database
    python view_db.py /path/to/folder                   # inspect folder or database at path
    python view_db.py --path /path/to/folder            # explicit path flag
    python view_db.py "closing price"                   # semantic search in default DB
    python view_db.py /path/to/folder "closing price"   # semantic search in specific DB
    python view_db.py "closing price" --symbol 360ONE   # filter search by symbol
    python view_db.py --symbol 360ONE                   # filter table stats by symbol
"""

import argparse
from collections import Counter
import os
from pathlib import Path
import sys
from typing import Optional, Tuple

import lancedb

from config.settings import LANCEDB_PATH, LANCEDB_TABLE


def resolve_paths(target_str: Optional[str]) -> Tuple[Path, Optional[Path]]:
    """Resolve the given path string into (lancedb_dir, input_folder_or_none)."""
    if not target_str:
        # Default resolution order:
        # 1. LANCEDB_PATH env variable
        # 2. Configured LANCEDB_PATH if it contains data
        # 3. Known local test folder if it exists
        # 4. Fallback to configured LANCEDB_PATH
        env_db = os.environ.get("LANCEDB_PATH")
        if env_db:
            p = Path(env_db).expanduser().resolve()
            return p, (p.parent if p.name == "lancedb" else None)

        settings_db = Path(LANCEDB_PATH).expanduser().resolve()
        if (settings_db / "documents.lance").exists() or (settings_db / "documents").exists():
            return settings_db, settings_db.parent / "input"

        fallback_test = Path("/Users/arnav/Projects/Software/test symbol data/lancedb").resolve()
        if fallback_test.exists():
            return fallback_test, fallback_test.parent

        return settings_db, settings_db.parent / "input"

    p = Path(target_str).expanduser().resolve()
    if not p.exists():
        print(f"Error: Path does not exist: {p}", file=sys.stderr)
        sys.exit(1)

    # 1. Directly a LanceDB database directory
    if (p / "documents.lance").exists() or (p / "documents").exists() or p.name == "lancedb":
        return p, (p.parent if p.name == "lancedb" else None)

    # 2. Folder containing a lancedb subdirectory
    if (p / "lancedb").is_dir():
        return p / "lancedb", p

    # 3. Folder containing data/lancedb
    if (p / "data" / "lancedb").is_dir():
        return p / "data" / "lancedb", (p / "data" / "input" if (p / "data" / "input").is_dir() else p)

    # 4. Sibling directory is lancedb (e.g. data/input -> data/lancedb)
    if (p.parent / "lancedb").is_dir():
        return p.parent / "lancedb", p

    # Fallback: assume the path itself is the DB directory
    return p, None


def summarize_input_folder(folder: Path) -> None:
    """Print a quick summary of files inside an input folder."""
    if not folder.is_dir():
        return

    ignored_parts = {".git", "lancedb", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "logs", "scratch"}
    counts: Counter[str] = Counter()
    symbols = set()

    for item in folder.rglob("*"):
        if item.is_file() and not item.name.startswith("."):
            if any(part in ignored_parts for part in item.parts):
                continue
            ext = item.suffix.lower() or "(no ext)"
            counts[ext] += 1
            try:
                rel = item.relative_to(folder)
                if len(rel.parts) > 1 and rel.parts[0] not in ignored_parts:
                    symbols.add(rel.parts[0])
            except ValueError:
                pass

    total_files = sum(counts.values())
    if total_files > 0:
        print(f"\nInput Folder: {folder}")
        print(f"Files Found : {total_files}")
        if symbols:
            sample_syms = sorted(symbols)[:8]
            suffix = "..." if len(symbols) > 8 else ""
            print(f"Subfolders  : {len(symbols)} symbol directory(ies) ({', '.join(sample_syms)}{suffix})")
        ext_summary = ", ".join(f"{ext}: {cnt}" for ext, cnt in counts.most_common(8))
        print(f"By Extension: {ext_summary}")


def parse_arguments() -> Tuple[Path, Optional[Path], Optional[str], Optional[str], int]:
    parser = argparse.ArgumentParser(
        description="Inspect a LanceDB table or input folder, and run semantic searches.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python view_db.py                                   # inspect default database
  python view_db.py /path/to/folder                   # inspect folder or database at path
  python view_db.py --path /path/to/folder            # explicit path flag
  python view_db.py "closing price"                   # semantic search in default DB
  python view_db.py /path/to/folder "closing price"   # semantic search in specific DB
  python view_db.py "closing price" --symbol 360ONE   # filter search by symbol
  python view_db.py --symbol 360ONE                   # filter table stats by symbol
""",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Path to input folder / LanceDB directory OR search query string",
    )
    parser.add_argument(
        "extra_query",
        nargs="?",
        default=None,
        help="Search query string (when the first argument is a path)",
    )
    parser.add_argument(
        "--path",
        "-p",
        "--db",
        dest="path",
        default=None,
        help="Path to LanceDB database directory or input folder containing 'lancedb'",
    )
    parser.add_argument(
        "--query",
        "-q",
        dest="query",
        default=None,
        help="Search query string",
    )
    parser.add_argument(
        "--symbol",
        "-s",
        default=None,
        help="Filter rows or search results by ticker symbol (e.g. 360ONE)",
    )
    parser.add_argument(
        "-k",
        type=int,
        default=5,
        help="Number of search results or sample rows to show (default: 5)",
    )

    args = parser.parse_args()

    target_path_str: Optional[str] = None
    query: Optional[str] = None

    if args.path:
        target_path_str = args.path
        query = args.query or args.target or args.extra_query
    else:
        if args.target:
            p = Path(args.target).expanduser()
            if p.exists() or args.target.startswith(("/", "./", "../", "~")):
                target_path_str = args.target
                query = args.query or args.extra_query
            else:
                target_path_str = None
                query = args.query or args.target
        else:
            query = args.query

    db_path, input_folder = resolve_paths(target_path_str)
    return db_path, input_folder, query, args.symbol, args.k


def main() -> None:
    db_path, input_folder, query, symbol, k = parse_arguments()

    if input_folder and input_folder.is_dir() and input_folder != db_path:
        summarize_input_folder(input_folder)

    print(f"\nDatabase : {db_path}")
    print(f"Table    : {LANCEDB_TABLE}")

    if not db_path.exists():
        print(f"\nDatabase directory does not exist: {db_path}")
        if input_folder:
            print(f"To ingest files from {input_folder}, run:")
            print(f"  uv run python main.py \"{input_folder}\"")
        sys.exit(1)

    try:
        db = lancedb.connect(str(db_path))
        table = db.open_table(LANCEDB_TABLE)
    except Exception as exc:
        print(f"\nCould not open table {LANCEDB_TABLE!r} at {db_path}: {exc}")
        if input_folder:
            print(f"To ingest files from {input_folder}, run:")
            print(f"  uv run python main.py \"{input_folder}\"")
        sys.exit(1)

    total_rows = table.count_rows()
    print(f"Rows     : {total_rows}")

    if total_rows == 0:
        print("\nTable is empty (0 rows).")
        if input_folder:
            print(f"To ingest files from {input_folder}, run:")
            print(f"  uv run python main.py \"{input_folder}\"")
        sys.exit(0)

    # Inspection mode (no query provided)
    if not query:
        print("\nColumns:")
        for field in table.schema:
            print(f"  {field.name:32} {field.type}")

        print("\nIndexes:")
        indexes = table.list_indices()
        for index in indexes:
            print(f"  {index.name}: {index.index_type} on {index.columns}")
        if not indexes:
            print("  none (searches are exact scans)")

        cols = ["doc_id", "symbol", "source_type", "content_kind", "source_filename"]
        df = table.search().select(cols).to_arrow().to_pandas()
        if symbol:
            df = df[df["symbol"] == symbol]
            print(f"\nFiltered by symbol: {symbol} ({len(df)} rows)")

        print(f"\nDocuments: {df['doc_id'].nunique()}")
        print("\nRows by symbol:\n", df["symbol"].fillna("(none)").value_counts().to_string())
        print("\nRows by file type:\n", df["source_type"].value_counts().to_string())
        print("\nRows by content kind:\n", df["content_kind"].value_counts().to_string())

        sample_cols = ["symbol", "source_filename", "content_kind", "page_range", "section_path", "text"]
        sample_query = table.search()
        if symbol:
            escaped_sym = symbol.replace("'", "''")
            sample_query = sample_query.where(f"symbol = '{escaped_sym}'")
        sample_rows = sample_query.select(sample_cols).limit(k).to_list()

        print(f"\nSample rows ({len(sample_rows)}):")
        for row in sample_rows:
            print(f"\n  [{row.get('symbol') or 'no symbol'}] {row.get('source_filename')}  ({row.get('content_kind')}, page {row.get('page_range')})")
            print(f"  section: {row.get('section_path')}")
            snippet = (row.get("text") or "").strip().replace("\n", " ")
            if len(snippet) > 300:
                snippet = snippet[:300] + "..."
            print(f"  {snippet!r}")
        sys.exit(0)

    # Query mode: perform semantic search
    print("\n--- Semantic Search ---")
    print(f"Query    : {query!r}")
    if symbol:
        print(f"Symbol   : {symbol!r}")
    print(f"Limit (k): {k}")
    print("Loading embedding model (bge-m3)...")

    from pipeline.embedder import Embedder

    vector = Embedder().encode([query])[0]
    search = table.search(vector).metric("cosine")
    if symbol:
        escaped_sym = symbol.replace("'", "''")
        search = search.where(f"symbol = '{escaped_sym}'")

    results = search.limit(k).to_list()
    if not results:
        print("\nNo matching results found.")
        return

    print(f"\nTop {len(results)} results (metric: cosine):\n")
    for i, row in enumerate(results, start=1):
        dist = row.get("_distance")
        sim_str = f"similarity: {1.0 - dist:.4f} (distance: {dist:.4f})" if dist is not None else ""
        print(f"[{i}] [{row.get('symbol') or 'no symbol'}] {row.get('source_filename')} | {sim_str}")
        details = []
        if row.get("content_kind"):
            details.append(f"kind: {row['content_kind']}")
        if row.get("page_range"):
            details.append(f"page: {row['page_range']}")
        if row.get("section_path"):
            details.append(f"section: {row['section_path']}")
        if details:
            print(f"    {' | '.join(details)}")
        text = (row.get("text") or "").strip()
        if len(text) > 400:
            text = text[:400] + "..."
        for line in text.splitlines():
            print(f"    {line}")
        print()


if __name__ == "__main__":
    main()
