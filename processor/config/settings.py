"""
Single source of truth for Processor configuration.

Every tunable can be overridden by an environment variable of the same name so
the identical codebase runs on a local M-series Mac and on a CUDA cloud box
without edits. Importing this module has no filesystem side effects; call
ensure_dirs() explicitly (main.py does) to create the working directories.
"""

import os
from pathlib import Path
from typing import Callable, Dict, Optional, TypeVar

T = TypeVar("T")


def _env(name: str, default: T, cast: Callable[[str], T] = str) -> T:  # type: ignore[assignment]
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return cast(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---- paths ------------------------------------------------------------------

BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = Path(_env("DATA_DIR", str(BASE_DIR / "data")))

INPUT_DIR = Path(_env("INPUT_DIR", str(DATA_DIR / "input")))      # purchased files land here
SCRATCH_DIR = Path(_env("SCRATCH_DIR", str(DATA_DIR / "scratch")))  # zip extraction, per-document temp
LOG_DIR = Path(_env("LOG_DIR", str(DATA_DIR / "logs")))

# ---- vector store (contract with the LLM side) ----------------------------------

LANCEDB_PATH = Path(_env("LANCEDB_PATH", str(DATA_DIR / "lancedb")))
LANCEDB_TABLE = _env("LANCEDB_TABLE", "documents")
DISTANCE_METRIC = "cosine"  # fixed by the contract; not env-overridable on purpose

# ---- embedding --------------------------------------------------------------

EMBEDDING_MODEL = _env("EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_DIM = 1024  # bge-m3 dense output; changing the model means a new table
EMBED_DEVICE: Optional[str] = _env("EMBED_DEVICE", None) or None  # None -> auto: cuda -> mps -> cpu
EMBED_BATCH_SIZE = _env("EMBED_BATCH_SIZE", 64, int)
EMBED_FP16 = _env_bool("EMBED_FP16", False)  # off until bge-m3 quality in fp16 is checked
EMBED_QUEUE_MAX_BATCHES = _env("EMBED_QUEUE_MAX_BATCHES", 8, int)  # backpressure between workers and embedder
# Longest input the model is asked to encode. Prose chunks stay under CHUNK_SIZE_TOKENS;
# the headroom is for a single table row too wide to split.
EMBED_MAX_SEQ_LENGTH = _env("EMBED_MAX_SEQ_LENGTH", 1024, int)
MPS_EMPTY_CACHE_EVERY = _env("MPS_EMPTY_CACHE_EVERY", 50, int)  # encode calls between torch.mps.empty_cache()
# On MPS every batch shape in use holds device memory permanently (see embedder.py), and the
# largest shape dominates: 64 rows x 1024 tokens kept ~28 GB resident, 32 rows about half that.
MPS_MAX_BATCH_SIZE = _env("MPS_MAX_BATCH_SIZE", 32, int)

# ---- chunking ---------------------------------------------------------------

# Measured in tokens of EMBEDDING_MODEL's tokenizer. bge-m3 accepts 8192; these
# are conservative starting points to be tuned against a retrieval eval.
CHUNK_SIZE_TOKENS = _env("CHUNK_SIZE_TOKENS", 512, int)
CHUNK_OVERLAP_TOKENS = _env("CHUNK_OVERLAP_TOKENS", 64, int)

# Tabular sources (csv/xlsx/json). Chunks are whole row groups with the header repeated.
TABULAR_ROWS_PER_CHUNK = _env("TABULAR_ROWS_PER_CHUNK", 40, int)
TABULAR_MAX_ROWS_PER_FILE = _env("TABULAR_MAX_ROWS_PER_FILE", 2_000_000, int)
# 'auto': a table whose header names at least TABULAR_RECORD_MIN_MATCHES of the
# downstream-LLM columns (trade_date, close, roe_pct, ...) is emitted one chunk per
# row with those typed columns filled. 'off': always row groups.
TABULAR_RECORD_MODE = _env("TABULAR_RECORD_MODE", "auto", str)
TABULAR_RECORD_MIN_MATCHES = _env("TABULAR_RECORD_MIN_MATCHES", 3, int)

# ---- cleaning ---------------------------------------------------------------

# A short paragraph repeated on this fraction of pages is treated as a running
# header/footer, but only in documents long enough for repetition to mean something.
CLEAN_MIN_PAGES_FOR_REPEAT = _env("CLEAN_MIN_PAGES_FOR_REPEAT", 8, int)
CLEAN_REPEAT_PAGE_FRACTION = _env("CLEAN_REPEAT_PAGE_FRACTION", 0.4, float)
CLEAN_MAX_BOILERPLATE_CHARS = _env("CLEAN_MAX_BOILERPLATE_CHARS", 80, int)
# Whole-block regexes (case-insensitive) that are always noise.
CLEAN_DENYLIST_PATTERNS = (
    r"page\s+\d+(\s+of\s+\d+)?",
    r"[._\-–—=*]{5,}",
    r"scanned (with|by) camscanner",
)

# ---- parallelism ----------------------------------------------------------------

# 'spawn' is mandatory: forking after torch/MPS initialises is unsafe on macOS.
MP_START_METHOD = "spawn"
# Parsing/chunking workers. Reserve two cores: one for the main process and
# one for the single embedding process, which owns the device.
# 0 runs everything inline in one process (tests, debugging).
# Hard ceiling on parse/chunk workers, whatever NUM_WORKERS or --workers ask for: each docling worker holds
# 1-3 GB and can grow past 5 GB on large PDFs, and the embedder and LM Studio need memory too. 12 workers on a
# 48 GB Mac pushed it 31 GB into swap (2026-11-24).
MAX_WORKERS = max(1, _env("MAX_WORKERS", 4, int))
NUM_WORKERS = min(MAX_WORKERS, _env("NUM_WORKERS", max(1, (os.cpu_count() or 4) - 2), int))
# A worker that reports nothing for this long is killed and its file marked failed. 0 disables.
WORKER_STALL_TIMEOUT_SECONDS = _env("WORKER_STALL_TIMEOUT_SECONDS", 1800, int)

# ---- writing ----------------------------------------------------------------

# A document's rows are written in one atomic add. Documents larger than this
# are written in parts and tracked in a ledger so a crash never leaves a
# half-written document looking complete.
WRITE_MAX_ROWS_PER_ADD = _env("WRITE_MAX_ROWS_PER_ADD", 20_000, int)
# Below this many rows a brute-force scan is exact and fast; no ANN index is built.
VECTOR_INDEX_MIN_ROWS = _env("VECTOR_INDEX_MIN_ROWS", 50_000, int)

# ---- parsing ----------------------------------------------------------------

# OCR uses docling's RapidOCR default model (English + Chinese); Hindi is not covered.
COMPUTE_BUDGET: Dict[str, object] = {
    "enable_ml_ocr": _env_bool("ENABLE_ML_OCR", True),
    # OCR reads only the low-text pages (in page ranges), so the cost is per OCR page (~5.5 s on CPU), not
    # a second parse of the whole file: a 140-page scan (125 OCR pages) takes about 12 minutes on one worker.
    "max_pages_for_ocr": _env("MAX_PAGES_FOR_OCR", 150, int),
    "min_chars_per_page_before_ocr": _env("MIN_CHARS_PER_PAGE_BEFORE_OCR", 25, int),
    "table_structure_mode": _env("TABLE_STRUCTURE_MODE", "accurate", str),  # fast | accurate
}

# Docling's layout/table models run inside each parse worker. Keep them on CPU
# with one thread each so N workers don't oversubscribe cores, and so the single
# embedding process is the only user of the MPS/CUDA device.
PARSER_DEVICE = _env("PARSER_DEVICE", "cpu", str)  # cpu | auto | mps | cuda
PARSER_THREADS = _env("PARSER_THREADS", 1, int)
# PDFs longer than this are converted in windows of this many pages: a worker's memory stays bounded by the
# window instead of growing with the document (a 430-page DRHP peaked at 11.5 GB in one conversion).
PDF_WINDOW_PAGES = max(1, _env("PDF_WINDOW_PAGES", 40, int))
# Skip IPO offer documents (DRHP / RHP / prospectus) before parsing: out of scope for a delisting corpus.
SCOPE_SKIP_OFFER_DOCUMENTS = _env_bool("SCOPE_SKIP_OFFER_DOCUMENTS", True)

# Zip handling limits (type_router).
ZIP_MAX_MEMBERS = _env("ZIP_MAX_MEMBERS", 5_000, int)
ZIP_MAX_UNCOMPRESSED_BYTES = _env("ZIP_MAX_UNCOMPRESSED_BYTES", 4 * 1024**3, int)
ZIP_MAX_DEPTH = _env("ZIP_MAX_DEPTH", 3, int)

# ---- provenance ---------------------------------------------------------------

# ---- unified pipeline: collector outbox, step locks, quant stage ---------------
# rag-collector's outbox of READY batches (data/export/ in the collector checkout next to this one)
# rag-unified layout (../collector) first, else a sibling rag-collector checkout
_COLLECTOR_DIR = next((BASE_DIR.parent / d for d in ("collector", "rag-collector") if (BASE_DIR.parent / d).is_dir()),
                      BASE_DIR.parent / "collector")
COLLECTOR_OUTBOX = Path(_env("COLLECTOR_OUTBOX", str(_COLLECTOR_DIR / "data" / "export")))
DENYLIST_FILE = Path(_env("DENYLIST_FILE", str(DATA_DIR / "denylist.txt")))  # doc_ids never indexed again
TRIAGE_DIR = Path(_env("TRIAGE_DIR", str(DATA_DIR / "triage")))  # one <run_id>.csv per ingest run
BATCH_LEDGER = Path(_env("BATCH_LEDGER", str(DATA_DIR / "processed_batches.jsonl")))  # one line per batch attempt
BATCH_MAX_ATTEMPTS = _env("BATCH_MAX_ATTEMPTS", 3, int)  # a batch with failed files is retried this often
WATCH_POLL_SECONDS = _env("WATCH_POLL_SECONDS", 60, int)
STEP_LOCK_DIR = Path(_env("STEP_LOCK_DIR", str(DATA_DIR / "locks")))
STOP_FILE = Path(_env("STOP_FILE", str(DATA_DIR / "STOP")))  # touch it to stop --watch after the current batch
UNIVERSE_DB = Path(_env("UNIVERSE_DB", str(DATA_DIR / "universe" / "universe.sqlite3")))  # latest collector snapshot
QUANT_DB = Path(_env("QUANT_DB", str(DATA_DIR / "quant.sqlite3")))
QUANT_VALUE_FIELD = _env("QUANT_VALUE_FIELD", "market_cap_cr", str)
QUANT_TABLE = _env("QUANT_TABLE", "quant_factors")  # LanceDB table of Z-factors (no vectors)

PIPELINE_VERSION = "0.1.0"  # bump when parse/clean/chunk behaviour changes
SCHEMA_VERSION = 2          # bump when the LanceDB schema in pipeline/schema.py changes


def ensure_dirs() -> None:
    """Create working directories. Called explicitly; never at import time."""
    for path in (DATA_DIR, INPUT_DIR, SCRATCH_DIR, LOG_DIR, LANCEDB_PATH):
        path.mkdir(parents=True, exist_ok=True)
