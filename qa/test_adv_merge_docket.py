"""Adversarial tests: dedupe / conflicting sources, docket consistency, retry/backoff, fetch failures."""
import json
import time

import pytest
import requests

import audit_dataset
import docket
import normalize_and_merge as m
import polite_fetcher as pf


def merge_rows(tmp_path, monkeypatch, rows):
    parsed = tmp_path / "parsed"
    parsed.mkdir()
    (parsed / "a.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(m, "PARSED", parsed)
    monkeypatch.setattr(m, "PROCESSED", tmp_path)
    monkeypatch.setattr(m, "log", lambda *a, **k: None)
    monkeypatch.setattr(m, "emit", lambda *a, **k: None)
    m.main()
    return [json.loads(x) for x in (tmp_path / "nse_delisted_master.jsonl").read_text().splitlines()]


NSE = "https://www.nseindia.com/a.pdf"
BSE = "https://www.bseindia.com/b.pdf"


# ---------- duplicates -------------------------------------------------------------------------
def test_same_company_with_and_without_isin_is_one_record(tmp_path, monkeypatch):
    rows = [  # NSE list row has the ISIN; the single-company order (README: "usually no ISIN") does not
        {"company_name": "Jindal Cotex Limited", "isin": "INE904J01010", "nse_symbol": "JINDCOT",
         "effective_delisting_date": "2017-03-24", "delisting_type": "COMPULSORY", "source_url": NSE,
         "confidence_status": "VERIFIED_PRIMARY", "local_artifact_path": "a"},
        {"company_name": "Jindal Cotex Limited", "nse_symbol": "JINDCOT", "order_date": "2017-03-20",
         "effective_delisting_date": "2017-03-24", "delisting_type": "COMPULSORY", "source_url": NSE + "?o",
         "confidence_status": "VERIFIED_PRIMARY", "local_artifact_path": "b"}]
    out = merge_rows(tmp_path, monkeypatch, rows)
    assert len(out) == 1, [(r["company_name"], r["isin"]) for r in out]


def test_audit_flags_isin_vs_name_split(tmp_path, monkeypatch):
    rows = [{"record_id": "1", "company_name": "Jindal Cotex Limited", "isin": "INE904J01010", "delisting_type": "COMPULSORY",
             "confidence_status": "VERIFIED_PRIMARY", "source_url": NSE, "local_artifact_path": "x", "order_date": "2017-01-01"},
            {"record_id": "2", "company_name": "Jindal Cotex Limited", "isin": "", "delisting_type": "COMPULSORY",
             "confidence_status": "VERIFIED_PRIMARY", "source_url": NSE, "local_artifact_path": "x", "order_date": "2017-01-01"}]
    monkeypatch.setattr(audit_dataset, "ROOT", tmp_path)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    res = audit_dataset.audit(rows)
    assert "possible_duplicate_event" in res["warnings"], res["warnings"]


def test_conflicting_types_a_few_days_apart_are_one_event(tmp_path, monkeypatch):
    rows = [{"company_name": "ABC Ltd", "isin": "INE002A01018", "effective_delisting_date": "2017-03-24",
             "delisting_type": "COMPULSORY", "source_url": NSE, "confidence_status": "VERIFIED_PRIMARY"},
            {"company_name": "ABC Ltd", "isin": "INE002A01018", "effective_delisting_date": "2017-03-28",
             "delisting_type": "OTHER", "source_url": "https://news.example.com/x", "confidence_status": "NEEDS_REVIEW"}]
    out = merge_rows(tmp_path, monkeypatch, rows)
    assert len(out) == 1, [(r["delisting_type"], r["effective_delisting_date"]) for r in out]


def test_conflicting_dates_keep_primary_and_record_the_conflict(tmp_path, monkeypatch):
    rows = [{"company_name": "ABC Ltd", "isin": "INE002A01018", "effective_delisting_date": "2017-03-24",
             "delisting_type": "COMPULSORY", "source_url": NSE, "confidence_status": "VERIFIED_PRIMARY"},
            {"company_name": "ABC Ltd", "isin": "INE002A01018", "effective_delisting_date": "2017-04-10",
             "delisting_type": "COMPULSORY", "source_url": "https://news.example.com/x", "confidence_status": "NEEDS_REVIEW"}]
    [r] = merge_rows(tmp_path, monkeypatch, rows)
    assert r["effective_delisting_date"] == "2017-03-24"
    # no field anywhere records that a source disagreed by 17 days
    assert any("conflict" in k for k in r), sorted(r)


def test_record_id_survives_a_better_source_arriving(tmp_path, monkeypatch):
    """README: record_id is 'Stable across re-runs'. A later VERIFIED source with a slightly different date
    becomes the group's best row and changes the id of the same delisting event."""
    first = [{"company_name": "ABC Ltd", "isin": "INE002A01018", "effective_delisting_date": "2017-03-28",
              "delisting_type": "COMPULSORY", "source_url": "https://news.example.com/x", "confidence_status": "NEEDS_REVIEW"}]
    id1 = merge_rows(tmp_path / "1" if (tmp_path / "1").mkdir() is None else None, monkeypatch, first)[0]["record_id"]
    second = first + [{"company_name": "ABC Ltd", "isin": "INE002A01018", "effective_delisting_date": "2017-03-24",
                       "delisting_type": "COMPULSORY", "source_url": NSE, "confidence_status": "VERIFIED_PRIMARY"}]
    (tmp_path / "2").mkdir()
    id2 = merge_rows(tmp_path / "2", monkeypatch, second)[0]["record_id"]
    assert id1 == id2


def test_year_out_of_range_rows_are_kept_in_master(tmp_path, monkeypatch):
    rows = [{"company_name": "Old Mills Ltd", "effective_delisting_date": "1998-01-05", "delisting_type": "COMPULSORY",
             "source_url": NSE, "confidence_status": "VERIFIED_PRIMARY"}]
    out = merge_rows(tmp_path, monkeypatch, rows)
    assert out == [], "project scope is 2006-2026; 1998 delistings still land in the master"


# ---------- docket -----------------------------------------------------------------------------
@pytest.fixture
def dk(tmp_path, monkeypatch):
    monkeypatch.setattr(docket, "DB", tmp_path / "docket.sqlite")
    monkeypatch.setattr(docket, "STATS", tmp_path / "docket_stats.json")
    monkeypatch.setattr(docket, "log", lambda *a, **k: None)
    return docket.Docket(tmp_path / "docket.sqlite")


def file1(dk, url="https://www.nseindia.com/x.pdf"):
    dk.file([{"url": url, "kind": "search", "year_tag": "2017"}], ["nseindia.com"])
    return url


def test_429_throttling_eventually_kills_the_url(dk):
    """429 is documented as retryable (never definitive), yet 4 throttled attempts mark the URL dead forever."""
    u = file1(dk)
    for _ in range(4):
        dk.db.execute("UPDATE items SET next_attempt_at=0 WHERE url=?", (u,))
        st = dk.done_fetch(u, {"http_status": 429, "quarantined": True})
    assert st != "dead", "a rate-limited URL should not be abandoned permanently"


def test_403_is_dead_on_first_attempt(dk):
    """A WAF 403 (temporary ban) is treated as a definitive 'missing' and never retried."""
    u = file1(dk)
    assert dk.done_fetch(u, {"http_status": 403, "quarantined": True}) != "dead"


def test_domain_block_never_expires(dk):
    for _ in range(3):
        dk.domain_result("nseindia.com", False, None, 3)  # 3 timeouts in a row
    dk.db.execute("UPDATE domains SET blocked_at=? WHERE domain='nseindia.com'", (time.time() - 7 * 86400,))
    assert "nseindia.com" not in dk.blocked(), "a week-old block from 3 timeouts is still in force"


def test_done_fetch_on_unknown_url_reports_a_status(dk):
    assert dk.done_fetch("https://unknown.example/x.pdf", {"http_status": 200, "local_path": "x"}) == "fetched"
    assert dk.db.execute("SELECT COUNT(*) n FROM items").fetchone()["n"] == 0  # nothing was recorded


def test_crashed_worker_lease_is_recovered_after_expiry(dk):
    u = file1(dk)
    assert dk.lease(u)
    assert not dk.lease(u)                                  # second worker cannot take it
    dk.db.execute("UPDATE items SET lease_until=? WHERE url=?", (time.time() - 1, u))
    assert dk.lease(u)                                      # recovered once the lease expires


def test_parse_error_retry_without_parser_change_loops(dk, tmp_path, monkeypatch):
    """--retry parse_error resets to fetched; the next sync marks it parse_error again (no attempt limit)."""
    u = file1(dk)
    art = tmp_path / "data" / "raw" / "priority_2017" / "x.pdf"
    art.parent.mkdir(parents=True)
    art.write_bytes(b"%PDF-broken")
    monkeypatch.setattr(docket, "DATA", tmp_path / "data")
    monkeypatch.setattr(docket, "PARSED", tmp_path / "data" / "processed" / "parsed")
    dk.done_fetch(u, {"http_status": 200, "local_path": "data/raw/priority_2017/x.pdf"})
    err = [{"file": "data/raw/priority_2017/x.pdf", "error": "PDFSyntaxError"}]
    assert dk.sync_parsed(err)["parse_error"] == 1
    assert dk.retry("parse_error", None) == 1
    assert dk.sync_parsed(err)["parse_error"] == 1


# ---------- fetch_one: HTTP failures -----------------------------------------------------------
class FakeResp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self.content, self.headers = status, body, headers or {}


class FakeSession:
    def __init__(self, seq):
        self.seq, self.calls = list(seq), 0

    def get(self, url, **kw):
        self.calls += 1
        x = self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]
        if isinstance(x, Exception):
            raise x
        return x


