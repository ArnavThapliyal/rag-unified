import json
from pathlib import Path

from pipeline.metadata import BatchMetadata, build_source_document
from pipeline.type_router import HANDLER_DOCUMENT, RoutedFile


def routed(path: Path, origin: Path | None = None, display: str | None = None) -> RoutedFile:
    return RoutedFile(path, HANDLER_DOCUMENT, "pdf", display or path.name, origin or path)


def make_file(tmp_path, name="r.pdf", data=b"%PDF-1.4 hello"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_no_metadata_at_all_gives_none_fields_and_never_raises(tmp_path):
    doc = build_source_document(routed(make_file(tmp_path)), BatchMetadata(tmp_path))
    assert (doc.symbol, doc.source_url, doc.doc_type, doc.vendor, doc.published_at, doc.allowed_roles) == (None,) * 6
    assert doc.extra == {} and doc.source_type == "pdf" and doc.source_filename == "r.pdf" and len(doc.doc_id) == 32


def test_works_without_a_batch_object(tmp_path):
    assert build_source_document(routed(make_file(tmp_path))).symbol is None


def test_sidecar_aliases_and_unknown_fields_are_kept(tmp_path):
    p = make_file(tmp_path)
    (tmp_path / "r.pdf.meta.json").write_text(json.dumps({
        "Ticker": "TCS", "URL": "http://x/y", "document_type": "annual_report", "Provider": "acme",
        "date": "2025-03-31", "roles": "analyst, admin", "id": "V-42", "custom_flag": 7,
    }))
    doc = build_source_document(routed(p), BatchMetadata(tmp_path))
    assert (doc.symbol, doc.source_url, doc.doc_type, doc.vendor, doc.published_at) == \
           ("TCS", "http://x/y", "annual_report", "acme", "2025-03-31")
    assert doc.allowed_roles == ["analyst", "admin"]
    assert doc.extra == {"custom_flag": 7, "vendor_doc_id": "V-42"}


def test_precedence_sidecar_over_file_entry_over_defaults_over_legacy(tmp_path):
    p = make_file(tmp_path)
    (tmp_path / "metadata.json").write_text(json.dumps({
        "defaults": {"vendor": "dflt", "doc_type": "dflt_type", "symbol": "DEF"},
        "files": {"r.pdf": {"doc_type": "file_type", "symbol": "FIL"}},
    }))
    (tmp_path / "manifest.json").write_text(json.dumps({"downloaded_files": [{"file": "/abs/r.pdf", "url": "http://legacy"}]}))
    (tmp_path / "r.pdf.meta.json").write_text(json.dumps({"symbol": "SIDE"}))
    doc = build_source_document(routed(p), BatchMetadata(tmp_path))
    assert doc.symbol == "SIDE"            # sidecar wins
    assert doc.doc_type == "file_type"     # file entry beats defaults
    assert doc.vendor == "dflt"            # default fills the gap
    assert doc.source_url == "http://legacy"  # legacy manifest supplies the url


def test_malformed_and_wrong_shaped_metadata_is_ignored(tmp_path):
    p = make_file(tmp_path)
    (tmp_path / "r.pdf.meta.json").write_text("{not json")
    (tmp_path / "metadata.json").write_text(json.dumps([1, 2, 3]))
    (tmp_path / "manifest.json").write_text(json.dumps({"downloaded_files": "oops", "files": {"r.pdf": "str"}}))
    doc = build_source_document(routed(p), BatchMetadata(tmp_path))
    assert doc.symbol is None and doc.extra == {}


def test_odd_values_are_coerced_or_nulled(tmp_path):
    p = make_file(tmp_path)
    (tmp_path / "r.pdf.meta.json").write_text(json.dumps({
        "symbol": 500325, "vendor": "  ", "doc_type": ["a"], "published_at": None, "roles": [],
    }))
    doc = build_source_document(routed(p), BatchMetadata(tmp_path))
    assert doc.symbol == "500325" and doc.vendor is None and doc.doc_type is None
    assert doc.published_at is None and doc.allowed_roles is None


def test_zip_member_uses_archive_metadata_and_records_archive(tmp_path):
    z = tmp_path / "bundle.zip"
    z.write_bytes(b"zip")
    member = make_file(tmp_path, "inner.pdf")
    (tmp_path / "bundle.zip.meta.json").write_text(json.dumps({"vendor": "acme"}))
    doc = build_source_document(routed(member, origin=z, display="bundle.zip!inner.pdf"), BatchMetadata(tmp_path))
    assert doc.vendor == "acme" and doc.source_filename == "bundle.zip!inner.pdf" and doc.extra["archive"] == "bundle.zip"


def test_identical_bytes_share_a_doc_id(tmp_path):
    a = build_source_document(routed(make_file(tmp_path, "a.pdf")))
    b = build_source_document(routed(make_file(tmp_path, "b.pdf")))
    assert a.doc_id == b.doc_id


# ---- downstream-LLM fields and arbitrary directory trees ----------------------------

def test_extended_fields_are_typed_and_bad_values_become_null_but_are_kept(tmp_path):
    p = make_file(tmp_path)
    (tmp_path / "r.pdf.meta.json").write_text(json.dumps({
        "ISIN": "INE002A01018", "close": "1,200.5", "is_fno": "yes", "roe_pct": "n/a", "no_of_trades": "42",
    }))
    doc = build_source_document(routed(p), BatchMetadata(tmp_path))
    assert doc.fields == {"isin": "INE002A01018", "close": 1200.5, "is_fno": True, "no_of_trades": 42}
    assert doc.extra == {"roe_pct": "n/a"}  # unconvertible: null column, original value preserved


def test_resolver_nearest_directory_wins_and_folder_names_carry_no_meaning(tmp_path):
    from pipeline.metadata import MetadataResolver

    deep = tmp_path / "TCS" / "2025" / "q2"          # folder names look like metadata; must be ignored
    deep.mkdir(parents=True)
    (tmp_path / "metadata.json").write_text(json.dumps({"defaults": {"vendor": "root_vendor", "sector": "root_sector"}}))
    (tmp_path / "TCS" / "metadata.json").write_text(json.dumps({"defaults": {"sector": "mid_sector"}}))
    (deep / "metadata.json").write_text(json.dumps({"files": {"r.pdf": {"industry": "leaf_industry"}}}))
    p = make_file(deep)
    sibling = make_file(deep, "other.pdf")

    resolver = MetadataResolver(tmp_path)
    doc = build_source_document(routed(p), resolver)
    assert doc.vendor == "root_vendor"                 # inherited from the root
    assert doc.fields["sector"] == "mid_sector"        # nearer directory overrides the root
    assert doc.fields["industry"] == "leaf_industry"   # per-file entry in the leaf
    assert doc.symbol is None                          # the 'TCS' folder name is NOT used
    other = build_source_document(routed(sibling), resolver)
    assert "industry" not in other.fields


def test_resolver_ignores_metadata_outside_its_root(tmp_path):
    from pipeline.metadata import MetadataResolver

    (tmp_path / "metadata.json").write_text(json.dumps({"defaults": {"vendor": "outside"}}))
    inner = tmp_path / "ingest"
    inner.mkdir()
    doc = build_source_document(routed(make_file(inner)), MetadataResolver(inner))
    assert doc.vendor is None


def test_is_metadata_file():
    from pipeline.metadata import is_metadata_file

    assert all(is_metadata_file(Path(n)) for n in ("a.pdf.meta.json", "A.META.JSON", "metadata.json", "manifest.json"))
    assert not any(is_metadata_file(Path(n)) for n in ("prices.json", "report.pdf", "metadata.csv"))
