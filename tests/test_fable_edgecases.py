"""Synthetic regressions for the four Fable semantic-review edge cases.

App code is loaded from this checkout's src directory. PDFs and images are
generated in pytest temporary directories; provider collaborators only record
calls and never use a network.
"""
from pathlib import Path
import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import fitz
import pytest
import pypdf
from PIL import Image

from _load_app import load_app
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ai_review as review


app = load_app()


class FakeAPI:
    def __init__(self, *, triage_enough=False):
        self.calls = []
        self.triage_enough = triage_enough

    def triage(self, vocab, image, text, total_pages):
        self.calls.append(("triage", bool(image), text))
        return {"enough_from_page1": self.triage_enough, "match": True,
                "name": "Synthetic", "group": "Other", "confidence": 95}

    def classify(self, vocab, images, text, **kwargs):
        self.calls.append(("classify", len(images), text, kwargs))
        return {"match": True, "name": "Synthetic", "group": "Other",
                "confidence": 95, "features": "synthetic readable evidence"}


def classify(path, api, *, adaptive=True):
    return app.classify_document_core(
        api, "synthetic vocabulary", path, resolution=1,
        adaptive_pages=adaptive, escalation_api=None)


def make_pdf(path, *, wide_first=False):
    doc = fitz.open()
    if wide_first:
        doc.new_page(width=7601, height=100)
    else:
        doc.new_page(width=612, height=792)
    second = doc.new_page(width=612, height=792)
    second.insert_text((72, 72), "LATER_PAGE_MARKER")
    for index in range(3, 10):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), f"ordinary page {index}")
    doc.save(path)
    doc.close()


def audit_corrupt_image(tmp_path):
    worker = tmp_path / "Worker"
    worker.mkdir()
    (worker / "broken.png").write_bytes(b"not an image")
    api = FakeAPI()
    kb = SimpleNamespace(
        vocabulary_block=lambda: "synthetic vocabulary",
        canonical_name=lambda name: "Synthetic" if name == "Synthetic" else None,
        group_of=lambda name: "Other")
    with patch.object(app, "resolve_auto_review", return_value=("Synthetic", "Other")), \
         patch.object(app, "processing_reports_dir", return_value=tmp_path), \
         patch.object(app, "record_processing_report"):
        rows, _ = app.run_accuracy_audit(api, api, kb, [worker], tmp_path,
                                         resolution=1)
    return rows, api


