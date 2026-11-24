import math

import lancedb
import pyarrow as pa
import pytest

from pipeline.schema import (
    VECTOR_INDEX_KWARGS, Chunk, ChunkRecord, SourceDocument, TableData,
    build_record, compute_doc_id, make_chunk_id, normalize_allowed_roles,
)

KW = dict(ingested_at="2026-11-10T00:00:00+00:00", ingest_run_id="r1",
          embedding_model="BAAI/bge-m3", pipeline_version="0.1.0")


def unit_vec(*head):
    v = list(head) + [0.0] * (1024 - len(head))
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def doc(**over):
    base = dict(path="x", doc_id="a" * 32, source_type="pdf", source_filename="x.pdf")
    return SourceDocument(**{**base, **over})


def test_chunk_id_is_deterministic_and_sensitive():
    a = make_chunk_id("d" * 32, 0, "Disclaimer")
    assert a == make_chunk_id("d" * 32, 0, "Disclaimer") and len(a) == 32
    assert a != make_chunk_id("d" * 32, 1, "Disclaimer")  # repeated text in one doc must not collide
    assert a != make_chunk_id("d" * 32, 0, "Disclaimer.")
    assert a != make_chunk_id("e" * 32, 0, "Disclaimer")


def test_doc_id_is_content_addressed(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"same")
    (tmp_path / "b.bin").write_bytes(b"same")
    (tmp_path / "c.bin").write_bytes(b"diff")
    assert compute_doc_id(tmp_path / "a.bin") == compute_doc_id(tmp_path / "b.bin")
    assert compute_doc_id(tmp_path / "a.bin") != compute_doc_id(tmp_path / "c.bin")


def test_vector_column_is_fixed_size_1024_and_metric_is_cosine():
    t = ChunkRecord.to_arrow_schema().field("embedding").type
    assert pa.types.is_fixed_size_list(t) and t.list_size == 1024
    assert VECTOR_INDEX_KWARGS["metric"] == "cosine"


def test_build_record_rejects_contract_violations():
    with pytest.raises(ValueError):
        build_record(doc(), Chunk(index=0, text="x"), [0.1] * 10, **KW)
    with pytest.raises(ValueError):
        build_record(doc(), Chunk(index=0, text="   "), unit_vec(1), **KW)
    with pytest.raises(ValueError):
        build_record(doc(), Chunk(index=0, text="x", content_kind="img"), unit_vec(1), **KW)


def test_lancedb_roundtrip_with_and_without_vendor_metadata(tmp_path):
    table = lancedb.connect(str(tmp_path)).create_table("t", schema=ChunkRecord)
    full = doc(doc_id="b" * 32, source_type="csv", symbol="TCS", allowed_roles=normalize_allowed_roles("analyst, admin"),
               extra={"vendor_doc_id": "X-9"})
    table.add([
        build_record(doc(), Chunk(index=0, text="hello"), unit_vec(1, 0), **KW),  # no vendor metadata at all
        build_record(full, Chunk(index=0, text="a | b", content_kind="table", metadata={"sheet": "S1"}), unit_vec(0, 1), **KW),
    ])
    rows = {r["doc_id"]: r for r in table.to_arrow().to_pylist()}
    assert rows["a" * 32]["allowed_roles"] is None and rows["a" * 32]["symbol"] is None
    assert rows["b" * 32]["allowed_roles"] == ["analyst", "admin"]
    assert '"sheet": "S1"' in rows["b" * 32]["metadata_json"] and "X-9" in rows["b" * 32]["metadata_json"]

    nearest = table.search(unit_vec(1, 0)).metric("cosine").limit(1).to_list()[0]
    assert nearest["doc_id"] == "a" * 32 and abs(nearest["_distance"]) < 1e-5


def test_table_markdown_repeats_header_and_escapes_cells():
    t = TableData(header=["a", "b"], rows=[["1", "x|y"], ["2", "line1\nline2"], ["3"]])
    md = t.to_markdown()
    assert md.splitlines()[0] == "| a | b |" and md.splitlines()[1] == "| --- | --- |"
    assert "x\\|y" in md and "line1 line2" in md and "| 3 |  |" in md
    assert t.to_markdown(1, 2).splitlines()[0] == "| a | b |"  # a slice still carries the header


