"""Office conversion must never touch a pre-existing same-stem PDF.

LibreOffice always writes `<stem>.pdf` into the requested outdir and silently
overwrites an existing file. Converting in place therefore destroyed a
pre-existing same-stem PDF (a distinct nine-page signed contract was lost that
way), and when a conversion produced nothing, the pre-existing `<stem>.pdf`
masqueraded as fresh converter output. The fixed path converts into a fresh
private directory, so `produced exists` proves NEW bytes, and moves the result
to the collision-free destination.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _load_app import load_app
from _pdf_fixtures import pdf_bytes

app = load_app()

CONVERTED = pdf_bytes("fresh converter output")


def fake_soffice(produce=True):
    """Simulate LibreOffice faithfully: write <stem>.pdf into --outdir,
    silently replacing anything already there; or produce nothing."""
    def run(cmd, **kwargs):
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        source = Path(cmd[-1])
        if produce:
            (outdir / (source.stem + ".pdf")).write_bytes(CONVERTED)
        return SimpleNamespace(returncode=0, stderr="", stdout="")
    return run


def convert(src, produce=True):
    with patch.object(app.PdfConverter, "_soffice", "C:/synthetic/soffice.exe"), \
         patch.object(app.subprocess, "run", side_effect=fake_soffice(produce)):
        return app.PdfConverter.convert_file(src)


def test_same_stem_conversion_preserves_the_existing_pdf(tmp_path):
    original = pdf_bytes("pre-existing distinct document")
    existing = tmp_path / "Contract.pdf"
    existing.write_bytes(original)
    office = tmp_path / "Contract.docx"
    office.write_bytes(b"synthetic office bytes")
    result = convert(office)
    assert result["status"] == "converted"
    # the pre-existing occurrence survives byte-for-byte…
    assert existing.read_bytes() == original
    # …and the fresh output lands on the next free name, original deleted
    assert result["pdf"] == tmp_path / "Contract (2).pdf"
    assert result["pdf"].read_bytes() == CONVERTED
    assert not office.exists()


def test_failed_conversion_never_lets_existing_pdf_masquerade_as_output(tmp_path):
    original = pdf_bytes("pre-existing distinct document")
    existing = tmp_path / "Contract.pdf"
    existing.write_bytes(original)
    office = tmp_path / "Contract.xlsx"   # no text fallback for spreadsheets
    office.write_bytes(b"synthetic office bytes")
    result = convert(office, produce=False)
    assert result["status"] == "failed"
    assert result["pdf"] is None
    # nothing moved, renamed or deleted: both originals stay exactly put
    assert existing.read_bytes() == original
    assert office.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Contract.pdf", "Contract.xlsx"]


def test_ordinary_unique_stem_conversion_still_works(tmp_path):
    office = tmp_path / "Job Description.docx"
    office.write_bytes(b"synthetic office bytes")
    result = convert(office)
    assert result["status"] == "converted"
    assert result["pdf"] == tmp_path / "Job Description.pdf"
    assert result["pdf"].read_bytes() == CONVERTED
    assert not office.exists()
