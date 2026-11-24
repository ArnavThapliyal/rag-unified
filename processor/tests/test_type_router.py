import zipfile

from pipeline.type_router import HANDLER_DOCUMENT, HANDLER_TABULAR, HANDLER_UNSUPPORTED, open_routed

PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def route(path, scratch):
    with open_routed(path, scratch) as routed:
        return [(r.handler, r.source_type, r.display_name, r.reason) for r in routed]


def test_zip_members_exist_during_processing_and_are_cleaned_after(tmp_path):
    zpath = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("data/a.csv", "x,y\n1,2\n")
        z.writestr("report.pdf", PDF_BYTES)

    seen = {}
    with open_routed(zpath, tmp_path / "scratch") as routed:
        for r in routed:
            seen[r.display_name] = r.path.read_bytes()  # the regression: files must exist here
        extracted = [r.path for r in routed]
    assert set(seen) == {"bundle.zip!a.csv", "bundle.zip!report.pdf"}
    assert seen["bundle.zip!report.pdf"] == PDF_BYTES
    assert all(not p.exists() for p in extracted)


def test_nested_zip_and_depth_limit(tmp_path, monkeypatch):
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("a.csv", "x\n1\n")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as z:
        z.write(inner, "inner.zip")
    assert route(outer, tmp_path / "s1") == [(HANDLER_TABULAR, "csv", "outer.zip!inner.zip!a.csv", None)]

    import pipeline.type_router as tr
    monkeypatch.setattr(tr, "ZIP_MAX_DEPTH", 1)
    result = route(outer, tmp_path / "s2")
    assert result[0][0] == HANDLER_UNSUPPORTED and "nesting" in result[0][3]


def test_zip_slip_and_macos_junk_are_skipped(tmp_path):
    zpath = tmp_path / "evil.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("../escape.csv", "x\n1\n")
        z.writestr("__MACOSX/._a.csv", "junk")
        z.writestr(".DS_Store", "junk")
        z.writestr("ok.csv", "x\n1\n")
    assert [r[2] for r in route(zpath, tmp_path / "scratch")] == ["evil.zip!ok.csv"]
    assert not (tmp_path.parent / "escape.csv").exists()


def test_corrupt_zip_is_reported_not_raised(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    result = route(bad, tmp_path / "scratch")
    assert result[0][0] == HANDLER_UNSUPPORTED and result[0][3]


def test_content_sniffing_when_extension_is_missing_or_wrong(tmp_path):
    (tmp_path / "noext").write_bytes(PDF_BYTES)
    (tmp_path / "lies.csv").write_bytes(PDF_BYTES)
    assert route(tmp_path / "noext", tmp_path / "s")[0][:2] == (HANDLER_DOCUMENT, "pdf")
    assert route(tmp_path / "lies.csv", tmp_path / "s")[0][:2] == (HANDLER_DOCUMENT, "pdf")


def test_legacy_office_and_unknown_are_unsupported_with_reason(tmp_path):
    (tmp_path / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0")
    (tmp_path / "mystery.xyz").write_bytes(b"\x00\x01\x02\x03")
    doc = route(tmp_path / "old.doc", tmp_path / "s")[0]
    assert doc[0] == HANDLER_UNSUPPORTED and ".docx" in doc[3]
    assert route(tmp_path / "mystery.xyz", tmp_path / "s")[0][0] == HANDLER_UNSUPPORTED


def test_extensions_route_to_the_right_handler(tmp_path):
    for name, handler in [("a.csv", HANDLER_TABULAR), ("a.json", HANDLER_TABULAR), ("a.htm", HANDLER_DOCUMENT)]:
        (tmp_path / name).write_text("<html><body>hi</body></html>" if name.endswith("htm") else "a,b\n1,2\n" if name.endswith("csv") else "[]")
        assert route(tmp_path / name, tmp_path / "s")[0][0] == handler