@pytest.fixture
def quiet_fetch(monkeypatch):
    slept = []
    monkeypatch.setattr(pf.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(pf, "log", lambda *a, **k: None)
    monkeypatch.setattr(pf, "append_jsonl", lambda *a, **k: None)
    return slept


@pytest.mark.parametrize("status", [500, 503, 429])
def test_retryable_status_backs_off_exponentially(quiet_fetch, status):
    s = FakeSession([FakeResp(status, b"err")])
    e = pf.fetch_one(s, "https://www.nseindia.com/x.pdf", "2017")
    assert s.calls == 4 and e["quarantined"] and e["http_status"] == status
    assert quiet_fetch == [4, 8, 16, 32], quiet_fetch   # also sleeps after the final attempt


def test_retry_after_is_capped_at_120s(quiet_fetch):
    s = FakeSession([FakeResp(429, b"", {"Retry-After": "3600"})])
    pf.fetch_one(s, "https://www.nseindia.com/x.pdf", "2017")
    assert quiet_fetch == [120] * 4


def test_retry_after_http_date_crashes(quiet_fetch):
    """Retry-After may be an HTTP date (RFC 9110); int() raises and the whole fetch worker dies."""
    s = FakeSession([FakeResp(503, b"", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})])
    pf.fetch_one(s, "https://www.nseindia.com/x.pdf", "2017")


