"""Lifted upload-label policy shared by Stage 2 and mirrored by Stage 3.

The portal removes whitespace and the extension before limiting the visible
attachment label to 50 characters.  Filesystem length is therefore not a safe
proxy.  This module deliberately has no application dependencies so both
desktop applications can package and test the same contract independently.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import re

NAMING_POLICY_VERSION = "lifted-upload-label-v1"
MAX_PLATFORM_ATTACHMENT_CHARS = 50

_KNOWN_EXTENSIONS = r"pdf|jpe?g|png|docx?|mhtml?|xlsx?|pptx?|txt"
_ILLEGAL = re.compile(r'[<>:"/\\|?*]')
_SPACE = re.compile(r"\s+")
_LOW_INFORMATION = {
    "a", "an", "and", "for", "from", "in", "of", "on", "the", "to",
    "with",
}
_SECONDARY = {"copy", "document", "file", "form", "policy", "record"}
_ABBREVIATIONS = {
    "administration": "Admin",
    "certificate": "Cert",
    "confirmation": "Confirm",
    "identification": "ID",
    "information": "Info",
    "qualification": "Qual",
    "registration": "Reg",
    "verification": "Verify",
}


def sanitize_label(value: str) -> str:
    """Return a readable Windows-safe label without silently slicing a word."""
    text = _ILLEGAL.sub(" ", str(value or ""))
    return _SPACE.sub(" ", text).strip(" .-") or "document"


def platform_attachment_display_key(value: str) -> str:
    """Return exactly the label shape measured by the Lifted portal."""
    name = Path(str(value or "")).name
    stem = re.sub(rf"\.({_KNOWN_EXTENSIONS})$", "", name, flags=re.I)
    return _SPACE.sub("", stem).casefold()


def normalized_length(value: str) -> int:
    return len(platform_attachment_display_key(value))


def _fits(label: str, suffix: str, limit: int) -> bool:
    return normalized_length(label + suffix) <= limit


def _join(words: list[str]) -> str:
    return " ".join(word for word in words if word).strip()


def concise_upload_label(value: str, *, reserved_suffix: str = "",
                         limit: int = MAX_PLATFORM_ATTACHMENT_CHARS) -> str:
    """Create a deterministic, word-complete portal-safe label.

    Suffix space is reserved before shortening.  Common connector words are
    removed first, then familiar administrative terms are abbreviated.  Only
    when the complete phrase still cannot fit are whole words selected and a
    stable digest appended; the digest prevents two lossy summaries silently
    becoming the same upload identity.
    """
    original = sanitize_label(value)
    suffix = _SPACE.sub(" ", str(reserved_suffix or "")).strip()
    suffix = (" " + suffix) if suffix else ""
    if _fits(original, suffix, limit):
        return original

    words = original.split()
    reduced = [word for word in words if word.casefold() not in _LOW_INFORMATION]
    if not reduced:
        reduced = words[:]
    candidate = _join(reduced)
    if _fits(candidate, suffix, limit):
        return candidate

    abbreviated = [_ABBREVIATIONS.get(word.casefold(), word) for word in reduced]
    candidate = _join(abbreviated)
    if _fits(candidate, suffix, limit):
        return candidate

    secondary_removed = [word for word in abbreviated
                         if word.casefold() not in _SECONDARY]
    if len(secondary_removed) >= 2:
        candidate = _join(secondary_removed)
        if _fits(candidate, suffix, limit):
            return candidate
        abbreviated = secondary_removed

    digest = hashlib.sha256(original.casefold().encode("utf-8")).hexdigest()[:7]
    marker = f" ~{digest}"
    budget = limit - normalized_length(suffix) - normalized_length(marker)
    if budget < 1:
        raise ValueError("The reserved filename suffix leaves no upload-label budget.")

    # Keep complete words in their original order.  Prefer the first words,
    # then retain the last distinguishing word whenever it fits.
    selected: list[str] = []
    used = 0
    for word in abbreviated:
        cost = normalized_length(word)
        if used + cost <= budget:
            selected.append(word)
            used += cost
    if abbreviated and abbreviated[-1] not in selected:
        last = abbreviated[-1]
        while selected and used + normalized_length(last) > budget:
            removed = selected.pop(-1)
            used -= normalized_length(removed)
        if normalized_length(last) <= budget and last not in selected:
            selected.append(last)
    if not selected:
        raise ValueError(
            f"No complete word from {original!r} fits the remaining upload-label budget."
        )
    candidate = _join(selected) + marker
    if not _fits(candidate, suffix, limit):
        raise AssertionError("Upload-label shortening exceeded its reserved budget.")
    return candidate


def portal_safe_stem(value: str, *, reserved_suffix: str = "") -> str:
    return concise_upload_label(value, reserved_suffix=reserved_suffix)


def final_filename(label: str, extension: str, *, reserved_suffix: str = "") -> str:
    ext = str(extension or "")
    if ext and not ext.startswith("."):
        ext = "." + ext
    stem = portal_safe_stem(label, reserved_suffix=reserved_suffix)
    filename = f"{stem}{(' ' + reserved_suffix.strip()) if reserved_suffix.strip() else ''}{ext}"
    if normalized_length(filename) > MAX_PLATFORM_ATTACHMENT_CHARS:
        raise AssertionError("Final emitted filename violates the upload-label policy.")
    return filename


def validate_controlled_names(names) -> list[str]:
    """Return controlled names whose canonical spelling exceeds the portal cap.

    Final-path helpers can still shorten arbitrary text safely, but a controlled
    vocabulary change must not silently turn its canonical label into a lossy
    alias.  Failing startup/tests makes that policy decision explicit.
    """
    failures = []
    for name in names:
        try:
            canonical = sanitize_label(str(name))
            if normalized_length(canonical) > MAX_PLATFORM_ATTACHMENT_CHARS:
                failures.append(str(name))
        except (TypeError, ValueError):
            failures.append(str(name))
    return failures
