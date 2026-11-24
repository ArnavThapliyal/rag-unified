"""End-to-end runs into a real LanceDB table, with the embedding model stubbed out."""

import json
import zipfile

import pytest

import pipeline.indexer as indexer_mod
from pipeline.indexer import Indexer, _Run
from pipeline.pipeline import PipelineRunner, discover_files
from pipeline.vector_store import VectorStore
from pipeline.worker import STATUS_FAILED, ItemResult
from stub_embedder import FlakyEmbedder, StubModel, broken_embedder_factory, make_stub_embedder
from pipeline.embedder import Embedder

HTML = """<html><body>
<h1>Annual Report</h1><p>Revenue grew twelve percent year on year.</p>
<h2>Segments</h2><p>The services segment led growth.</p>
<table><tr><th>Segment</th><th>Revenue</th></tr><tr><td>Services</td><td>120</td></tr>
<tr><td>Products</td><td>80</td></tr></table>
</body></html>"""

PRICES = "symbol,trade_date,open,high,low,close\nTCS,2025-11-07,4100,4150,4090,4120.5\nINFY,2025-11-07,1500,1510,1495,1505\n"


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "drop"
    (root / "vendor_a" / "deep").mkdir(parents=True)
    (root / "report.html").write_text(HTML)
    (root / "report.html.meta.json").write_text(json.dumps(
        {"ticker": "ACME", "doc_type": "annual_report", "roles": ["analyst"], "sector": "IT", "custom": 7}))
    (root / "vendor_a" / "prices.csv").write_text(PRICES)
    (root / "vendor_a" / "deep" / "notes.json").write_text(json.dumps([{"topic": "capex", "comment": "rising"}]))
    (root / "vendor_a" / "metadata.json").write_text(json.dumps({"defaults": {"vendor": "vendor_a"}}))
    with zipfile.ZipFile(root / "bundle.zip", "w") as z:
        z.writestr("inner/memo.html", "<html><body><p>Memo inside an archive.</p></body></html>")
    (root / "legacy.doc").write_bytes(b"\xd0\xcf\x11\xe0 old word file")
    (root / "broken.docx").write_text("this is not a real docx")
    return root


def make_indexer(tmp_path, **kw):
    store = VectorStore(tmp_path / "db", "documents")
    kw.setdefault("workers", 0)
    if kw["workers"] == 0:
        kw.setdefault("embedder", Embedder(model=StubModel(), batch_size=16))
    return Indexer(store=store, scratch_dir=tmp_path / "scratch", **kw), store


def all_rows(store):
    return store.open_or_create().table.to_arrow().to_pylist()


def check_first_run(summary, store):
    assert summary["by_status"] == {"indexed": 4, "skipped_unsupported": 1, "failed": 1}
    assert summary["failed_files"] == ["broken.docx"] and summary["files_skipped"] == 1
    assert summary["by_source_type"] == {"html": 2, "csv": 1, "json": 1}
    rows = all_rows(store)
    assert summary["chunks_written"] == len(rows) and len({r["chunk_id"] for r in rows}) == len(rows)
    assert all(len(r["embedding"]) == 1024 and r["ingest_run_id"] == summary["run_id"] for r in rows)

    report = [r for r in rows if r["source_filename"] == "report.html"]
    assert {r["content_kind"] for r in report} == {"prose", "table"}
    assert all((r["symbol"], r["doc_type"], r["allowed_roles"], r["sector"]) == ("ACME", "annual_report", ["analyst"], "IT")
               for r in report)
    table = next(r for r in report if r["content_kind"] == "table")
    meta = json.loads(table["metadata_json"])
    assert "| Segment | Revenue |" in table["text"] and meta["custom"] == 7
    assert meta["table_id"] == f"{table['doc_id']}:0" and (meta["row_start"], meta["row_end"]) == (1, 2)
    assert "Segments" in table["section_path"]

    prices = sorted((r for r in rows if r["source_filename"] == "prices.csv"), key=lambda r: r["chunk_index"])
    assert [(r["symbol"], r["close"], r["trade_date"], r["vendor"]) for r in prices] == [
        ("TCS", 4120.5, "2025-11-07", "vendor_a"), ("INFY", 1505.0, "2025-11-07", "vendor_a")]
    assert all(r["content_kind"] == "table" and r["source_type"] == "csv" for r in prices)

    notes = next(r for r in rows if r["source_filename"] == "notes.json")       # metadata inherited from a parent dir
    assert notes["vendor"] == "vendor_a" and notes["symbol"] is None and notes["allowed_roles"] is None
    memo = next(r for r in rows if r["source_filename"] == "bundle.zip!memo.html")
    assert memo["text"] == "Memo inside an archive." and memo["vendor"] is None and memo["page_range"] is None
    return len(rows)


