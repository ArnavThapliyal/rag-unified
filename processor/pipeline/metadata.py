"""
Vendor metadata -> SourceDocument. Nothing here raises: metadata is optional
and inconsistent across vendors, so a missing, empty or malformed source just
means the field is None (or "unknown" for source_type) and a warning is logged.

Metadata is looked up in this order, later entries filling only what earlier
ones left empty:

  1. Sidecar next to the file:  <filename>.meta.json   (e.g. report.pdf.meta.json)
  2. Batch file (metadata.json, or the legacy manifest.json) in the file's directory
     or any parent directory up to the ingest root; the nearest one wins:
         {"defaults": {...applied to every file...},
          "files": {"<filename>": {...}}}
     The legacy crawler shape {"downloaded_files": [{"file": ..., "url": ...}]}
     is still understood and supplies source_url only.

Vendors name fields differently; _ALIASES maps the common spellings onto our
columns, and any key that is exactly one of schema.EXTENDED_FIELDS (isin, close,
roe_pct, ...) fills that typed column. Anything unrecognised is kept under
SourceDocument.extra (stored in metadata_json), never dropped. Folder names are
never read as metadata.

Sidecar and batch files are metadata, not documents: use is_metadata_file() to
keep them out of ingestion.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from pipeline.schema import EXTENDED_FIELDS, SourceDocument, coerce_extended, compute_doc_id, normalize_allowed_roles
from pipeline.type_router import RoutedFile

logger = logging.getLogger(__name__)

_BATCH_FILES = ("metadata.json", "manifest.json")

# our field -> accepted vendor spellings (compared case-insensitively)
_ALIASES: Dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "ticker", "scrip", "scrip_code"),
    "source_url": ("source_url", "url", "link", "source"),
    "doc_type": ("doc_type", "document_type", "type", "category"),
    "vendor": ("vendor", "provider", "supplier", "publisher"),
    "published_at": ("published_at", "published", "publication_date", "date", "published_date"),
    "allowed_roles": ("allowed_roles", "roles", "entitlements", "access_roles"),
    "vendor_doc_id": ("vendor_doc_id", "document_id", "id", "doc_id"),
}
_LOOKUP = {alias: field for field, aliases in _ALIASES.items() for alias in aliases}


def _load_json_dict(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning(f"ignoring unreadable metadata file {path}: {exc}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"ignoring metadata file {path}: top level is {type(data).__name__}, expected an object")
        return {}
    return data


def _as_str(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _normalize(raw: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Split a raw vendor dict into (core fields by our names, EXTENDED_FIELDS values, extra)."""
    known: Dict[str, Any] = {}
    extended: Dict[str, Any] = {}
    extra: Dict[str, Any] = {}
    for key, value in raw.items():
        lowered = str(key).strip().lower()
        field = _LOOKUP.get(lowered)
        if field is not None:
            known.setdefault(field, value)  # first alias wins
        elif lowered in EXTENDED_FIELDS:
            extended.setdefault(lowered, value)
        else:
            extra[str(key)] = value
    return known, extended, extra


def is_metadata_file(path: Path) -> bool:
    """True for sidecar (*.meta.json) and batch (metadata.json, manifest.json) files."""
    name = path.name.lower()
    return name.endswith(".meta.json") or name in _BATCH_FILES


def _legacy_urls(batch: Dict[str, Any]) -> Dict[str, str]:
    """filename -> url from the old crawler manifest."""
    urls: Dict[str, str] = {}
    items = batch.get("downloaded_files")
    if not isinstance(items, list):
        return urls
    for item in items:
        if isinstance(item, dict) and item.get("file") and item.get("url"):
            urls[Path(str(item["file"])).name] = str(item["url"])
    return urls


