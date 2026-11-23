"""Adversarial tests: search failures, corrupt files, OCR, disk guard, credentials, agent permissions."""
import json
import subprocess
import zipfile

import pytest

import common
import credentials
import ocr
import parse_nse_records as p
import tool_guard as g
import web_search as ws
from search_providers.base import Answer, SearchProvider

ROOT = str(g.ROOT)


# ---------- search providers -------------------------------------------------------------------
class Fake(SearchProvider):
    name = "fake"

    def __init__(self, behaviour):
        super().__init__({})
        self.behaviour = behaviour

    def search(self, session, query, page, units):
        b = self.behaviour
        if isinstance(b, Exception):
            raise b
        return b


def make_search(monkeypatch, tmp_path, behaviour):
    monkeypatch.setattr(ws, "ENGINE_STATE", tmp_path / "engine_state.json")
    monkeypatch.setattr(ws, "log", lambda *a, **k: None)
    monkeypatch.setattr(ws, "polite_sleep", lambda *a, **k: None)
    s = dict(ws.SEARCH_DEFAULTS, providers=["fake"], provider_settings={}, official_domains=["nseindia.com"])
    return ws.Search(s, registry={"fake": lambda settings: Fake(behaviour)})


def test_provider_exception_is_a_runtime_error(monkeypatch, tmp_path):
    srch = make_search(monkeypatch, tmp_path, ConnectionError("refused"))
    with pytest.raises(RuntimeError):
        srch.page(None, "q", 1)


def test_clean_empty_answer_is_returned_not_raised(monkeypatch, tmp_path):
    srch = make_search(monkeypatch, tmp_path, Answer([], []))
    ans, prov, units = srch.page(None, "q", 1)
    assert ans.results == [] and prov == "fake"


def test_all_units_failing_returns_failures(monkeypatch, tmp_path):
    srch = make_search(monkeypatch, tmp_path, Answer([], [("fake", "too many requests (429)")]))
    ans, _, _ = srch.page(None, "q", 1)
    assert ans.failures and srch.rot["fake"].state["fake"]["benched_until"] > 0
    with pytest.raises(ws.SearchExhausted):   # benched: the next query raises with a retry time
        srch.page(None, "q2", 1)


def test_corrupt_cache_file_poisons_the_query(monkeypatch, tmp_path):
    """A cache file truncated by a crash mid-write (it is written non-atomically) makes every later run of
    that query raise JSONDecodeError; collect.py records the query as 'failed', also on --retry-failed."""
    monkeypatch.setattr(ws, "SEARCH_CACHE", tmp_path / "data" / "search_cache")
    srch = make_search(monkeypatch, tmp_path, Answer([{"url": "https://www.nseindia.com/a.pdf"}], []))
    (tmp_path / "data" / "search_cache").mkdir(parents=True)
    ws.cache_path.__globals__["SEARCH_CACHE"] = tmp_path / "data" / "search_cache"
    ws.cache_path("nse delisting 2017", 1).write_text('{"query": "nse delisting 2017", "results": [{"url": "htt')
    ws.run("nse delisting 2017", 1, "2017", srch)   # should treat the cache as a miss and search again


def test_searxng_non_json_200_marks_provider_down(monkeypatch, tmp_path):
    from search_providers.searxng import SearXNG

    class R:
        status_code = 200
        def json(self):
            raise ValueError("Expecting value")

    class S:
        def get(self, *a, **k):
            return R()
    with pytest.raises(ValueError):
        SearXNG({}).search(S(), "q", 1, ["bing"])


