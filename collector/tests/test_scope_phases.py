"""scope.target picks which phases are searched: delisted, listed or both (and RAG_SCOPE_TARGET for one run)."""
import json

import collect
import common

PLAN = {"phases": [
    {"name": "master", "year_tag": "master", "queries": ["nse delisted list"]},                  # no scope: delisted
    {"name": "listed", "scope": "listed", "year_tag": "per_query", "queries": [{"q": "annual report 2025", "year_tag": "2025"}]},
    {"name": "listed_added", "scope": "listed", "year_tag": "per_query", "queries": []},
]}


def phases(target, phase=None):
    return sorted({it["phase"] for it in collect.plan_items(PLAN, phase, target)})


def test_each_target_searches_only_its_phases():
    assert phases("delisted") == ["master"]
    assert phases("listed") == ["listed"]
    assert phases("both") == ["listed", "master"]
    assert phases("delisted", "listed") == ["listed"]  # a phase named explicitly always runs


def test_env_overrides_the_config_for_one_run(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CONFIG", tmp_path)
    (tmp_path / "query_plan.json").write_text(json.dumps({"scope": {"target": "delisted"}}))
    assert common.scope_target() == "delisted"
    monkeypatch.setenv("RAG_SCOPE_TARGET", "both")
    assert common.scope_target() == "both"
    monkeypatch.setenv("RAG_SCOPE_TARGET", "nonsense")
    assert common.scope_target() == "delisted"


def test_the_shipped_plan_has_a_scope_on_every_phase_and_a_listed_phase():
    plan = json.loads((common.CONFIG / "query_plan.json").read_text(encoding="utf-8"))
    assert all(ph.get("scope") in ("delisted", "listed") for ph in plan["phases"])
    assert any(ph["scope"] == "listed" and ph["queries"] for ph in plan["phases"])
    assert plan["scope"]["target"] in ("delisted", "listed", "both")


def test_both_alternates_delisted_and_listed_queries():
    items = [{"phase": "master", "query": f"d{i}"} for i in range(3)] + [{"phase": "listed", "query": "l0"}]
    assert [it["query"] for it in collect.interleave_scopes(items, PLAN)] == ["d0", "l0", "d1", "d2"]
