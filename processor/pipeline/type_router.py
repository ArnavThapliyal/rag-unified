"""
Decide how each delivered file should be processed.

    with open_routed(path, scratch_dir) as routed:
        for item in routed:  # RoutedFile
            ...

A zip is extracted into a private temp dir that lives until the `with` block
exits, so callers can parse the members before they are cleaned up. Files that
cannot be processed are returned with handler UNSUPPORTED and a reason, never
silently dropped, so the caller can count and log them.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, List, Optional

import magic  # python-magic: content sniffing, needs the libmagic system library

from config.settings import ZIP_MAX_DEPTH, ZIP_MAX_MEMBERS, ZIP_MAX_UNCOMPRESSED_BYTES

logger = logging.getLogger(__name__)

HANDLER_DOCUMENT = "structured_doc_handler"  # docling: pdf, docx, pptx, html
HANDLER_TABULAR = "tabular_handler"          # tabular.py: csv, xlsx, xls, json
HANDLER_UNSUPPORTED = "unsupported"

# canonical source_type -> handler
_TYPE_HANDLERS = {
    "pdf": HANDLER_DOCUMENT,
    "docx": HANDLER_DOCUMENT,
    "pptx": HANDLER_DOCUMENT,
    "html": HANDLER_DOCUMENT,
    "csv": HANDLER_TABULAR,
    "xlsx": HANDLER_TABULAR,
    "xls": HANDLER_TABULAR,
    "json": HANDLER_TABULAR,
}

_EXTENSION_TYPES = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".doc": "doc",
    ".ppt": "ppt",
    ".html": "html",
    ".htm": "html",
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".xls": "xls",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
    ".zip": "zip",
}

_MIME_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/csv": "csv",
    "application/csv": "csv",
    "application/json": "json",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    # Recognised so they are reported as unsupported with a clear reason.
    "application/msword": "doc",
    "application/vnd.ms-powerpoint": "ppt",
}
# 'text/plain' and 'application/octet-stream' are ambiguous: the extension decides.

_UNSUPPORTED_REASONS = {
    "doc": "legacy binary Word (.doc); convert to .docx",
    "ppt": "legacy binary PowerPoint (.ppt); convert to .pptx",
}
_OOXML = {"docx", "pptx", "xlsx"}  # these sniff as 'zip' on some libmagic versions

_IGNORED_NAMES = {".ds_store", "thumbs.db"}


@dataclass(frozen=True)
class RoutedFile:
    path: Path            # file to read (an extracted temp file for zip members)
    handler: str          # HANDLER_*
    source_type: str      # canonical type: pdf, docx, ..., or 'unknown'
    display_name: str     # name to record as source_filename ("bundle.zip!report.pdf" for members)
    origin: Path          # the file as delivered (the zip itself for members)
    reason: Optional[str] = None  # why it is unsupported


def detect_type(path: Path) -> Optional[str]:
    """Canonical type from extension and content sniffing; None if unrecognised."""
    ext_type = _EXTENSION_TYPES.get(path.suffix.lower())

    mime_type: Optional[str] = None
    try:
        mime = magic.from_file(str(path), mime=True)
        mime_type = _MIME_TYPES.get(mime)
    except Exception as exc:
        logger.warning(f"content sniffing failed for {path.name}: {exc}")

    if mime_type is None:
        return ext_type
    if ext_type is None or ext_type == mime_type:
        return mime_type
    if mime_type == "zip" and ext_type in _OOXML:
        return ext_type
    logger.warning(f"{path.name}: extension says {ext_type!r} but content looks like {mime_type!r}; using content")
    return mime_type


def _route_one(path: Path, origin: Path, display_name: str) -> RoutedFile:
    file_type = detect_type(path)
    if file_type is None:
        return RoutedFile(path, HANDLER_UNSUPPORTED, "unknown", display_name, origin, "unrecognised file type")
    handler = _TYPE_HANDLERS.get(file_type)
    if handler is None:
        reason = _UNSUPPORTED_REASONS.get(file_type, f"unsupported type {file_type!r}")
        return RoutedFile(path, HANDLER_UNSUPPORTED, file_type, display_name, origin, reason)
    return RoutedFile(path, handler, file_type, display_name, origin)


def _is_ignorable(member: str) -> bool:
    parts = PurePosixPath(member).parts
    return (
        not parts
        or "__MACOSX" in parts
        or parts[-1].lower() in _IGNORED_NAMES
        or parts[-1].startswith("._")
    )


def _extract_zip(zip_path: Path, scratch_dir: Path, stack: ExitStack) -> List[Path]:
    """Extract into a temp dir owned by `stack`; returns the extracted file paths, or raises ValueError."""
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir() and not _is_ignorable(i.filename)]
        if len(infos) > ZIP_MAX_MEMBERS:
            raise ValueError(f"{len(infos)} members exceeds limit {ZIP_MAX_MEMBERS}")
        total = sum(i.file_size for i in infos)
        if total > ZIP_MAX_UNCOMPRESSED_BYTES:
            raise ValueError(f"uncompressed size {total} exceeds limit {ZIP_MAX_UNCOMPRESSED_BYTES}")

        scratch_dir.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix="zip_", dir=scratch_dir))
        stack.callback(shutil.rmtree, target, True)

        extracted: List[Path] = []
        root = target.resolve()
        for info in infos:
            dest = (target / info.filename).resolve()
            if root not in dest.parents:  # zip-slip: member escapes the extraction dir
                logger.warning(f"{zip_path.name}: skipping unsafe member path {info.filename!r}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            extracted.append(dest)
        return extracted


def _route_into(path: Path, origin: Path, display_name: str, scratch_dir: Path, depth: int,
                stack: ExitStack, out: List[RoutedFile]) -> None:
    routed = _route_one(path, origin, display_name)
    if routed.source_type != "zip":
        out.append(routed)
        return

    if depth >= ZIP_MAX_DEPTH:
        out.append(RoutedFile(path, HANDLER_UNSUPPORTED, "zip", display_name, origin,
                              f"zip nesting deeper than {ZIP_MAX_DEPTH}"))
        return
    try:
        members = _extract_zip(path, scratch_dir, stack)
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        logger.error(f"cannot extract {display_name}: {exc}")
        out.append(RoutedFile(path, HANDLER_UNSUPPORTED, "zip", display_name, origin, f"zip not extractable: {exc}"))
        return

    for member in sorted(members):
        _route_into(member, origin, f"{display_name}!{member.name}", scratch_dir, depth + 1, stack, out)


@contextmanager
def open_routed(file_path: Path | str, scratch_dir: Path | str) -> Iterator[List[RoutedFile]]:
    """
    Route one delivered file (expanding zips). Extracted files stay on disk
    until the with-block exits.
    """
    path = Path(file_path)
    scratch = Path(scratch_dir)
    with ExitStack() as stack:
        out: List[RoutedFile] = []
        if not path.is_file():
            logger.warning(f"not a file, skipping: {path}")
        elif path.name.lower() in _IGNORED_NAMES or path.name.startswith("._"):
            pass
        else:
            _route_into(path, path, path.name, scratch, 0, stack, out)
        yield out
