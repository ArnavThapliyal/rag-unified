import warnings

import lancedb
import pytest

import pipeline.vector_store as vs
from pipeline.schema import Chunk, SourceDocument, build_record
from pipeline.vector_store import SchemaMismatchError, VectorStore
from stub_embedder import text_vector

KW = dict(ingested_at="2026-11-16T00:00:00+00:00", ingest_run_id="r1",
          embedding_model="BAAI/bge-m3", pipeline_version="0.1.0")


def doc_id(n):
    return f"{n:032x}"


def rows(n_doc, count, **doc_over):
    doc = SourceDocument(path="p", doc_id=doc_id(n_doc), source_type="pdf", source_filename=f"f{n_doc}.pdf", **doc_over)
    return [build_record(doc, Chunk(index=i, text=f"doc{n_doc} chunk{i}"), text_vector(f"doc{n_doc} chunk{i}"), **KW)
            for i in range(count)]


@pytest.fixture
def store(tmp_path):
    return VectorStore(tmp_path / "db", "documents").open_or_create()


def test_create_add_reopen_and_nulls_for_missing_metadata(tmp_path, store):
    assert store.add(rows(1, 3)) == 3 and store.add([]) == 0
    assert store.add(rows(2, 2, symbol="TCS", allowed_roles=["analyst"], fields={"close": 101.5})) == 2

    again = VectorStore(tmp_path / "db", "documents").open_or_create()
    assert again.count() == 5
    got = again.get_by_doc_id(doc_id(1))
    assert [r["chunk_index"] for r in got] == [0, 1, 2]
    assert got[0]["symbol"] is None and got[0]["allowed_roles"] is None and got[0]["close"] is None
    tcs = again.get_by_doc_id(doc_id(2))[0]
    assert (tcs["symbol"], tcs["allowed_roles"], tcs["close"]) == ("TCS", ["analyst"], 101.5)
    assert len(tcs["embedding"]) == 1024


def test_existing_doc_ids_has_doc_and_delete(store):
    store.add(rows(1, 2) + rows(2, 2))
    assert store.existing_doc_ids([doc_id(1), doc_id(2), doc_id(3)]) == {doc_id(1), doc_id(2)}
    assert store.existing_doc_ids([]) == set()
    store.delete_doc(doc_id(1))
    assert not store.has_doc(doc_id(1)) and store.has_doc(doc_id(2)) and store.count() == 2


def test_only_well_formed_doc_ids_reach_a_filter(store):
    for bad in ("x' OR '1'='1", "abc", "ABCDEF" + doc_id(1)[6:], None):
        with pytest.raises(ValueError):
            store.has_doc(bad)
        with pytest.raises(ValueError):
            store.delete_doc(bad)


def test_existing_table_with_a_different_schema_is_refused(tmp_path):
    db = lancedb.connect(str(tmp_path / "db"))
    db.create_table("documents", data=[{"chunk_id": "a", "content": "old", "embedding": [0.1, 0.2]}])
    with pytest.raises(SchemaMismatchError, match="missing column doc_id"):
        VectorStore(tmp_path / "db", "documents").open_or_create()


def test_malformed_row_fails_before_anything_is_written(store):
    good = rows(1, 2)
    bad = dict(good[1], embedding=[0.1, 0.2])
    with pytest.raises(Exception):
        store.add([good[0], bad])
    assert store.count() == 0


def test_partial_document_is_removed_by_recover_after_a_crash(tmp_path, store):
    store.add(rows(1, 2))                 # a complete document
    store.begin_partial(doc_id(2))
    store.add(rows(2, 3))                 # first part written, then the process "dies"

    fresh = VectorStore(tmp_path / "db", "documents").open_or_create()
    assert fresh.recover() == [doc_id(2)]
    assert fresh.count() == 2 and not fresh.has_doc(doc_id(2)) and fresh.recover() == []

    fresh.begin_partial(doc_id(3))
    fresh.add(rows(3, 2))
    fresh.finish_partial(doc_id(3))       # completed normally: survives recovery
    assert fresh.recover() == [] and fresh.has_doc(doc_id(3))

    fresh.begin_partial(doc_id(4))
    fresh.add(rows(4, 2))
    fresh.abort_partial(doc_id(4))
    assert not fresh.has_doc(doc_id(4)) and fresh.recover() == []


def test_finalize_builds_cosine_index_and_search_finds_the_exact_row(store, monkeypatch):
    monkeypatch.setattr(vs, "VECTOR_INDEX_MIN_ROWS", 300)
    assert store.finalize() == {"rows": 0, "vector_index": "none"}
    for n in range(8):
        store.add(rows(n, 50))
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        report = store.finalize()
    assert report == {"rows": 400, "vector_index": "created"}
    by_columns = {tuple(i.columns): i for i in store.table.list_indices()}
    assert {("doc_id",), ("symbol",), ("embedding",)} <= set(by_columns)
    assert by_columns[("embedding",)].index_details["metric_type"] == "COSINE"

    hit = store.table.search(text_vector("doc3 chunk7")).metric("cosine").limit(1).to_list()[0]
    assert hit["text"] == "doc3 chunk7" and hit["_distance"] < 0.2

    store.add(rows(9, 5))
    assert store.finalize()["vector_index"] == "updated" and store.count() == 405


def test_small_table_gets_scalar_indexes_but_no_vector_index(store):
    store.add(rows(1, 5))
    assert store.finalize()["vector_index"] == "none"
    columns = {tuple(i.columns) for i in store.table.list_indices()}
    assert ("doc_id",) in columns and ("embedding",) not in columns
    hit = store.table.search(text_vector("doc1 chunk2")).metric("cosine").limit(1).to_list()[0]
    assert hit["text"] == "doc1 chunk2" and abs(hit["_distance"]) < 1e-5