def test_inline_run_ingests_documents_and_tables_and_a_rerun_writes_nothing(tree, tmp_path):
    indexer, store = make_indexer(tmp_path)
    count = check_first_run(indexer.index_files(discover_files(tree), tree), store)
    assert not any((tmp_path / "scratch").iterdir())          # zip extraction cleaned up

    again, store2 = make_indexer(tmp_path)
    summary = again.index_files(discover_files(tree), tree)
    assert summary["by_status"] == {"skipped_duplicate": 4, "skipped_unsupported": 1, "failed": 1}
    assert summary["chunks_written"] == 0 and len(all_rows(store2)) == count


def test_parallel_run_matches_the_inline_run(tree, tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))    # child processes re-read settings from the environment
    indexer, store = make_indexer(tmp_path, workers=2, embedder_factory=make_stub_embedder)
    count = check_first_run(indexer.index_files(discover_files(tree), tree), store)

    again, store2 = make_indexer(tmp_path, workers=2, embedder_factory=make_stub_embedder)
    summary = again.index_files(discover_files(tree), tree)
    assert summary["files_indexed"] == 0 and summary["by_status"]["skipped_duplicate"] == 4
    assert len(all_rows(store2)) == count

    inline, inline_store = make_indexer(tmp_path / "other")
    inline.index_files(discover_files(tree), tree)
    assert sorted(r["chunk_id"] for r in all_rows(inline_store)) == sorted(r["chunk_id"] for r in all_rows(store2))


def test_exit_code_semantics_through_the_runner(tmp_path):
    root = tmp_path / "ok"
    root.mkdir()
    (root / "a.html").write_text("<html><body><p>Only good files here.</p></body></html>")
    (root / "empty.html").write_text("<html><body></body></html>")
    indexer, _ = make_indexer(tmp_path)
    summary = PipelineRunner(indexer=indexer).run(root)
    assert (summary["files_found"], summary["files_indexed"], summary["files_failed"]) == (2, 1, 0)
    assert summary["by_status"] == {"indexed": 1, "empty": 1}

    rerun = PipelineRunner(indexer=make_indexer(tmp_path)[0]).run(root)
    assert rerun["files_failed"] == 0 and rerun["status"] == "no_data"   # a clean re-run is not a failure


def test_same_bytes_at_two_paths_are_ingested_once(tmp_path):
    root = tmp_path / "dup"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    for folder in ("a", "b"):
        (root / folder / "same.html").write_text("<html><body><p>Identical content.</p></body></html>")
    indexer, store = make_indexer(tmp_path)
    summary = indexer.index_files(discover_files(root), root)
    assert summary["by_status"] == {"indexed": 1, "skipped_duplicate": 1} and len(all_rows(store)) == 1


def big_csv(path, n):
    path.write_text("name,comment\n" + "".join(f"row{i},text {i}\n" for i in range(n)))


def test_large_document_is_written_in_parts_and_completes(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod, "WRITE_MAX_ROWS_PER_ADD", 3)
    root = tmp_path / "big"
    root.mkdir()
    (root / "p.csv").write_text("symbol,trade_date,open,close\n" + "".join(f"S{i},2025-01-01,1,{i}\n" for i in range(40)))
    indexer, store = make_indexer(tmp_path)
    summary = indexer.index_files(discover_files(root), root)
    assert summary["chunks_written"] == 40 and len(all_rows(store)) == 40
    assert store.recover() == []                                   # ledger is clear after a clean finish


