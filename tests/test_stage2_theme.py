"""Palette selection is UI-only and safe to consume from every Stage 2 dialog."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stage2_theme import PALETTES, resolve_palette, tk_colours


def test_approved_palettes_and_safe_default_are_exposed():
    assert set(PALETTES) == {"A", "B", "C"}
    assert resolve_palette().key == "C"
    assert resolve_palette("unexpected").key == "C"
    assert resolve_palette("a").name == "Deep jade"
    assert resolve_palette("B").name == "Graphite"
    assert resolve_palette("C").name == "Graphite + jade"


def test_legacy_dialog_colour_aliases_are_complete_and_contrasted():
    for key in PALETTES:
        colours = tk_colours(key)
        assert {"BG", "PANEL", "RAISED", "LINE", "FG", "MUTED", "PRIMARY",
                "PRIMARY_TEXT", "ATTENTION", "ERROR"} <= set(colours)
        assert colours["BG"] != colours["PANEL"]
        assert colours["PRIMARY"] != colours["RAISED"]