# ---- downstream-LLM columns -----------------------------------------------------

LLM_FIELDS = """isin nse_symbol series sector industry market_cap_cr free_float_pct is_fno trade_date open high low
close total_traded_qty turnover_cr deliverable_qty delivery_pct no_of_trades bulk_block_deal_flag futures_oi
oi_change_pct quarter_ending sales_growth_yoy sales_growth_qoq ebitda_margin_pct pat_growth_yoy eps_ttm roce_pct
roe_pct debt_to_equity interest_coverage cash_flow_from_ops pe_ratio sector_median_pe ev_ebitda peg_ratio
promoter_holding_pct promoter_pledge_pct fii_holding_chg_qoq dii_holding_chg_qoq retail_holding_chg_qoq
surveillance_stage broadcast_timestamp announcement_category announcement_headline announcement_body_md
concall_commentary_md concall_guidance_md sast_insider_activity bulk_block_deal_participants""".split()


def test_every_llm_field_is_a_nullable_typed_column():
    from pipeline.schema import EXTENDED_FIELDS

    assert len(LLM_FIELDS) == 50 and len(set(LLM_FIELDS)) == 50
    assert set(EXTENDED_FIELDS) == set(LLM_FIELDS)

    arrow = ChunkRecord.to_arrow_schema()
    for name in LLM_FIELDS:
        field = arrow.field(name)
        assert field.nullable, name
        expected = {str: pa.types.is_string, float: pa.types.is_floating, int: pa.types.is_integer, bool: pa.types.is_boolean}
        assert expected[EXTENDED_FIELDS[name]](field.type), (name, field.type)


def test_coerce_extended_converts_or_nulls_never_raises():
    from pipeline.schema import coerce_extended

    clean, rejected = coerce_extended({
        "close": "1,234.50", "pe_ratio": "(12.5)", "promoter_holding_pct": "51.2%", "no_of_trades": "1500",
        "total_traded_qty": 12.0, "is_fno": "Yes", "bulk_block_deal_flag": 0, "isin": " INE002A01018 ",
        "announcement_body_md": ["a", "b"], "open": "n/a", "futures_oi": 1.5, "is_fno_x": "ignored", "high": "",
        "low": True,
    })
    assert clean["close"] == 1234.5 and clean["pe_ratio"] == -12.5 and clean["promoter_holding_pct"] == 51.2
    assert clean["no_of_trades"] == 1500 and clean["total_traded_qty"] == 12 and isinstance(clean["total_traded_qty"], int)
    assert clean["is_fno"] is True and clean["bulk_block_deal_flag"] is False
    assert clean["isin"] == "INE002A01018" and clean["announcement_body_md"] == '["a", "b"]'
    assert clean["high"] is None
    assert sorted(rejected) == ["futures_oi", "low", "open"]  # 'n/a', fractional int, bool-as-number
    assert "is_fno_x" not in clean


def test_chunk_fields_override_document_fields_and_roundtrip(tmp_path):
    table = lancedb.connect(str(tmp_path)).create_table("t", schema=ChunkRecord)
    d = doc(fields={"isin": "INE002A01018", "sector": "Energy", "close": 100.0, "is_fno": True})
    row_a = build_record(d, Chunk(index=0, text="row a", fields={"close": 101.5, "trade_date": "2025-11-07", "futures_oi": 5000}),
                         unit_vec(1), **KW)
    row_b = build_record(d, Chunk(index=1, text="row b"), unit_vec(0, 1), **KW)
    bare = build_record(doc(doc_id="c" * 32), Chunk(index=0, text="no market data"), unit_vec(1, 1), **KW)
    table.add([row_a, row_b, bare])

    rows = {r["text"]: r for r in table.to_arrow().to_pylist()}
    assert (rows["row a"]["close"], rows["row a"]["trade_date"], rows["row a"]["futures_oi"]) == (101.5, "2025-11-07", 5000)
    assert (rows["row b"]["close"], rows["row b"]["isin"], rows["row b"]["is_fno"]) == (100.0, "INE002A01018", True)
    assert all(rows["no market data"][name] is None for name in LLM_FIELDS)