def test_embedding_failure_midway_removes_the_partial_document_and_spares_others(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod, "WRITE_MAX_ROWS_PER_ADD", 3)
    root = tmp_path / "flaky"
    root.mkdir()
    (root / "a_good.html").write_text("<html><body><p>Fine.</p></body></html>")
    (root / "b_big.csv").write_text("symbol,trade_date,open,close\n" + "".join(f"S{i},2025-01-01,1,{i}\n" for i in range(40)))
    indexer, store = make_indexer(tmp_path, embedder=FlakyEmbedder(fail_after=20))
    summary = indexer.index_files(discover_files(root), root)
    assert summary["by_status"] == {"indexed": 1, "failed": 1} and summary["failed_files"] == ["b_big.csv"]
    rows = all_rows(store)
    assert [r["source_filename"] for r in rows] == ["a_good.html"]  # nothing of the failed document remains
    assert store.recover() == []


def test_embedding_process_that_cannot_start_aborts_the_run(tree, tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    indexer, _ = make_indexer(tmp_path, workers=1, embedder_factory=broken_embedder_factory)
    with pytest.raises(RuntimeError, match="could not load the embedding model"):
        indexer.index_files(discover_files(tree), tree)


def test_lost_worker_marks_only_its_file_failed(tmp_path):
    store = VectorStore(tmp_path / "db", "documents").open_or_create()
    run = _Run(store, "r1")
    run.on_done(ItemResult(("/in/a.zip", "a.zip!one.html"), "empty"))
    run.on_worker_lost("/in/a.zip", "worker process died (exit code -11)")
    run.on_worker_lost(None, "died at startup")
    run.on_unreported(["/in/a.zip", "/in/never_seen.pdf"])
    by_name = {r.name: (r.status, r.reason) for r in run.results}
    assert by_name["a.zip"] == (STATUS_FAILED, "worker process died (exit code -11)")
    assert by_name["never_seen.pdf"][0] == STATUS_FAILED and by_name["a.zip!one.html"][0] == "empty"
    assert run.summary()["files_failed"] == 2


def _wedged(path, ctx):
    """A parser stuck in native code: ignores SIGTERM and never yields."""
    import signal
    import time

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
    yield  # pragma: no cover


def test_stalled_worker_that_ignores_sigterm_is_killed(tree, tmp_path, monkeypatch):
    """Neon C-2: a stalled worker gets SIGKILL after the grace period; the run ends, nothing is left running."""
    import multiprocessing as mp

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(indexer_mod, "MP_START_METHOD", "fork")   # so the children see the patched parser
    monkeypatch.setattr(indexer_mod, "process_path", _wedged)
    monkeypatch.setattr(indexer_mod, "WORKER_STALL_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(indexer_mod, "_KILL_GRACE_SECONDS", 1)
    indexer, _ = make_indexer(tmp_path, workers=1, embedder_factory=make_stub_embedder)
    files = discover_files(tree)[:1]
    summary = indexer.index_files(files, tree)
    assert summary["files_failed"] == 1
    assert not [p for p in mp.active_children() if p.name.startswith("worker")]


def test_worker_count_is_hard_capped(tmp_path, monkeypatch):
    # 12 docling workers (cpu_count - 2) pushed a 48 GB Mac 31 GB into swap: --workers can't exceed MAX_WORKERS
    import config.settings as settings
    assert 1 <= settings.NUM_WORKERS <= settings.MAX_WORKERS
    monkeypatch.setattr(indexer_mod, "MAX_WORKERS", 4)
    assert Indexer(workers=12, scratch_dir=tmp_path).workers == 4
    assert Indexer(workers=2, scratch_dir=tmp_path).workers == 2
    assert Indexer(workers=0, scratch_dir=tmp_path).workers == 0
