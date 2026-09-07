"""Small, reusable colour definitions for Stage 2 Tk surfaces.

The palette is deliberately presentation-only: callers may switch it while a
run is active without changing any processing or review configuration.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Stage2Palette:
    key: str
    name: str
    bg: str
    panel: str
    raised: str
    line: str
    text: str
    muted: str
    primary: str
    primary_text: str
    attention: str
    error: str


PALETTES = {
    "A": Stage2Palette("A", "Deep jade", "#06100e", "#0b1915", "#123027",
                       "#285346", "#eff7f3", "#a8bcb4", "#45d6a2", "#03110c",
                       "#ebc887", "#ff9e8e"),
    "B": Stage2Palette("B", "Graphite", "#08090b", "#111316", "#202428",
                       "#383e44", "#f0f2f4", "#aab0b7", "#e4e9ee", "#07090b",
                       "#dec098", "#ffaaa0"),
    "C": Stage2Palette("C", "Graphite + jade", "#060708", "#0e1012", "#1a1e21",
                       "#32383e", "#eff1f3", "#a8afb6", "#45d6a2", "#03110c",
                       "#dec098", "#ff9e8e"),
}


def resolve_palette(value=None):
    """Return the selected palette; C is the safe, approved default."""
    if isinstance(value, Stage2Palette):
        return value
    return PALETTES.get(str(value or "C").upper(), PALETTES["C"])


def tk_colours(value=None):
    """Names used by the older Settings/Jobs/Tools and workflow Tk modules."""
    palette = resolve_palette(value)
    return {"BG": palette.bg, "PANEL": palette.panel, "RAISED": palette.raised,
            "LINE": palette.line, "BORDER": palette.line, "TEXT": palette.text,
            "FG": palette.text, "MUTED": palette.muted, "FG_DIM": palette.muted,
            "SILVER": palette.primary, "PRIMARY": palette.primary,
            "PRIMARY_TEXT": palette.primary_text, "ATTENTION": palette.attention,
            "ERROR": palette.error}


def configure_ttk_styles(style, value=None, *, prefix="Stage2"):
    """Configure reusable ttk controls and return the resolved palette.

    Callers still set their own Frame/Label colours from :func:`tk_colours`;
    this provides the themed ttk surfaces shared by legacy dialogs without
    taking ownership of their layout or application state.
    """
    palette = resolve_palette(value)
    style.configure(f"{prefix}.TCombobox", fieldbackground=palette.raised,
                    background=palette.panel, foreground=palette.text,
                    arrowcolor=palette.text)
    style.map(f"{prefix}.TCombobox", fieldbackground=[("readonly", palette.raised)],
              foreground=[("readonly", palette.text)])
    style.configure(f"{prefix}.Treeview", background=palette.panel,
                    fieldbackground=palette.panel, foreground=palette.text,
                    borderwidth=0, rowheight=29)
    style.configure(f"{prefix}.Treeview.Heading", background=palette.raised,
                    foreground=palette.text)
    style.configure(f"{prefix}.TNotebook", background=palette.bg, borderwidth=0)
    style.configure(f"{prefix}.TNotebook.Tab", background=palette.panel,
                    foreground=palette.muted, padding=(14, 8))
    style.map(f"{prefix}.TNotebook.Tab", background=[("selected", palette.raised)],
              foreground=[("selected", palette.text)])
    return palette
