import time

import pytest

import searxng_client as sc


@pytest.fixture
def rot(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "ENGINE_STATE", tmp_path / "engine_state.json")
    monkeypatch.setattr(sc, "log", lambda *a, **k: None)
    settings = {**sc.SEARCH_DEFAULTS, "engines": ["a", "b", "c", "d"], "engines_per_query": 2,
                "official_domains": ["nseindia.com"]}
    return sc.Rotation(settings)


def test_picks_longest_rested_engines(rot):
    rot.state["a"]["last_used"] = 5
    rot.state["b"]["last_used"] = 1
    assert rot.pick() == ["c", "d"]
    rot.record(["c", "d"], [{"url": "https://www.nseindia.com/x", "engines": ["c"]}], [])
    assert rot.pick() == ["b", "a"]


def test_failing_engine_is_benched_by_reason(rot):
    rot.record(["a", "b"], [], [["a", "Suspended: too many requests"], ["b", "CAPTCHA"]])
    assert rot.state["a"]["benched_until"] == pytest.approx(time.time() + 3600, abs=5)
    assert rot.state["b"]["benched_until"] == pytest.approx(time.time() + 6 * 3600, abs=5)
    assert set(rot.usable()) == {"c", "d"}


def test_junk_or_empty_answers_bench_an_engine(rot):
    junk = [{"url": "https://steamcommunity.com/x", "engines": ["a"]}]
    for _ in range(3):
        rot.record(["a"], junk, [])
    assert "a" not in rot.usable() and "irrelevant" in rot.state["a"]["reason"]


def test_all_benched_raises_with_earliest_return(rot):
    for e in "abcd":
        rot.bench(e, "too many requests")
    rot.state["c"]["benched_until"] = time.time() + 60
    with pytest.raises(sc.EnginesExhausted) as exc:
        rot.pick()
    assert exc.value.until == rot.state["c"]["benched_until"]


def test_relevant_results_from_new_sites_do_not_bench(rot):
    news = [{"url": "https://www.capitalmarket.com/x", "title": "Exchange to delist 200 firms", "engines": ["a"]}]
    for _ in range(4):
        rot.record(["a"], news, [])
    assert "a" in rot.usable()


def test_longest_matching_cooldown_wins(rot):
    rot.record(["a"], [], [["a", "Suspended: access denied"]])
    assert rot.state["a"]["benched_until"] == pytest.approx(time.time() + 12 * 3600, abs=5)
