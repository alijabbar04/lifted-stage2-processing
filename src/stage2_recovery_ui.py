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
    lines = [f"{completed} worker folder(s) completed. The following source issues remain.", ""]
    if empty:
        lines += [f"NO DOCUMENTS SUPPLIED ({len(empty)} folders)",
                  *[f"  {name}" for name in empty[:12]],
                  "Supply the missing originals, or resolve the intentionally empty scope.", ""]
    if unreadable:
        lines += [f"SOURCE DOCUMENTS NEED ATTENTION ({len(unreadable)})"]
        labels = {"unreadable_source": "Unreadable document",
                  "source_missing": "Source missing", "source_changed": "Source changed"}
        for item in unreadable[:12]:
            if isinstance(item, dict):
                label = labels.get(item.get("reason"), "Source evidence unavailable")
                lines.append(f"  {item.get('worker', '?')}: {Path(str(item.get('path', ''))).name} - {label}")
        lines += ["Obtain complete originals. Do not replace documents in an active/pending run;",
                  "a replacement needs a deliberately reconciled or new processing scope.", ""]
    if other:
        lines += [f"OTHER INCOMPLETE WORKERS ({len(other)})",
                  *[f"  {name}" for name in other[:12]],
                  "See Details & full log for their individual reasons.", ""]
    if len(empty) > 12 or len(unreadable) > 12 or len(other) > 12:
        lines += ["Lists are shortened here; full source outcomes remain in the saved batch state.", ""]
    lines += ["These exceptions are not processed workers or an accuracy pass.",
              "Completed work and the saved request history are retained. Do not resubmit the whole batch."]
    return "\n".join(lines)
