"""Small integration regressions for release configuration and dialog guards."""
from types import SimpleNamespace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import stage2_ai_workflows as workflows
import stage2_workflow_ui as ui
from stage2_theme import resolve_palette


def test_public_defaults_do_not_pin_a_personal_account():
    assert workflows.DEFAULT_EXPECTED_EMAIL == ""
    cfg = {"ai_workflows": {"audit-review_expected_email": "local@example.com"}}
    assert workflows.auto_review_defaults(cfg)["expected_email"] == "local@example.com"
    cfg["ai_workflows"]["auto_review"] = {"expected_email": "explicit@example.com"}
    assert workflows.auto_review_defaults(cfg)["expected_email"] == "explicit@example.com"


def test_running_automatic_review_blocks_competing_manual_launch():
    assert ui.is_processing_busy(SimpleNamespace(_review_busy=True))
    assert not ui.is_processing_busy(SimpleNamespace(_review_busy=False))


def test_new_dialogs_use_saved_palette_not_module_import_default():
    try:
        for key in ("B", "A", "C"):
            ui._use_app_palette(SimpleNamespace(cfg={"ui_palette": key}))
            palette = resolve_palette(key)
            assert (ui.BG, ui.PANEL, ui.SILVER) == (palette.bg, palette.panel, palette.primary)
    finally:
        ui._use_app_palette(SimpleNamespace(cfg={"ui_palette": "C"}))
