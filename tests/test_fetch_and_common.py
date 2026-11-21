import json

import common
import polite_fetcher as pf

PAGE = """<html><head><title>Orders of Delisting</title></head><body>
<a href="https://nsearchives.nseindia.com//web/mediaattachment/2026-10/Delisting_Order-SECURCRED.pdf">order</a>
<a href="https://nsearchives.nseindia.com/web/inline-files/SOP_for_Delisting_of_Equity_Shares_0.pdf">SOP</a>
<a href="https://nsearchives.nseindia.com/web/inline-files/Annexure%201_Checklist.pdf">checklist</a>
<a href="https://example.com/other/Delisting_Order.pdf">off-domain</a>
<a href="https://beta.bseindia.com/downloads1/Initial_Public_Notice_10042023.zip">newspaper scans</a>
<a href="/static/list/page.html">html page</a>
</body></html>"""


def test_harvest_links_filters_and_normalises(tmp_path):
    f = tmp_path / "page.html"
    f.write_text(PAGE)
    links = pf.harvest_links(f, "https://www.nseindia.com/static/list/orders-of-delisting")
    assert links == ["https://nsearchives.nseindia.com/web/mediaattachment/2026-10/Delisting_Order-SECURCRED.pdf"]


def test_already_fetched_skips_success_and_definitive_4xx(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.jsonl"
    entries = [
        {"url": "https://a/ok", "http_status": 200, "quarantined": False},
        {"url": "https://a/404", "http_status": 404, "quarantined": True},
        {"url": "https://a/429", "http_status": 429, "quarantined": True},
        {"url": "https://a/500", "http_status": 500, "quarantined": True},
        {"url": "https://a/timeout", "http_status": None, "quarantined": True},
    ]
    manifest.write_text("\n".join(json.dumps(e) for e in entries))
    monkeypatch.setattr(pf, "MANIFEST", manifest)
    assert set(pf.already_fetched()) == {"https://a/ok", "https://a/404"}


def test_pick_ext():
    assert pf.pick_ext("https://x/a", "application/pdf") == ".pdf"
    assert pf.pick_ext("https://x/list.xlsx", "application/octet-stream") == ".xlsx"


def test_domain_rules():
    assert common.domain_of("https://www.nseindia.com/x") == "nseindia.com"
    assert common.confidence_for_url("https://nsearchives.nseindia.com/a.pdf") == "VERIFIED_PRIMARY"
    assert common.confidence_for_url("https://www.livemint.com/a") == "CORROBORATED_SECONDARY"
    assert common.confidence_for_url("https://www.nseindia.in/fake") == "NEEDS_REVIEW"


def test_raw_dir_for_tag():
    assert common.raw_dir_for_tag("2017").name == "priority_2017"
    assert common.raw_dir_for_tag("master").name == "master_lists"
    assert common.raw_dir_for_tag("2012").name == "archive_2006_2026"


def test_disk_levels_and_warning_tiers(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "disk_settings", lambda: dict(common.DISK_DEFAULTS))
    monkeypatch.setattr(common, "notify", lambda *a: None)
    monkeypatch.setattr(common, "log", lambda *a, **k: None)

    def fake(data_gb, free_gb):
        monkeypatch.setattr(common, "dir_size", lambda _p: int(data_gb * 1e9))
        monkeypatch.setattr(common.shutil, "disk_usage", lambda _p: type("U", (), {"free": int(free_gb * 1e9)}))

    fake(1, 300)
    assert common.disk_status()["level"] == "ok"
    fake(6, 300)
    assert common.disk_status()["level"] == "warn" and common.disk_status()["warn_tier"] == 1
    fake(11, 300)
    assert common.disk_status()["warn_tier"] == 2
    fake(1, 10)
    assert common.disk_status()["level"] == "critical"

    state = {}
    fake(6, 300)
    common.check_disk(state)
    assert state["disk_warn_tier"] == 1