class BatchMetadata:
    """Metadata shared by a unit directory, loaded once and queried per file."""

    def __init__(self, unit_dir: Optional[Path] = None):
        self._defaults: Dict[str, Any] = {}
        self._files: Dict[str, Dict[str, Any]] = {}
        self._legacy_urls: Dict[str, str] = {}
        if unit_dir is None:
            return
        for name in _BATCH_FILES:
            batch = _load_json_dict(unit_dir / name)
            if not batch:
                continue
            defaults = batch.get("defaults")
            if isinstance(defaults, dict):
                for k, v in defaults.items():
                    self._defaults.setdefault(k, v)
            files = batch.get("files")
            if isinstance(files, dict):
                for fname, entry in files.items():
                    if isinstance(entry, dict):
                        self._files.setdefault(str(fname), entry)
            for fname, url in _legacy_urls(batch).items():
                self._legacy_urls.setdefault(fname, url)

    def lookup(self, delivered: Path) -> Dict[str, Any]:
        return self.for_file(delivered.name)

    def for_file(self, filename: str) -> Dict[str, Any]:
        """Batch-level metadata for one filename: file entry over defaults over legacy url."""
        merged: Dict[str, Any] = {}
        if filename in self._legacy_urls:
            merged["source_url"] = self._legacy_urls[filename]
        merged.update(self._defaults)
        merged.update(self._files.get(filename, {}))
        return merged


class MetadataResolver:
    """
    Batch metadata for an arbitrary directory tree. For a file, every directory
    from `root` down to the file's own is consulted, the nearest overriding the
    farther. Directory names themselves carry no meaning.
    """

    def __init__(self, root: Path):
        self._root = root.resolve()
        self._cache: Dict[Path, BatchMetadata] = {}

    def _batch(self, directory: Path) -> BatchMetadata:
        if directory not in self._cache:
            self._cache[directory] = BatchMetadata(directory)
        return self._cache[directory]

    def lookup(self, delivered: Path) -> Dict[str, Any]:
        directory = delivered.resolve().parent
        chain = [directory, *directory.parents]
        if self._root in chain:
            chain = chain[: chain.index(self._root) + 1]
        else:
            chain = chain[:1]  # outside the root: only its own directory
        merged: Dict[str, Any] = {}
        for d in reversed(chain):  # root first, nearest last (wins)
            merged.update(self._batch(d).for_file(delivered.name))
        return merged


def build_source_document(
    routed: RoutedFile,
    batch: Optional[BatchMetadata | MetadataResolver] = None,
    doc_id: Optional[str] = None,
) -> SourceDocument:
    """
    Assemble the SourceDocument for a routed file. Never raises for metadata
    problems; compute_doc_id can still raise OSError if the file is unreadable,
    which the indexer isolates as a per-file failure.
    """
    delivered = routed.origin  # sidecars sit next to the file as delivered (the zip, for members)
    raw: Dict[str, Any] = {}
    if batch is not None:
        raw.update(batch.lookup(delivered))
    raw.update(_load_json_dict(delivered.with_name(delivered.name + ".meta.json")))

    known, extended, extra = _normalize(raw)
    fields, rejected = coerce_extended(extended)
    if rejected:
        logger.warning(f"{routed.display_name}: could not convert {', '.join(rejected)}; stored as null")
        extra.update({k: extended[k] for k in rejected})  # keep the original value for traceability

    return SourceDocument(
        path=routed.path,
        doc_id=doc_id or compute_doc_id(routed.path),
        source_type=routed.source_type or "unknown",
        source_filename=routed.display_name,
        symbol=_as_str(known.get("symbol")),
        source_url=_as_str(known.get("source_url")),
        doc_type=_as_str(known.get("doc_type")),
        vendor=_as_str(known.get("vendor")),
        published_at=_as_str(known.get("published_at")),
        allowed_roles=normalize_allowed_roles(known.get("allowed_roles")),
        fields={k: v for k, v in fields.items() if v is not None},
        extra={
            **extra,
            **({"vendor_doc_id": _as_str(known["vendor_doc_id"])} if _as_str(known.get("vendor_doc_id")) else {}),
            **({"archive": routed.origin.name} if routed.origin != routed.path else {}),  # zip members
        },
    )