def test_corrupt_image_is_zero_evidence_and_zero_page_audit_error(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not a real image")
    api = FakeAPI()
    with pytest.raises(app.UnreadableDocumentError):
        classify(path, api, adaptive=False)
    assert api.calls == []
    rows, audit_api = audit_corrupt_image(tmp_path)
    assert audit_api.calls == []
    assert rows[0]["Review Status"] == "Unable To Determine"
    assert rows[0]["Pages Examined"] == 0


def test_valid_image_and_pdf_controls_remain_usable(tmp_path):
    image = tmp_path / "ordinary.png"
    Image.new("RGB", (32, 24), "navy").save(image)
    image_api = FakeAPI()
    assert classify(image, image_api, adaptive=False)["result"]["name"] == "Synthetic"
    assert image_api.calls[0][0] == "classify"
    with patch.object(app, "HAS_PIL", False):
        fitz_image_api = FakeAPI()
        assert classify(image, fitz_image_api, adaptive=False)["result"]["name"] == "Synthetic"
        assert fitz_image_api.calls[0][0] == "classify"
    assert review.page_reference_limit(image) == (1, "ok")

    pdf = tmp_path / "ordinary.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "ordinary readable PDF")
    doc.save(pdf)
    doc.close()
    pdf_api = FakeAPI()
    assert classify(pdf, pdf_api, adaptive=False)["result"]["name"] == "Synthetic"
    assert review.page_reference_limit(pdf) == (1, "ok")


def _rendered_png(path, *, zoom=1, rotate=0):
    with patch.object(app, "HAS_PIL", False):
        images, text = app.DocRender.render(path, zoom=zoom, pages="first",
                                            rotate=rotate)
    assert text == "" and len(images) == 1
    return Image.open(BytesIO(base64.b64decode(images[0]))).convert("RGB")


def _pillow_png(path, *, zoom=1, rotate=0):
    images, text = app.DocRender.render(path, zoom=zoom, pages="first",
                                        rotate=rotate)
    assert text == "" and len(images) == 1
    return Image.open(BytesIO(base64.b64decode(images[0]))).convert("RGB")


def _red_centroid(image):
    points = []
    for y in range(image.height):
        for x in range(image.width):
            red, green, blue = image.getpixel((x, y))
            if red > 220 and green < 40 and blue < 40:
                points.append((x, y))
    assert points
    return (sum(x for x, _ in points) / len(points),
            sum(y for _, y in points) / len(points))


def test_fitz_only_image_rendering_honors_clockwise_rotation(tmp_path):
    path = tmp_path / "asymmetric.png"
    image = Image.new("RGB", (100, 60), "white")
    for y in range(20):
        for x in range(30):
            image.putpixel((x, y), (255, 0, 0))
    image.save(path)
    unrotated = _rendered_png(path)
    for rotate in (90, 180, 270):
        fitz_image = _rendered_png(path, rotate=rotate)
        pillow_image = _pillow_png(path, rotate=rotate)
        assert fitz_image.size == ((unrotated.height, unrotated.width)
                                   if rotate in (90, 270) else unrotated.size)
        x, y = _red_centroid(fitz_image)
        pillow_x, pillow_y = _red_centroid(pillow_image)
        assert (x / fitz_image.width, y / fitz_image.height) == pytest.approx(
            (pillow_x / pillow_image.width, pillow_y / pillow_image.height),
            abs=0.04)


def test_fitz_only_image_rendering_caps_longest_side(tmp_path):
    path = tmp_path / "large.png"
    Image.new("RGB", (2000, 800), "teal").save(path)
    zoom = 2
    rendered = _rendered_png(path, zoom=zoom)
    assert max(rendered.size) <= app.DocRender._max_px(zoom)


def test_page_one_triage_never_receives_later_page_text(tmp_path):
    path = tmp_path / "later-readable.pdf"
    make_pdf(path)
    p1_images, p1_text = app.DocRender.render(path, zoom=1, pages="first")
    assert p1_images and "LATER_PAGE_MARKER" not in p1_text
    api = FakeAPI(triage_enough=False)
    out = classify(path, api)
    triage = next(call for call in api.calls if call[0] == "triage")
    classified = next(call for call in api.calls if call[0] == "classify")
    assert triage[2] == ""
    assert "LATER_PAGE_MARKER" in classified[2]
    assert not out["page1_only"]


def test_unrenderable_page_one_skips_triage_and_uses_later_page(tmp_path):
    path = tmp_path / "wide-first-later-readable.pdf"
    make_pdf(path, wide_first=True)
    with patch.object(app, "HAS_PIL", False):
        p1_images, p1_text = app.DocRender.render(path, zoom=1, pages="first")
        assert p1_images == [] and p1_text == ""
        api = FakeAPI(triage_enough=True)
        out = classify(path, api)
    assert not any(call[0] == "triage" for call in api.calls)
    classified = next(call for call in api.calls if call[0] == "classify")
    assert "LATER_PAGE_MARKER" in classified[2]
    assert not out["page1_only"]


def test_fitz_fallback_accepts_real_pypdf_failing_pdf_but_keeps_encryption(tmp_path):
    path = tmp_path / "recoverable-no-eof.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "REAL PARSER MISMATCH")
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data.rsplit(b"%%EOF", 1)[0])
    with fitz.open(path) as repaired:
        assert repaired.page_count == 1
        assert "REAL PARSER MISMATCH" in repaired[0].get_text()
    with pytest.raises(Exception):
        pypdf.PdfReader(str(path))
    assert review.page_reference_limit(path) == (1, "ok")

    encrypted = tmp_path / "encrypted.pdf"
    source = fitz.open()
    source.new_page()
    source.save(encrypted, encryption=fitz.PDF_ENCRYPT_AES_256,
                owner_pw="owner", user_pw="user")
    source.close()
    assert review.page_reference_limit(encrypted) == (None, "encrypted")


def test_page_reference_limit_rejects_corrupt_image_bytes(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"definitely not image bytes")
    assert review.page_reference_limit(path) == (None, "unreadable")
