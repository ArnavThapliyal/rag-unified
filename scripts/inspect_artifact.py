"""Show a short, bounded excerpt of a downloaded artifact so the agent can debug parsing
without loading the whole file into context.

Examples:
  python scripts/inspect_artifact.py data/raw/master_lists/ab12.pdf
  python scripts/inspect_artifact.py data/raw/master_lists/ab12.xlsx --lines 30
  python scripts/inspect_artifact.py --zero-rows          # list parsed artifacts that yielded 0 rows
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from common import METADATA, ROOT, emit


def excerpt(path: Path, lines: int) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
    elif ext in (".xlsx", ".xls"):
        import pandas as pd
        sheets = pd.read_excel(path, sheet_name=None, header=None, nrows=lines)
        text = "\n".join(f"--- sheet: {n} ---\n{df.to_string(max_cols=12, max_colwidth=30)}"
                         for n, df in list(sheets.items())[:3])
    elif ext in (".html", ".htm"):
        from bs4 import BeautifulSoup
        text = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "lxml").get_text("\n", strip=True)
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
    out = [ln for ln in text.splitlines() if ln.strip()][:lines]
    return "\n".join(ln[:200] for ln in out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", type=Path)
    ap.add_argument("--lines", type=int, default=40, help="max lines to print (cap 120)")
    ap.add_argument("--zero-rows", action="store_true")
    a = ap.parse_args()

    if a.zero_rows:
        p = METADATA / "artifacts.csv"
        rows = list(csv.DictReader(p.open(encoding="utf-8"))) if p.exists() else []
        zero = [{"local_path": r["local_path"], "source_url": r["source_url"], "content_type": r["content_type"]}
                for r in rows if r["parsed"] == "True" and r["rows_extracted"] == "0"
                and not (r.get("relevance") or "").startswith("irrelevant")]
        emit({"zero_row_artifacts": len(zero), "items": zero[:30]})
        return 0
    if not a.path:
        ap.error("path or --zero-rows required")
    p = a.path if a.path.is_absolute() else ROOT / a.path
    print(excerpt(p, min(a.lines, 120)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
