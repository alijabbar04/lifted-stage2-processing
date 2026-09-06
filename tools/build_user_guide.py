"""Build the maintainable Stage 2 user guide with ReportLab.

Usage: python tools/build_user_guide.py [--source ...] [--output ...]
Optional QA: --render-dir <folder> [--pdftoppm <executable>]

The intentionally small Markdown subset supports headings, paragraphs, lists,
tables, fenced code, callouts and explicit <!-- pagebreak --> markers. Unknown
formatting remains visible text rather than silently disappearing.
"""
from __future__ import annotations

import argparse
import html
from pathlib import Path
import re
import shutil
import subprocess

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (LongTable, PageBreak, Paragraph,
                               Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle)

ROOT = Path(__file__).resolve().parents[1]
BLACK = colors.HexColor("#090B0D")
INK = colors.HexColor("#20272E")
MUTED = colors.HexColor("#5D6670")
LINE = colors.HexColor("#D9DEE3")
PALE = colors.HexColor("#F2F4F6")


def register_fonts():
    choices = [
        (Path("C:/Windows/Fonts/segoeui.ttf"), Path("C:/Windows/Fonts/segoeuib.ttf"), Path("C:/Windows/Fonts/segoeuii.ttf")),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf")),
    ]
    for regular, bold, italic in choices:
        if all(path.is_file() for path in (regular, bold, italic)):
            pdfmetrics.registerFont(TTFont("Guide", str(regular)))
            pdfmetrics.registerFont(TTFont("Guide-Bold", str(bold)))
            pdfmetrics.registerFont(TTFont("Guide-Italic", str(italic)))
            pdfmetrics.registerFontFamily("Guide", normal="Guide", bold="Guide-Bold", italic="Guide-Italic", boldItalic="Guide-Bold")
            return "Guide", "Guide-Bold"
    return "Helvetica", "Helvetica-Bold"


def inline(text):
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`]+)`", r'<font name="Courier">\1</font>', escaped)
    return escaped


class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, guide_version="", guide_edition="", **kwargs):
        super().__init__(*args, **kwargs)
        self._guide_version = guide_version
        self._guide_edition = guide_edition
        self._states = []

    def showPage(self):
        self._states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        pages = len(self._states)
        for state in self._states:
            self.__dict__.update(state)
            self.setStrokeColor(LINE)
            self.line(18 * mm, 17 * mm, A4[0] - 18 * mm, 17 * mm)
            self.setFont("Helvetica", 8)
            self.setFillColor(MUTED)
            self.drawString(18 * mm, 12 * mm, f"OBSIDIAN COMPACT  |  {self._guide_version}  |  {self._guide_edition.upper()}")
            self.drawRightString(A4[0] - 18 * mm, 12 * mm, f"{self._pageNumber} / {pages}")
            super().showPage()
        super().save()


