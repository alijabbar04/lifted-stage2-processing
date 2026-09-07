"""Small, structurally valid synthetic PDFs for offline regression tests.

Real parsers (pypdf/PyMuPDF) read these, so tests can bind claimed page
evidence to actual byte-backed page counts instead of fake text files. The
`text` argument keeps every fixture's hash distinct. No real worker document
content is ever embedded here.
"""
import io


def pdf_bytes(text="synthetic", pages=1):
    """A minimal valid PDF with `pages` blank-ish pages and unique content."""
    total = pages
    bodies = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{3 + i} 0 R" for i in range(total))
    bodies[2] = f"<< /Type /Pages /Kids [{kids}] /Count {total} >>".encode()
    for i in range(total):
        bodies[3 + i] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                         f"/Contents {3 + total + i} 0 R >>").encode()
    safe = text.replace("\\", "/").replace("(", "[").replace(")", "]")
    for i in range(total):
        stream = f"BT 72 720 Td ({safe} page {i + 1}) Tj ET".encode()
        bodies[3 + total + i] = (f"<< /Length {len(stream)} >>\nstream\n".encode()
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
