from pipeline.chunker import chunk_document, estimate_tokens
from pipeline.schema import (
    BLOCK_CAPTION, BLOCK_HEADER, BLOCK_LIST_ITEM, BLOCK_PARAGRAPH, BLOCK_TABLE, Block, TableData,
)


def words(text):
    return len(text.split())


def para(text, page=None):
    return Block(BLOCK_PARAGRAPH, text, page_number=page)


def head(text, level, page=None):
    return Block(BLOCK_HEADER, text, page_number=page, level=level)


def table_block(header, rows, page=None):
    t = TableData(header, rows)
    return Block(BLOCK_TABLE, t.to_markdown(), page_number=page, table=t)


def sentence(i, n=10):
    return " ".join(f"w{i}_{k}" for k in range(n - 1)) + f" end{i}."


def test_no_chunk_exceeds_the_limit_and_nothing_is_lost():
    blocks = [para(sentence(i), page=1 + i // 5) for i in range(40)]
    chunks = chunk_document(blocks, size_fn=words, max_tokens=50, overlap_tokens=0)
    assert len(chunks) > 1 and all(words(c.text) <= 50 for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    joined = " ".join(c.text for c in chunks)
    assert all(f"end{i}." in joined for i in range(40))


def test_oversized_paragraph_is_split_by_sentence_then_word_then_character():
    long_para = " ".join(sentence(i) for i in range(30))            # 300 words in sentences
    no_sentences = " ".join(f"x{k}" for k in range(120))            # 120 words, no punctuation
    one_word = "z" * 400                                            # a single unbreakable token run
    for text, size_fn, limit in ((long_para, words, 40), (no_sentences, words, 40), (one_word, estimate_tokens, 20)):
        chunks = chunk_document([para(text)], size_fn=size_fn, max_tokens=limit, overlap_tokens=0)
        assert len(chunks) > 1 and all(size_fn(c.text) <= limit for c in chunks)
        assert "".join(c.text for c in chunks).replace(" ", "").replace("\n", "") == text.replace(" ", "")


def test_overlap_repeats_the_tail_within_a_section_only():
    blocks = [head("One", 1)] + [para(sentence(i)) for i in range(12)] + [head("Two", 1), para(sentence(99))]
    chunks = chunk_document(blocks, size_fn=words, max_tokens=45, overlap_tokens=12)
    one = [c for c in chunks if c.section_path == "One"]
    assert len(one) >= 2 and all(words(c.text) <= 45 for c in chunks)
    for prev, nxt in zip(one, one[1:]):
        assert prev.text.split()[-1] in nxt.text.split()[:12]     # tail of prev starts next
    two = [c for c in chunks if c.section_path == "Two"]
    assert len(two) == 1 and "end11." not in two[0].text          # no overlap across the section break


def test_section_path_follows_nested_headings_and_headings_never_dangle():
    blocks = [
        head("Report", 1), para("Intro text."),
        head("Financials", 2), head("Revenue", 3), para("Revenue rose."),
        head("Costs", 3), para("Costs fell."),
        head("Outlook", 2), para("Stable."),
    ]
    chunks = chunk_document(blocks, size_fn=words, max_tokens=200, overlap_tokens=0)
    assert [c.section_path for c in chunks] == ["Report", "Report > Financials", "Report > Outlook"]
    assert chunks[1].text == "## Financials\n\n### Revenue\n\nRevenue rose.\n\n### Costs\n\nCosts fell."
    assert not any(c.text.rstrip().splitlines()[-1].startswith("#") for c in chunks)


def test_tables_are_isolated_keep_whole_rows_and_repeat_the_header():
    rows = [[f"item{i}", str(i * 10)] for i in range(25)]
    blocks = [head("Results", 1, 2), para("Before the table.", 2), Block(BLOCK_CAPTION, "Table 1: Sales", 2),
              table_block(["name", "value"], rows, page=3), para("After the table.", 4)]
    chunks = chunk_document(blocks, size_fn=words, max_tokens=60, overlap_tokens=10)
    tables = [c for c in chunks if c.content_kind == "table"]
    prose = [c for c in chunks if c.content_kind == "prose"]
    assert len(tables) > 1 and [c.text for c in prose] == ["# Results\n\nBefore the table.", "After the table."]
    assert tables[0].text.startswith("Table 1: Sales\n\n| name | value |")
    assert all("| name | value |" in c.text and c.section_path == "Results" and c.page_range == "3" for c in tables)
    assert all(c.metadata["table_ordinal"] == 0 and c.metadata["caption"] == "Table 1: Sales" for c in tables)
    assert tables[0].metadata["row_start"] == 1 and tables[-1].metadata["row_end"] == 25
    body = [line for c in tables for line in c.text.splitlines() if line.startswith("| item")]
    assert body == [f"| item{i} | {i * 10} |" for i in range(25)]   # every row once, none split
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_page_range_spans_only_the_blocks_in_the_chunk():
    blocks = [para(sentence(0), 5), para(sentence(1), 6), para(sentence(2), 7), para(sentence(3), 7)]
    chunks = chunk_document(blocks, size_fn=words, max_tokens=22, overlap_tokens=0)
    assert [c.page_range for c in chunks] == ["5-6", "7"]
    html = chunk_document([para("No pages here.")], size_fn=words)
    assert html[0].page_range is None


def test_lists_render_compactly_and_output_is_deterministic():
    blocks = [para("Points:"), Block(BLOCK_LIST_ITEM, "first"), Block(BLOCK_LIST_ITEM, "second"), para("Done.")]
    a = chunk_document(blocks, size_fn=words)
    b = chunk_document(blocks, size_fn=words)
    assert a == b and a[0].text == "Points:\n\n- first\n- second\n\nDone."


def test_empty_and_heading_only_documents():
    assert chunk_document([]) == []
    assert chunk_document([para("   ")]) == []
    only = chunk_document([head("Title", 1), head("Sub", 2)], size_fn=words)
    assert len(only) == 1 and only[0].text == "# Title\n\n## Sub" and only[0].section_path == "Title > Sub"
    table_only = chunk_document([head("T", 1), table_block(["a"], [["1"]])], size_fn=words)
    assert [c.content_kind for c in table_only] == ["table"] and table_only[0].section_path == "T"