def test_timeouts_are_retried_then_quarantined(quiet_fetch):
    s = FakeSession([requests.Timeout("read timed out")])
    e = pf.fetch_one(s, "https://www.nseindia.com/x.pdf", "2017")
    assert s.calls == 4 and e["http_status"] is None and e["quarantined"] and "timed out" in e["error"]


def test_error_after_an_earlier_5xx_loses_the_error(quiet_fetch):
    s = FakeSession([FakeResp(502, b"bad gateway"), requests.ConnectionError("reset"), requests.ConnectionError("reset"),
                     requests.ConnectionError("reset")])
    e = pf.fetch_one(s, "https://www.nseindia.com/x.pdf", "2017")
    assert e["http_status"] == 502 and "error" not in e   # last failure (connection reset) is not recorded


def test_soft_block_page_with_200_counts_as_success(quiet_fetch):
    """Akamai/WAF 'Access Denied' pages served with 200 are saved as the document and never re-fetched."""
    s = FakeSession([FakeResp(200, b"<html><title>Access Denied</title>Reference #18.abc</html>",
                              {"Content-Type": "text/html"})])
    e = pf.fetch_one(s, "https://nsearchives.nseindia.com/content/delisted.pdf", "2017")
    assert e["quarantined"], "an HTML error page delivered for a .pdf URL is accepted as the PDF"


def test_no_download_size_limit(quiet_fetch):
    big = b"%PDF-1.4" + b"0" * 50_000_000   # 50 MB in memory, written to disk without any cap
    s = FakeSession([FakeResp(200, big, {"Content-Type": "application/pdf"})])
    e = pf.fetch_one(s, "https://www.nseindia.com/huge.pdf", "2017")
    assert e["bytes"] < 25_000_000, "no maximum file size is enforced"


def test_interrupted_download_has_no_partial_file(quiet_fetch, monkeypatch):
    """fetch_one writes <name>.part then renames: a crash during write leaves only a dot-file, which
    parse_nse_records skips (names starting with '.') - but nothing ever deletes it."""
    import common
    def boom(self, data):
        raise KeyboardInterrupt
    monkeypatch.setattr(pf.Path, "write_bytes", boom)
    s = FakeSession([FakeResp(200, b"%PDF-1.4 data", {"Content-Type": "application/pdf"})])
    with pytest.raises(KeyboardInterrupt):
        pf.fetch_one(s, "https://www.nseindia.com/y.pdf", "2017")
    assert not list(common.RAW.rglob("y*.pdf"))
