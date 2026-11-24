"""Recursive discovery and indexer routing on an irregular directory tree."""

import os

import pytest

import pipeline.pipeline as pp
from pipeline.pipeline import discover_files

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def rel(root, files):
    return sorted(str(f.relative_to(root)) for f in files)


@pytest.fixture
def messy_tree(tmp_path):
    root = tmp_path / "drop"
    (root / "any" / "depth" / "of" / "folders").mkdir(parents=True)
    (root / ".hidden_dir").mkdir()
    (root / "scratch").mkdir()
    files = {
        "top.csv": "a,b\n1,2\n",
        "any/report.pdf": PDF,
        "any/depth/of/folders/deep.json": '[{"x": 1}]',
        "any/depth/of/folders/deep.json.meta.json": '{"vendor": "acme"}',   # sidecar: not a document
        "any/metadata.json": '{"defaults": {"sector": "Energy"}}',          # batch file: not a document
        ".hidden_dir/secret.csv": "a\n1\n",
        ".DS_Store": "junk",
        "any/._resource": "junk",
        "scratch/notes.csv": "a\n1\n",                                       # an ordinary folder called scratch IS input
    }
    for name, content in files.items():
        p = root / name
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    os.symlink(root / "top.csv", root / "link_to_top.csv")
    os.symlink(root, root / "any" / "loop")
    return root


def test_discovery_is_recursive_ignores_layout_and_skips_non_documents(messy_tree):
    found = rel(messy_tree, discover_files(messy_tree))
    assert found == ["any/depth/of/folders/deep.json", "any/report.pdf", "scratch/notes.csv", "top.csv"]


def test_discovery_accepts_a_single_file_and_skips_pipeline_work_dirs(messy_tree, monkeypatch, tmp_path):
    assert discover_files(messy_tree / "top.csv") == [messy_tree / "top.csv"]

    work = messy_tree / "any" / "depth"
    monkeypatch.setattr(pp, "_WORK_DIRS", (work,))
    assert not any("depth" in str(f) for f in discover_files(messy_tree))


def test_runner_summary_and_invalid_indexer_result(messy_tree, monkeypatch):
    class Stub:
        def index_files(self, files, root):
            assert root == messy_tree and len(files) == 4
            return {"status": "success", "files_indexed": 3, "files_failed": 1, "files_skipped": 0,
                    "chunks_written": 9, "failed_files": ["x.pdf"]}

    s = pp.PipelineRunner(indexer=Stub()).run(messy_tree)
    assert (s["files_found"], s["files_indexed"], s["files_failed"], s["chunks_written"], s["failed_files"]) == (4, 3, 1, 9, ["x.pdf"])

    class Bad:
        def index_files(self, files, root):
            return None

    with pytest.raises(ValueError):
        pp.PipelineRunner(indexer=Bad()).run(messy_tree)


def test_dry_run_reports_types_and_reasons_without_writing(messy_tree, tmp_path, monkeypatch):
    (messy_tree / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0 legacy word")
    (messy_tree / "blob.bin").write_bytes(b"\x00\x01\x02")
    monkeypatch.setattr(pp, "SCRATCH_DIR", tmp_path / "scratch_work")
    report = pp.dry_run(messy_tree)
    assert report["files_found"] == 6 and report["routing_errors"] == []
    assert report["by_source_type"] == {"csv": 2, "json": 1, "pdf": 1}
    assert sum(report["unsupported"].values()) == 2 and any(".doc" in reason for reason in report["unsupported"])
