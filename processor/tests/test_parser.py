"""
Parser tests on formats docling handles without ML models (docx, html), which
is enough to prove the item-class mapping (the bug that made headings paragraphs).
A model-backed PDF smoke test lives in test_parser_pdf.py behind RUN_SLOW=1.
"""

import pytest
from docx import Document

from pipeline.parser import Parser
from pipeline.schema import BLOCK_HEADER, BLOCK_LIST_ITEM, BLOCK_PARAGRAPH, BLOCK_TABLE


@pytest.fixture(scope="module")
def parser():
    return Parser()


def kinds(result):
    return [(b.type, b.content) for b in result.blocks]


def test_docx_headings_lists_and_tables_are_recognised(parser, tmp_path):
    d = Document()
    d.add_heading("Annual Report", level=1)
    d.add_heading("Financial Highlights", level=2)
    d.add_paragraph("Revenue grew strongly.")
    d.add_paragraph("first point", style="List Bullet")
    t = d.add_table(rows=3, cols=2)
    for r, row in enumerate([["Metric", "FY25"], ["Revenue", "1,200"], ["Profit", "300"]]):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    p = tmp_path / "r.docx"
    d.save(p)

    result = parser.parse(p)
    by_type = {}
    for b in result.blocks:
        by_type.setdefault(b.type, []).append(b)

    assert [(b.content, b.level) for b in by_type[BLOCK_HEADER]] == [("Annual Report", 1), ("Financial Highlights", 2)]
    assert [b.content for b in by_type[BLOCK_LIST_ITEM]] == ["first point"]
    assert any("Revenue grew" in b.content for b in by_type[BLOCK_PARAGRAPH])

    (table,) = by_type[BLOCK_TABLE]
    assert table.table.header == ["Metric", "FY25"]
    assert table.table.rows == [["Revenue", "1,200"], ["Profit", "300"]]
    assert table.content.splitlines()[0] == "| Metric | FY25 |"
    assert all(b.page_number is None for b in result.blocks)  # docx has no pages
    assert result.page_count is None and result.ocr_pages == 0


def test_html_structure(parser, tmp_path):
    p = tmp_path / "r.html"
    p.write_text(
        "<html><body><h1>Title</h1><h2>Section</h2><p>Body text.</p>"
        "<ul><li>one</li><li>two</li></ul>"
        "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
    )
    result = parser.parse(p)
    types = [b.type for b in result.blocks]
    assert types.count(BLOCK_HEADER) == 2 and types.count(BLOCK_LIST_ITEM) == 2 and types.count(BLOCK_TABLE) == 1
    table = next(b for b in result.blocks if b.type == BLOCK_TABLE)
    assert table.table.header == ["a", "b"] and table.table.rows == [["1", "2"]]


def test_missing_file_raises_filenotfound(parser, tmp_path):
    with pytest.raises(FileNotFoundError):
        parser.parse(tmp_path / "nope.pdf")


def test_corrupt_file_raises_instead_of_returning_empty(parser, tmp_path):
    p = tmp_path / "bad.docx"
    p.write_bytes(b"this is not a docx")
    with pytest.raises(Exception):
        parser.parse(p)


def test_header_promotion_heuristic():
    from pipeline.parser import _looks_like_header
    assert _looks_like_header(["Metric", "FY24", "FY25"])
    assert not _looks_like_header(["Revenue", "1,200", "(300)"])   # data row
    assert not _looks_like_header(["", "2024", "2023"])             # blank cell / year labels: keep generic names
    assert not _looks_like_header(["Item", "12.5%", "7"])


def test_low_text_threshold_uses_the_instance_budget():
    """Neon L-3: a per-instance min_chars_per_page_before_ocr is honoured."""
    from pipeline.parser import Parser
    from pipeline.schema import Block

    blocks = [Block("paragraph", "x" * 40, 1), Block("paragraph", "y" * 5, 2)]
    assert Parser({"min_chars_per_page_before_ocr": 25})._low_text_pages(blocks, 2) == [2]
    assert Parser({"min_chars_per_page_before_ocr": 100})._low_text_pages(blocks, 2) == [1, 2]
