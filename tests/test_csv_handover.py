import csv
from _load_app import load_app

app = load_app()


def test_csv_reports_keep_tables_and_archive_together(tmp_path, monkeypatch):
    out = tmp_path / "Audit.csv"
    app._write_audit_workbook([], out, orientation_rows=[])
    assert out.exists()
    manifest = out.with_name("Audit - Tables.csv")
    with manifest.open(encoding="utf-8-sig", newline="") as stream:
        tables = list(csv.DictReader(stream))
    assert [r["table"] for r in tables] == ["Audit", "Summary"]
    monkeypatch.setattr(app, "ARCHIVED_PROCESSING_REPORTS", tmp_path / "Archive")
    archived = app.archive_processing_report(out, "Care")
    assert archived.exists()
    for row in tables:
        assert (archived.parent / row["file"]).exists()
    assert (archived.parent / manifest.name).exists()
    assert not out.exists()


def test_legacy_excel_writer_remains_supported(tmp_path):
    out = tmp_path / "Legacy.xlsx"
    app._write_audit_workbook([], out)
    assert out.exists()


def test_processing_handover_preserves_approved_profile(tmp_path):
    care = tmp_path / "Care"
    source = care / "Alice"
    source.mkdir(parents=True)
    (source / "document.pdf").write_bytes(b"synthetic")
    path = app.pipeline.refresh_roster(care)
    app.pipeline.update_roster(path, [{"folder_name": "Alice", "matched_id": "12", "approved": "yes"}])
    destination = care / "Care [Processed]" / "Alice"
    destination.parent.mkdir()
    source.rename(destination)
    engine = object.__new__(app.Engine)
    engine.log = lambda *_: None
    engine._record_roster_handover(source, destination)
    row = app.pipeline.read_rows(path)[0]
    assert row["matched_id"] == "12" and row["approved"] == "yes"
    assert row["stage2_state"] == "complete"
    assert (path.parent / row["source_path"]).resolve() == destination.resolve()
