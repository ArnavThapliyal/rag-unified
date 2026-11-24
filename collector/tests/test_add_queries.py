import pytest

import add_queries as aq


@pytest.fixture(autouse=True)
def no_historical(monkeypatch):
    monkeypatch.setattr(aq, "existing_queries", lambda plan: {
        (q["q"] if isinstance(q, dict) else q).lower() for ph in plan["phases"] for q in ph["queries"]})


def plan():
    return {"phases": [{"name": "master", "year_tag": "master", "queries": ["NSE delisted list"]}],
            "fetch_domains": ["nseindia.com"], "deny_domains": ["youtube.com"], "search": {"providers": ["searxng"]}}


def test_appends_to_agent_added_and_skips_duplicates_and_junk():
    p = plan()
    out = aq.add(p, ["SEBI order delisting 2008", "nse delisted list", "x", "a `rm -rf` b"], "2008", [])
    assert out["added_queries"] == ["SEBI order delisting 2008"]
    assert [s["why"] for s in out["skipped_queries"]] == ["already in the plan", "too short/long", "disallowed characters"]
    assert p["phases"][-1] == {"name": "agent_added", "scope": "delisted", "year_tag": "per_query", "pages": 1,
                               "queries": [{"q": "SEBI order delisting 2008", "year_tag": "2008"}]}
    assert p["search"] == {"providers": ["searxng"]}  # nothing else touched


def test_domains_validated():
    p = plan()
    out = aq.add(p, [], None, ["www.CapitalMarket.com", "m.youtube.com", "not a host", "nseindia.com"])
    assert out["added_domains"] == ["capitalmarket.com"] and len(out["skipped_domains"]) == 3


def test_year_tag_required_and_checked():
    for tag in (None, "2008; rm", "1850"):
        with pytest.raises(ValueError):
            aq.add(plan(), ["SEBI order delisting 2008"], tag, [])


def test_concurrent_calls_keep_the_plan_valid_and_lose_nothing(tmp_path, monkeypatch):
    # Cline can run several add_queries calls at once; they used to share one .tmp file and corrupt the plan
    import json
    import threading
    plan_file = tmp_path / "query_plan.json"
    plan_file.write_text(json.dumps(plan()), encoding="utf-8")
    monkeypatch.setattr(aq, "PLAN", plan_file)
    monkeypatch.setattr(aq, "PLAN_LOCK", tmp_path / ".query_plan.lock")
    monkeypatch.setattr(aq, "log", lambda *a, **k: None)
    monkeypatch.setattr(aq, "emit", lambda *a, **k: None)
    codes = []
    threads = [threading.Thread(target=lambda i=i: codes.append(aq.main(
        ["--year-tag", "2026", "--query", f"Company number {i} delisted NSE"]))) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes == [0] * 12
    added = json.loads(plan_file.read_text(encoding="utf-8"))["phases"][-1]["queries"]
    assert sorted(q["q"] for q in added) == sorted(f"Company number {i} delisted NSE" for i in range(12))
    assert not list(tmp_path.glob("*.tmp"))


def test_listed_queries_go_to_their_own_scoped_phase():
    p = plan()
    out = aq.add(p, ["site:bseindia.com annual report 2024-25 pdf"], "2025", [], scope="listed")
    assert out["added_queries"] and p["phases"][-1]["name"] == "listed_added" and p["phases"][-1]["scope"] == "listed"
    with pytest.raises(ValueError):
        aq.add(p, ["some other query here"], "2025", [], scope="everything")
