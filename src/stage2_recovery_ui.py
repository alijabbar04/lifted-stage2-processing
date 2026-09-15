"""Read-only presentation of retained source outcomes; never processing authority."""
from pathlib import Path


def source_attention_message(summary):
    def names(key):
        value = summary.get(key, [])
        return value if isinstance(value, list) else []

    completed = max(0, int(summary.get("completed_workers", 0) or 0))
    empty = names("empty_workers")
    unreadable = names("unreadable_documents")
    other = names("other_incomplete_workers")
    lines = [f"{completed} worker folder(s) completed. Local source preflight is retained.", ""]
    if empty:
        lines += [f"NO DOCUMENTS SUPPLIED ({len(empty)} folders)",
                  *[f"  {name}" for name in empty[:12]],
                  "Supply the missing originals, or resolve the intentionally empty scope.", ""]
    if unreadable:
        lines += [f"SOURCE DOCUMENTS NEED ATTENTION ({len(unreadable)})"]
        labels = {"unreadable_source": "Unreadable document",
                  "source_missing": "Source missing", "source_changed": "Source changed",
                  "password_protected_pdf": "Password-protected PDF (structure awaits decryption)",
                  "corrupt_or_truncated_pdf": "Corrupt or truncated PDF", "zero_page_pdf": "Zero-page PDF",
                  "unsupported_format": "Unsupported format", "no_renderable_or_extractable_content": "No usable page evidence or text"}
        labels["file_size_limit"] = "Exceeds the saved run's file-size limit"
        for item in unreadable[:12]:
            if isinstance(item, dict):
                label = labels.get(item.get("reason"), "Source evidence unavailable")
                lines.append(f"  {item.get('worker', '?')}: {Path(str(item.get('path', ''))).name} - {label}")
        lines += ["Use Source recovery to supply passwords or import a verified replacement.",
                  "Do not replace active files outside this hash-bound recovery workflow.", ""]
    if other:
        lines += [f"OTHER INCOMPLETE WORKERS ({len(other)})",
                  *[f"  {name}" for name in other[:12]],
                  "See Details & full log for their individual reasons.", ""]
    if len(empty) > 12 or len(unreadable) > 12 or len(other) > 12:
        lines += ["This preview is shortened; the recovery centre has the full list and Export complete issue list.", ""]
    lines += ["These exceptions are not processed workers or an accuracy pass.",
              "Completed work and the saved request history are retained. Do not resubmit the whole batch."]
    return "\n".join(lines)
