"""Pre-flight triage, the scope gate, windowed PDF parsing with per-range OCR, the denylist, purge, memory report."""

import json

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import pipeline.parser as parser_mod
import pipeline.worker as worker_mod
from pipeline import memwatch, triage
from pipeline.parser import Parser, _page_runs
from pipeline.schema import Block, compute_doc_id
from pipeline.vector_store import VectorStore
from test_indexer_e2e import all_rows, make_indexer


def make_pdf(path, pages):
    """pages: list of page texts ('' = a page with no text layer)."""
    c = canvas.Canvas(str(path), pagesize=A4)
    for text in pages:
        y = 800
        for line in text.splitlines():
            c.drawString(50, y, line)
            y -= 16
        c.showPage()
    c.save()
    return path


BODY = "The company reported revenue growth across its segments during the year under review."


# ---- triage ----------------------------------------------------------------------

def test_probe_counts_pages_and_low_text_pages(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", [BODY, "", BODY, ""])
    pr = triage.probe(p)
    assert (pr.kind, pr.pages, pr.low_text_pages, pr.size_class) == ("pdf", 4, 2, "pdf_1-10p")
    assert not pr.offer_document and pr.est_seconds > 0 and not pr.error


def test_offer_documents_are_recognised_but_passing_mentions_are_not(tmp_path):
    drhp = make_pdf(tmp_path / "drhp.pdf", ["DRAFT RED HERRING PROSPECTUS\nDated March 1, 2026\nBook Built Issue",
                                            BODY, BODY])
    cover = make_pdf(tmp_path / "p.pdf", ["PROSPECTUS\nInitial public offer of equity shares", BODY])
    reg30 = make_pdf(tmp_path / "r.pdf", ["Disclosure under Regulation 30\nclassified as Promoter in the "
                                          "Prospectus dated September 30, 2024", BODY])
    exit_offer = make_pdf(tmp_path / "e.pdf", ["LETTER OF OFFER\nfor the voluntary delisting of equity shares", BODY])
    assert triage.probe(drhp).offer_document and triage.probe(cover).offer_document
    assert not triage.probe(reg30).offer_document and not triage.probe(exit_offer).offer_document


def test_unreadable_files_never_raise(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 not really")
    assert triage.probe(bad).pages is None
    assert triage.probe(tmp_path / "missing.pdf").error


def test_order_by_cost_starts_the_expensive_files_first(tmp_path):
    small = make_pdf(tmp_path / "s.pdf", [BODY])
    big = make_pdf(tmp_path / "b.pdf", [BODY] * 30)
    scan = make_pdf(tmp_path / "scan.pdf", [""] * 12)
    order = triage.order_by_cost([triage.probe(p) for p in (small, big, scan)])
    assert order[-1] == str(small) and set(order[:2]) == {str(big), str(scan)}


# ---- windowed parsing and per-range OCR ------------------------------------------------

class FakeDoc:
    def __init__(self, pages, ocr):
        self.pages, self.ocr = pages, ocr

    def num_pages(self):
        return len(self.pages)


class FakeConverter:
    def __init__(self, ocr, calls):
        self.ocr, self.calls = ocr, calls

    def convert(self, path, raises_on_error=True, page_range=(1, 10**9)):
        self.calls.append((self.ocr, page_range))
        a, b = page_range
        return type("R", (), {"document": FakeDoc(list(range(a, min(b, 100) + 1)), self.ocr)})()


def fake_parser(monkeypatch, scanned, budget=150):
    """A 100-page PDF whose `scanned` pages have no text layer until OCR reads them."""
    calls = []
    p = Parser({"enable_ml_ocr": True, "max_pages_for_ocr": budget, "min_chars_per_page_before_ocr": 25})
    monkeypatch.setattr(p, "_converter", lambda use_ocr: FakeConverter(use_ocr, calls))
    monkeypatch.setattr(p, "_extract_blocks", lambda doc: ([
        Block("paragraph", ("ocr text " * 10) if doc.ocr else ("" if n in scanned else BODY), n)
        for n in doc.pages], []))
    monkeypatch.setattr(parser_mod, "_pdf_page_count", lambda path: 100)
    monkeypatch.setattr(parser_mod, "PDF_WINDOW_PAGES", 40)
    return p, calls


def test_big_pdfs_are_converted_in_windows_and_only_low_text_runs_are_ocred(monkeypatch, tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    p, calls = fake_parser(monkeypatch, scanned={5, 6, 7, 90})
    r = p.parse(f)
    assert calls == [(False, (1, 40)), (False, (41, 80)), (False, (81, 100)), (True, (5, 7)), (True, (90, 90))]
    assert (r.page_count, r.low_text_pages, r.ocr_pages) == (100, 4, 4)
    pages = [b.page_number for b in r.blocks]
    assert pages == sorted(pages) and len(pages) == 100
    assert all(b.content.startswith("ocr") == (b.page_number in {5, 6, 7, 90}) for b in r.blocks)


def test_over_the_ocr_budget_nothing_is_ocred_and_the_gap_is_counted(monkeypatch, tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    p, calls = fake_parser(monkeypatch, scanned=set(range(1, 61)), budget=50)
    r = p.parse(f)
    assert all(not ocr for ocr, _ in calls)
    assert (r.low_text_pages, r.ocr_pages) == (60, 0) and "exceed the OCR budget" in r.warnings[0]


def test_page_runs_merge_nearby_pages_and_stay_under_the_window():
    assert _page_runs([3, 4, 5, 7, 20]) == [(3, 7), (20, 20)]
    assert _page_runs(list(range(1, 101)))[0] == (1, parser_mod.PDF_WINDOW_PAGES)


# ---- scope gate, denylist, purge (inline indexer, stub embedder) --------------------------

OFFER_HTML = "<html><head><title>Red Herring Prospectus - Acme Ltd</title></head><body><p>Issue.</p></body></html>"
GOOD_HTML = "<html><head><title>Delisting order</title></head><body><p>Acme is delisted.</p></body></html>"


def test_offer_documents_are_skipped_out_of_scope_and_reported(tmp_path):
    root = tmp_path / "drop"
    root.mkdir()
    (root / "rhp.html").write_text(OFFER_HTML)
    (root / "order.html").write_text(GOOD_HTML)
    idx, store = make_indexer(tmp_path)
    s = idx.index_files(sorted(root.iterdir()), root)
    assert s["by_status"] == {"indexed": 1, "skipped_out_of_scope": 1}
    assert s["out_of_scope_files"] == ["rhp.html"] and s["files_skipped"] == 1 and s["files_failed"] == 0
    assert {r["source_filename"] for r in all_rows(store)} == {"order.html"}
    assert s["triage"]["by_class"] == {"html_small": 2}


def test_denylisted_documents_are_never_indexed_again(tmp_path, monkeypatch):
    root = tmp_path / "drop"
    root.mkdir()
    (root / "order.html").write_text(GOOD_HTML)
    deny = tmp_path / "denylist.txt"
    deny.write_text(f"# purged\n{compute_doc_id(root / 'order.html')}  # 2026-11-25 order.html wrong company\n")
    monkeypatch.setattr(worker_mod, "DENYLIST_FILE", deny)
    idx, store = make_indexer(tmp_path)
    assert idx.index_files([root / "order.html"], root)["by_status"] == {"skipped_denylisted": 1}


def test_purge_is_a_dry_run_until_yes_and_then_denies(tmp_path, monkeypatch):
    import config.settings as settings
    import pipeline.steps as steps
    import pipeline.vector_store as vs

    root = tmp_path / "drop"
    root.mkdir()
    (root / "order.html").write_text(GOOD_HTML)
    idx, store = make_indexer(tmp_path)
    idx.index_files([root / "order.html"], root)
    monkeypatch.setattr(vs, "VectorStore", lambda: VectorStore(tmp_path / "db", "documents"))
    monkeypatch.setattr(settings, "DENYLIST_FILE", tmp_path / "denylist.txt")

    dry = steps.purge_step([], ["order.html"], "test", yes=False)
    assert dry["status"] == "noop" and dry["counts"]["documents"] == 1 and all_rows(store)
    assert steps.purge_step([], ["../etc"], "", yes=False)["status"] == "blocked"
    done = steps.purge_step([], ["order.html"], "wrong company", yes=True)
    assert done["status"] == "ok" and done["counts"]["rows_deleted"] >= 1 and not all_rows(store)
    assert worker_mod.read_denylist(tmp_path / "denylist.txt") == {compute_doc_id(root / "order.html")}


# ---- memory ---------------------------------------------------------------------------

def test_footprint_and_system_memory_are_measured():
    import os

    assert (memwatch.footprint_mb(os.getpid()) or 0) > 0
    mem = memwatch.system_memory()
    assert mem["total_mb"] > 0 and 0 <= mem["free_pct"] <= 100


def test_headroom_counts_the_embedder_and_workers_only_when_no_ingest_runs():
    mem = {"total_mb": 49152, "available_mb": 40000}
    idle = memwatch.headroom(mem, 4000, 16000, 4, ingest_running=False)
    busy = memwatch.headroom(mem, 4000, 16000, 4, ingest_running=True)
    assert idle["more_workers_fit"] == (40000 - 49152 * 15 // 100 - 16000 - 16000) // 4000
    assert busy["more_workers_fit"] > idle["more_workers_fit"]
    assert memwatch.headroom({}, 4000, 16000, 4, False) == {}


def test_batch_scope_target_turns_the_offer_document_gate_off(tmp_path):
    import pipeline.steps as steps

    files = tmp_path / "batch" / "files"
    files.mkdir(parents=True)
    assert steps._skip_offer_documents(files) is None  # no manifest: SCOPE_SKIP_OFFER_DOCUMENTS decides
    for target, expected in (("delisted", None), ("both", False), ("listed", False)):
        (files.parent / "manifest.json").write_text(json.dumps({"scope_target": target}))
        assert steps._skip_offer_documents(files) is expected

    root = tmp_path / "drop"
    root.mkdir()
    (root / "rhp.html").write_text(OFFER_HTML)
    idx, _ = make_indexer(tmp_path, skip_offer_documents=False)
    assert idx.index_files([root / "rhp.html"], root)["by_status"] == {"indexed": 1}
