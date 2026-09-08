"""Small, structurally valid synthetic PDFs for offline regression tests.

Real parsers (pypdf/PyMuPDF) read these, so tests can bind claimed page
evidence to actual byte-backed page counts instead of fake text files. The
`text` argument keeps every fixture's hash distinct. No real worker document
content is ever embedded here.
"""
import io


def pdf_bytes(text="synthetic", pages=1, page_texts=None):
    """A minimal valid PDF with `pages` pages, unique content, and a real font
    resource so the page text is extractable the way a real PDF's is.

    `page_texts` optionally gives each page its own text (one entry per page)
    so a fixture can place distinct evidence - a signature block, a check
    date - on a specific page, as real documents do."""
    if page_texts is not None:
        pages = len(page_texts)
    total = pages

    def _safe(value):
        return value.replace("\\", "/").replace("(", "[").replace(")", "]")

    # objects: 1 catalog, 2 pages, 3 font, 4..3+n pages, 4+n..3+2n contents
    bodies = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
              3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = " ".join(f"{4 + i} 0 R" for i in range(total))
    bodies[2] = f"<< /Type /Pages /Kids [{kids}] /Count {total} >>".encode()
    for i in range(total):
        bodies[4 + i] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                         f"/Resources << /Font << /F1 3 0 R >> >> "
                         f"/Contents {4 + total + i} 0 R >>").encode()
    safe = _safe(text)
    for i in range(total):
        body = _safe(page_texts[i]) if page_texts is not None else f"{safe} page {i + 1}"
        # wrap into real lines on the page: text extractors clip text that
        # runs off the sheet, so a long paragraph must be laid out as lines
        words, lines, line = body.split(" "), [], ""
        for word in words:
            if line and len(line) + 1 + len(word) > 85:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        lines.append(line)
        ops = " T* ".join(f"({chunk}) Tj" for chunk in lines[:48])
        stream = f"BT /F1 12 Tf 14 TL 72 740 Td {ops} ET".encode()
        bodies[4 + total + i] = (f"<< /Length {len(stream)} >>\nstream\n".encode()
                                 + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for number in sorted(bodies):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + bodies[number] + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n".encode() + b"0000000000 65535 f \n"
    for number in sorted(bodies):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def encrypted_pdf_bytes(text="synthetic encrypted", password="synthetic-pw"):
    """A password-protected PDF no test can read without the password."""
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(pdf_bytes(text))))
    writer.encrypt(user_password=password, owner_password=password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def corrupt_pdf_bytes(text="synthetic corrupt"):
    """Starts like a PDF but is unparseable and unrenderable."""
    return (b"%PDF-1.7\n% " + text.encode() + b"\n"
            b"1 0 obj << /Type /Catalog\n"  # truncated mid-object, no xref
            )
