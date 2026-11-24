"""Shared fixtures: keep test runs out of the real data directory."""
import pytest


@pytest.fixture(autouse=True)
def _triage_dir_in_tmp(tmp_path, monkeypatch):
    import pipeline.indexer as indexer

    monkeypatch.setattr(indexer, "TRIAGE_DIR", tmp_path / "triage")
