from pipeline.cleaner import clean_blocks
from pipeline.schema import (
    BLOCK_CODE, BLOCK_HEADER, BLOCK_LIST_ITEM, BLOCK_PARAGRAPH, BLOCK_TABLE, Block, TableData,
)


def para(text, page=None):
    return Block(BLOCK_PARAGRAPH, text, page_number=page)


def texts(blocks):
    return [b.content for b in blocks]


def test_short_document_keeps_text_repeated_on_every_page():
    blocks = [para("Acme Ltd", p) for p in (1, 2, 3)] + [para("Real content here.", 1)]
    kept, removed = clean_blocks(blocks, page_count=3)
    assert texts(kept).count("Acme Ltd") == 3 and removed == []


def test_running_footer_removed_on_long_document_and_logged():
    blocks = []
    for page in range(1, 11):
        blocks += [para(f"Body text of page {page}.", page), para("Acme Ltd | Annual Report 2025", page)]
    kept, removed = clean_blocks(blocks, page_count=10)
    assert "Acme Ltd | Annual Report 2025" not in texts(kept) and len(kept) == 10
    assert any("running header/footer: Acme Ltd | Annual Report 2025 (x10)" in line for line in removed)


def test_values_headings_and_tables_that_repeat_are_kept():
    table = TableData(["a"], [["Nil"]])
    blocks = []
    for page in range(1, 11):
        blocks += [
            para("Nil", page), para("12.5%", page), para("N.A.", page),
            Block(BLOCK_HEADER, "Notes", page_number=page, level=2),
            Block(BLOCK_TABLE, table.to_markdown(), page_number=page, table=table),
            Block(BLOCK_LIST_ITEM, "see note", page_number=page),
        ]
    kept, removed = clean_blocks(blocks, page_count=10)
    assert len(kept) == len(blocks) and removed == []


def test_repetition_pass_is_skipped_for_page_less_documents():
    blocks = [para("Menu") for _ in range(30)]
    kept, _ = clean_blocks(blocks, page_count=None)
    assert len(kept) == 30


def test_denylist_patterns_are_removed():
    blocks = [para("Page 3 of 12", 3), para("page 4", 4), para("----------", 1),
              para("Scanned with CamScanner", 1), para("Revenue grew 7 percent.", 1)]
    kept, removed = clean_blocks(blocks, page_count=4)
    assert texts(kept) == ["Revenue grew 7 percent."] and len(removed) == 4


def test_printed_page_numbers_are_removed_only_at_page_edges():
    blocks = []
    for page in range(1, 6):      # footer numbers offset by 10 from the pdf page index (front matter)
        blocks += [para(f"Body of page {page}.", page), para(str(page + 10), page)]
    blocks += [para("Last page body.", 6), para("- 6 -", 6)]            # equals its page number
    kept, removed = clean_blocks(blocks, page_count=6)
    assert texts(kept) == [f"Body of page {p}." for p in range(1, 6)] + ["Last page body."]
    assert removed == ["printed page number (x6)"]


def test_loose_numbers_are_data_not_page_numbers():
    # what a table looks like when structure detection fails: cells as separate paragraphs
    blocks = [para("Segment results", 3), para("Services", 3), para("1,240", 3), para("810", 3),
              para("Others", 3), para("95", 3), para("Closing remarks.", 3), para("2025")]
    kept, removed = clean_blocks(blocks, page_count=3)
    assert texts(kept) == [b.content for b in blocks] and removed == []

    ends_with_number = [para("Total employees", 4), para("95", 4)]       # last block, but no page-number pattern
    kept, removed = clean_blocks(ends_with_number, page_count=9)
    assert texts(kept) == ["Total employees", "95"] and removed == []


def test_whitespace_normalised_only_in_prose_and_input_untouched():
    table = TableData(["a  b"], [["1   2"]])
    original = para("  spread \n over\tlines  ", 1)
    blocks = [
        original,
        Block(BLOCK_CODE, "def f():\n    return 1", page_number=1),
        Block(BLOCK_LIST_ITEM, "item  one", page_number=1),
        Block(BLOCK_TABLE, table.to_markdown(), page_number=1, table=table),
        para("   ", 1),
    ]
    kept, _ = clean_blocks(blocks, page_count=1)
    assert kept[0].content == "spread over lines" and original.content == "  spread \n over\tlines  "
    assert kept[1].content == "def f():\n    return 1" and kept[2].content == "item  one"
    assert kept[3].table is table and kept[3].page_number == 1
    assert len(kept) == 4  # the blank paragraph is dropped