def build(source: Path, output: Path):
    regular, bold = register_fonts()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("GuideBody", fontName=regular, fontSize=9.7, leading=13.5, textColor=INK,
                              spaceAfter=7, alignment=TA_LEFT, allowWidows=0, allowOrphans=0))
    styles.add(ParagraphStyle("GuideTitle", parent=styles["GuideBody"], fontName=bold, fontSize=29, leading=35,
                              textColor=BLACK, spaceBefore=12, spaceAfter=10, keepWithNext=True))
    styles.add(ParagraphStyle("GuideH1", parent=styles["GuideBody"], fontName=bold, fontSize=19, leading=24,
                              textColor=BLACK, spaceBefore=3, spaceAfter=13, keepWithNext=True))
    styles.add(ParagraphStyle("GuideH2", parent=styles["GuideBody"], fontName=bold, fontSize=11.5, leading=15,
                              textColor=BLACK, spaceBefore=8, spaceAfter=6, keepWithNext=True))
    styles.add(ParagraphStyle("GuideTable", parent=styles["GuideBody"], fontSize=9.1, leading=12.2, spaceAfter=0))
    styles.add(ParagraphStyle("GuideTableHead", parent=styles["GuideTable"], fontName=bold, textColor=colors.white))
    styles.add(ParagraphStyle("GuideBullet", parent=styles["GuideBody"], leftIndent=12, firstLineIndent=0,
                              bulletIndent=0, spaceAfter=5))
    styles.add(ParagraphStyle("GuideCode", parent=styles["GuideBody"], fontName="Courier", fontSize=8.5,
                              leading=12, leftIndent=10, spaceBefore=4, spaceAfter=9, backColor=PALE, borderPadding=9))
    width = A4[0] - 36 * mm
    story = []
    source_text = source.read_text(encoding="utf-8-sig")
    version_match = re.search(r"^## User guide \| ([^\n]+)", source_text, re.MULTILINE)
    edition_match = re.search(r"^Edition: ([^.]+)\.", source_text, re.MULTILINE)
    if not version_match or not edition_match:
        raise ValueError("The guide source must specify its User guide | version and Edition: date.")
    guide_version, guide_edition = version_match[1].strip(), edition_match[1].strip()
    lines = source_text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line == "<!-- pagebreak -->":
            story.append(PageBreak())
            index += 1
        elif line.startswith("```"):
            block = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            story.append(Preformatted("\n".join(block), styles["GuideCode"]))
            index += 1
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            name = "GuideTitle" if level == 1 else "GuideH1" if level == 2 else "GuideH2"
            story.append(Paragraph(inline(line[level:].strip()), styles[name]))
            index += 1
        elif line.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                fields = [part.strip() for part in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", field) for field in fields):
                    rows.append(fields)
                index += 1
            widths = [width * 0.34, width * 0.66]
            if rows and rows[0][-1].lower() == "page":
                widths = [width * 0.86, width * 0.14]
            if len(rows[0]) != 2:
                widths = [width / len(rows[0])] * len(rows[0])
            table = LongTable([[Paragraph(inline(cell), styles["GuideTableHead"] if row_index == 0 else styles["GuideTable"])
                                for cell in row] for row_index, row in enumerate(rows)], colWidths=widths,
                               repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), BLACK),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LINEBELOW", (0, 0), (-1, 0), 0.4, BLACK),
                ("LINEBELOW", (0, -1), (-1, -1), 0.4, LINE),
            ]))
            story += [table, Spacer(1, 9)]
        elif line.startswith("> "):
            text = []
            while index < len(lines) and lines[index].strip().startswith("> "):
                text.append(lines[index].strip()[2:])
                index += 1
            callout = Table([[Paragraph(inline(" ".join(text)), styles["GuideTable"])]], colWidths=[width])
            callout.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PALE),
                                        ("LINEBEFORE", (0, 0), (0, -1), 3, BLACK),
                                        ("LEFTPADDING", (0, 0), (-1, -1), 12),
                                        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                                        ("TOPPADDING", (0, 0), (-1, -1), 10),
                                        ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
            story += [callout, Spacer(1, 10)]
        elif re.match(r"^(?:- |\d+\. )", line):
            match = re.match(r"^(-|\d+\.) (.*)$", line)
            story.append(Paragraph(inline(match.group(2)), styles["GuideBullet"], bulletText=match.group(1)))
            index += 1
        else:
            para = [line]
            index += 1
            while index < len(lines) and lines[index].strip() and not re.match(r"^(?:#|\||>|```|<!--|- |\d+\. )", lines[index].strip()):
                para.append(lines[index].strip())
                index += 1
            story.append(Paragraph(inline(" ".join(para)), styles["GuideBody"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(output), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
                            topMargin=22 * mm, bottomMargin=23 * mm, title=f"Stage 2 - Processing | User Guide {guide_version}",
                            author="Lifted", subject="Obsidian Compact processing, audit review and code-learning guide")
    def header(pdf, document):
        pdf.saveState()
        pdf.setFillColor(BLACK)
        pdf.rect(0, A4[1] - 13 * mm, A4[0], 13 * mm, fill=1, stroke=0)
        pdf.setFillColor(colors.white)
        pdf.setFont(bold, 8.5)
        pdf.drawString(18 * mm, A4[1] - 8.5 * mm, "STAGE 2  /  PROCESSING")
        pdf.setFont(regular, 8)
        pdf.drawRightString(A4[0] - 18 * mm, A4[1] - 8.5 * mm, "USER GUIDE")
        pdf.restoreState()
    def numbered_canvas(*args, **kwargs):
        return NumberedCanvas(*args, guide_version=guide_version, guide_edition=guide_edition, **kwargs)
    doc.build(story, onFirstPage=header, onLaterPages=header, canvasmaker=numbered_canvas)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "docs" / "USER_GUIDE.md")
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "USER_GUIDE.pdf")
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--pdftoppm", default=shutil.which("pdftoppm"))
    args = parser.parse_args()
    output = build(args.source, args.output)
    from pypdf import PdfReader
    reader = PdfReader(output)
    expected_pages = args.source.read_text(encoding="utf-8-sig").count("<!-- pagebreak -->") + 1
    if len(reader.pages) != expected_pages:
        raise SystemExit(f"Layout overflow: expected {expected_pages} pages, rendered {len(reader.pages)}. Adjust layout before release.")
    all_text = "\n".join(page.extract_text() for page in reader.pages)
    for required in ("Luna", "Opus", "Fable", "Sol", "API Usage", "Discord", "Telegram", "Master_Filename_Review_Ledger.xlsx"):
        if required not in all_text:
            raise SystemExit(f"Missing required guide text: {required}")
    if "\ufffd" in all_text or "\u25a0" in all_text:
        raise SystemExit("Unexpected replacement glyph in PDF text.")
    if args.render_dir:
        if not args.pdftoppm:
            raise SystemExit("Pass --pdftoppm with the bundled/system Poppler executable for visual QA.")
        args.render_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(args.pdftoppm), "-png", "-scale-to", "1400", str(output), str(args.render_dir / "page")], check=True)
    print(f"Built {output} ({len(reader.pages)} pages, {output.stat().st_size:,} bytes).")


if __name__ == "__main__":
    main()
