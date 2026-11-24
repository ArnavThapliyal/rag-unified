#!/usr/bin/env python3
"""
RAG File Processing & Integrity Checker (check_pipeline.py)

A diagnostic and integrity auditing tool for the RAG processor.
Inspects how files are processed through the pipeline, detects faults,
identifies bottlenecks/risks, and verifies file & database integrity.

Usage:
    # 1. Single file diagnostic (inspect parser, cleaner, chunker, metadata, integrity)
    python check_pipeline.py --file path/to/document.pdf

    # 2. Directory pre-flight & integrity scan (audit a folder of purchased/scraped files)
    python check_pipeline.py --dir path/to/folder

    # 3. LanceDB integrity & corruption check (audit stored tables, chunks, hashes, vectors)
    python check_pipeline.py --db path/to/lancedb

    # 4. End-to-end reconciliation (reconcile directory files against LanceDB state)
    python check_pipeline.py --dir path/to/folder --db path/to/lancedb
"""

import argparse
import hashlib
import json
import logging
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Reduce noisy loggers during checking
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
for mod in ("docling", "transformers", "urllib3", "torch"):
    logging.getLogger(mod).setLevel(logging.ERROR)


def get_sha256(path: Path) -> str:
    """Compute 32-character SHA256 hex digest matching schema.compute_doc_id."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()[:32]
    except Exception as e:
        return f"ERROR: {e}"


def check_file_integrity(path: Path) -> Dict[str, Any]:
    """Verify physical file integrity, readability, size, and magic sniffing."""
    res: Dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "is_file": path.is_file(),
        "is_symlink": path.is_symlink(),
        "size_bytes": 0,
        "doc_id": "",
        "readable": False,
        "sniffed_type": "unknown",
        "handler": "unsupported",
        "issues": [],
    }

    if not path.exists():
        res["issues"].append("File does not exist")
        return res

    if path.is_symlink():
        res["issues"].append("File is a symlink (skipped by pipeline to prevent duplicate ingest)")

    try:
        res["size_bytes"] = path.stat().st_size
        if res["size_bytes"] == 0:
            res["issues"].append("File is zero bytes (empty file)")
    except Exception as e:
        res["issues"].append(f"Cannot stat file: {e}")

    try:
        with open(path, "rb") as f:
            f.read(1024)
        res["readable"] = True
    except Exception as e:
        res["issues"].append(f"File unreadable: {e}")

    res["doc_id"] = get_sha256(path)

    # Content sniffing check
    try:
        from pipeline.type_router import HANDLER_UNSUPPORTED, open_routed
        from config.settings import SCRATCH_DIR
        with open_routed(path, SCRATCH_DIR) as routed:
            if not routed:
                res["handler"] = HANDLER_UNSUPPORTED
                res["issues"].append("Not recognized as a regular data file or empty archive")
            else:
                items_info = []
                for item in routed:
                    items_info.append({
                        "name": item.display_name,
                        "source_type": item.source_type,
                        "handler": item.handler,
                        "reason": item.reason,
                    })
                res["routed_items"] = items_info
                res["sniffed_type"] = items_info[0]["source_type"] if items_info else "unknown"
                res["handler"] = items_info[0]["handler"] if items_info else HANDLER_UNSUPPORTED
                if res["handler"] == HANDLER_UNSUPPORTED:
                    res["issues"].append(f"Unsupported format: {items_info[0].get('reason', 'no handler')}")
    except Exception as e:
        res["issues"].append(f"Type router failure: {e}")

    return res


def check_metadata(path: Path) -> Dict[str, Any]:
    """Check metadata resolution and audit OPA authorization / financial fields."""
    from pipeline.metadata import MetadataResolver
    from pipeline.type_router import RoutedFile, HANDLER_DOCUMENT

    meta_info: Dict[str, Any] = {
        "symbol": None,
        "doc_type": None,
        "vendor": None,
        "allowed_roles": None,
        "extended_fields_count": 0,
        "extended_fields": {},
        "extra_fields": {},
        "missing_critical": [],
    }

    try:
        root = path.parent
        resolver = MetadataResolver(root)
        rf = RoutedFile(path=path, source_type=path.suffix.lstrip(".").lower(), handler=HANDLER_DOCUMENT, display_name=path.name)
        meta = resolver.resolve(rf)

        meta_info["symbol"] = meta.symbol
        meta_info["doc_type"] = meta.doc_type
        meta_info["vendor"] = meta.vendor
        meta_info["allowed_roles"] = meta.allowed_roles
        meta_info["extended_fields_count"] = len(meta.fields)
        meta_info["extended_fields"] = meta.fields
        meta_info["extra_fields"] = meta.extra

        if not meta.symbol:
            meta_info["missing_critical"].append("symbol")
        if not meta.doc_type:
            meta_info["missing_critical"].append("doc_type")
        if meta.allowed_roles is None:
            meta_info["missing_critical"].append("allowed_roles (null = no restriction declared; check OPA policy)")
    except Exception as e:
        meta_info["error"] = str(e)

    return meta_info


def inspect_single_file(path: Path) -> None:
    """Perform a deep-dive diagnostic on a single file through all processing stages."""
    print("=" * 80)
    print(f"RAG FILE PROCESSING & INTEGRITY AUDIT: {path.name}")
    print("=" * 80)

    # 1. Integrity & Routing
    print("\n[STAGE 1: FILE INTEGRITY & ROUTING]")
    integ = check_file_integrity(path)
    print(f"  Path          : {integ['path']}")
    print(f"  Size          : {integ['size_bytes']:,} bytes")
    print(f"  doc_id        : {integ['doc_id']} (SHA-256 content-address)")
    print(f"  Readable      : {'Yes' if integ['readable'] else 'NO'}")
    print(f"  Detected Type : {integ['sniffed_type']}")
    print(f"  Handler       : {integ['handler']}")

    if integ["issues"]:
        print("  ⚠️  Integrity Warnings / Faults:")
        for issue in integ["issues"]:
            print(f"      - {issue}")

    if not integ["readable"] or integ["handler"] == "unsupported":
        print("\n  [STOPPING] File cannot be processed by the document/tabular pipeline.")
        return

    # 2. Metadata Audit
    print("\n[STAGE 2: METADATA & AUTHORIZATION AUDIT]")
    meta = check_metadata(path)
    print(f"  Symbol        : {meta.get('symbol') or '(None - vendor metadata missing)'}")
    print(f"  Doc Type      : {meta.get('doc_type') or '(None)'}")
    print(f"  Vendor        : {meta.get('vendor') or '(None)'}")
    print(f"  Allowed Roles : {meta.get('allowed_roles') if meta.get('allowed_roles') is not None else '(None - public/unrestricted)'}")
    print(f"  Financials    : {meta.get('extended_fields_count')} typed column(s) populated")
    if meta.get("missing_critical"):
        print("  ⚠️  Metadata Gaps:")
        for gap in meta["missing_critical"]:
            print(f"      - Missing: {gap}")

    # 3. Parsing & Structure Extraction
    print("\n[STAGE 3: PARSER EXECUTION & HEALTH]")
    from pipeline.type_router import HANDLER_DOCUMENT, HANDLER_TABULAR, open_routed
    from pipeline.schema import CONTENT_KIND_TABLE, Block, TableData
    from config.settings import SCRATCH_DIR

    with open_routed(path, SCRATCH_DIR) as routed:
        if not routed:
            print("  No routed files found.")
            return

        for item in routed:
            print(f"\n  Inspecting Item: {item.display_name} (type: {item.source_type}, handler: {item.handler})")
            if item.handler == HANDLER_DOCUMENT:
                try:
                    from pipeline.parser import Parser
                    parser = Parser()
                    res = parser.parse(item.path)
                    print(f"    Total Blocks      : {len(res.blocks)}")
                    print(f"    Page Count        : {res.page_count or 'N/A'}")
                    print(f"    OCR Pages Used    : {res.ocr_pages}")
                    if res.warnings:
                        print("    ⚠️  Parser Warnings:")
                        for w in res.warnings:
                            print(f"        - {w}")

                    # Block breakdown
                    types = Counter(b.type for b in res.blocks)
                    print(f"    Block Breakdown   : {dict(types)}")

                    # 4. Cleaner Stage
                    print("\n[STAGE 4: CLEANER EXECUTION & BOILERPLATE REMOVAL]")
                    from pipeline.cleaner import clean_blocks
                    cleaned, removed = clean_blocks(res.blocks, res.page_count)
                    print(f"    Blocks Retained   : {len(cleaned)} (removed: {len(removed)})")
                    if removed:
                        print("    Sample Boilerplate / Page Numbers Dropped:")
                        for r in removed[:5]:
                            print(f"        - {r}")
                        if len(removed) > 5:
                            print(f"        ... (+{len(removed)-5} more)")

                    # 5. Chunker Stage
                    print("\n[STAGE 5: CHUNKER EXECUTION & EMBEDDING TOKENS AUDIT]")
                    from pipeline.chunker import chunk_document, token_counter
                    from config.settings import CHUNK_SIZE_TOKENS, EMBED_MAX_SEQ_LENGTH
                    counter = token_counter()
                    chunks = list(chunk_document(cleaned, size_fn=counter))
                    print(f"    Total Chunks Generated : {len(chunks)}")
                    if chunks:
                        lengths = [counter(c.text) for c in chunks]
                        prose_chunks = [c for c in chunks if c.content_kind != CONTENT_KIND_TABLE]
                        table_chunks = [c for c in chunks if c.content_kind == CONTENT_KIND_TABLE]
                        print(f"    Prose Chunks           : {len(prose_chunks)}")
                        print(f"    Table Chunks           : {len(table_chunks)}")
                        print(f"    Token Stats (Min/Avg/Max): {min(lengths)} / {sum(lengths)//len(lengths)} / {max(lengths)} tokens")
                        
                        over_chunks = [i for i, l in enumerate(lengths) if l > CHUNK_SIZE_TOKENS]
                        if over_chunks:
                            print(f"    ⚠️  {len(over_chunks)} chunk(s) exceed CHUNK_SIZE_TOKENS ({CHUNK_SIZE_TOKENS})")
                        trunc_chunks = [i for i, l in enumerate(lengths) if l > EMBED_MAX_SEQ_LENGTH]
                        if trunc_chunks:
                            print(f"    🚨 CRITICAL FAULT: {len(trunc_chunks)} chunk(s) exceed model limit ({EMBED_MAX_SEQ_LENGTH}) and will be truncated!")

                        # Hash Integrity sample
                        print("\n[STAGE 6: CHUNK HASH & DETERMINISM AUDIT]")
                        from pipeline.schema import make_chunk_id, hash_text
                        doc_id = integ["doc_id"]
                        first = chunks[0]
                        c_id = make_chunk_id(doc_id, first.index, first.text)
                        c_hash = hash_text(first.text)
                        print(f"    Sample Chunk 0 ID   : {c_id}")
                        print(f"    Sample Content Hash : {c_hash}")
                        print(f"    Text Preview        : {repr(first.text[:120])}...")

                except Exception as e:
                    print(f"    🚨 Parser failed with error: {e}")

            elif item.handler == HANDLER_TABULAR:
                try:
                    from pipeline.tabular import read_table_chunks
                    from pipeline.chunker import token_counter
                    counter = token_counter()
                    warnings: List[str] = []
                    chunks = list(read_table_chunks(item.path, item.source_type, size_fn=counter, warnings=warnings))
                    print(f"    Tabular Chunks Emitted: {len(chunks)}")
                    if warnings:
                        print("    ⚠️  Tabular Warnings:")
                        for w in warnings:
                            print(f"        - {w}")
                    if chunks:
                        first = chunks[0]
                        print(f"    Sample Table Preview (Chunk 0):")
                        print("\n".join("      " + line for line in first.text.split("\n")[:8]))
                        if len(chunks) > 5000:
                            print(f"    ⚠️  Large table ({len(chunks)} chunks). Ingest will require partial transaction ledger.")
                except Exception as e:
                    print(f"    🚨 Tabular handler failed with error: {e}")


def scan_directory(dir_path: Path, db_path: Optional[Path] = None) -> None:
    """Pre-flight audit of an entire directory of files, checking integrity, formats, and bottlenecks."""
    print("=" * 80)
    print(f"RAG DIRECTORY PROCESSING & INTEGRITY AUDIT: {dir_path}")
    print("=" * 80)

    from pipeline.pipeline import discover_files
    from pipeline.type_router import open_routed, HANDLER_DOCUMENT, HANDLER_TABULAR, HANDLER_UNSUPPORTED
    from config.settings import SCRATCH_DIR

    files = discover_files(dir_path)
    print(f"Total candidate files found: {len(files)}")

    type_counts: Counter[str] = Counter()
    handler_counts: Counter[str] = Counter()
    issues: List[str] = []
    zero_byte_files: List[Path] = []
    scanned_pdf_candidates: List[Path] = []
    large_tabular_candidates: List[Path] = []
    doc_ids: Dict[str, List[Path]] = defaultdict(list)
    missing_symbol_count = 0
    missing_role_count = 0

    from pipeline.metadata import MetadataResolver
    from pipeline.type_router import RoutedFile
    resolver = MetadataResolver(dir_path)

    for p in files:
        size = p.stat().st_size
        if size == 0:
            zero_byte_files.append(p)
            continue

        doc_id = get_sha256(p)
        doc_ids[doc_id].append(p)

        # Check metadata
        rf = RoutedFile(path=p, source_type=p.suffix.lstrip(".").lower(), handler=HANDLER_DOCUMENT, display_name=p.name)
        meta = resolver.resolve(rf)
        if not meta.symbol:
            missing_symbol_count += 1
        if meta.allowed_roles is None:
            missing_role_count += 1

        # Check routing
        try:
            with open_routed(p, SCRATCH_DIR) as routed:
                if not routed:
                    type_counts["unsupported"] += 1
                    handler_counts[HANDLER_UNSUPPORTED] += 1
                else:
                    for item in routed:
                        type_counts[item.source_type] += 1
                        handler_counts[item.handler] += 1
                        if item.source_type == "pdf" and size > 5 * 1024 * 1024:
                            scanned_pdf_candidates.append(p)
                        if item.source_type in ("csv", "xlsx", "xls") and size > 2 * 1024 * 1024:
                            large_tabular_candidates.append(p)
        except Exception as e:
            issues.append(f"Failed to route {p.name}: {e}")

    print("\n[FILE TYPE DISTRIBUTION]")
    for st, count in type_counts.most_common():
        print(f"  {st:15s}: {count:5d}")

    print("\n[PROCESSING HANDLER BREAKDOWN]")
    for h, count in handler_counts.items():
        print(f"  {h:25s}: {count:5d}")

    print("\n[METADATA COMPLETENESS AUDIT]")
    print(f"  Files missing 'symbol'       : {missing_symbol_count:5d} / {len(files)}")
    print(f"  Files with null allowed_roles : {missing_role_count:5d} / {len(files)} (evaluated by OPA)")

    # Duplicates detection
    duplicates = {did: paths for did, paths in doc_ids.items() if len(paths) > 1}
    print("\n[INTEGRITY & DEDUPLICATION AUDIT]")
    print(f"  Unique doc_ids (content hash) : {len(doc_ids)}")
    print(f"  Duplicate file groups found   : {len(duplicates)}")
    if duplicates:
        print("  Sample duplicates (identical content, different names/paths):")
        for did, paths in list(duplicates.items())[:3]:
            print(f"    Hash {did}:")
            for p in paths[:2]:
                print(f"      - {p}")

    # Bottleneck warnings
    print("\n[POTENTIAL PIPELINE FAULTS & PERFORMANCE BOTTLENECKS]")
    if zero_byte_files:
        print(f"  ⚠️  {len(zero_byte_files)} zero-byte file(s) found (will be marked empty/unsupported)")
    if scanned_pdf_candidates:
        print(f"  ⚠️  {len(scanned_pdf_candidates)} large PDF(s) (>5MB) detected (risk of long OCR stalls)")
    if large_tabular_candidates:
        print(f"  ⚠️  {len(large_tabular_candidates)} large tabular file(s) (>2MB) detected (risk of record-mode vector explosion)")
    if issues:
        print(f"  🚨  {len(issues)} routing/sniffing error(s):")
        for err in issues[:5]:
            print(f"      - {err}")

    # Optional LanceDB reconciliation
    if db_path and db_path.exists():
        reconcile_with_db(doc_ids, db_path)


def audit_lancedb(db_path: Path) -> None:
    """Comprehensive integrity check of the LanceDB table, schema, hashes, and chunks."""
    print("=" * 80)
    print(f"LANCEDB TABLE INTEGRITY AUDIT: {db_path}")
    print("=" * 80)

    import lancedb
    from config.settings import LANCEDB_TABLE
    from pipeline.schema import ChunkRecord

    if not db_path.exists():
        print(f"Database path does not exist: {db_path}")
        return

    # 1. Check ledger for crashed inflight documents
    ledger_path = db_path / "_inflight_docs.json"
    if ledger_path.exists():
        try:
            with open(ledger_path, "r") as f:
                inflight = json.load(f)
            if inflight:
                print(f"🚨 UNFINALIZED TRANSACTIONS IN LEDGER ({len(inflight)} doc_ids):")
                print(f"   {inflight}")
                print("   Run VectorStore.recover() or next ingest to clean up.")
            else:
                print("✓ Ledger file is clean (no hanging transactions).")
        except Exception as e:
            print(f"⚠️  Ledger file corrupted: {e}")
    else:
        print("✓ Ledger file: None (clean state).")

    # 2. Connect and check schema
    try:
        db = lancedb.connect(str(db_path))
        if LANCEDB_TABLE not in db.table_names():
            print(f"Table '{LANCEDB_TABLE}' not found in database.")
            return
        table = db.open_table(LANCEDB_TABLE)
        total_rows = len(table)
        print(f"✓ Table '{LANCEDB_TABLE}' opened successfully. Total rows: {total_rows:,}")
    except Exception as e:
        print(f"🚨 Failed to connect/open table: {e}")
        return

    # Schema validation
    expected_schema = ChunkRecord.to_arrow_schema()
    actual_schema = table.schema
    if actual_schema.equals(expected_schema):
        print("✓ Arrow Schema: PERFECT MATCH with pipeline ChunkRecord contract.")
    else:
        print("⚠️  Arrow Schema MISMATCH with ChunkRecord!")

    if total_rows == 0:
        print("Table is empty.")
        return

    # Sample rows for deep integrity check
    print("\n[VERIFYING CHUNK INTEGRITY & DATA QUALITY]")
    df = table.search().limit(min(1000, total_rows)).to_pandas()

    # Document & chunk index continuity check
    doc_chunks = defaultdict(list)
    for _, row in df.iterrows():
        doc_chunks[row["doc_id"]].append(row["chunk_index"])

    discontinuous = 0
    for doc_id, indices in doc_chunks.items():
        sorted_indices = sorted(indices)
        expected = list(range(len(sorted_indices)))
        # Only verify if we likely have all chunks or beginning
        if sorted_indices[0] == 0:
            for i, idx in enumerate(sorted_indices):
                if i != idx:
                    discontinuous += 1
                    break

    print(f"  Distinct doc_ids sampled : {len(doc_chunks)}")
    print(f"  Chunk index discontinuities: {discontinuous} document(s)")

    # Vector integrity check
    nan_vectors = 0
    zero_vectors = 0
    non_unit_vectors = 0
    dim_mismatches = 0

    for vec in df["embedding"]:
        if len(vec) != 1024:
            dim_mismatches += 1
            continue
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0:
            zero_vectors += 1
        elif any(math.isnan(x) or math.isinf(x) for x in vec):
            nan_vectors += 1
        elif abs(norm - 1.0) > 0.05:  # Normalized vectors should have norm ~ 1.0
            non_unit_vectors += 1

    print(f"  Vector dimension mismatches: {dim_mismatches}")
    print(f"  NaN / Inf vector values    : {nan_vectors}")
    print(f"  Zero-length vectors        : {zero_vectors}")
    print(f"  Non-unit norm vectors      : {non_unit_vectors} (note: cosine search expects unit vectors)")

    # Content Hash verification
    hash_corruptions = 0
    for _, row in df.iterrows():
        expected_hash = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
        if row["content_hash"] != expected_hash:
            hash_corruptions += 1

    print(f"  Content hash mismatches    : {hash_corruptions} (text vs stored content_hash)")
    if hash_corruptions == 0:
        print("✓ Chunk text integrity verified (zero bitrot / text corruption detected).")


def reconcile_with_db(doc_ids: Dict[str, List[Path]], db_path: Path) -> None:
    """Reconcile files on disk against documents present in LanceDB."""
    print("\n[LANCEDB RECONCILIATION]")
    import lancedb
    from config.settings import LANCEDB_TABLE
    try:
        db = lancedb.connect(str(db_path))
        if LANCEDB_TABLE not in db.table_names():
            print(f"  Table '{LANCEDB_TABLE}' does not exist yet.")
            return
        tbl = db.open_table(LANCEDB_TABLE)
        existing_doc_ids = set(tbl.search().select(["doc_id"]).limit(50000).to_pandas()["doc_id"].unique())
        
        disk_ids = set(doc_ids.keys())
        already_indexed = disk_ids.intersection(existing_doc_ids)
        pending = disk_ids - existing_doc_ids

        print(f"  Documents in LanceDB      : {len(existing_doc_ids)}")
        print(f"  Disk files ALREADY indexed : {len(already_indexed)} document(s)")
        print(f"  Disk files PENDING ingest  : {len(pending)} document(s)")
    except Exception as e:
        print(f"  ⚠️  Could not reconcile with LanceDB: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RAG File Processing & Integrity Checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--file", "-f", type=Path, help="Deep-dive check on a single document or tabular file")
    parser.add_argument("--dir", "-d", type=Path, help="Pre-flight processing & integrity scan on a folder of files")
    parser.add_argument("--db", "-p", type=Path, help="Audit LanceDB database integrity and chunk validity")

    args = parser.parse_args()

    if not args.file and not args.dir and not args.db:
        # Default fallback to checking default settings or showing help
        from config.settings import LANCEDB_PATH, INPUT_DIR
        print("No path specified. Running check on configured defaults...")
        if Path(INPUT_DIR).exists():
            scan_directory(Path(INPUT_DIR), Path(LANCEDB_PATH) if Path(LANCEDB_PATH).exists() else None)
        elif Path(LANCEDB_PATH).exists():
            audit_lancedb(Path(LANCEDB_PATH))
        else:
            parser.print_help()
            sys.exit(0)
        return

    if args.file:
        inspect_single_file(args.file)
    if args.dir:
        scan_directory(args.dir, args.db)
    elif args.db and not args.dir:
        audit_lancedb(args.db)


if __name__ == "__main__":
    main()