# ---------- corrupt / unusual documents --------------------------------------------------------
def test_corrupt_pdf_raises(tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrunc")
    with pytest.raises(Exception):
        p.parse_file(f)


def test_html_saved_as_pdf_raises(tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"<html><body>Access Denied</body></html>")
    with pytest.raises(Exception):
        p.parse_file(f)


def test_corrupt_xlsx_raises(tmp_path):
    f = tmp_path / "x.xlsx"
    f.write_bytes(b"PK\x03\x04garbage")
    with pytest.raises(Exception):
        p.parse_file(f)


def test_zip_bomb_like_xlsx_is_not_bounded(tmp_path):
    """No size/row guard before pandas loads every sheet into memory."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws_ = wb.active
    ws_.append(["Company Name", "Symbol", "Date of Delisting"])
    for i in range(20000):
        ws_.append([f"Company {i} Ltd", f"C{i}", "01-01-2017"])
    f = tmp_path / "big.xlsx"
    wb.save(f)
    rows, _ = p.parse_file(f)
    assert len(rows) <= 5000, f"{len(rows)} rows accepted from one sheet without any plausibility cap"


def test_parse_main_retries_broken_files_every_batch(tmp_path, monkeypatch):
    """A file that raises gets no parsed output, so every process batch re-parses (and re-OCRs) it."""
    raw = common.RAW / "priority_2017"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "broken.pdf").write_bytes(b"%PDF-1.4 broken")
    outs = []
    for _ in range(2):
        r = subprocess.run([str(common.ROOT / ".venv/bin/python"), str(common.ROOT / "scripts/parse_nse_records.py")],
                           capture_output=True, text=True, cwd=common.ROOT)
        outs.append(json.loads(r.stdout.strip().splitlines()[-1]))
    (raw / "broken.pdf").unlink()
    assert outs[1]["errors"] == [], "the same broken file is re-parsed on every batch"


# ---------- OCR --------------------------------------------------------------------------------
def test_ocr_failure_is_cached_forever(tmp_path, monkeypatch):
    from PIL import Image
    pdf = tmp_path / "raw" / "scan.pdf"
    pdf.parent.mkdir()
    Image.new("RGB", (200, 200), "white").save(pdf)
    monkeypatch.setattr(ocr, "OCR_DIR", tmp_path / "ocr")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="Error opening data file"))
    assert ocr.ocr_pdf(pdf) == ""
    assert not ocr.cache_path(pdf).exists(), "a failed OCR (tesseract exit 1) is cached as empty text forever"


def test_ocr_timeout_propagates(tmp_path, monkeypatch):
    from PIL import Image
    pdf = tmp_path / "raw" / "scan2.pdf"
    pdf.parent.mkdir()
    Image.new("RGB", (200, 200), "white").save(pdf)
    monkeypatch.setattr(ocr, "OCR_DIR", tmp_path / "ocr")
    monkeypatch.setattr(ocr, "available", lambda: True)

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 120)
    monkeypatch.setattr(ocr.subprocess, "run", slow)
    ocr.ocr_pdf(pdf)   # one slow page should not turn the whole document into a parse_error


def test_real_tesseract_on_a_synthetic_scan(tmp_path, monkeypatch):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("L", (1700, 400), 255)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 48)
    except OSError:
        font = ImageFont.load_default()
    d.text((40, 60), "Ref: NSE/LIST/2018/ 47637 May 18, 2018", font=font, fill=0)
    d.text((40, 160), "INDIA ABC Ltd INE002A01018 delisted w.e.f. 15-08-2017", font=font, fill=0)
    pdf = tmp_path / "raw" / "scan3.pdf"
    pdf.parent.mkdir()
    img.save(pdf, resolution=150)
    monkeypatch.setattr(ocr, "OCR_DIR", tmp_path / "ocr")
    text = ocr.ocr_pdf(pdf)
    print("OCR TEXT:", repr(text))
    assert "INE002A01018" in text.replace("O", "0").replace(" ", "") or "INE002A01018" in text


# ---------- disk guard -------------------------------------------------------------------------
def test_disk_setting_with_bad_value_crashes(monkeypatch, tmp_path):
    plan = tmp_path / "query_plan.json"
    plan.write_text(json.dumps({"disk": {"min_free_gb": "twenty"}}))
    monkeypatch.setattr(common, "CONFIG", tmp_path)
    common.disk_settings()


def test_process_worker_never_checks_disk():
    src = (common.ROOT / "scripts" / "process_worker.py").read_text()
    src2 = (common.ROOT / "scripts" / "parse_nse_records.py").read_text()
    assert "check_disk" in src or "check_disk" in src2, "OCR/parsing can fill the disk unchecked"


# ---------- credentials ------------------------------------------------------------------------
def test_tracked_env_is_refused(tmp_path, monkeypatch):
    env = common.ROOT / ".env"
    env.write_text("SERPER_API_KEY=sk-test-123\n")
    subprocess.run(["git", "-C", str(common.ROOT), "add", "-f", ".env"], check=True)
    credentials._file_values.cache_clear()
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "USER_KEYS", tmp_path / "none.env")
    try:
        with pytest.raises(RuntimeError):
            credentials.get("SERPER_API_KEY")
    finally:
        subprocess.run(["git", "-C", str(common.ROOT), "rm", "-q", "--cached", ".env"], check=True)
        env.unlink()
        credentials._file_values.cache_clear()


def test_redact_hides_keys(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "sk-secret-xyz")
    from search_providers.serper import Serper
    assert "sk-secret-xyz" not in Serper({}).redact("401 for https://x?key=sk-secret-xyz")


def test_where_never_returns_the_value(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "bk-secret")
    assert credentials.where("BRAVE_API_KEY") == "environment"


def test_credential_lookup_failure_mode_when_tracked_env_exists(monkeypatch):
    """If .env is tracked, *every* get() raises RuntimeError - including from SearchProvider.health(),
    which web_search.Search.blocker() calls unguarded: --check / collect.py crash instead of skipping."""
    pass


# ---------- tool guard: agent permission boundary ----------------------------------------------
def cmd(c):
    return g.check("run_commands", {"commands": [c]})


@pytest.mark.parametrize("c", [
    ".venv/bin/python scripts/common.py log x | cut -c1-200 .env",                # cut reads a file operand
    ".venv/bin/python scripts/common.py log x | cut -c1-200 /Users/arnav/.config/nse-delisted-comp/keys.env",
    ".venv/bin/python scripts/common.py log x | grep -eKEY .env",                 # attached -e pattern
    ".venv/bin/python scripts/common.py log x | grep -r API_KEY",                 # recursive search of cwd
    ".venv/bin/python scripts/common.py log x | sort -oscripts/collect.py",        # attached -o: writes a file
    ".venv/bin/python scripts/common.py log x | sort --files0-from=.env",
])
def test_pipe_filters_cannot_read_or_write_files(c):
    assert cmd(c), f"tool guard allowed: {c}"


@pytest.mark.parametrize("c", [
    ".venv/bin/python scripts/docket.py --stats --unblock nseindia.com",         # agent may not unblock
    ".venv/bin/python scripts/docket.py --list dead --retry dead",
    ".venv/bin/python scripts/docket.py --stats --promote --limit 100000",
    ".venv/bin/python scripts/report_issue.py --close=1",                         # closing is for the user
    ".venv/bin/python scripts/report_issue.py --clos 1",
    ".venv/bin/python scripts/job.py start --unlimited",                          # caps are the user's
    ".venv/bin/python scripts/job.py start --max-files all --max-docs-per-company all",
])
def test_script_argument_rules(c):
    assert cmd(c), f"tool guard allowed: {c}"


@pytest.mark.parametrize("old,new", [
    ('"min_free_gb": 20', '"min_free_gb": 0'),                                    # disables the disk guard
    ('"delay_s": [10, 20]', '"delay_s": [0, 0]'),                                 # removes NSE politeness delay
    ('"max_per_run": 12', '"max_per_run": 5000'),
    ('"deny_domains": [', '"deny_domains_x": ['),
    ('"block_domain_after_failures": 3', '"block_domain_after_failures": 9999'),
])
def test_agent_cannot_edit_politeness_or_disk_settings(old, new):
    assert g.check("editor", {"path": f"{ROOT}/config/query_plan.json", "old_text": old, "new_text": new}), new


def test_agent_cannot_delete_the_plan():
    patch = "*** Begin Patch\n*** Delete File: config/query_plan.json\n*** End Patch"
    assert g.check("apply_patch", {"input": patch})


def test_hook_fails_closed_on_garbage_input(monkeypatch, capsys):
    monkeypatch.setattr(g, "record", lambda *a: None)
    monkeypatch.setattr(g, "audit", lambda *a: None)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("not json"))
    g.main()
    out = json.loads(capsys.readouterr().out)
    assert "overrideInput" in out


def test_control_dry_run_is_allowed():
    assert cmd(".venv/bin/python scripts/collect.py --dry-run --stage discover --max-queries 999") is None
