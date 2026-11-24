"""
Model-backed PDF tests (layout + table models, EasyOCR). They download model
weights on first run and take a while, so they only run with RUN_SLOW=1.
"""

import os

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from pipeline.parser import Parser
from pipeline.schema import BLOCK_HEADER, BLOCK_TABLE

pytestmark = pytest.mark.skipif(not os.environ.get("RUN_SLOW"), reason="set RUN_SLOW=1 to run model-backed tests")


@pytest.fixture(scope="module")
def parser():
    return Parser()


def test_text_pdf_headings_table_and_page_numbers(parser, tmp_path):
    styles = getSampleStyleSheet()
    grid = Table([["Metric", "FY24", "FY25"], ["Revenue", "1,000", "1,200"], ["Profit", "200", "300"], ["Margin", "20%", "25%"]])
    grid.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.8, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    story = [
        Paragraph("Management Discussion and Analysis", styles["Heading1"]),
        Paragraph("The company delivered strong growth during the financial year across all of its business segments. " * 4, styles["BodyText"]),
        Paragraph("Financial Highlights", styles["Heading2"]),
        Spacer(1, 10), grid, Spacer(1, 10),
        PageBreak(),
        Paragraph("Risk Factors", styles["Heading1"]),
        Paragraph("Market conditions may change and affect the results of operations in future periods. " * 4, styles["BodyText"]),
    ]
    p = tmp_path / "report.pdf"
    SimpleDocTemplate(str(p), pagesize=A4).build(story)

    result = parser.parse(p)
    headers = [b.content for b in result.blocks if b.type == BLOCK_HEADER]
    assert "Management Discussion and Analysis" in headers and "Risk Factors" in headers
    tables = [b for b in result.blocks if b.type == BLOCK_TABLE]
    assert tables and tables[0].table.header[0] == "Metric" and ["Revenue", "1,000", "1,200"] in tables[0].table.rows
    assert result.page_count == 2
    risk = next(b for b in result.blocks if b.content == "Risk Factors")
    assert risk.page_number == 2
    assert result.ocr_pages == 0


def test_scanned_pdf_triggers_ocr(parser, tmp_path):
    img = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    for i, line in enumerate(["Quarterly Revenue Summary", "Total revenue was 4500 crore", "Net profit was 800 crore"]):
        draw.text((100, 200 + i * 120), line, fill="black", font=font)
    p = tmp_path / "scan.pdf"
    img.save(p, "PDF", resolution=150)

    result = parser.parse(p)
    text = " ".join(b.content for b in result.blocks).lower()
    assert result.ocr_pages == 1
    assert "revenue" in text and "4500" in text.replace(",", "")
