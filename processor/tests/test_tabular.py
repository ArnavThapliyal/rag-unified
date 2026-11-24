import json

import pytest
from openpyxl import Workbook

from pipeline.schema import CONTENT_KIND_TABLE, TableData
from pipeline.tabular import read_table_chunks, table_chunks


def read(path, kind, **kw):
    return list(read_table_chunks(path, kind, **kw))


def test_csv_header_repeats_in_every_chunk_and_row_ranges_are_contiguous(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("sym,price\n" + "\n".join(f"S{i},{i}" for i in range(1, 11)) + "\n")
    chunks = read(p, "csv", rows_per_chunk=4)
    assert [(c.metadata["row_start"], c.metadata["row_end"]) for c in chunks] == [(1, 4), (5, 8), (9, 10)]
    assert [c.index for c in chunks] == [0, 1, 2]
    for c in chunks:
        assert c.content_kind == CONTENT_KIND_TABLE
        assert c.text.splitlines()[0] == "| sym | price |"
        assert c.metadata["columns"] == ["sym", "price"]


def test_csv_preserves_numeric_strings_and_fixes_headers(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("id,,id\n0010,1.50,x\n,,\n0020,2,y\n")  # blank + duplicate header, a fully blank row
    (chunk,) = read(p, "csv")
    assert chunk.metadata["columns"] == ["id", "col_2", "id_2"]
    assert "0010" in chunk.text and "1.50" in chunk.text  # not coerced to 10 / 1.5
    assert chunk.metadata["row_end"] == 2  # blank row dropped


def test_csv_semicolon_delimiter_and_latin1(tmp_path):
    p = tmp_path / "a.csv"
    p.write_bytes("name;value\nCafé;1\n".encode("cp1252"))
    (chunk,) = read(p, "csv")
    assert chunk.metadata["columns"] == ["name", "value"] and "Café" in chunk.text


def test_row_size_budget_splits_chunks(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("a,b\n" + "\n".join(f"{'x' * 50},{i}" for i in range(20)) + "\n")
    chunks = read(p, "csv", rows_per_chunk=1000, max_size=200)
    assert len(chunks) > 1 and all(len(c.text) <= 260 for c in chunks)
    assert sum(c.metadata["row_end"] - c.metadata["row_start"] + 1 for c in chunks) == 20


def test_max_rows_truncates_with_warning(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("a\n" + "\n".join(str(i) for i in range(50)) + "\n")
    warnings = []
    chunks = read(p, "csv", max_rows=10, rows_per_chunk=100, warnings=warnings)
    assert chunks[-1].metadata["row_end"] == 10 and any("truncated" in w for w in warnings)


def test_xlsx_multi_sheet_numbers_chunks_consecutively(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Q1"
    ws.append(["name", "revenue"])
    ws.append(["A", 100.0])
    ws.append(["B", 2.5])
    ws2 = wb.create_sheet("Q2")
    ws2.append(["name", "revenue"])
    ws2.append(["C", 7])
    p = tmp_path / "a.xlsx"
    wb.save(p)
    chunks = read(p, "xlsx")
    assert [c.metadata["sheet"] for c in chunks] == ["Q1", "Q2"]
    assert [c.index for c in chunks] == [0, 1]
    assert "| A | 100 |" in chunks[0].text and "2.5" in chunks[0].text  # 100.0 -> 100, 2.5 kept


def test_json_list_nested_object_and_jsonl(tmp_path):
    records = [{"id": 1, "meta": {"k": "v"}, "tags": ["a", "b"]}, {"id": 2, "extra": True}]
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"data": records, "page": 1}))
    (chunk,) = read(p, "json")
    assert chunk.metadata["columns"] == ["id", "meta.k", "tags", "extra"]
    assert '["a", "b"]' in chunk.text

    q = tmp_path / "b.jsonl"
    q.write_text("\n".join(json.dumps(r) for r in records))
    assert read(q, "json")[0].metadata["columns"] == ["id", "meta.k", "tags", "extra"]


def test_json_without_tabular_shape_raises(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError):
        read(p, "json")


def test_unknown_source_type_raises(tmp_path):
    with pytest.raises(ValueError):
        read(tmp_path / "x", "pdf")


def test_table_chunks_for_in_memory_tables_carry_provenance():
    t = TableData(header=["a", "b"], rows=[[str(i), "v"] for i in range(5)])
    chunks = table_chunks(t, start_index=7, rows_per_chunk=2, section_path="Sec", page_range="3")
    assert [c.index for c in chunks] == [7, 8, 9]
    assert all(c.section_path == "Sec" and c.page_range == "3" for c in chunks)


# ---- record mode, table identity, bad rows ------------------------------------

PRICES = "SYMBOL,trade_date,Open,HIGH,low,close,total_traded_qty,is_fno,note\n" \
         "TCS,2025-11-07,\"4,100.5\",4150,4090,4120.25,1500000,Y,ok\n" \
         "INFY,2025-11-07,1500,1510,n/a,1505,,N,\n"


def test_price_file_becomes_one_chunk_per_row_with_typed_fields(tmp_path):
    p = tmp_path / "bhav.csv"
    p.write_text(PRICES)
    chunks = list(read_table_chunks(p, "csv"))
    assert len(chunks) == 2 and all(c.content_kind == "table" for c in chunks)
    tcs, infy = chunks
    assert tcs.symbol == "TCS" and infy.symbol == "INFY"
    assert tcs.fields == {"trade_date": "2025-11-07", "open": 4100.5, "high": 4150.0, "low": 4090.0,
                          "close": 4120.25, "total_traded_qty": 1500000, "is_fno": True}
    assert tcs.text.splitlines()[:3] == ["SYMBOL: TCS", "trade_date: 2025-11-07", "Open: 4,100.5"]
    assert "low" not in infy.fields and infy.metadata["unconverted"] == {"low": "n/a"}  # null column, value kept
    assert "note" not in infy.text and "total_traded_qty" not in infy.text              # empty cells are omitted
    assert (tcs.metadata["row_start"], tcs.metadata["row_end"], infy.metadata["row_start"]) == (1, 1, 2)
    assert tcs.metadata["table_ordinal"] == 0 and tcs.metadata["record"] is True


def test_record_mode_off_or_too_few_matching_columns_keeps_row_groups(tmp_path):
    p = tmp_path / "bhav.csv"
    p.write_text(PRICES)
    grouped = list(read_table_chunks(p, "csv", record_mode="off"))
    assert len(grouped) == 1 and grouped[0].fields == {} and grouped[0].text.startswith("| SYMBOL |")

    q = tmp_path / "misc.csv"
    q.write_text("name,close,comment\nA,1,x\nB,2,y\n")   # only one matching column
    assert len(list(read_table_chunks(q, "csv"))) == 1


def test_each_sheet_is_its_own_table_with_a_stable_ordinal(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Q1"
    ws.append(["name", "revenue"])
    ws.append(["A", 1])
    ws2 = wb.create_sheet("Q2")
    ws2.append(["name", "revenue"])
    ws2.append(["B", 2])
    p = tmp_path / "a.xlsx"
    wb.save(p)
    first = [(c.metadata["table_ordinal"], c.metadata["sheet"]) for c in read_table_chunks(p, "xlsx")]
    again = [(c.metadata["table_ordinal"], c.metadata["sheet"]) for c in read_table_chunks(p, "xlsx")]
    assert first == again == [(0, "Q1"), (1, "Q2")]


def test_unparseable_rows_are_skipped_and_counted_not_fatal(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"a": 1}\n{broken\n{"a": 2}\n')
    warnings = []
    chunks = list(read_table_chunks(p, "json", warnings=warnings))
    assert "| 1 |" in chunks[0].text and "| 2 |" in chunks[0].text
    assert any("skipped 1 malformed JSON line" in w for w in warnings)

    import pytest
    garbage = tmp_path / "garbage.json"
    garbage.write_text("this is not json\nat all\n")
    with pytest.raises(ValueError):
        list(read_table_chunks(garbage, "json"))
