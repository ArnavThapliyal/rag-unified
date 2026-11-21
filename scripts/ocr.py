"""OCR for scanned (image-only) PDFs, using the tesseract CLI on pages rendered by pypdfium2.

  python scripts/ocr.py data/raw/master_lists/013bba5a417251a5.pdf      # print the OCR text

parse_nse_records.py calls ocr_pdf() when a PDF has no text layer. Results are cached in
data/processed/ocr/<folder>__<name>.txt, so each scanned file is read once (≈2–4 s per page).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import PROCESSED

OCR_DIR = PROCESSED / "ocr"
MAX_PAGES = 12   # delisting orders are 2–8 pages; long scanned reports are not worth the time
DPI = 300


def available() -> bool:
    return shutil.which("tesseract") is not None


def cache_path(pdf: Path) -> Path:
    return OCR_DIR / f"{pdf.parent.name}__{pdf.stem}.txt"


def ocr_pdf(pdf: Path, max_pages: int = MAX_PAGES) -> str:
    """Text of the first max_pages pages ('' if tesseract is missing or nothing is readable)."""
    cached = cache_path(pdf)
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    if not available():
        return ""
    import pypdfium2 as pdfium

    texts = []
    doc = pdfium.PdfDocument(str(pdf))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(min(len(doc), max_pages)):
                img = doc[i].render(scale=DPI / 72).to_pil().convert("L")
                png = Path(tmp) / f"p{i}.png"
                img.save(png)
                r = subprocess.run(["tesseract", str(png), "stdout", "-l", "eng", "--psm", "6"],
                                   capture_output=True, text=True, timeout=120)
                texts.append(r.stdout)
    finally:
        doc.close()
    text = "\n".join(texts)
    OCR_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    print(ocr_pdf(Path(sys.argv[1])))
