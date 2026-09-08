"""
STAGE 2  -  PROCESSING   (Doc Review AI Station + PDF conversion)  -  Windows
=============================================================================
This is "Stage 2" of the care-home compliance pipeline. It takes the merged
worker folders produced by Stage 1 and, worker by worker:

      CONVERT EVERYTHING TO PDF  ->  RENAME (AI)  ->  DEDUPE  ->  RANK  ->  ORGANISE

It combines two earlier tools:
  - the PDF-conversion stage of the batch orchestrator, and
  - the full Doc Review (AI) Station (classify / rename / second-pass rank /
    organise), whose UI this app keeps (with a black ribbon).

WHAT IT DOES, PER WORKER FOLDER
-------------------------------
You pick the folder of MERGED worker sub-folders from Stage 1. Inside each
worker sub-folder are loose documents (images, Office files, .msg, PDFs).

For each worker, in order:
  0. CONVERT TO PDF. Every non-PDF document (images, .docx/.doc, .xlsx, .pptx,
     .txt, .msg, .eml, etc.) is converted to a PDF in place; the original is
     removed once the PDF is written. PDFs are left as-is. Conversion uses
     PyMuPDF / Pillow for images and LibreOffice (headless) for Office files,
     with a plain-text fallback. After this step the whole worker folder is
     PDFs only, which the rest of the pipeline then renames.
  1. Every document is rendered and sent to the Claude API, which decides which
     of the controlled filenames it matches. The file is renamed automatically
     (fully automatic - no per-file confirmation).
       - Anything in the "Other" group is named  "Other".
       - A document titled "schedule of statement of main terms and conditions
         of employment" is named  "Employment Contract".
       - A document the API cannot confidently match to the list PAUSES the run
         and asks you to define it, with two buttons: "Relevant" or "Other".
         Your choice is written back into the Filename Identification Record
         workbook (Relevant -> Important tab, Other -> Other tab) so it is known
         next time.
  2. After every document for that worker is renamed, a second review pass
     RANKS duplicates. For every document type that has 2 or more copies, the
     copies are scored on NEWEST + CLEAREST + MOST RELEVANT and renamed with a
     numbered suffix where a HIGHER number is the BETTER document:
         DBS Document            (worst copy)
         DBS Document (01)
         DBS Document (02)       (best copy)
     A type with only one copy keeps its plain name. Two dated types keep their
     date and, when there are multiples, also carry the rank number:
       - Certificate of Sponsorship -> "Certificate of Sponsorship - (15-02-2025)"
         (or "... - (15-02-2025) (02)" when several exist), and
       - Share Code Check Result    -> "Share Code Check Result - (13-02-2025)".
     (Dates use hyphens because "/" is illegal in Windows filenames.)
  3. The worker's documents are organised into exactly two sub-folders, split
     by whether a document needs Stage 3's individual overwrite-upload flow:
       - "Overwrite Documents" : only the OVERWRITE_TYPES (BRP, Share Code
         Document, National Insurance Number, Certificate of Sponsorship, Share
         Code Check Result, ECS Notice, Proof of Car Insurance, UK Driving
         Licence, Non UK Driving Licence, DBS Document, eVisa Screenshot, UKVI
         Draft Application, Proof of Vehicle Tax, Proof of Car Ownership, Visa
         Vignette, Employment Contract). Left loose, keeping any rank/date suffix on the filename
         (the suffixes encode the upload order).
       - "Bulk"                : EVERY other document (all non-OVERWRITE_TYPES,
         including "Other" and any type not in the set), split into "Batch 01",
         "Batch 02", ... sub-folders of up to BATCH_SIZE (30) files each.
     (No program/manifest file is ever written into a worker folder.)
  4. The app moves straight on to the next worker - no end-of-worker prompt.

EVERYTHING LIVES IN ONE PLACE
-----------------------------
No loose files on your Desktop. On first run the app creates

      C:\\DocReviewAIStation\\

and keeps inside it:
  - config.json                              (API key, model, FX rate)
  - Filename Identification Record.xlsx      (the controlled vocabulary;
                                              auto-seeded on first run, then
                                              updated as you classify unknowns)
  - filename change record.csv               (every rename made)
  - Doc Review AI filename change record.xlsx(logged only when you override an
                                              AI suggestion)

SETTINGS (cog button): paste your Anthropic API key, choose the model
(Haiku / Sonnet / Opus) and set the USD->GBP rate used for the cost figure.

A running cost estimate (GBP) for the whole session is shown in the
session-information panel, based on the real token counts the API returns.

Requirements:
    pip install pymupdf pillow openpyxl keyring

    For converting Office documents (.docx/.doc/.xlsx/.pptx) to PDF, install
    LibreOffice (https://www.libreoffice.org). If LibreOffice is not found the
    app still runs: images, PDFs and plain text are converted natively, and
    Office files fall back to a text-only PDF (or are left unchanged if even
    that fails). The conversion step can be turned off in Settings.

    (keyring is optional but recommended: it stores the Anthropic API key in the
    operating-system credential store - Windows Credential Manager / macOS
    Keychain / Secret Service - instead of in a plaintext file. If keyring is
    not installed the app still runs and falls back to the ANTHROPIC_API_KEY
    environment variable.)

Run:
    python Stage2_Processing.pyw
"""

import os
import re
import csv
import sys
import io
import json
import stat
import time
import base64
import shutil
import subprocess
import hashlib
import logging
import math
import platform
import datetime
import threading
import traceback
import collections
import copy
import functools
import tempfile
import uuid
import urllib.request
import urllib.error
import urllib.parse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

# Keep the source directory importable when this .pyw is loaded directly by
# the offline harnesses (importlib does not add it to sys.path for us).
_SOURCE_DIR = Path(__file__).resolve().parent
if str(_SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOURCE_DIR))
import pipeline_shared as pipeline
from stage2_compact_ui import CompactDashboard
from stage2_notifications import NotificationService, normalize_settings
import stage2_ai_workflows as ai_workflows
from stage2_theme import PALETTES, resolve_palette
from stage2_locking import (DOCUMENT_WRITER_LOCK, PathWriterLock,
                            WriterLockBusy)
from local_orientation import (
    MODEL_NAME as ORIENTATION_MODEL_NAME,
    MODEL_REVISION as ORIENTATION_MODEL_REVISION,
    MODEL_SHA256 as ORIENTATION_MODEL_SHA256,
    OnnxOrientationPredictor,
    atomic_write_json,
    orientation_decision,
    thumbnail_evidence,
)


def bundled_resource(*parts) -> Path:
    """Resolve a source-tree or PyInstaller-bundled read-only asset."""
    root = Path(getattr(sys, "_MEIPASS", _SOURCE_DIR.parent))
    return root.joinpath(*parts)

# ---------- optional libs ----------
try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import openpyxl
    from openpyxl import Workbook, load_workbook
    HAS_XLSX = True
except ImportError:
    HAS_XLSX = False

# keyring lets us store the API key in the OS credential store instead of a
# plaintext file. It is optional - if it is missing we fall back to an
# environment variable and (only if the user insists) an obfuscated local file.
try:
    import keyring
    HAS_KEYRING = True
except Exception:
    HAS_KEYRING = False


# ====================================================================
# THEME
# ====================================================================
BG        = "#060708"
PANEL     = "#0e1012"
PANEL2    = "#16191c"
BORDER    = "#282c30"
FG        = "#eff1f3"
FG_DIM    = "#a2a8af"
ACCENT    = "#e4e9ee"
GREEN     = "#1f9d55"
GREEN_HI  = "#27c468"
RED       = "#c0392b"
RED_HI    = "#e74c3c"
AMBER     = "#d29922"
AMBER_HI  = "#f0b429"
RIBBON    = "#000000"   # Stage 2: the top ribbon is pure black
RIBBON_FG = "#ffffff"

# Embedded 64x64 PNG used as a runtime window icon fallback (works even if
# the stage2.ico file is not alongside the script).
APP_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAMh0lEQVR4nO2bf3Bc1XXHP+fe91ba"
    "XUmrX9jYWAbsGuNfQDGY8KMxoXZpSttMAJkmtFCmOC5NmdIyKbRNKjvMpE1a2rSdTAppk5KhASQY"
    "YAIOBDxg8NgYMETYGDDYYMuWbUmWVvJK2t333j39YyVjbGNp5RWIjr//7b53dt/53nPP+d777oFR"
    "oLFZ7eLn1GtqUjOa+z9lyOIm9Rqb1YLKiDcf72Jjs9qWRhwiOvzd9S9plVfGxCSiGu47U9KHf9XY"
    "rLZlmUQfZ/KxBBxueN1avUTyfDnXx6LBrvQ8UA9F0eMT+IlCUHCU19S9G6vkTS/JY8F+ftGyTPI0"
    "qWElevhAfmh2FFSampBVq8Rds1p/kzx39L7/9tL9r/yc9PaXyezegosCRCaO78NQVZKnzqZy+nym"
    "XPRlUrMu3BKrsv/a8tvyY4CmJjWrVok73EaO+IXCZxFd9ktt6tu+d+XbD/w1+15+VMNsf2SsiPHj"
    "ZkI6T8EZF+RcFIZqPF9qZl1k5974b0w67/zmnjfbbvnl7dO7aVLDYSR8xJOhsHdXP6r3HNiyYfmm"
    "u6+Jsum9xCprrBgLqqi6I/97QkEQEAM4woE+p85F82++x5++9OZXGOCLj91Ad+HGwnTwhg0bVW2L"
    "SNT4pK7seG3T8nXfuiQfS1T4Zal60ShEo/DT8ahIKApDg+QlUgYwr9+7PB8OZC6ctey2BxG5srFZ"
    "pQUioJDNG5uHnF+tiw+2Hfj7TXdfFcbilb7xyuSz4vixoC5C1ZFI1cfeuv8vg85NG5Y0Pql3tiyT"
    "qFAmh6bAcH1vncea1h/88eVta++LylL19rPs/EdgDC4/qOW1DXrxylfTdfMq59x/KZ0omMZmtatW"
    "iXtnMRdm9nxw+b6XH3ZlFTX/f5wHcA6vrEIy7dtcz7a1tUGGG0F04b143o6eTQaI3CDX7X/1EYLB"
    "fleWihvGSICIYK0t2bOr6lGfnSs+Eas6jOdJ23M/0skLf/faRtV/mQuRN6Nmh9ukKvmHWZTe/hJi"
    "RdCj9MKoIMYQhSH9B3vGZH9sWD4scooYj0QyeRQxI0HVYWPlJtPWKtnuznmJTafUrLpAuryWZcui"
    "r72qqV3d6bMzu7fgxeJmLKXOGEM2O0gqVc2KFSuoqKggiqIxCibFGEN//yBd3Wk8z8M5h+/HaG/f"
    "zZpnnsb3/eJIUMVYX7LpvS6Xbo+b7ClzgBc9gJxF1Dlfw4ARlgfHhIgQhiHV1dU88MCDLLni82Tz"
    "Ecac2JJBVdn+wR727u/Cs4ZEsoIN69fx1OoniMViRUcBCOpCUGd0SAJ4H14SZYwKz1pL/8EeVqxY"
    "wZIrPs/OtnY8zxvZcKTHFaGuOkE6bWnf20l1ENCfyZygDB8Su8pHhVApUFFRQS5weJ5XEgKGcdbM"
    "6YjAwf5cSRMslJiAY835Exmt4RCPIsfMM6axrzNNGIZjDdRjoqQEHO6siKCq5IOgoM+LgKJ41mKt"
    "PVTyhkmYPKmWKCrdeqSkBEBh1ARwzvHixla6071Fh62qYj3L4ot+narKJM5Fh6LhlLqakj5vyQkQ"
    "kULVFqEmVQko1tpRZ+zhyPE8D98r2B1uOhYRdDyUnIDDcf6C2WOpqqAFIiIX4VxBE5Ta8WGMGwEi"
    "QhCG5IMxJi39aE6xdnw2YcY5B/yK7nTfCZUuESEfBJzZMJUvXLZorCr9YzHOOaAKoKgc8OEPDe/u"
    "QBhGVFUkxqD8RsbEzAEACm4ompCCxijojBI+IBM1ByhYa47QAR+7tX9CmJA54EgdMPZV5ciYcDng"
    "WDpgPDExc8BJHcBJHTCMkzrgpA7gpA6Akzrgs6cDACqrkkTqsNaMPnkJMKQDjBVCFx76PafgEMR6"
    "iPEQQvQEy+O4TAGnjoQXZ+kFi8b8Ol1EiCJX2FZzjky+j2QM4pJDgwHyB7NgPGxZAjEWdWObIiUn"
    "wKmS9ONs79vD6raNmEOhW3wGN2LJu4CF9Q2cV30BG3Y5Xs7NZfa138aEg/S8u56+na0E/T34yZox"
    "kVBSApyL8K3wbm8bN63/Lrv7O/HEjsF1BQxKSKQBcxI3MydRSXu/YOxs5l//LdRBlIfMnjfZ/vj3"
    "2LPup/gVQ/uFRZTLkhLgxXyMKKt3b2R3fxdTEvWELqTYWigISoCVCmb4t1Mp8+nOKZUxwTkl11tI"
    "isYYqk+fx8Lb7qN65iLefvAbGK+8uGcu6u6PQUGgGHbs2IGIwRqLJ4bQRURjygEFAk63fwX5+eyP"
    "Qox4WIG4L1jrkY0gn4NowGGNY8bVXyfKZnj7oTuJVdaNejqUhIAoikhWpHj4oYdYevHlJC9Kjrlu"
    "C5aIXiabG6g2C7jwjJDZ9R5BBDvTsKkdMnn4tVo4bwrUxQ27+4T1OwOmf+kOera9SMcbT+MnUqMi"
    "oXRTQMDGYqz4s1uYcdMl1HzxVKJ0nuKOVAqOPL7UkeJ3+PNLlCtmWNr7CiXwugXwzWfh9b3w3Ssh"
    "G0DgYEql8PuzDX+3DqYtvY3OzU+POg+UjABVxRqLSZbz/iObOKXhIvw59RC6Yd9GhKghkjyx8GxO"
    "jVdxxQzHmx2GFY9D5OC0KrAGyi3c9Rxs7ShEw11L4LLTDbOqlD0NF5KoayDb24l4/ohElDQJqlMw"
    "ghcoPd9/BX9mcW9xxFiCgW6qLl9Mbgns7XXMm2T4ydUFZze0wevtUOZB6z7wDPgWauLgVNjd40jW"
    "VpGcOof+rjZ8vwzV40+D0gshVfAEIiW/uaMoU7Ee+VwaWRCQVfiHtXDDQjjnVJhVB1+aAz/YCI+/"
    "BeUeRApNV8C8SfDvL8HOHqiqErAxYHTJd3wWQ0MnWiThF2UmxkNCIcj3UW7gjf1w6xNQWQaLz4Q7"
    "fwMunQ4PbymM+p2fLxBzx9OwZjtMrhCiMCIc7ENkdOuPcV0O44qVQA5rPA5sW09Mlf/4PUPrPti8"
    "H6anCvN/Vy8Yge8shZm18Fo7zKqHRdOUF9pg87Yu+ve8ifXjo5Lh40tAkVAXYcuSZHa+RtfOHfR+"
    "biZXneW47hxD5GDdTvhZa2H0K2NwMAfzJ8H5UwEidgcezzzyLEFfBzZZ+wmXwVLBWKL+NO88+m3u"
    "mnwffhCRLBMiFfpyHya+W58EK4UgiyKH8Q2DmV72P/M98MtHvXt0RJX+9E+Bq4vwk9W0r/spnc/+"
    "kHidz8GcYzAXEfcU3xTybCYHvVmlbyBkUA05Y2j971s52PYGtix56LzwSPAAAosoKqM1Gm8USKjh"
    "rQduJxhIM+uav0EshFlwkSvsGBnBWMGr8sh299D6n3/B/o0PEKuoG+Fgt4Iq4gqD7zU1NRkOcDCT"
    "qt2RmHLWeQNdH6jvl8l4v5AYEQLGK+ed5r+lZ9t6Tr/yFlIzPkcsVYsY0AAGu9vo3rqG95/8Pn27"
    "Wo+/BhBBXahlVZPFr5qUFcN7AN7zl680a78g4bVPaGvl9PPO7Xj9Fw4xZrR1dNwwNACxynq6Nj9F"
    "5+aniNc1kJw6B+NZwsEMmT1byfd2YPxyYlX1xx95EaIgqxUN55jymqm74pexF1XxeL5w3Zbx8JRF"
    "1934/up/njBTAUBdiJdIAUq+r5PsgbZCs5IYTCyOX1FbaOQY4WyziCHK591pl91gyk8xP79XJFh4"
    "z6u+WbtKQlUVl2RN5ZnnvlMz62IJB/qcmNKexzsRqItQ5xDPx0uk8BPVePFKROyhnoDjQgQNA41V"
    "pEz9uVfl8PkxwKavLYwMwLIWTMslMhiv5Ttzb/yhUTc0kSZab5DqEBnR0GboKEud9cn194Znf+Vu"
    "U9kw9cGWJbK1sVktIs4ADHdQNG9YeX/d3LmPzL/5v/yBvgN5MRaRidkiODoIxosxkO4Ip116vX/6"
    "lX/yvnPc3qRqWhoLSe6Qdy2NOFau1ME+lk9fetMrC/7wn2L53q4gyg+qWL+QFydaRBwTgohBrAfq"
    "dDDdETRc+lVvwfKf9BiPZY9dLQcKtxWapj7q0VBL2W/dvas2NafhZ/s2vnDl5h8tJ9O+LTSeNTYW"
    "F7GxCU2DiwJ1YU6jfN7Fkilv9lf+kRlX/em7znH9o9fIK0d2kh7ly+HNhcue1m9mdnfe2r31hUlt"
    "z9/HwV1vkOvd6zQKJ2Y0KJRVTTbxSWdw2mVfpf7cJfnqWbP/t/u9Pd9Yc+u0A8dqoz22F6oy9IZX"
    "/+BZnWx8/qh/P1dnD3TMyx1or9LC+5lPwqWioOqIpeoH43XTPohPsqv9BP9z/6WyBaBJ1awSOapc"
    "jNw8fRhjX9+qdekO5lKGZQL2VHke+HW81/sr9g4/97EawIuDqix+Tj0mwkqpCIy23f//AIQnLRWI"
    "e0kZAAAAAElFTkSuQmCC"
)

MONO = ("Consolas", 10)
UI   = ("Segoe UI", 10)
UI_B = ("Segoe UI", 10, "bold")
UI_H = ("Segoe UI", 13, "bold")

IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif", ".webp"}
PDF_EXT = {".pdf"}
# Document types the AI pipeline can read directly once rendered.
DOC_EXT = IMG_EXT | PDF_EXT | {".docx", ".doc", ".txt"}

# ----- Stage 2: source types we will CONVERT to PDF before the AI pipeline --
# Office formats handled by LibreOffice (headless). Email formats handled by a
# small native extractor. Images / .txt are converted natively.
OFFICE_EXT = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".odt",
              ".ods", ".odp", ".rtf"}
EMAIL_EXT  = {".msg", ".eml"}
# Everything Stage 2 will attempt to turn into a PDF.
CONVERT_EXT = IMG_EXT | OFFICE_EXT | EMAIL_EXT | {".txt"}
# Files the converter scans for inside a worker folder (PDF kept as-is).
CONVERT_SCAN_EXT = CONVERT_EXT | PDF_EXT

BATCH_SIZE = 30


# ====================================================================
# MODELS & PRICING  (edit these IDs / prices if Anthropic updates them)
# ====================================================================
# THIS IS THE SINGLE SOURCE OF TRUTH FOR PER-TOKEN PRICES.
# Prices are in US dollars per MILLION tokens ("in" = input, "out" = output),
# taken from Anthropic's public pricing. LAST VERIFIED: 2026-09-02.
# https://platform.claude.com/docs/en/about-claude/pricing
# If Anthropic changes prices, edit ONLY the numbers here - every cost estimate
# and the live cost meter read from this block via MODELS_BY_ID.
#
# Opus is intentionally separated out. For a high-volume *classification*
# workflow it is far more expensive (5x the input price of Haiku) and rarely
# more accurate at this task, so it is hidden unless the user explicitly turns
# on the "advanced models" setting AND confirms an extra warning.
SAFE_MODELS = {
    "Haiku  (cheapest)":  {"id": "claude-haiku-4-5",  "in": 1.00, "out": 5.00},
    "Sonnet (balanced)":  {"id": "claude-sonnet-4-6", "in": 3.00, "out": 15.00},
}
ADVANCED_MODELS = {
    "Opus   (most able, EXPENSIVE)": {"id": "claude-opus-4-8", "in": 5.00, "out": 25.00},
}
# MODELS is the full registry (used for pricing lookups). The Settings dialog
# decides which subset to actually *offer* based on the advanced-models flag.
MODELS = {**SAFE_MODELS, **ADVANCED_MODELS}
DEFAULT_MODEL = "Haiku  (cheapest)"

MODELS_BY_ID = {m["id"]: m for m in MODELS.values()}
FX_RATE = [0.79]   # mutable holder for USD->GBP, updated from config/settings

# The Message Batches API bills at HALF the standard per-token price (input AND
# output) in exchange for asynchronous (up to 24h) processing. Applied in ONE
# place (batch cost estimates + reconciliation) so the discount is never
# hard-coded elsewhere.  Ref: Anthropic Message Batches pricing, 2026-09-02.
BATCH_DISCOUNT = 0.5

# Anthropic vision token cost for an image is approximately
# (width_px * height_px) / 750.  Used by the page/resolution cost estimator.
VISION_TOKENS_PER_PIXEL = 1.0 / 750.0

# Rough token sizes for the ESTIMATOR only (never billing). The real system
# prompt + controlled vocabulary is measured live at estimate time; these cover
# the smaller, variable parts of each request.
EST_TEXT_TOKENS_PER_DOC = 400     # extracted-text block sent alongside images
EST_OUTPUT_TOKENS_PER_DOC = 150   # the small JSON object the model returns
# Average number of page-images actually sent per document. With adaptive pages
# on, most docs are settled from page 1; without it, multi-page PDFs send more.
EST_IMAGES_PER_DOC_ADAPTIVE = 1.3
EST_IMAGES_PER_DOC_FULL = 1.8
# Fraction of documents that trigger a (live-priced) second-pass review call
# (dating / ranking / signed checks). Rough - most folders have few duplicates.
EST_SECOND_PASS_FRACTION = 0.15
# Batch v1.3.1 reserves a discounted follow-up for only the genuinely
# unresolved first-pass results.  The reserve is deliberately shown separately
# from the primary batch estimate; actual submission contains exactly the
# unresolved documents and is budget-gated again before it is sent.
EST_BATCH_FOLLOWUP_FRACTION = 0.10
# The optional audit re-classifies every final document live and adjudicates
# only prospective mismatches.  This is an estimate, not a billing assumption.
EST_AUDIT_ADJUDICATION_FRACTION = 0.15

# --------------------------------------------------------------------
# SAFETY / COST-CONTROL DEFAULTS
# These provide hard ceilings so a single click can never trigger an unbounded
# run. They are all user-editable in Settings but default to sensible values.
# --------------------------------------------------------------------
DEFAULT_MAX_WORKERS   = 100      # stop after this many worker folders in one run
DEFAULT_MAX_FILES     = 2000     # stop after this many files sent in one run
DEFAULT_MAX_BUDGET_GBP = 35.0    # cumulative ceiling across every enabled phase
DEFAULT_MAX_FILE_MB   = 25.0     # skip any single file larger than this
CONFIRM_COST_THRESHOLD_GBP = 1.0 # pre-flight estimate above this needs confirmation

# rough average tokens/file used ONLY for the pre-flight estimate (not billing -
# billing always uses the real token counts the API returns)
EST_TOKENS_IN_PER_FILE  = 1500
EST_TOKENS_OUT_PER_FILE = 200

# rough seconds spent per file for the pre-flight TIME estimate. This is a wall-
# clock figure (API round-trip + render + occasional second-pass calls) for
# files that are already on local disk. It is user-tunable in Settings, because
# the true rate depends on the machine, connection, and whether OneDrive files
# are local or cloud-only. Cloud-only files can be far slower (download time).
DEFAULT_SEC_PER_FILE = 6.0


# ====================================================================
# CONTROLLED VOCABULARY  (the master list)
# These are the *seed* values written into the Filename Identification
# Record workbook on first run. After that the workbook on disk is the
# source of truth and is updated when you classify unknown documents.
# ====================================================================
SEED_CRUCIAL = [
    ("BRP", "A PHYSICAL, credit-card-sized Biometric Residence Permit CARD. It has "
            "TWO sides and BOTH sides are 'BRP':\n"
            "  - FRONT: face photo, card number, 'Residence Permit' wording, visa "
            "category/type, and an expiry date.\n"
            "  - BACK (no face photo): Date and Place of birth, Sex, Nationality, a "
            "remarks area often reading 'NO PUBLIC FUNDS' and 'NI NUMBER <number>', "
            "and a two-line machine-readable zone (MRZ) at the bottom.\n"
            "The BACK of a BRP is still 'BRP', NOT 'Passport' and NOT 'National "
            "Insurance Number' - even though it shows an MRZ and an NI number. A BRP "
            "is a small CARD, never a passport booklet page. It is also NOT a "
            "'Share Code Check Result' (a right-to-work check record). And it is "
            "NOT a DRIVING LICENCE: a photocard headed 'Driver Licence' / "
            "'Driving Licence' / 'Provisional Driver Licence' (with a licence "
            "number, licence class and conditions) is 'UK Driving Licence' or "
            "'Non UK Driving Licence', never 'BRP' - a BRP is headed 'RESIDENCE "
            "PERMIT'. If you see either side of a residence-permit card, this is "
            "'BRP'."),
    ("Certificate of Sponsorship", "The UKVI Certificate of Sponsorship (CoS) "
                                   "details record itself. To be this type the "
                                   "document must show the CoS's OWN fields: a "
                                   "CoS/certificate number, sponsor (licence) "
                                   "name/number, the worker's personal details, "
                                   "the JOB (title and/or SOC occupation code, "
                                   "usually salary and work dates), and dates "
                                   "such as 'Date assigned' and an expiry (use "
                                   "by) date. Merely mentioning sponsorship, "
                                   "'Tier 2', 'Skilled Worker' or a CoS number "
                                   "does NOT make a document a CoS. It is NOT: "
                                   "a Sponsorship Management System screenshot "
                                   "or navigation/summary page without the full "
                                   "job details (that is 'CoS Summary'); an "
                                   "email or letter discussing a CoS (that is "
                                   "'CoS Query Email'); a police/criminal-record "
                                   "certificate ('Proof of Police Check'); a "
                                   "training certificate ('Training "
                                   "Certificate'); a visa approval letter "
                                   "('Health and Social Worker Visa Letter'); "
                                   "a visa application receipt ('Proof of Visa "
                                   "Application'); an offer letter; or a "
                                   "university 'Confirmation of Acceptance for "
                                   "Studies' (CAS) - a CAS is the STUDENT-visa "
                                   "equivalent issued by a university, never a "
                                   "CoS (file it 'Other - Confirmation of "
                                   "Acceptance for Studies (CAS)'). THE WORD "
                                   "'CERTIFICATE' IN A TITLE NEVER MAKES A "
                                   "DOCUMENT A CoS - require the CoS number AND "
                                   "the sponsor AND the job/salary fields to be "
                                   "visible. In particular a document headed "
                                   "'Certificate of Motor Insurance' or otherwise "
                                   "from an INSURER (policyholder, vehicle "
                                   "registration mark, policy number, cover dates) "
                                   "is 'Proof of Car Insurance'; a bank statement "
                                   "(bank logo, account/sort code, transaction "
                                   "table) is 'Bank Statement'; a gov.uk 'Vehicle "
                                   "Tax and MOT status' page is 'Proof of Vehicle "
                                   "Tax'. None of these become a CoS by sitting in "
                                   "a sponsorship folder or carrying a "
                                   "'CERTIFIED TRUE COPY' stamp."),
    ("DBS Check Notes", "Free-text notes, a memo, a chase-up email, or a "
                        "hand-typed/handwritten record specifically ABOUT A DBS "
                        "CHECK. To be this type the TEXT ITSELF must mention DBS - "
                        "e.g. 'DBS', 'Disclosure and Barring Service', 'Disclosure', "
                        "'Barring', 'DBS Update Service', 'enhanced/standard/basic "
                        "check', or a disclosure/DBS reference number. It is the "
                        "informal notes ABOUT a check, not the certificate and not "
                        "the check-record sheet. "
                        "CRITICAL: notes/handwriting that do NOT mention DBS are NOT "
                        "'DBS Check Notes', however 'notes-like' they look. In "
                        "particular it is NOT: probation notes / end-of-probation "
                        "feedback ('Probation Review'); supervision notes "
                        "('Supervision'); interview notes ('Interview Notes'); any "
                        "vehicle/insurance declaration ('Use of Own Car "
                        "Declaration'); any safeguarding questionnaire "
                        "('Safeguarding Questionnaire'); shift complaints, training "
                        "lists, or payslip queries; or any other handwritten HR form. "
                        "It is NOT an 'Adult First Certificate' and NOT a "
                        "'DBS Document' (the official certificate)."),
    ("DBS Check", "The employer's/agency's DBS CHECK RECORD or RESULT SUMMARY - "
                  "NOT the certificate itself. To be this type the document's "
                  "CONTENT must carry an explicit DBS signal - one or more of: "
                  "'DBS', 'Disclosure and Barring Service', 'Disclosure number', "
                  "'DBS Reference', 'Barred List', 'Certificate Issue Date', "
                  "'Result Date', 'Adults/Children's Workforce', an "
                  "enhanced/standard/basic check, or a DBS Update Service status "
                  "page. This includes: a 'DBS Service Check' / 'DBS Update Service' "
                  "status check or service-check printout; AND a 'DBS Check list' / "
                  "results-summary sheet an employer fills in to record and verify a "
                  "check (Disclosure number, DBS Reference, Barred List, Workforce "
                  "Type, Result Date, 'Verification By'). A sheet whose purpose is to "
                  "LOG or VERIFY the outcome of a DBS check is 'DBS Check'. "
                  "WITHOUT an explicit DBS signal it is NOT a 'DBS Check', however "
                  "form-like it looks: it is NOT probation notes / end-of-probation "
                  "feedback ('Probation Review'), NOT supervision notes, NOT "
                  "interview notes, NOT any vehicle/insurance declaration ('Use of "
                  "Own Car Declaration'), NOT a safeguarding questionnaire, and NOT "
                  "any HR form that merely happens to be handwritten or signed. "
                  "It is 'DBS Check', NEVER 'DBS Document' (the certificate)."),
    ("DBS Document", "The OFFICIAL DBS CERTIFICATE itself - the actual certificate an "
                     "employer keeps. Valid forms are ONLY: the GREEN paper DBS "
                     "certificate; a 'DBS Certificate Record' that reproduces the "
                     "certificate's own content (Name, Date of Birth, Date of original "
                     "DBS Check, Type of check, Outcome, Unique Reference Number, "
                     "counter-signatory); and a certificate bearing a 'uCheck' logo. "
                     "It IS the certificate - a formal issued document with a "
                     "certificate/disclosure number and the disclosure outcome. It is "
                     "NOT a 'DBS Service Check' or a 'DBS Check list'/results-summary "
                     "sheet with employer verification fields ('Result Date', "
                     "'Position', 'Workforce Type', 'Verification By', 'Disclosure "
                     "Level', 'Barred List') - that is 'DBS Check'. It is NOT internal "
                     "notes ('DBS Check Notes') and NOT an 'Adult First Certificate'. "
                     "If in doubt between the certificate and a check/verification "
                     "record, only call it 'DBS Document' when it clearly IS the "
                     "issued certificate."),
    ("Employment Contract", "The actual contract / statement-of-terms document, "
                            "titled e.g. 'Statement of Main Terms of Employment', "
                            "'Schedule of Statement of Main Terms and Conditions "
                            "of Employment', 'Contract of Employment', or "
                            "'Employment Contract'. NOT a separate in-pack page "
                            "such as a Deductions from Pay Agreement, pay "
                            "agreement, policy, job description, or lone "
                            "signature page. It is also NOT any document that "
                            "simply arrived filed under a contract name: a gov.uk "
                            "'Check MOT history' printout is 'Other - MOT history "
                            "check', a 'Vehicle Tax and MOT status' page is "
                            "'Proof of Vehicle Tax', and a university letter "
                            "confirming a student's enrolment/term dates is 'Term "
                            "Time Evidence'. Require contract wording - parties, "
                            "job title, hours, notice, terms - on the page."),
    ("Employment Contract Amendment", "A FORMAL VARIATION/AMENDMENT to "
                        "existing contract terms - titled e.g. 'Amendment to "
                        "Contract', 'Variation of Terms', 'Change to Terms and "
                        "Conditions', 'Change in contract', typically short "
                        "and referring back to an existing contract, changing "
                        "the POSITION/role, hours, or contractual terms "
                        "(possibly together with pay). It is NOT the contract "
                        "/ statement-of-terms itself ('Employment Contract'), "
                        "and NOT a simple pay-rise letter: a letter that ONLY "
                        "notifies a new salary/hourly rate with no change of "
                        "role or terms is 'Other - salary increase letter', "
                        "not an amendment."),
    ("eVisa Screenshot", "A screenshot of the UKVI ONLINE 'view and prove your "
                         "immigration status' / eVisa account page - shows the "
                         "person's name, photo, immigration status (e.g. 'Skilled "
                         "Worker'), and the conditions/validity of their leave, "
                         "from the gov.uk view-immigration-status service. It is "
                         "the person's OWN status page, NOT the employer's "
                         "right-to-work check result (that is 'Share Code Check "
                         "Result') and NOT a BRP card."),
    ("Health and Social Worker Visa Letter", "A visa APPROVAL/grant LETTER or "
                        "email mentioning the Health and Care Worker route - "
                        "letter format, addressed to the applicant, confirming "
                        "the visa decision. It is a LETTER: NOT the visa "
                        "sticker in a passport ('Visa Vignette'), NOT the "
                        "online status page ('eVisa Screenshot'), and NOT an "
                        "application/payment receipt ('Proof of Visa "
                        "Application')."),
    ("Job Description", "Responsibilities, duties, requirements, person specification"),
    ("Share Code Check Result", "The Home Office 'View a right to work' / 'View "
                                "immigration status' RESULT PAGE produced after an "
                                "employer enters a share code online. Identify it by "
                                "its CONTENT, not by any stamp or signature. Tell-tale "
                                "signs: the worker's name and photo, wording such as "
                                "'They have permission to work in the UK until <date>', "
                                "a 'Details of check' box with a Company name, Date of "
                                "check and Reference number, conditions/permitted-work "
                                "bullet points, or the right-to-work.service.gov.uk / "
                                "gov.uk view-right-to-work layout. THIS IS THE SAME "
                                "THING AS AN EMPLOYER'S RIGHT-TO-WORK CHECK RESULT - "
                                "there is no separate 'Proof of Right to Work' category; "
                                "any right-to-work check result / verification record is "
                                "'Share Code Check Result'. It is NOT the share code "
                                "itself (that is 'Share Code Document'), NOT a BRP card, "
                                "and NOT any unrelated stamped/signed certificate."),
    ("Share Code Document", "A document that simply SHOWS or COMMUNICATES the share "
                            "CODE itself - typically a screenshot of the "
                            "right-to-work.service.gov.uk 'Details to give your "
                            "employer' page displaying a 9-character share code "
                            "(format like 'WCK 3NN 68S'), or an email/letter quoting "
                            "the code. The key feature is the prominent share CODE. It "
                            "is NOT the employer's result page (that is 'Share Code "
                            "Check Result')."),
    ("Visa Vignette", "A visa STICKER (vignette) placed inside a passport - "
                      "including a UK ENTRY CLEARANCE vignette (headed 'UK ENTRY "
                      "CLEARANCE', showing place of issue, number of entries, visa "
                      "type/category such as 'D - SKILLED WORKER MIGRANT', name, "
                      "passport number, validity dates, an immigration-officer "
                      "stamp, and a machine-readable zone that begins with 'V' - a "
                      "passport bio page's MRZ begins 'P<'). It is the VISA STICKER, "
                      "NOT the passport identity page itself ('Passport'). A passport "
                      "page (or a photocopy of one) whose DOMINANT feature is the "
                      "entry-clearance visa sticker is 'Visa Vignette' EVEN THOUGH a "
                      "passport number and a photo also appear on it - the visa "
                      "sticker wins over the fact that it sits in a passport."),
]

SEED_IMPORTANT = [
    ("Adult First Certificate", "A formal DBS 'Adult First' RESULT/CLEARANCE - "
                                "short official wording confirming an Adult First check "
                                "(used while the full DBS is pending), with applicant "
                                "name and a reference number. It is a formal result, NOT "
                                "internal notes. If the document is free-text notes or a "
                                "memo about a DBS check rather than a formal Adult First "
                                "result, it is 'DBS Check Notes', not this."),
    ("Arc Card", "ARC logo, applicant registration card, photo, asylum seeker details"),
    ("Availability", "Weekly schedule, days/times available, availability form"),
    ("Bank Statement", "A bank's own account STATEMENT - bank logo/letterhead "
                       "(e.g. Halifax, Lloyds, Monzo), the account holder's name, "
                       "account number and sort code, a statement period, and a "
                       "table of transactions with running balances. Use this "
                       "controlled type rather than an 'Other - bank account "
                       "statement' label, and rather than 'Proof of Address' even "
                       "though a statement also shows an address. It is NOT a "
                       "bank LETTER about the account (opening confirmation, "
                       "online-banking notice) - that has no transaction table "
                       "and is an Other document."),
    ("Birth Certificate", "Birth registration details, registrar, date/place of birth"),
    ("CV", "A curriculum vitae / résumé - the worker's own summary of their "
           "employment history, job timeline, qualifications, education, skills "
           "and contact details, often with references. ANY document that is a CV "
           "or résumé is this type, regardless of layout or how it is titled "
           "('CV', 'Curriculum Vitae', 'Resume', or just a name with a work "
           "history). Always classify a CV as 'CV' - never as 'Other - Curriculum "
           "Vitae' or any other Other label."),
    ("Certificate of Driving Competency", "Driving competency/pass certificate, candidate number"),
    ("Driving Licence Summary", "The DVLA ONLINE licence-summary / 'share your "
                                "driving licence' printout - NOT the physical card. "
                                "Headed 'Driver & Vehicle Licensing Agency' / 'Licence "
                                "summary', it is a (usually multi-page) gov.uk printout "
                                "showing 'Driving status', 'Endorsements'/penalty "
                                "points, a check code, the driver number, licence "
                                "valid-from/valid-to dates, and 'Can drive' / "
                                "'Provisionally drive' category tables. A gov.uk "
                                "licence printout with a CHECK CODE and category "
                                "tables is the Summary, NOT 'UK Driving Licence' (the "
                                "physical photocard)."),
    ("Driving Permit", "An INTERNATIONAL DRIVING PERMIT (IDP) - the booklet or "
                       "card headed 'INTERNATIONAL DRIVING PERMIT' (often also in "
                       "French, 'Permis de conduire international'), issued under "
                       "the United Nations / Geneva or Vienna road traffic "
                       "conventions by a national motoring authority or automobile "
                       "association as an official TRANSLATION of the holder's "
                       "national licence, listing the vehicle categories the "
                       "holder may drive and the countries it is valid in. Also "
                       "any other official permit to drive that is not a licence "
                       "card. It is NOT a national driving licence: the overseas "
                       "licence itself is 'Non UK Driving Licence' and the DVLA "
                       "photocard is 'UK Driving Licence'."),
    ("Driving Practical Test Certificate", "DVSA practical test pass certificate"),
    ("Driving Theory Test Certificate", "DVSA theory test pass certificate"),
    ("ECS Notice", "A Home Office EMPLOYER CHECKING SERVICE (ECS) notice/letter "
                   "- e.g. a 'Positive Verification Notice' - with a Home "
                   "Office reference, addressed to the employer, stating "
                   "whether the person has a right to work. It is a LETTER "
                   "from the ECS, NOT the online share-code result page (that "
                   "is 'Share Code Check Result') and NOT the person's own "
                   "eVisa page ('eVisa Screenshot')."),
    ("Emergency Contact Details", "An emergency-contact / next-of-kin form - names "
                                  "of emergency contacts, their relationship to the "
                                  "worker, addresses and phone numbers (often headed "
                                  "'Emergency Details' / 'Emergency Contact' and "
                                  "marked 'Strictly Confidential'). Its SUBJECT is who "
                                  "to contact in an emergency. It is NOT a 'Use of Own "
                                  "Car Declaration' or any vehicle document."),
    ("Employee Handbook", "The STAFF/CAREGIVER HANDBOOK booklet itself - the "
                           "employer's general policies-and-procedures "
                           "handbook (many sections, typically many pages). "
                           "It is NOT a narrower document with its own "
                           "subject, even one extracted from the handbook "
                           "pack - use the specific type instead: a signed "
                           "handbook receipt/acceptance page is 'Employee "
                           "Handbook Receipt'; a GDPR/data-protection privacy "
                           "notice is 'Employee GDPR Privacy Notice'; a "
                           "confidentiality undertaking is 'Employee "
                           "Confidentiality Agreement'; a code-of-conduct or "
                           "caregiver-promise acknowledgement is 'Code of "
                           "Conduct Acknowledgement'."),
    ("Employment Application Form", "A job APPLICATION FORM with multiple "
                           "sections - personal details, application questions, "
                           "declarations, and often an employment-history part. "
                           "Use this for the full multi-section application pack. "
                           "If the visible content is ONLY the employment-history "
                           "grid (From/To, employer, job title, salary, reason "
                           "for leaving), classify it as 'Employment History' "
                           "instead."),
    ("Employment History", "An EMPLOYMENT HISTORY form/grid - a table headed "
                           "'Employment history' with columns like From/To "
                           "(month & year), name and address of employer, job "
                           "title, salary and reason for leaving, usually "
                           "handwritten by the worker and signed/dated. Often a "
                           "section scanned out of an application pack. ALSO "
                           "includes an 'EDUCATION & EMPLOYMENT CHRONOLOGY' "
                           "form (the CQC full education-and-employment-history "
                           "grid: school/employer rows, dates, job title or "
                           "subject, reasons for leaving, gap explanations). "
                           "It is NOT a 'CV' (a CV is the worker's own typed "
                           "summary document) and NOT the full multi-section "
                           "'Employment Application Form'. CHECK EVERY PAGE "
                           "BEFORE CHOOSING THIS TYPE: if ANY page of the file "
                           "carries a 'Position applied for' / post-applied-for "
                           "header, a personal-details section, application "
                           "questions, or the signed declarations that close an "
                           "application pack, the file is the whole 'Employment "
                           "Application Form' - a history grid inside it is one "
                           "SECTION of that pack, not the document. Use "
                           "'Employment History' only when the history grid is "
                           "genuinely all there is."),
    ("Financial Evidence", "Savings, funds, financial support evidence"),
    ("Health Declaration", "A MEDICAL/HEALTH self-declaration form - health questions "
                           "about the worker's own medical history, conditions, "
                           "fitness to work, illnesses, medication or disabilities, "
                           "ANSWERED by the worker. It is specifically about HEALTH. "
                           "It is NOT a 'Safeguarding Questionnaire' (a 'Safeguarding "
                           "Questions' form about safeguarding, whistleblowing, the "
                           "Mental Capacity Act, etc.), and NOT a RECEIPT/"
                           "acknowledgement page where the worker merely signs to "
                           "confirm receiving a pack of policies or documents - even "
                           "when health documents are among those listed, a "
                           "signature-for-receipt page is 'Employee Handbook Receipt' "
                           "or a policy acknowledgement, never a Health Declaration."),
    ("Hire Request Offer Letter", "Offer of employment, role, salary, start date"),
    ("ID", "A general identification document ONLY when no specific ID type "
           "fits. Use the specific type whenever one matches: a passport page "
           "is 'Passport', a residence-permit card is 'BRP', a driving licence "
           "is 'UK Driving Licence'/'Non UK Driving Licence', a staff badge is "
           "'ID Badge', an ARC card is 'Arc Card'. Only a national identity "
           "card or other ID with no dedicated category is 'ID'."),
    ("ID Badge", "A PHOTOGRAPH of a physical staff photo-ID / lanyard card - a "
                 "plastic badge showing the worker's photo, name, job title, "
                 "employer branding/logo (e.g. a Bluebird Care card) and often a "
                 "region or office. It is a picture of a physical badge, NOT a form "
                 "or declaration. It is NOT a 'Use of Own Car Declaration' or any "
                 "other form - it has no questions or signatures, it is an ID card. "
                 "It is also NOT a gov.uk / Home Office 'View a right to work' / "
                 "'View a job applicant's right to work' / 'Prove your right to "
                 "work' page - a FULL PAGE carrying the worker's photo together "
                 "with 'They have permission to work in the UK until <date>' and a "
                 "'Details of check' box (Company name, Date of check, Reference "
                 "number) is 'Share Code Check Result', even when the page is "
                 "scanned ROTATED and even when the incoming filename says 'ID "
                 "Badge'. The badge is a small plastic CARD photographed on a desk "
                 "or lanyard; the right-to-work result is a printed web page."),
    ("Intranet Access Declaration", "An IT / intranet / system-access "
                                    "acknowledgement form (e.g. a 'NEST Access "
                                    "(Intranet) Declaration') - it asks whether the "
                                    "employee has received/read/understood the "
                                    "intranet or a computer system, records any "
                                    "questions or comments, and has employee + "
                                    "line-manager sign-off. Its SUBJECT is IT/system "
                                    "access. Despite the word 'Declaration', it is "
                                    "NOT a 'Use of Own Car Declaration' or any "
                                    "vehicle document."),
    ("Interview Notes", "Interview assessment, candidate responses, interviewer comments"),
    ("Job Advert", "A SINGLE vacancy advertisement / recruitment posting - "
                   "the advert for a role (title, duties, pay, how to "
                   "apply). It is NOT an employer job-board DASHBOARD "
                   "screenshot listing multiple postings with applicant "
                   "counts / sponsored flags (e.g. an Indeed employer view) "
                   "- that is an Other document, and it is NOT any DBS "
                   "document even when kept near DBS paperwork."),
    ("Marriage Name Change Certificate", "Marriage certificate showing surname change"),
    ("Medical Conditions", "A record or list of the worker's DECLARED MEDICAL "
                           "CONDITIONS (conditions, allergies, medication) - an "
                           "information record rather than a signed "
                           "questionnaire. If it is the signed health "
                           "self-declaration FORM with health questions and a "
                           "declaration signature, it is 'Health Declaration' "
                           "instead."),
    ("NMC Registration", "NMC registration confirmation, PIN number"),
    ("National Insurance Number", "Any document showing a National Insurance number "
                                  "(format: two letters, six digits, one letter, e.g. "
                                  "'QQ 12 34 56 C'). INCLUDES a National Insurance CARD "
                                  "(front OR back) - the back of an NI card often shows "
                                  "the words 'NI NUMBER' or 'National Insurance Number' "
                                  "in small print with the number. Also includes HMRC/DWP "
                                  "letters quoting the NI number. If you see 'NI NUMBER' / "
                                  "'National Insurance' text or the NINO format anywhere, "
                                  "this is the answer - it is NOT a 'Passport'."),
    ("No Reference Risk Assessment", "Risk assessment due to missing references"),
    ("Non UK Driving Licence", "A FOREIGN (non-UK) driving licence card - issued "
                               "by any overseas authority (e.g. a New South Wales "
                               "(Australia) driver licence, an EU licence, "
                               "a Sri Lankan/Indian/Nigerian licence), in any "
                               "language. A photo card with the holder's photo, "
                               "licence number, licence class/vehicle categories, "
                               "issue/expiry dates and conditions, headed 'Driver "
                               "Licence' or 'Driving Licence' with a non-UK issuer. "
                               "Either side counts. It is NOT a 'BRP' (that says "
                               "'RESIDENCE PERMIT'), NOT the UK DVLA photocard "
                               "('UK Driving Licence'), NOT the DVLA online "
                               "'Driving Licence Summary', NOT a passport, and NOT "
                               "'ID' - use this specific type for any foreign "
                               "driving licence. Two documents are excluded: a "
                               "card carrying a UK flag panel ('UK'/'GB'/Union "
                               "Jack) or 'DVLA' is 'UK Driving Licence'; a "
                               "document headed 'INTERNATIONAL DRIVING PERMIT' "
                               "is 'Driving Permit'."),
    ("PPE Declaration", "PPE acknowledgement/declaration form"),
    ("Passport", "The photo/identity page of a PASSPORT booklet: a large face photo, "
                 "passport number (format like a single letter + 8 digits or 9 digits), "
                 "nationality, place of birth, date of birth, and a machine-readable "
                 "zone (TWO lines of <<< chevrons at the very bottom starting 'P<'). "
                 "The header reads 'PASSPORT'. It must clearly be the identity page of a "
                 "passport booklet. It is NOT: a UK ENTRY CLEARANCE visa sticker/"
                 "vignette inside a passport (that is 'Visa Vignette' - headed 'UK "
                 "ENTRY CLEARANCE' with a visa type and immigration-officer stamp); a "
                 "POLICE / criminal-record certificate (that is 'Proof of Police "
                 "Check'); a National Insurance card (that is 'National Insurance "
                 "Number'); or the BACK of a BRP card (Date/Place of birth, Sex, "
                 "Nationality, 'NO PUBLIC FUNDS', an NI number and an MRZ on a small "
                 "CARD - that is 'BRP'). A page with an entry-clearance/visa sticker, "
                 "a police-check letterhead, or a credit-card-sized permit is NEVER "
                 "'Passport' even if a passport number or an MRZ appears on it. Only "
                 "the passport BOOKLET identity page (headed 'PASSPORT', with the "
                 "'P<' MRZ) is 'Passport'. A small CREDIT-CARD-SIZED PHOTOCARD "
                 "with NO two-line 'P<' MRZ is NEVER 'Passport', however "
                 "identity-document it looks and however heavily a red 'CERTIFIED "
                 "TRUE COPY' stamp obscures it: a card headed 'DRIVING LICENCE' "
                 "(including 'PROVISIONAL DRIVING LICENCE') with a driver number "
                 "and an entitlement category table is a driving licence - 'UK "
                 "Driving Licence' whenever it shows 'DVLA' or a UK/GB flag "
                 "panel, whatever country the holder's place of birth names; and "
                 "a card headed 'RESIDENCE PERMIT' with a card number and 'NO "
                 "PUBLIC FUNDS' is 'BRP'. FINAL CHECK before "
                 "answering 'Passport': read the FIRST character of the "
                 "machine-readable zone - 'P<' means passport bio page; 'V' means "
                 "a visa and the page is 'Visa Vignette'; 'IR' means a residence "
                 "permit and the page is 'BRP'; and NO MRZ at all on a small card "
                 "means it is a licence or permit, not a passport."),
    ("Payslip", "Employer name, pay period, deductions, net pay"),
    ("Probation Review", "A probation assessment / review, OR end-of-probation "
                         "feedback notes - printed OR handwritten, from the "
                         "employee OR the line manager. To be this type the "
                         "document must carry EXPLICIT probation context: "
                         "'probation' / 'probationary period' / 'end of "
                         "probation' wording (or an explicit probation-review "
                         "meeting heading). Handwritten probation feedback is "
                         "STILL a 'Probation Review'. WITHOUT that probation "
                         "wording, lookalike HR forms are NOT this type: a "
                         "general staff appraisal / performance-scoring or "
                         "self-assessment form, an appraisal preparation form, "
                         "an induction checklist or sign-off, a supervision "
                         "record ('Supervision'), and a sickness/absence report "
                         "are each their own document. It is also NOT 'DBS "
                         "Check Notes' - probation feedback has nothing to do "
                         "with a DBS check unless the text explicitly discusses "
                         "a DBS/Disclosure/Barring check."),
    ("Proof of Address", "A COUNCIL TAX BILL or a UTILITY BILL is 'Proof of "
                         "Address' - always this controlled type, NEVER an "
                         "'Other - council tax bill' / 'Other - energy bill' "
                         "label. More generally: a bill or official letter "
                         "evidencing WHERE THE WORKER LIVES - a council tax "
                         "bill/demand notice from a local authority, a utility "
                         "bill (gas, electricity, water, broadband - e.g. "
                         "British Gas, EDF, Octopus, Thames Water), or a "
                         "similar addressed statement, showing the worker's "
                         "name and full postal address. If the "
                         "document is a BANK STATEMENT (bank logo, account and "
                         "sort code, a transaction table) file it as 'Bank "
                         "Statement' - that specific type wins over this one."),
    ("Proof of Car Insurance", "ANY document FROM AN INSURER evidencing a motor "
                               "insurance policy: a certificate of motor "
                               "insurance, policy schedule, COVER SUMMARY, "
                               "'statement of insurance' or policy summary pack "
                               "(e.g. Hastings Direct, Admiral, Aviva) - showing "
                               "the policyholder, vehicle registration mark, "
                               "policy number, cover dates and class/type of "
                               "cover (ideally incl. business use). Multi-page "
                               "policy documents count: the SUBJECT is the "
                               "insurance cover. It is NOT the V5C registration "
                               "certificate/logbook (that is 'Proof of Car "
                               "Ownership'), NOT the worker's signed own-vehicle "
                               "form ('Use of Own Car Declaration'), and NOT "
                               "vehicle tax ('Proof of Vehicle Tax')."),
    ("Proof of Car Ownership", "A V5C vehicle registration certificate / logbook or "
                               "vehicle purchase/ownership document - identified by "
                               "wording like 'Registration Certificate', 'V5C', "
                               "'registered keeper', a vehicle registration mark, and "
                               "'DVLA'. It is a document about the VEHICLE and who "
                               "keeps it. It is NOT a driving licence: a photocard "
                               "(or the back of one, with a driving-category table "
                               "'Cat./From/To/Codes') is 'UK Driving Licence', about "
                               "the DRIVER's entitlement to drive - never 'Proof of "
                               "Car Ownership'. The DVLA stamp/print does NOT make "
                               "it any kind of right-to-work or identity document."),
    ("Proof of DBS Update Service Consent", "A CONSENT form in which the WORKER "
                        "gives the employer permission to perform DBS Update "
                        "Service status checks - consent wording plus the "
                        "worker's signature. It is the consent, NOT the "
                        "employer's status-check result ('DBS Check'), NOT the "
                        "Update Service registration confirmation ('Update "
                        "Service Registration'), and NOT the certificate "
                        "('DBS Document')."),
    ("Proof of English Proficiency", "An English-language TEST RESULT or "
                        "certificate - e.g. an IELTS 'Test Report Form' "
                        "(British Council / IDP / Cambridge Assessment), OET, "
                        "or similar: candidate details, listening/reading/"
                        "writing/speaking scores, an overall band score and/or "
                        "CEFR level, test centre stamp. Many carry a 'UKVI "
                        "Number' because the test was taken for a visa - the "
                        "UKVI number does NOT make it a visa document: it is "
                        "NOT a 'Visa Vignette', NOT a 'Passport', and NOT any "
                        "immigration document. A language TEST RESULT is "
                        "always this type."),
    ("Proof of Police Check", "A POLICE clearance / criminal-record certificate - e.g. "
                              "an ACRO Police Certificate, a Disclosure, or an overseas "
                              "police/criminal-record certificate. Identified by police/"
                              "criminal-record wording, an issuing police or government "
                              "authority, an applicant name and a certificate/reference "
                              "number, and a statement about convictions/'no trace'. It "
                              "often carries an official stamp and signature - this does "
                              "NOT make it a passport or a right-to-work document. It is "
                              "NOT 'Passport' even though it may show a passport number "
                              "and personal details."),
    ("Proof of Tuberculosis Test", "A TUBERCULOSIS (TB) screening / medical certificate "
                                   "from an approved clinic - often headed 'UK "
                                   "Pre-Departure Tuberculosis Detection Programme "
                                   "Medical Certificate' with an IOM (International "
                                   "Organization for Migration) or 'UK Visas & "
                                   "Immigration' header. Shows the applicant, a "
                                   "TB/chest-x-ray result ('No evidence of active "
                                   "pulmonary TB'), clinic details, and usually a "
                                   "doctor's stamp and signature. Despite the UKVI "
                                   "header and a passport-number field (it travels "
                                   "inside visa packs and passports), it is a MEDICAL "
                                   "certificate: NEVER 'Passport', NEVER 'Visa "
                                   "Vignette', and never any right-to-work document."),
    ("Proof of Vaccination", "Vaccination record/card/certificate"),
    ("Proof of Vehicle Tax", "Vehicle tax confirmation, DVLA tax status - the "
                             "page must state the vehicle's TAX position "
                             "('Vehicle status: Taxed', a tax due date). A page "
                             "that shows no tax status is not this type."),
    ("Proof of Visa Application", "Application submission receipt, payment confirmation"),
    ("Reference", "A COMPLETED employment/character reference ABOUT the "
                  "worker - the reference CONTENT must actually be present: "
                  "a reference letter, a FILLED-IN reference check form "
                  "(e.g. a 'REFERENCE CHECK FORM' with the referee's "
                  "name/company and the questions answered, signed and "
                  "dated), or an email whose body contains the referee's "
                  "actual answers about the worker. A filled-in reference "
                  "form is ALWAYS 'Reference' - never a 'template'. It is "
                  "NOT: correspondence requesting/chasing/forwarding a "
                  "reference ('Reference Request Email'); NOT a blank, "
                  "unfilled reference form (Other - blank reference form "
                  "template); and NOT 'Reference Consent' (the worker's "
                  "permission form). A LETTER ON A FORMER EMPLOYER'S "
                  "LETTERHEAD - an 'experience certificate', 'service "
                  "certificate' or 'to whom it may concern' letter - IS a "
                  "'Reference', not a 'Training Certificate' and not an Other "
                  "label, BUT ONLY IF IT ASSESSES THE PERSON: it must give an "
                  "opinion on their conduct, character, performance or "
                  "suitability, or recommend them ('we found her hard-working "
                  "and reliable', 'I have no hesitation in recommending "
                  "her'). A letter that merely CONFIRMS THE FACTS of "
                  "employment - job title, dates, salary, 'this is to confirm "
                  "that X was employed here from ... to ...' - with no view "
                  "on the person is NOT a reference; file it 'Other - "
                  "employment verification letter'. A title reading "
                  "'Reference Request' does not make a FILLED-IN form a "
                  "request: judge by whether the referee's answers are on "
                  "the page."),
    ("Reference Consent", "A short form in which the WORKER GIVES PERMISSION for the "
                          "employer/agency to obtain a reference from a previous "
                          "employer or college - e.g. wording like 'I <name> give "
                          "permission to <company> to apply for and receive a reference "
                          "from my previous employer/college', with a signature and "
                          "date. It is a CONSENT form. The signature and date do NOT "
                          "make it a right-to-work document, a reference, or a contract. "
                          "It is NOT 'Share Code Check Result' and NOT 'Reference' (the "
                          "reference itself)."),
    ("Reference File", "Bundle containing references/supporting reference documents"),
    ("Reference Manual Completion Evidence", "Evidence reference checks completed manually"),
    ("Receipt of Uniform and Deposit Declaration", "A form acknowledging UNIFORM "
                                    "items issued to the worker - listing garments "
                                    "(e.g. tunic, fleece, jacket, polo), their sizes "
                                    "and quantities, and any DEPOSIT amount taken, "
                                    "with employee + employer signatures. Its SUBJECT "
                                    "is uniform/clothing and a deposit. Despite the "
                                    "word 'Declaration', it is NOT a 'Use of Own Car "
                                    "Declaration' or any vehicle document."),
    ("Risk Assessment", "Risk matrix, hazards, controls, assessment outcome"),
    ("Safeguarding Questionnaire", "A safeguarding competency / training "
                                   "questionnaire - typically headed 'Safeguarding "
                                   "Questions', with the worker's name and date and "
                                   "(often handwritten) answers to questions about "
                                   "safeguarding, whistleblowing, the Mental Capacity "
                                   "Act, types/signs of abuse, and reporting "
                                   "procedures. It tests safeguarding knowledge. It is "
                                   "NOT a 'Health Declaration' (which is about the "
                                   "worker's own medical health) and NOT a policy "
                                   "document - it is a completed questionnaire/form. "
                                   "MATCH ONLY A FORM THAT ACTUALLY ASKS "
                                   "SAFEGUARDING-KNOWLEDGE QUESTIONS: this type is "
                                   "not a bin for any signed Watra/employer "
                                   "'PERSONAL INFORMATION' or 'Declaration' sheet. "
                                   "It is NOT a criminal-record self-declaration "
                                   "(a 'Criminal Record Check Declaration' citing "
                                   "the Rehabilitation of Offenders Act and asking "
                                   "the worker to declare convictions/cautions - "
                                   "that is 'Other - Criminal Record Check "
                                   "Declaration'), NOT a 'Staff Handbook "
                                   "Declaration' / handbook receipt ('Employee "
                                   "Handbook Receipt'), and NOT a Home Office "
                                   "right-to-work result page ('Share Code Check "
                                   "Result')."),
    ("Spot Check", "Spot check form, observations, compliance review"),
    ("Supervision", "Supervision meeting notes, actions, signatures"),
    ("Tenancy Agreement Signature Evidence", "Signed tenancy agreement/signature page"),
    ("Term Time Evidence", "Evidence of a worker's STUDENT STATUS and term "
                           "dates - typically a letter or portal printout from a "
                           "university or college (e.g. Coventry University, "
                           "Birmingham City University) confirming the student is "
                           "enrolled on a named course, with the course dates / "
                           "term and vacation dates that govern how many hours "
                           "they may work. It is a letter ABOUT studying, never "
                           "an 'Employment Contract'."),
    ("Training Certificate", "ANY course/training completion certificate from "
                           "any provider - e-learning, workshop, first aid, "
                           "mandatory training, moving & handling, medication - "
                           "INCLUDING the CARE CERTIFICATE ('This is to certify "
                           "that <name> is awarded the Care Certificate based on "
                           "the standards set by Health Education England / "
                           "Skills for Care'). Certificates are often scanned "
                           "rotated or photographed as screenshots - still this "
                           "type. Never file a completion certificate under an "
                           "Other label. IT MUST BE A COMPLETION CERTIFICATE - "
                           "a document that CERTIFIES/AWARDS that a named person "
                           "completed a named course, with the provider, the "
                           "course and a completion date. It is NOT a 'TRAINING "
                           "REPAYMENT AGREEMENT' (a signed contract in which the "
                           "worker agrees to repay training costs if they leave, "
                           "with repayment percentages, employee AND employer "
                           "signatures - that is 'Other - Training Repayment "
                           "Agreement'), and NOT an employer 'experience "
                           "certificate' / character letter about a person's job "
                           "and conduct (that is 'Reference'). A signed agreement "
                           "about training is a contract, not a certificate."),
    ("UK Driving Licence", "The PHYSICAL UK photocard DRIVING LICENCE (DVLA-issued, "
                           "pink/green plastic CARD). BOTH sides are 'UK Driving "
                           "Licence':\n"
                           "  - FRONT: the holder's PHOTO, name, date and place of "
                           "birth, a driver number, issue and expiry dates, "
                           "signature, and a UK flag panel (a Union Jack, or a "
                           "blue panel with the letters 'UK' or 'GB').\n"
                           "  READ FIELD 4c - THE ISSUING AUTHORITY. On the "
                           "numbered photocard, 4c names the issuer: '4c. DVLA' "
                           "means this type, full stop. Field 3 is the holder's "
                           "DATE AND PLACE OF BIRTH - a line like "
                           "'3. 04.12.1998 PAKISTAN' says where the person was "
                           "BORN, not who issued the card, and never makes the "
                           "licence foreign. A card headed 'PROVISIONAL DRIVING "
                           "LICENCE' with a Union Jack and 4c. DVLA is a UK "
                           "provisional licence and IS this type.\n"
                           "  - BACK (no photo): the entitlement CATEGORY TABLE - "
                           "columns headed 'Cat.', 'From', 'To', 'Codes' with "
                           "category letters (B, B1, BE...) and small vehicle "
                           "pictograms, plus a licence number. A scan/photocopy of "
                           "the category-table side alone is STILL 'UK Driving "
                           "Licence'.\n"
                           "It is the CARD itself. It is NOT the DVLA online "
                           "'Licence summary' / share-code printout (a gov.uk "
                           "document with 'Driving status', a check code and 'Can "
                           "drive' category tables - that is 'Driving Licence "
                           "Summary'), NOT a V5C logbook ('Proof of Car Ownership' "
                           "- that is about the VEHICLE, not the driver), NOT a "
                           "passport, NOT a BRP, and NOT a national ID card. If it "
                           "is a printed gov.uk page with a check code rather than "
                           "a photocard, it is the Summary, not this."),
    ("UKVI Draft Application", "Draft visa application form before submission"),
    ("Update Service Registration", "The DBS UPDATE SERVICE REGISTRATION "
                        "confirmation - confirms the WORKER has registered "
                        "their certificate on the Update Service (registration "
                        "wording, an ID/reference, renewal/subscription "
                        "details). It is the registration itself, NOT the "
                        "employer's status-check result ('DBS Check'), NOT the "
                        "worker's consent form ('Proof of DBS Update Service "
                        "Consent'), and NOT the certificate ('DBS Document')."),
    ("Use of Own Car Declaration", "A declaration form specifically ABOUT USING "
                                   "ONE'S OWN VEHICLE FOR WORK (e.g. a Bluebird Care "
                                   "'Use of own car form declaration'). To be this "
                                   "type the form's SUBJECT must be the car/vehicle: "
                                   "it asks the worker to confirm valid CAR INSURANCE "
                                   "(incl. business use for travelling between "
                                   "calls/clients), a valid MOT, vehicle "
                                   "roadworthiness, a valid DRIVING LICENCE, and any "
                                   "licence endorsements/points. Only classify as this "
                                   "when the vehicle is what the form is about. It "
                                   "has NOTHING to do with DBS: it is NOT a "
                                   "'DBS Check', NOT 'DBS Check Notes', and NOT a "
                                   "'DBS Document'. It is NOT 'Proof of Car "
                                   "Insurance' (the insurer's certificate) and NOT a "
                                   "driving licence. Crucially, it is NOT any OTHER "
                                   "kind of employee declaration/acknowledgement that "
                                   "has nothing to do with a vehicle - in particular "
                                   "it is NOT a 'Health Declaration', NOT a "
                                   "'Safeguarding Questionnaire', NOT an "
                                   "'Emergency Contact Details' form, NOT a uniform/"
                                   "deposit receipt ('Receipt of Uniform and Deposit "
                                   "Declaration'), NOT an ID badge or any ID/photo "
                                   "card, NOT an 'Intranet Access Declaration', and "
                                   "NOT any other signed 'Declaration' whose subject "
                                   "is not a vehicle. The word 'Declaration' in the "
                                   "title or a signature does NOT make a form this "
                                   "type."),
    ("Working Time Directive Waiver", "48-hour opt-out form, employee signature"),
]

# Seeded OTHER-group types: document kinds that plainly recur in care-home HR
# files but are not compliance uploads. Giving them a controlled name (in the
# Other tier, so they still file as "Other - <label>") stops them being
# shoehorned into the nearest compliance type. User-added Other entries in the
# workbook are kept alongside these.
SEED_OTHER = [
    ("Hospital Discharge Letter", "An NHS hospital DISCHARGE letter/summary - "
                    "hospital or NHS-trust letterhead, diagnoses, a clinical "
                    "narrative, and 'Discharge Details' (date/time of "
                    "discharge, disposition). It is a clinical document about "
                    "an episode of care. It is NOT any DBS document (even if "
                    "kept near DBS paperwork), NOT a 'Health Declaration' "
                    "(the form the worker fills in about their own health), "
                    "and NOT 'Medical Conditions'."),
    ("Corrective Action Report", "An internal audit FINDINGS/ACTIONS document "
                    "- titled e.g. 'Corrective Action Report' or an audit "
                    "email headed 'ACTIONS REQUIRED', with an audit "
                    "reference, a table of findings ('Compliant'/'Non-"
                    "Compliant'), 'Action Required', 'Action By' and due "
                    "dates. It is ABOUT compliance gaps. Even when its items "
                    "mention DBS checks, passports, licences or health "
                    "declarations, it is NEVER a DBS type, 'Probation "
                    "Review', 'Supervision' or any of the documents it "
                    "discusses."),
    ("Media Use Agreement", "A media/photography POLICY plus consent or "
                    "permission form - wording about using photographs, "
                    "images or media of staff/customers (e.g. a 'Media' "
                    "policy with a photography permission/consent section, "
                    "signed by the employee). Its SUBJECT is photos/media "
                    "consent. Despite the signature, it is NOT a 'Use of Own "
                    "Car Declaration', NOT a 'PPE Declaration', and NOT an "
                    "employment contract."),
    ("Mobile Phone Use Agreement", "A MOBILE PHONE use policy acknowledgement "
                    "/ agreement form - e.g. 'Mobile Phone Use Agreement "
                    "Form', confirming the employee has read the mobile-"
                    "phones policy, sometimes with business-issued handset "
                    "details (IMEI/SIM). Its SUBJECT is phone use. It is NOT "
                    "a 'Use of Own Car Declaration' or any vehicle document, "
                    "and NOT an 'Intranet Access Declaration' (that is about "
                    "IT/system access)."),
    ("Document Validation Report", "An identity-document VALIDATION/"
                    "VERIFICATION REPORT from a checking service (e.g. "
                    "TrustID) - headed 'Document Validation Report', with "
                    "'Document Checks', 'MRZ/Checksum Validation', PASSED/"
                    "FAILED/NOT PERFORMED flags, and small images of the "
                    "checked document. It REPORTS ON a passport/BRP/ID: it "
                    "is NEVER 'BRP', 'Passport', 'eVisa Screenshot' or "
                    "'Share Code Check Result', even though it pictures and "
                    "names those documents."),
    ("CoS Summary", "A Sponsorship Management System (SMS) screenshot or "
                    "navigation/summary page about a CoS - breadcrumbs like "
                    "'Sponsorship management system > Workers', screens "
                    "titled 'Amend a CoS', 'Edit sponsor note', 'Manage live "
                    "CoS', or a small 'CoS summary' box (CoS number, status, "
                    "expiry, personal details) WITHOUT the full job/salary "
                    "details of the assigned certificate. It is a sponsor-"
                    "side summary ABOUT a CoS, NOT the 'Certificate of "
                    "Sponsorship' itself."),
    ("CoS Query Email", "An EMAIL or letter discussing, querying or chasing a "
                    "Certificate of Sponsorship - e.g. correspondence with "
                    "the Home Office Business Helpdesk about a CoS's status. "
                    "It is correspondence ABOUT a CoS, NOT the 'Certificate "
                    "of Sponsorship' itself and NOT a 'CoS Summary' "
                    "screenshot."),
    ("Personal Profile Form", "An employer HR PERSONAL-DETAILS form (e.g. a "
                    "'Personal Profile' form) capturing the employee's "
                    "surname/forename, date of birth, address, NI number, "
                    "marital status, contact numbers, emergency contacts and "
                    "doctor, signed and dated. Often scanned upside down or "
                    "bundled with a staff ID-badge template page - per the "
                    "mixed-bundle rule, a pack STARTING with this form is "
                    "this type, even if a badge page follows. It is NOT 'ID "
                    "Badge' (a photo of the physical badge alone) and NOT an "
                    "'Employment Application Form'."),
    ("Employee Handbook Receipt", "A SIGNED handbook receipt/acceptance page "
                    "- headed e.g. 'Employee Receipt and Acceptance', "
                    "acknowledging the employee has received and read the "
                    "staff/caregiver handbook, with print name, signature "
                    "and date (often together with a confidentiality "
                    "'Policy and Pledge' box on the same page). It is the "
                    "acknowledgement PAGE, not the handbook itself "
                    "('Employee Handbook')."),
    ("Employee GDPR Privacy Notice", "A GDPR / data-protection PRIVACY NOTICE "
                    "for employees - how the employer collects, uses, "
                    "stores and shares the employee's personal data, legal "
                    "bases, retention, data-subject rights. Its SUBJECT is "
                    "data protection. It is NOT the 'Employee Handbook' "
                    "even when issued inside the handbook pack."),
    ("Employee Confidentiality Agreement", "A CONFIDENTIALITY undertaking / "
                    "non-disclosure agreement signed by the employee - "
                    "keeping client and company information confidential, "
                    "disciplinary consequences of disclosure. Its SUBJECT "
                    "is confidentiality. It is NOT the 'Employee Handbook' "
                    "and NOT a GDPR privacy notice (that is about how the "
                    "EMPLOYER handles the employee's data)."),
    ("Code of Conduct Acknowledgement", "A CODE OF CONDUCT / behavioural "
                    "standards document or its signed acknowledgement - "
                    "including branded pledges like a 'CareGiver Promise' - "
                    "setting out expected professional behaviour, dress, "
                    "boundaries, gifts policy etc., usually signed by the "
                    "employee. Its SUBJECT is conduct standards. It is NOT "
                    "the 'Employee Handbook', NOT a 'Supervision' record "
                    "and NOT 'Interview Notes'."),
    ("Reference Request Email", "EMAIL correspondence about obtaining a "
                    "reference - requesting one from a referee, chasing it, "
                    "or a covering email chain returning/filing a completed "
                    "form ('please find attached the completed reference "
                    "form', 'please can you file this'). ONLY when no "
                    "reference content is present: CHECK EVERY PAGE first - "
                    "if the completed reference form or the referee's "
                    "written reply about the worker appears on ANY later "
                    "page of the scan, the whole file is a 'Reference', not "
                    "this type."),
    # ---- 2026-08-11 Watra post-audit: four Other documents that recur often
    # enough to deserve a stable definition and a stable filed name. They stay
    # in the Other tier deliberately (bulk upload, no Stage 3 slot) - the point
    # is that every copy lands on the SAME 'Other - <name>' string instead of a
    # different free-form label each run.
    ("MOT test certificate", "The DVSA MOT TEST CERTIFICATE (VT20) for a "
                    "vehicle - 'Driver & Vehicle Standards Agency' / 'MOT Test "
                    "Certificate' heading, a test number, the tested vehicle, "
                    "an expiry date and the testing station. It is the "
                    "certificate ISSUED AT A TEST. It is NOT the gov.uk "
                    "'Check MOT history' web printout ('Other - MOT history "
                    "check') and NOT vehicle tax ('Proof of Vehicle Tax'). "
                    "[Seeded 2026-08-11: the app had minted this name into "
                    "BOTH the Important and Other tabs of the workbook with "
                    "two different descriptions.]"),
    ("MOT history check", "The gov.uk check-mot.service.gov.uk 'Check MOT "
                    "history' printout - the registration mark and make/model "
                    "as a large heading, 'MOT valid until <date>', colour, "
                    "fuel type, date first registered, and the vehicle's past "
                    "MOT tests with mileage and advisories. There is no "
                    "controlled type for it, so this Other name is the right "
                    "answer - never 'Employment Contract', and never 'Proof of "
                    "Vehicle Tax' unless the page also states a tax status."),
    ("Criminal Record Check Declaration", "A worker's SELF-DECLARATION about "
                    "criminal convictions - typically headed 'PERSONAL "
                    "INFORMATION' / 'Criminal Record Check Declaration', citing "
                    "the Rehabilitation of Offenders Act and exempt roles, "
                    "asking the worker to declare ALL convictions, cautions, "
                    "charges and pending cases (often answered 'None'), signed "
                    "and dated. It is the worker's own declaration, NOT a DBS "
                    "certificate or check record (the DBS types), and NOT a "
                    "'Safeguarding Questionnaire' - it asks about the worker's "
                    "record, not about safeguarding knowledge."),
    ("Training Repayment Agreement", "A signed AGREEMENT to repay training "
                    "costs - headed e.g. 'TRAINING REPAYMENT AGREEMENT', naming "
                    "the employer and the worker, the course funded and its "
                    "cost, and a repayment scale if the worker leaves within a "
                    "stated period, with BOTH parties' signatures. It is a "
                    "contractual document about money. It is NOT a 'Training "
                    "Certificate' (which certifies a course was COMPLETED) "
                    "despite the word 'training' in its title."),
    ("Employment offer acceptance letter", "The WORKER'S acceptance of a job "
                    "offer - a short letter or signed tear-off slip confirming "
                    "they accept the offered post, usually repeating the job "
                    "title and start date and signed by the worker. It is the "
                    "reply TO an offer: the employer's offer itself is 'Hire "
                    "Request Offer Letter', and the contract is 'Employment "
                    "Contract'."),
]

# Category names that have been RETIRED / RENAMED. If an existing workbook from
# a previous version still lists them, they are removed on load so the AI can no
# longer choose them. "Proof of Right to Work" is now folded into "Share Code
# Check Result". "Use of Own Care Declaration" is a misspelling of "Use of Own
# Car Declaration" that a model once minted (and older code accepted); any
# workbook that picked it up is pruned here. Matching is case-insensitive.
RETIRED_NAMES = {"Proof of Right to Work", "Use of Own Care Declaration",
                 # hyphenated duplicate of 'Non UK Driving Licence' — the
                 # Lifted portal's document type has no hyphen, so files named
                 # with it could not be typed at upload (Stage 3 search found
                 # no option). Canonical name lives in SEED_IMPORTANT.
                 "Non-UK Driving Licence"}


# ====================================================================
# OVERWRITE TYPES
# The controlled document types that Stage 3 uploads ONE AT A TIME using the
# individual "Upload document" + "Overwrite current document" flow. Everything
# NOT in this set is bulk-uploaded. organize_worker() files these into an
# "Overwrite Documents" sub-folder and everything else into "Bulk/Batch NN".
# Stage 3 mirrors this exact set - keep the two in sync if you edit it.
# Names here are the canonical controlled-vocabulary names (see the SEED lists).
# ====================================================================
OVERWRITE_TYPES = {
    "BRP",
    "Share Code Document",
    "National Insurance Number",
    "Certificate of Sponsorship",
    "Share Code Check Result",
    "ECS Notice",
    "Proof of Car Insurance",
    "UK Driving Licence",
    "Non UK Driving Licence",
    "DBS Document",
    "eVisa Screenshot",
    "UKVI Draft Application",
    "Proof of Vehicle Tax",
    "Proof of Car Ownership",
    "Visa Vignette",
    "Employment Contract",
}

# Normalised lookup (case-, space- and hyphen-insensitive) plus the legacy
# alias "Proof of Right to Work" -> treated as "Share Code Check Result".
def _norm_type(s: str) -> str:
    return re.sub(r"[\s\-]+", " ", (s or "").strip().lower())

_OVERWRITE_TYPES_NORM = ({_norm_type(t) for t in OVERWRITE_TYPES}
                         | {_norm_type("Proof of Right to Work")})


def base_controlled_name(stem: str) -> str:
    """Strip a trailing rank suffix ' (01)'/'(02)'... and/or a date suffix
    ' - (DD-MM-YYYY)' from a ranked/dated filename stem to recover the base
    controlled type. E.g.:
      'Certificate of Sponsorship - (29-07-2025) (01)' -> 'Certificate of Sponsorship'
      'DBS Document (02)'                               -> 'DBS Document'
      'BRP'                                             -> 'BRP'
    """
    s = (stem or "").strip()
    # rank suffix comes last ('Name - (date) (NN)'): strip it first
    s = re.sub(r"\s*\(\d{1,3}\)\s*$", "", s)
    # then a trailing ' - (DD-MM-YYYY)' date suffix
    s = re.sub(r"\s*-\s*\(\d{1,2}-\d{1,2}-\d{2,4}\)\s*$", "", s)
    return s.strip()


def is_overwrite_type(stem: str) -> bool:
    """True if a document's (base) controlled type is in OVERWRITE_TYPES -
    tolerant of rank/date suffixes, spacing, hyphenation, and the legacy
    'Proof of Right to Work' alias."""
    return _norm_type(base_controlled_name(stem)) in _OVERWRITE_TYPES_NORM


# ====================================================================
# PATHS  (platform-aware)
# Use the correct per-user application-data directory on each OS instead of a
# hardcoded C:\ path (which behaves as a *relative* path on macOS/Linux and
# can drop files in unexpected places). The result is the same on Windows for
# the user's intended workflow, but safe everywhere.
#   Windows : %APPDATA%\DocReviewAIStation   (falls back to ~\DocReviewAIStation)
#   macOS   : ~/Library/Application Support/DocReviewAIStation
#   Linux   : $XDG_DATA_HOME/DocReviewAIStation  (falls back to ~/.local/share/...)
# ====================================================================
APP_NAME = "DocReviewAIStation"
# Shown in the window title so a support question ("which build is this?") can
# be answered from a screenshot. Bump it with any classification change - see
# CHANGELOG.md.
APP_VERSION = "1.5.1"
APP_BUILD = "2026.09.08-slack1"

def default_app_dir() -> Path:
    sysname = platform.system()
    try:
        if sysname == "Windows":
            base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
            return Path(base) / APP_NAME
        if sysname == "Darwin":
            return Path.home() / "Library" / "Application Support" / APP_NAME
        # Linux / other POSIX
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        return Path(base) / APP_NAME
    except Exception:
        return Path.home() / APP_NAME

APP_DIR = default_app_dir()

# Shared root for suite assets the INSTALLERS manage (guides, helper tools) and
# for generated reports. Kept in one place so every consumer agrees.
LIFTED_APPDATA_DIR = (Path(os.environ.get("LOCALAPPDATA")
                           or (Path.home() / "AppData" / "Local")) / "Lifted")

# ---- shared cross-app API usage ledger (api_usage.py beside this file; a
# copy ships in every Lifted tool - recording NEVER raises, and the app still
# works fine if the module is missing) ----
try:
    import api_usage as _api_usage
    _api_usage.set_app("Stage 2 Processing")
except Exception:
    _api_usage = None
CONFIG_PATH        = APP_DIR / "config.json"
RECORD_XLSX        = APP_DIR / "Filename Identification Record.xlsx"
RENAME_CSV         = APP_DIR / "filename change record.csv"
OVERRIDE_XLSX      = APP_DIR / "Doc Review AI filename change record.xlsx"
FAILED_CSV         = APP_DIR / "failed_files.csv"


def desktop_path() -> Path:
    home = Path.home()
    for cand in (home / "OneDrive" / "Desktop", home / "Desktop"):
        if cand.exists():
            return cand
    return home


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


def ensure_app_dir():
    global APP_DIR, CONFIG_PATH, RECORD_XLSX, RENAME_CSV, OVERRIDE_XLSX, FAILED_CSV
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fallback to a Desktop folder if the chosen app-data dir is not writable
        APP_DIR = desktop_path() / "Doc Review AI Station"
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH   = APP_DIR / "config.json"
        RECORD_XLSX   = APP_DIR / "Filename Identification Record.xlsx"
        RENAME_CSV    = APP_DIR / "filename change record.csv"
        OVERRIDE_XLSX = APP_DIR / "Doc Review AI filename change record.xlsx"
        FAILED_CSV    = APP_DIR / "failed_files.csv"
    _harden_dir_permissions(APP_DIR)


def _harden_dir_permissions(d: Path):
    """Best-effort: make the app-data directory readable/writable by the owner
    only. On POSIX this is chmod 700. On Windows the default ACL on a per-user
    %APPDATA% folder already restricts to the user, so this is a no-op there."""
    try:
        if platform.system() != "Windows":
            os.chmod(d, stat.S_IRWXU)  # 0o700
    except Exception:
        pass


# ====================================================================
# CREDENTIAL STORE  (API key handling)
# --------------------------------------------------------------------
# The API key is NEVER written to config.json. It is resolved, in order:
#   1. the OS credential store via `keyring` (Windows Credential Manager /
#      macOS Keychain / Linux Secret Service)            <-- preferred
#   2. the ANTHROPIC_API_KEY environment variable        <-- fallback
#   3. an obfuscated local key file, 0600, used only if the user explicitly
#      saves a key and keyring is unavailable            <-- last resort
# Resolution order for *reading* is keyring -> env -> file.
# ====================================================================
KEYRING_SERVICE = "DocReviewAIStation"
KEYRING_USER    = "anthropic_api_key"

def _key_file() -> Path:
    # Resolved dynamically so it tracks APP_DIR even after the ensure_app_dir()
    # fallback relocates the app-data directory.
    return APP_DIR / "api_key.local"   # only used as a last resort


def _obfuscate(s: str) -> str:
    """Light reversible obfuscation for the last-resort key file. This is NOT
    encryption - it only stops the key being read at a casual glance / by code
    that greps for 'sk-'. Real protection comes from keyring + file perms."""
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _deobfuscate(s: str) -> str:
    try:
        return base64.b64decode(s.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def get_api_key() -> str:
    """Resolve the API key from the most secure source available."""
    if HAS_KEYRING:
        try:
            v = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
            if v:
                return v.strip()
        except Exception:
            traceback.print_exc()
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env
    try:
        if _key_file().exists():
            return _deobfuscate(_key_file().read_text(encoding="utf-8").strip())
    except Exception:
        traceback.print_exc()
    return ""


def set_api_key(key: str) -> str:
    """Persist the API key to the most secure store available.
    Returns a short human-readable description of where it was stored."""
    key = (key or "").strip()
    ensure_app_dir()
    # Always clear any stale last-resort file first.
    try:
        if _key_file().exists():
            _key_file().unlink()
    except Exception:
        pass
    if not key:
        # treat empty as "remove the stored key"
        if HAS_KEYRING:
            try:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except Exception:
                pass
        return "cleared"
    if HAS_KEYRING:
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
            return "OS credential store (keyring)"
        except Exception:
            traceback.print_exc()
    # last resort: obfuscated 0600 file
    try:
        _key_file().write_text(_obfuscate(key), encoding="utf-8")
        try:
            if platform.system() != "Windows":
                os.chmod(_key_file(), stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        except Exception:
            pass
        return f"local protected file ({_key_file().name})"
    except Exception:
        traceback.print_exc()
        return "FAILED to store"


def api_key_source_label() -> str:
    """Where a currently-resolvable key is coming from (for the UI)."""
    if HAS_KEYRING:
        try:
            if keyring.get_password(KEYRING_SERVICE, KEYRING_USER):
                return "OS credential store"
        except Exception:
            pass
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "ANTHROPIC_API_KEY env var"
    if _key_file().exists():
        return "local protected file"
    return "not set"


# ====================================================================
# CONFIG  (NON-SECRET settings only - the API key is never stored here)
# ====================================================================
# Default limits live here too, so they can be tuned per-install.
def load_config() -> dict:
    cfg = {"model": DEFAULT_MODEL, "fx": 0.79,
           "resolution": 1.5, "skip_when_clear": False, "adaptive_pages": True,
           "auto_other": False, "advanced_models": False, "redact_logs": False,
           "move_mode": False, "convert_pdf": True,
           # low-confidence documents get ONE follow-up call to a stronger
           # model (see SECOND_OPINION_MODEL_ID); set false to disable
           "second_opinion": True,
           # Free, bundled, CPU-only page orientation. v1.3.1 deliberately
           # starts in audit/shadow mode until a representative local benchmark
           # has demonstrated that automatic thresholds are safe.
           "orientation_mode": "audit",  # off | audit | automatic
           "orientation_confidence": 0.95,
           "orientation_margin": 0.20,
           # detect files that wrongly contain SEVERAL documents (sometimes
           # other workers') and split them before classification
           "bundle_split": True,
           # delete processing residue per worker when done (.splitbak
           # backups; .zip archives whose documents were extracted)
           "cleanup_leftovers": True,
           # OPTIONAL second accuracy check after the run: re-checks every
           # renamed document (full pages, adjudicated) and writes
           # Filename_Audit_Report.xlsx into the care-home folder
           "post_run_audit": True,
           "ui_palette": "C",
           "run_mode": "live",   # "live" | "batch" - last-used processing mode
           "max_workers": DEFAULT_MAX_WORKERS, "max_files": DEFAULT_MAX_FILES,
           "max_budget_gbp": DEFAULT_MAX_BUDGET_GBP,
           "max_file_mb": DEFAULT_MAX_FILE_MB,
           "sec_per_file": DEFAULT_SEC_PER_FILE}
    try:
        if CONFIG_PATH.exists():
            on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # MIGRATION: older versions stored "api_key" in plaintext here. If we
            # find one, move it into the secure store and strip it from the file.
            legacy = (on_disk.pop("api_key", "") or "").strip()
            cfg.update(on_disk)
            if legacy:
                set_api_key(legacy)
                save_config(cfg)   # rewrite config.json without the key
    except Exception:
        traceback.print_exc()
    if cfg.get("model") not in MODELS:
        cfg["model"] = DEFAULT_MODEL
    # clamp resolution to a readable range (too low and the model can't read it)
    try:
        cfg["resolution"] = max(1.0, min(3.0, float(cfg.get("resolution", 1.5))))
    except Exception:
        cfg["resolution"] = 1.5
    cfg["skip_when_clear"] = bool(cfg.get("skip_when_clear", False))
    cfg["adaptive_pages"] = bool(cfg.get("adaptive_pages", True))
    cfg["auto_other"] = bool(cfg.get("auto_other", False))
    cfg["advanced_models"] = bool(cfg.get("advanced_models", False))
    cfg["redact_logs"] = bool(cfg.get("redact_logs", False))
    cfg["move_mode"] = bool(cfg.get("move_mode", False))
    cfg["convert_pdf"] = bool(cfg.get("convert_pdf", True))
    cfg["second_opinion"] = bool(cfg.get("second_opinion", True))
    if cfg.get("orientation_mode") not in ("off", "audit", "automatic"):
        cfg["orientation_mode"] = "audit"
    try:
        cfg["orientation_confidence"] = max(
            0.5, min(0.999, float(cfg.get("orientation_confidence", 0.95))))
    except Exception:
        cfg["orientation_confidence"] = 0.95
    try:
        cfg["orientation_margin"] = max(
            0.0, min(0.999, float(cfg.get("orientation_margin", 0.20))))
    except Exception:
        cfg["orientation_margin"] = 0.20
    cfg["bundle_split"] = bool(cfg.get("bundle_split", True))
    cfg["cleanup_leftovers"] = bool(cfg.get("cleanup_leftovers", True))
    cfg["post_run_audit"] = bool(cfg.get("post_run_audit", False))
    if cfg.get("run_mode") not in ("live", "batch"):
        cfg["run_mode"] = "live"
    # v1.3.0 installations could carry a local £2,500,000 ceiling.  That value
    # defeats the spend guard.  Migrate only that known accidental value,
    # backing up the non-secret config before writing the safer £35 ceiling.
    try:
        if float(cfg.get("max_budget_gbp", 0) or 0) == 2_500_000.0:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = CONFIG_PATH.with_name(
                f"config.pre-v1.3.1-budget-backup-{stamp}.json")
            if CONFIG_PATH.exists():
                shutil.copy2(CONFIG_PATH, backup)
            cfg["max_budget_gbp"] = 35.0
            save_config(cfg)
            cfg["_budget_safety_reset"] = str(backup)
    except Exception:
        traceback.print_exc()
    # numeric limits, clamped to sane minimums
    for k, dflt, lo in (("max_workers", DEFAULT_MAX_WORKERS, 1),
                         ("max_files", DEFAULT_MAX_FILES, 1),
                         ("max_budget_gbp", DEFAULT_MAX_BUDGET_GBP, 0.0),
                         ("max_file_mb", DEFAULT_MAX_FILE_MB, 0.1),
                         ("sec_per_file", DEFAULT_SEC_PER_FILE, 0.1)):
        try:
            cfg[k] = max(lo, float(cfg.get(k, dflt)))
        except Exception:
            cfg[k] = dflt
    cfg["max_workers"] = int(cfg["max_workers"])
    cfg["max_files"] = int(cfg["max_files"])
    # If a model not allowed by the current advanced flag is selected, drop back.
    if (not cfg["advanced_models"]) and cfg["model"] not in SAFE_MODELS:
        cfg["model"] = DEFAULT_MODEL
    cfg["notifications"] = normalize_settings(cfg.get("notifications"))
    cfg["show_document_preview"] = bool(cfg.get("show_document_preview", False))
    cfg["ui_palette"] = resolve_palette(cfg.get("ui_palette")).key
    return cfg


def save_config(cfg: dict) -> bool:
    try:
        ensure_app_dir()
        # Defensive: never let an api_key field leak into the plaintext file.
        clean = {k: v for k, v in cfg.items() if k != "api_key"}
        clean["notifications"] = normalize_settings(clean.get("notifications"))
        CONFIG_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")
        return True
    except Exception:
        traceback.print_exc()
        return False


# ====================================================================
# KNOWLEDGE BASE  (the Filename Identification Record workbook)
# Tabs: Crucial, Important, Other.  Each tab: Filename | Description.
# This is the single source of truth for the controlled vocabulary.
# ====================================================================
class KnowledgeBase:
    TABS = ["Crucial", "Important", "Other"]

    def __init__(self):
        self.crucial = {}    # name -> description
        self.important = {}
        self.other = {}
        self._load_or_seed()

    # ---- disk ----
    def _load_or_seed(self):
        ensure_app_dir()
        if RECORD_XLSX.exists() and HAS_XLSX:
            try:
                self._read()
                return
            except Exception:
                traceback.print_exc()
        # seed
        self.crucial   = dict(SEED_CRUCIAL)
        self.important = dict(SEED_IMPORTANT)
        self.other     = dict(SEED_OTHER)
        self._write()

    def _read(self):
        wb = load_workbook(RECORD_XLSX)
        def grab(name):
            d = {}
            if name in wb.sheetnames:
                ws = wb[name]
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if row and row[0]:
                        d[str(row[0]).strip()] = str(row[1]).strip() if len(row) > 1 and row[1] else ""
            return d
        self.crucial   = grab("Crucial")
        self.important = grab("Important")
        self.other     = grab("Other")
        changed = False
        # Drop any retired/renamed categories left over from an older workbook.
        # Case-insensitive: retired names may have been minted by the model in
        # any casing (e.g. 'use of own care declaration').
        retired_low = {r.lower() for r in RETIRED_NAMES}
        for d in (self.crucial, self.important, self.other):
            for k in [k for k in d if k.lower() in retired_low]:
                del d[k]
                changed = True
        # The SEED descriptions are the maintained, authoritative ones. For any
        # canonical (seeded) name, overwrite the on-disk description with the
        # seed so description improvements always take effect - even on an
        # existing workbook from a previous run. Names you added yourself
        # (unknowns you classified) are NOT seeded, so they keep their text.
        for k, v in SEED_CRUCIAL:
            if self.crucial.get(k) != v:
                self.crucial[k] = v
                changed = True
        for k, v in SEED_IMPORTANT:
            if self.important.get(k) != v:
                self.important[k] = v
                changed = True
        for k, v in SEED_OTHER:
            if self.other.get(k) != v:
                self.other[k] = v
                changed = True
        # Drop workbook entries that duplicate a seeded canonical name in a
        # different casing (e.g. a user-added 'proof of car insurance' next to
        # the seeded 'Proof of Car Insurance'): two spellings of one type give
        # the model two conflicting definitions for the same answer.
        seeded = {k.lower(): k for k, _ in SEED_CRUCIAL}
        seeded.update({k.lower(): k for k, _ in SEED_IMPORTANT})
        seeded.update({k.lower(): k for k, _ in SEED_OTHER})
        for d in (self.crucial, self.important, self.other):
            for k in [k for k in d
                      if k.lower() in seeded and k != seeded[k.lower()]]:
                del d[k]
                changed = True
        # ...and drop a seeded name that ALSO sits in the wrong tab. A type the
        # user accepted from the unknown-document prompt lands in Important;
        # if the same name is later seeded into Other (or vice versa) the
        # workbook ends up holding it twice with two different descriptions,
        # and group_of() silently answers with whichever tab it checks first -
        # so the type's tier, and therefore its Stage 3 upload route, depends
        # on tab order rather than on the source. Found on 2026-08-11: 'MOT
        # test certificate' was in Important AND Other at once.
        tabs = {"Crucial": self.crucial, "Important": self.important,
                "Other": self.other}
        for name, seed in (list((k, "Crucial") for k, _ in SEED_CRUCIAL)
                           + list((k, "Important") for k, _ in SEED_IMPORTANT)
                           + list((k, "Other") for k, _ in SEED_OTHER)):
            for tab, d in tabs.items():
                if tab != seed and name in d:
                    del d[name]
                    changed = True
        if changed:
            try:
                self._write()
            except Exception:
                traceback.print_exc()

    def _write(self):
        if not HAS_XLSX:
            return
        wb = Workbook()
        wb.remove(wb.active)
        from openpyxl.styles import Font, PatternFill
        head_font = Font(name="Arial", bold=True, color="FFFFFF")
        head_fill = PatternFill("solid", start_color="1F4E79")
        for tab, data in (("Crucial", self.crucial),
                          ("Important", self.important),
                          ("Other", self.other)):
            ws = wb.create_sheet(tab)
            ws["A1"] = "Filename"
            ws["B1"] = "Description / Identification Features"
            for c in ("A1", "B1"):
                ws[c].font = head_font
                ws[c].fill = head_fill
            for name in sorted(data, key=natural_key):
                ws.append([name, data[name]])
            ws.column_dimensions["A"].width = 42
            ws.column_dimensions["B"].width = 70
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    cell.font = Font(name="Arial")
        wb.save(RECORD_XLSX)

    # ---- queries ----
    def all_names(self):
        """All canonical names across Crucial + Important (Other is the catch-all)."""
        names = set(self.crucial) | set(self.important)
        return names

    def group_of(self, name: str) -> str:
        if name in self.crucial:
            return "Crucial"
        if name in self.important:
            return "Important"
        if name in self.other:
            return "Other"
        return ""

    def canonical_name(self, name: str) -> str:
        """Resolve a model-returned name to the exact controlled name,
        tolerating case/spacing/hyphen differences ('dbs document' ->
        'DBS Document', 'driving licence  summary' -> 'Driving Licence
        Summary'). Returns "" when the name is not in the vocabulary at all -
        callers must then treat the document as unmatched rather than accept
        an invented name."""
        if not name:
            return ""
        if self.group_of(name):
            return name
        want = _norm_type(name)
        for d in (self.crucial, self.important, self.other):
            for k in d:
                if _norm_type(k) == want:
                    return k
        return ""

    # Other-tab names longer than this are accidental paragraph dumps from the
    # define-unknown dialog (a whole description saved as the "name"). They are
    # kept in the workbook but excluded from the prompt: they cannot be used as
    # filenames, and they bloat every request.
    MAX_PROMPT_NAME_LEN = 80

    def vocabulary_block(self) -> str:
        """A text block of every known filename + description, for the API prompt."""
        lines = []
        for label, data in (("CRUCIAL (important compliance documents)", self.crucial),
                            ("IMPORTANT", self.important),
                            ("OTHER (catch-all - files here are named 'Other - <short description>')", self.other)):
            lines.append(f"## {label}")
            for name in sorted(data, key=natural_key):
                if len(name) > self.MAX_PROMPT_NAME_LEN:
                    continue
                # the bare "Other" workbook row is not an identification -
                # offering it invites match=true name="Other" with no label,
                # which files uselessly; unknowns must use match=false +
                # other_label instead
                if name == "Other":
                    continue
                desc = data[name]
                lines.append(f'- "{name}"  -  {desc}' if desc else f'- "{name}"')
            lines.append("")
        return "\n".join(lines)

    # ---- mutations (classifying unknowns) ----
    def add(self, name: str, description: str, group: str):
        name = " ".join(name.split())
        if not name:
            return
        # Backstop for non-dialog callers: a "name" longer than this is a
        # pasted description, unusable as a filename or vocabulary entry
        # (the dialog enforces its own, friendlier limit).
        if len(name) > 120:
            description = (description.strip() or name)
            name = name[:117].rstrip() + "..."
        if group == "Other":
            self.other[name] = description.strip()
        else:  # Relevant -> Important
            self.important[name] = description.strip()
        self._write()


# ====================================================================
# RENAME LOG (CSV)  - every rename
# ====================================================================
class RenameLog:
    def __init__(self):
        self.path = RENAME_CSV
        if not self.path.exists():
            try:
                ensure_app_dir()
                with open(self.path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(
                        ["timestamp", "care_home", "worker", "original_name",
                         "new_name", "group", "source"])
            except Exception:
                traceback.print_exc()

    def record(self, care_home, worker, original, new, group, source):
        try:
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(
                    [datetime.datetime.now().isoformat(timespec="seconds"),
                     care_home, worker, original, new, group, source])
        except Exception:
            traceback.print_exc()


# ====================================================================
# FAILED-FILE LOG (CSV) - every file that was skipped or errored, with its
# full path and the reason, so you can review/copy them after a run.
# ====================================================================
class FailedLog:
    def __init__(self):
        self.path = FAILED_CSV
        self.count = 0
        if not self.path.exists():
            try:
                ensure_app_dir()
                with open(self.path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(
                        ["timestamp", "care_home", "worker", "filename",
                         "full_path", "reason", "detail"])
            except Exception:
                traceback.print_exc()

    def record(self, care_home, worker, path, reason, detail=""):
        self.count += 1
        try:
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(
                    [datetime.datetime.now().isoformat(timespec="seconds"),
                     care_home, worker, getattr(path, "name", str(path)),
                     str(path), reason, detail])
        except Exception:
            traceback.print_exc()


# ====================================================================
# OVERRIDE LOG (XLSX) - only when you change the AI's suggestion
# ====================================================================
class OverrideLog:
    def __init__(self):
        self.path = OVERRIDE_XLSX.with_suffix(".csv")

    def record(self, worker, original, ai_name, final_name):
        try:
            ensure_app_dir()
            fields = ["timestamp", "worker", "original_filename", "AI_suggested_name", "you_changed_to"]
            with pipeline.roster_lock(self.path):
                exists = self.path.exists() and self.path.stat().st_size > 0
                with self.path.open("a", newline="", encoding="utf-8-sig") as stream:
                    writer = csv.writer(stream)
                    if not exists:
                        writer.writerow(fields)
                    writer.writerow([datetime.datetime.now().isoformat(timespec="seconds"),
                                     worker, original, ai_name, final_name])
        except Exception:
            traceback.print_exc()


def write_run_status(care_home: str, worker: str, status: str, detail: str = ""):
    """Write a clearly-marked status row into a 'Run Status' sheet in the main
    record workbook (RECORD_XLSX). Used to flag where a run stopped (e.g. API
    credit exhausted) and which worker was being processed at the time. The row
    is highlighted so it stands out. Falls back to a CSV if openpyxl is missing."""
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    if not HAS_XLSX:
        # plain-text fallback so the information is never lost
        try:
            ensure_app_dir()
            sp = APP_DIR / "run_status.csv"
            new = not sp.exists()
            with open(sp, "a", newline="", encoding="utf-8") as f:
                wcsv = csv.writer(f)
                if new:
                    wcsv.writerow(["timestamp", "care_home",
                                   "worker_at_stop", "status", "detail"])
                wcsv.writerow([ts, care_home, worker, status, detail])
        except Exception:
            traceback.print_exc()
        return
    try:
        ensure_app_dir()
        from openpyxl.styles import Font, PatternFill, Alignment
        if RECORD_XLSX.exists():
            wb = load_workbook(RECORD_XLSX)
        else:
            wb = Workbook()
            # leave the default first sheet for the normal records
            wb.active.title = "Records"
        if "Run Status" in wb.sheetnames:
            ws = wb["Run Status"]
        else:
            ws = wb.create_sheet("Run Status")
            ws.append(["Timestamp", "Care home",
                       "Worker being processed at stop", "Status", "Detail"])
            for c in ws[1]:
                c.font = Font(name="Arial", bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="444444")
            ws.column_dimensions["A"].width = 22
            ws.column_dimensions["B"].width = 22
            ws.column_dimensions["C"].width = 34
            ws.column_dimensions["D"].width = 30
            ws.column_dimensions["E"].width = 60
        ws.append([ts, care_home, worker, status, detail])
        # highlight the row just written in amber so it is unmissable
        r = ws.max_row
        amber = PatternFill("solid", fgColor="FFE08A")
        bold = Font(name="Arial", bold=True)
        for col in range(1, 6):
            cell = ws.cell(row=r, column=col)
            cell.fill = amber
            if col in (3, 4):   # worker + status emphasised
                cell.font = bold
            cell.alignment = Alignment(vertical="top", wrap_text=(col == 5))
        wb.save(RECORD_XLSX)
    except Exception:
        traceback.print_exc()


# ====================================================================
# STAGE 2: CONVERT-TO-PDF
# --------------------------------------------------------------------
# Turns every non-PDF document in a worker folder into a PDF, in place, before
# the AI rename/dedupe/batch pipeline runs. Strategy per type:
#   - PDF                       : left unchanged
#   - image (png/jpg/...)       : embedded full-page into a PDF (PyMuPDF; Pillow
#                                 fallback)
#   - .txt                      : rendered to a simple text PDF (PyMuPDF)
#   - .msg / .eml               : email body + header extracted, then text->PDF
#   - Office (.docx/.xlsx/...)  : LibreOffice headless --convert-to pdf; if
#                                 LibreOffice is missing, .docx falls back to its
#                                 extracted text -> text PDF; anything else is
#                                 left unchanged and reported.
# The original file is deleted only after its PDF has been written successfully.
# ====================================================================
class PdfConverter:
    # cache the discovered LibreOffice path across the whole run
    _soffice = "__unset__"

    # ---- LibreOffice discovery ----
    @staticmethod
    def soffice_path():
        if PdfConverter._soffice != "__unset__":
            return PdfConverter._soffice
        found = None
        # explicit PATH entry first
        for exe in ("soffice", "soffice.exe", "soffice.com"):
            p = shutil.which(exe)
            if p:
                found = p
                break
        if not found:
            # common install locations (Windows / macOS / Linux)
            candidates = [
                r"C:\Program Files\LibreOffice\program\soffice.exe",
                r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                "/usr/bin/soffice",
                "/usr/lib/libreoffice/program/soffice",
                "/opt/libreoffice/program/soffice",
            ]
            for c in candidates:
                if Path(c).exists():
                    found = c
                    break
        PdfConverter._soffice = found
        if found:
            print(f"[convert] LibreOffice found: {found}")
        else:
            print("[convert] LibreOffice not found - Office files will use the "
                  "text-only fallback where possible.")
        return found

    @staticmethod
    def has_libreoffice() -> bool:
        return bool(PdfConverter.soffice_path())

    # ---- low-level builders ----
    @staticmethod
    def _image_to_pdf(src: Path, dest: Path) -> bool:
        """Embed an image as a single full-page PDF."""
        # Preferred: PyMuPDF (keeps it small and handles most formats)
        if HAS_FITZ:
            try:
                doc = fitz.open()
                img = fitz.open(src)
                rect = img[0].rect if len(img) else fitz.Rect(0, 0, 595, 842)
                pdfbytes = img.convert_to_pdf()
                img.close()
                imgpdf = fitz.open("pdf", pdfbytes)
                doc.insert_pdf(imgpdf)
                doc.save(str(dest))
                doc.close()
                return dest.exists()
            except Exception:
                traceback.print_exc()
        # Fallback: Pillow
        if HAS_PIL:
            try:
                im = Image.open(src)
                if im.mode in ("RGBA", "P", "LA"):
                    im = im.convert("RGB")
                im.save(str(dest), "PDF", resolution=150.0)
                return dest.exists()
            except Exception:
                traceback.print_exc()
        return False

    @staticmethod
    def _text_to_pdf(text: str, dest: Path, title: str = "") -> bool:
        """Render plain text to a simple multi-page A4 PDF (PyMuPDF)."""
        if not HAS_FITZ:
            return False
        try:
            doc = fitz.open()
            margin, fontsize, leading = 50, 9, 12
            page_w, page_h = 595, 842   # A4 points
            usable_h = page_h - 2 * margin
            max_lines = int(usable_h / leading)
            # wrap long lines crudely at ~100 chars so nothing runs off-page
            raw_lines = []
            for ln in (text or "").splitlines() or [""]:
                if len(ln) <= 100:
                    raw_lines.append(ln)
                else:
                    for i in range(0, len(ln), 100):
                        raw_lines.append(ln[i:i + 100])
            if title:
                raw_lines = [title, "-" * min(len(title), 100), ""] + raw_lines
            page = doc.new_page(width=page_w, height=page_h)
            y = margin
            count = 0
            for ln in raw_lines:
                if count >= max_lines:
                    page = doc.new_page(width=page_w, height=page_h)
                    y = margin
                    count = 0
                try:
                    page.insert_text((margin, y), ln, fontsize=fontsize,
                                     fontname="cour")
                except Exception:
                    page.insert_text((margin, y), ln, fontsize=fontsize)
                y += leading
                count += 1
            doc.save(str(dest))
            doc.close()
            return dest.exists()
        except Exception:
            traceback.print_exc()
            return False

    @staticmethod
    def _email_text(src: Path) -> str:
        """Extract a readable header+body string from a .msg or .eml file."""
        ext = src.suffix.lower()
        if ext == ".eml":
            try:
                import email
                from email import policy
                msg = email.message_from_bytes(src.read_bytes(),
                                               policy=policy.default)
                head = []
                for h in ("From", "To", "Cc", "Date", "Subject"):
                    if msg[h]:
                        head.append(f"{h}: {msg[h]}")
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_content()
                            break
                    if not body:
                        for part in msg.walk():
                            if part.get_content_type() == "text/html":
                                html = part.get_content()
                                body = re.sub(r"<[^>]+>", "", html)
                                break
                else:
                    body = msg.get_content()
                return "\n".join(head) + "\n\n" + (body or "")
            except Exception:
                traceback.print_exc()
                return ""
        if ext == ".msg":
            # try extract_msg if present; otherwise scrape printable strings
            try:
                import extract_msg
                m = extract_msg.Message(str(src))
                head = []
                for label, val in (("From", m.sender), ("To", m.to),
                                   ("Cc", m.cc), ("Date", m.date),
                                   ("Subject", m.subject)):
                    if val:
                        head.append(f"{label}: {val}")
                body = m.body or ""
                m.close()
                return "\n".join(head) + "\n\n" + body
            except Exception:
                # crude fallback: pull ASCII runs out of the binary
                try:
                    raw = src.read_bytes()
                    runs = re.findall(rb"[\x20-\x7e]{6,}", raw)
                    txt = "\n".join(r.decode("ascii", "ignore") for r in runs)
                    return txt[:20000]
                except Exception:
                    return ""
        return ""

    @staticmethod
    def _office_to_pdf(src: Path, out_dir: Path, log=lambda m: None) -> Path:
        """Convert an Office/ODF file to PDF with LibreOffice headless.
        Returns the produced PDF path, or None on failure."""
        soffice = PdfConverter.soffice_path()
        if not soffice:
            return None
        # Use a private user profile dir so a running LibreOffice instance does
        # not clash with the headless conversion.
        profile = (default_app_dir() / "lo_profile").as_uri()
        cmd = [soffice, "--headless", "--norestore", "--nolockcheck",
               f"-env:UserInstallation={profile}",
               "--convert-to", "pdf", "--outdir", str(out_dir), str(src)]
        try:
            flags = 0
            if os.name == "nt":
                flags = 0x08000000   # CREATE_NO_WINDOW
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=180, creationflags=flags)
            produced = out_dir / (src.stem + ".pdf")
            if produced.exists():
                return produced
            log(f"      LibreOffice produced no PDF for {src.name} "
                f"(rc={proc.returncode})")
            if proc.stderr:
                print(f"[convert] soffice stderr: {proc.stderr.strip()[:300]}")
        except subprocess.TimeoutExpired:
            log(f"      LibreOffice timed out converting {src.name}")
        except Exception as e:
            log(f"      LibreOffice error on {src.name}: {e}")
            traceback.print_exc()
        return None

    # ---- per-file entry point ----
    @staticmethod
    def convert_file(src: Path, log=lambda m: None) -> dict:
        """Convert a single file to a PDF sitting beside it. Returns a dict:
          {"status": "converted"|"kept"|"failed", "pdf": Path|None, "src": src}
        - "kept"      : already a PDF, nothing to do
        - "converted" : a PDF was written and the original deleted
        - "failed"    : could not convert; original left untouched
        """
        ext = src.suffix.lower()
        if ext in PDF_EXT:
            return {"status": "kept", "pdf": src, "src": src}
        if ext not in CONVERT_EXT:
            return {"status": "failed", "pdf": None, "src": src}

        dest = unique_path(src.parent, src.stem, ".pdf")
        ok = False

        try:
            if ext in IMG_EXT:
                ok = PdfConverter._image_to_pdf(src, dest)
            elif ext == ".txt":
                txt = src.read_text(encoding="utf-8", errors="ignore")
                ok = PdfConverter._text_to_pdf(txt, dest, title=src.name)
            elif ext in EMAIL_EXT:
                txt = PdfConverter._email_text(src)
                if txt.strip():
                    ok = PdfConverter._text_to_pdf(txt, dest, title=src.name)
            elif ext in OFFICE_EXT:
                # Convert into a FRESH private directory, never src.parent:
                # LibreOffice always writes <stem>.pdf and silently overwrites
                # an existing file, so converting in place destroyed a
                # pre-existing same-stem PDF (a nine-page signed contract was
                # lost exactly this way) - and when a conversion failed, that
                # pre-existing <stem>.pdf could masquerade as fresh output.
                # An empty out_dir makes 'produced exists' proof of NEW bytes.
                produced = None
                with tempfile.TemporaryDirectory(
                        prefix="lifted-convert-") as td:
                    made = PdfConverter._office_to_pdf(src, Path(td), log)
                    if made and made.exists() and made.stat().st_size:
                        if dest.exists():   # appeared since dest was chosen
                            dest = unique_path(src.parent, src.stem, ".pdf")
                        shutil.move(str(made), str(dest))
                        produced = dest
                if produced:
                    ok = True
                elif ext == ".docx":
                    # text-only fallback for Word when LibreOffice unavailable
                    txt = DocRender._docx_text(src)
                    if txt.strip():
                        ok = PdfConverter._text_to_pdf(txt, dest, title=src.name)
        except Exception as e:
            log(f"      conversion error on {src.name}: {e}")
            traceback.print_exc()
            ok = False

        if ok and dest.exists() and dest.stat().st_size > 0:
            # remove the original now that the PDF exists
            try:
                src.unlink()
            except Exception as e:
                log(f"      ! converted but could not delete original "
                    f"{src.name}: {e}")
            return {"status": "converted", "pdf": dest, "src": src}

        # clean up any empty/partial output
        try:
            if dest.exists() and (not dest.stat().st_size):
                dest.unlink()
        except Exception:
            pass
        return {"status": "failed", "pdf": None, "src": src}

    # ---- whole-worker entry point ----
    @staticmethod
    def convert_worker(worker_dir: Path, log=lambda m: None,
                       check_stop=lambda: None) -> dict:
        """Convert every convertible loose file in worker_dir to PDF.
        Returns counts: {"converted", "kept", "failed", "failed_names"}."""
        counts = {"converted": 0, "kept": 0, "failed": 0, "failed_names": []}
        # snapshot first: we mutate the folder as we go. Program artefacts
        # (e.g. a stray '_overwrite_order.xlsx') are skipped so they are never
        # turned into a PDF and mistaken for a document.
        targets = [p for p in worker_dir.rglob("*")
                   if p.is_file() and p.suffix.lower() in CONVERT_SCAN_EXT
                   and not _is_junk(p) and not is_program_file(p)]
        targets.sort(key=lambda p: natural_key(p.name))
        for p in targets:
            check_stop()
            if not p.exists():
                continue
            res = PdfConverter.convert_file(p, log)
            st = res["status"]
            if st == "converted":
                counts["converted"] += 1
                log(f"    > converted to PDF: {p.name}")
            elif st == "kept":
                counts["kept"] += 1
            else:
                counts["failed"] += 1
                counts["failed_names"].append(p.name)
                log(f"    ! could not convert to PDF (left as-is): {p.name}")
        return counts


class DocRender:
    """Renders pages of a document to base64 PNGs for the API, and extracts any
    embedded text. Resolution (zoom) and how many pages are sent are configurable
    so the cost can be tuned. A higher zoom = sharper image = more tokens."""

    MAX_PAGES = 3          # hard ceiling on pages ever sent to the API
    DEFAULT_ZOOM = 1.5     # default render scale (overridden by settings)

    # ---- content crop / up-res for small documents on a big blank page ----
    # A photocopied ID card sits alone in the middle of an A4 sheet, so a
    # full-page render spends nearly all its pixels on white paper and the
    # card's own text (the 'DRIVING LICENCE' header, the MRZ, the category
    # table) comes out unreadable - which is how 14 driving licences and 4
    # BRPs were filed as 'Passport' in the 2026-08-11 Watra audit. When the
    # page's actual content covers only a small part of the sheet, render that
    # region instead, at a zoom that spends the same pixel budget on it.
    CROP_MAX_FRAC = 0.34     # crop only when content covers less of the page
    CROP_MIN_FRAC = 0.0015   # ...but is not just scanner speckle
    CROP_PAD_FRAC = 0.04     # margin kept around the content, as page fraction
    CROP_MAX_BOOST = 3.0     # ceiling on the extra zoom the crop may buy
    _CROP_PROBE_PX = 200     # long side of the throwaway raster used to find it

    @staticmethod
    def _content_clip(page, ink_thresh: int = 205):
        """The sub-rectangle of `page` that actually carries content, or None
        when the content already fills the sheet (or the page is blank). Uses
        a tiny throwaway raster, so the cost is negligible."""
        if not HAS_PIL:
            return None
        try:
            rect = page.rect
            if rect.width <= 0 or rect.height <= 0:
                return None
            z = DocRender._CROP_PROBE_PX / max(rect.width, rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
            from io import BytesIO
            im = Image.open(BytesIO(pix.tobytes("png"))).convert("L")
            w, h = im.size
            px = im.point(lambda v: 1 if v < ink_thresh else 0,
                          mode="L").tobytes()
            # ignore isolated specks: a row/column counts only with >= 2 marks
            rows = [sum(px[y * w:(y + 1) * w]) for y in range(h)]
            cols = [sum(px[x::w]) for x in range(w)]
            ys = [i for i, v in enumerate(rows) if v >= 2]
            xs = [i for i, v in enumerate(cols) if v >= 2]
            if not xs or not ys:
                return None
            frac = ((xs[-1] - xs[0] + 1) * (ys[-1] - ys[0] + 1)) / float(w * h)
            if not (DocRender.CROP_MIN_FRAC <= frac <= DocRender.CROP_MAX_FRAC):
                return None
            padx = rect.width * DocRender.CROP_PAD_FRAC
            pady = rect.height * DocRender.CROP_PAD_FRAC
            clip = fitz.Rect(
                max(rect.x0, rect.x0 + xs[0] / w * rect.width - padx),
                max(rect.y0, rect.y0 + ys[0] / h * rect.height - pady),
                min(rect.x1, rect.x0 + (xs[-1] + 1) / w * rect.width + padx),
                min(rect.y1, rect.y0 + (ys[-1] + 1) / h * rect.height + pady))
            if clip.width <= 0 or clip.height <= 0:
                return None
            return clip
        except Exception:
            return None

    @staticmethod
    def _max_px(zoom: float) -> int:
        # cap the longest image side proportional to zoom so payloads stay sane
        # (zoom 1.0 -> ~1100px, 1.5 -> ~1400px, 2.0 -> ~1700px, 3.0 -> ~2200px)
        return int(800 + zoom * 480)

    @staticmethod
    def page_count(path: Path) -> int:
        ext = path.suffix.lower()
        if ext in IMG_EXT:
            return 1
        if ext in PDF_EXT and HAS_FITZ:
            try:
                doc = fitz.open(path)
                n = len(doc)
                doc.close()
                return n
            except Exception:
                return 1
        return 1

    @staticmethod
    def render(path: Path, zoom: float = None, pages="all", rotate: int = 0,
               max_pages: int = None):
        """Return (list_of_b64_png, extracted_text).
        pages: "all" (a representative sample of up to MAX_PAGES - first two
        pages plus the LAST page for longer documents, so a signature/result
        page at the end is seen), "first" (page 1 only), or a list of 0-based
        page indices. rotate: degrees CLOCKWISE to rotate every rendered page
        (used by the rotation retry for sideways scans), or a
        {page_index: degrees} dict to straighten individual pages (used by the
        pre-classification deskew, where a scan mixes orientations). Empty
        image list if unrenderable (text may still come back for text-based
        PDFs/docx/txt)."""
        if zoom is None:
            zoom = DocRender.DEFAULT_ZOOM
        ext = path.suffix.lower()
        try:
            if ext in IMG_EXT:
                # images are single-page; "first"/list still returns the image
                if isinstance(rotate, dict):
                    rotate = int(rotate.get(0, 0) or 0)
                return DocRender._image(path, zoom, rotate)
            if ext in PDF_EXT and HAS_FITZ:
                return DocRender._pdf(path, zoom, pages, rotate, max_pages)
            if ext == ".txt":
                return [], path.read_text(encoding="utf-8", errors="ignore")[:8000]
            if ext in (".docx", ".doc"):
                return [], DocRender._docx_text(path)
        except Exception:
            traceback.print_exc()
        return [], ""

    @staticmethod
    def render_page1_rotations(path: Path, zoom: float = None):
        """Render page 1 in all four orientations (0/90/180/270 clockwise) for
        the rotation retry: the model judges from whichever copy is upright,
        so the retry never depends on guessing the rotation direction."""
        out = []
        for deg in (0, 90, 180, 270):
            imgs, _ = DocRender.render(path, zoom=zoom, pages="first",
                                       rotate=deg)
            if imgs:
                out.append(imgs[0])
        return out

    @staticmethod
    def _image(path: Path, zoom: float, rotate: int = 0):
        data = path.read_bytes()
        w = h = 0
        if HAS_PIL:
            try:
                from io import BytesIO
                im = Image.open(BytesIO(data)).convert("RGB")
                if rotate in (90, 180, 270):
                    # PIL rotates counter-clockwise; rotate is clockwise
                    im = im.rotate(-rotate, expand=True)
                cap = DocRender._max_px(zoom)
                if max(im.size) > cap:
                    im.thumbnail((cap, cap))
                buf = BytesIO()
                im.save(buf, format="PNG")
                data = buf.getvalue()
                w, h = im.size
            except Exception:
                traceback.print_exc()
                # A supported image extension is not evidence by itself.  Do
                # not base64 raw bytes that Pillow could not decode: the caller
                # will surface the existing zero-evidence unreadable outcome.
                return [], ""
        elif HAS_FITZ:
            try:
                with fitz.open(path) as doc:
                    if len(doc) != 1:
                        return [], ""
                    # In fitz's rendered screen coordinates, positive
                    # prerotate degrees match this caller's clockwise contract.
                    mat = fitz.Matrix(zoom, zoom)
                    if rotate in (90, 180, 270):
                        mat = mat.prerotate(rotate)
                    page = doc[0]
                    cap = DocRender._max_px(zoom)
                    longest = max(page.rect.width * zoom,
                                  page.rect.height * zoom)
                    if longest > cap:
                        # Pillow normally performs this cap.  Size this matrix
                        # before rasterizing so the fitz-only fallback never
                        # creates the oversized pixmap just to shrink it.
                        scale = (cap - 1) / longest
                        mat = fitz.Matrix(zoom * scale, zoom * scale)
                        if rotate in (90, 180, 270):
                            mat = mat.prerotate(rotate)
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                data = pix.tobytes("png")
                w, h = pix.width, pix.height
            except Exception:
                traceback.print_exc()
                return [], ""
        else:
            # Without a local decoder, extension-only bytes are unverifiable.
            return [], ""
        data, w, h = DocRender._fit_api_limits(data, w, h, path.name, 1)
        if data is None:
            return [], ""
        return [base64.b64encode(data).decode("ascii")], ""

    # API image limits (with headroom): reject/shrink anything beyond these.
    API_MAX_IMG_BYTES = 4_500_000   # API hard limit ~5 MB; keep headroom
    API_MAX_IMG_PX    = 7500        # API hard limit ~8000 px on the long side

    @staticmethod
    def _fit_api_limits(png_bytes: bytes, w: int, h: int, fname: str, pageno: int):
        """Ensure a rendered PNG is within the API's per-image size/dimension
        limits. Logs the dimensions/size to the console. If it's too big, tries
        to downscale with PIL; if that's not possible (or PIL missing), returns
        (None,...) so the caller skips that image rather than triggering a
        400 invalid_request_error. Returns (bytes_or_None, w, h)."""
        size = len(png_bytes) if png_bytes else 0
        # console-only diagnostic (captured by the dev console)
        print(f"[render] {fname} p{pageno}: {w}x{h}px, {size/1024:.0f} KB")
        too_big = (size > DocRender.API_MAX_IMG_BYTES
                   or (w and w > DocRender.API_MAX_IMG_PX)
                   or (h and h > DocRender.API_MAX_IMG_PX))
        if not too_big:
            return png_bytes, w, h
        if not HAS_PIL:
            print(f"[render] {fname} p{pageno}: TOO LARGE for the API and Pillow "
                  f"is not installed to shrink it - skipping this page. "
                  f"Install pillow (pip install pillow) to handle large scans.")
            return None, w, h
        # progressively downscale / recompress until within limits
        try:
            from io import BytesIO
            im = Image.open(BytesIO(png_bytes)).convert("RGB")
            for _ in range(8):
                size = 0
                buf = BytesIO()
                # prefer JPEG for big photos (far smaller than PNG)
                im.save(buf, format="JPEG", quality=80)
                blob = buf.getvalue()
                size = len(blob)
                if (size <= DocRender.API_MAX_IMG_BYTES
                        and max(im.size) <= DocRender.API_MAX_IMG_PX):
                    print(f"[render] {fname} p{pageno}: shrunk to "
                          f"{im.size[0]}x{im.size[1]}px, {size/1024:.0f} KB (JPEG)")
                    return blob, im.size[0], im.size[1]
                # shrink 20% and retry
                im.thumbnail((int(im.size[0] * 0.8), int(im.size[1] * 0.8)))
            print(f"[render] {fname} p{pageno}: still too large after shrinking - "
                  f"skipping this page.")
            return None, im.size[0], im.size[1]
        except Exception:
            traceback.print_exc()
            return None, w, h

    @staticmethod
    def _pdf(path: Path, zoom: float, pages, rotate: int = 0,
             max_pages: int = None):
        imgs, text = [], []
        doc = fitz.open(path)
        try:
            total = len(doc)
            if pages == "first":
                idxs = [0] if total else []
            elif pages == "all":
                if total <= DocRender.MAX_PAGES:
                    idxs = list(range(total))
                else:
                    # representative sample: first two pages + the LAST page,
                    # so a signature/result page at the end of a long scan is
                    # seen (same page count as before, so same cost)
                    idxs = [0, 1, total - 1]
            else:  # explicit list of indices
                # MAX_PAGES is a cost guard on the ordinary classification
                # sample. Segmentation deliberately raises it (max_pages) so it
                # can see a whole short file; the bundle scan does the same by
                # chunking. Everything else keeps the old three-page ceiling.
                lim = DocRender.MAX_PAGES if max_pages is None else max_pages
                idxs = [i for i in pages if 0 <= i < total][:lim]
            base_mat = fitz.Matrix(zoom, zoom)
            per_page = rotate if isinstance(rotate, dict) else {}
            if not per_page and rotate in (90, 180, 270):
                # prerotate() is counter-clockwise; rotate is clockwise
                base_mat = base_mat.prerotate((360 - rotate) % 360)
            cap = DocRender._max_px(zoom)
            for i in idxs:
                page = doc[i]
                text.append(page.get_text())
                deg = int(per_page.get(i, 0) or 0)
                if deg in (90, 180, 270):
                    mat = fitz.Matrix(zoom, zoom).prerotate((360 - deg) % 360)
                else:
                    mat = base_mat
                # a small document marooned on a big blank sheet is rendered
                # from its content region, at a zoom that makes it readable
                clip = DocRender._content_clip(page)
                if clip is not None:
                    boost = min(DocRender.CROP_MAX_BOOST,
                                max(page.rect.width, page.rect.height)
                                / max(clip.width, clip.height, 1e-6))
                    mat = mat.prescale(boost, boost)
                pix = page.get_pixmap(matrix=mat, alpha=False, clip=clip)
                png = pix.tobytes("png")
                w, h = pix.width, pix.height
                if HAS_PIL and max(pix.width, pix.height) > cap:
                    try:
                        from io import BytesIO
                        im = Image.open(BytesIO(png)).convert("RGB")
                        im.thumbnail((cap, cap))
                        b = BytesIO(); im.save(b, format="PNG"); png = b.getvalue()
                        w, h = im.size
                    except Exception:
                        traceback.print_exc()
                # Hard safety: keep each rendered image within the API's limits
                # (~5MB / ~8000px). Without this, very large or high-DPI scans
                # cause '400 invalid_request_error'. Returns possibly-smaller PNG.
                png, w, h = DocRender._fit_api_limits(png, w, h, path.name, i + 1)
                if png is None:
                    continue   # could not bring it under limits - skip this page
                imgs.append(base64.b64encode(png).decode("ascii"))
            # A page-one triage request may only see page-one evidence.  The
            # later supplemental text remains useful to the full/sample pass,
            # but must not make a later page look like page 1.
            if pages != "first":
                for i in range(min(total, 12)):
                    if i not in idxs:
                        text.append(doc[i].get_text())
        finally:
            doc.close()
        return imgs, "\n".join(text)[:12000]

    @staticmethod
    def _docx_text(path: Path):
        try:
            import zipfile
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            xml = re.sub(r"<w:p[ >]", "\n", xml)
            text = re.sub(r"<[^>]+>", "", xml)
            return re.sub(r"\n{3,}", "\n\n", text)[:12000]
        except Exception:
            # Surface the cause for diagnostics rather than swallowing it; the
            # caller still gets "" and will mark the doc unrenderable.
            traceback.print_exc()
            return ""


# ====================================================================
# CLAUDE API CLIENT
# ====================================================================
DISAMBIGUATION_RULES = (
    "THREE RULES THAT OVERRIDE EVERYTHING ELSE:\n"
    "I. CLASSIFY A DOCUMENT BY WHAT IT *IS*, NEVER BY WHAT IT MENTIONS, "
    "REFERENCES, OR ACCOMPANIES. An email ABOUT a Certificate of Sponsorship "
    "is not a Certificate of Sponsorship. A summary OF a document is not that "
    "document. A report or email that QUOTES a DBS number is not a DBS "
    "document. A validation/verification report ABOUT a BRP or passport is "
    "not a BRP or passport. A training certificate that mentions 'Tier 2', a "
    "UKVI number, or sponsorship is still a training certificate. A medical "
    "certificate carried in a visa pack is still a medical certificate. "
    "Always ask 'what artefact am I actually looking at?', never 'what topic "
    "does it mention?'.\n"
    "II. SUBJECT MATTER BEATS LAYOUT, KEYWORDS, TITLE-WORDS, STAMPS AND "
    "SIGNATURES. The word 'Declaration' or 'Notes', a signed/tabular layout, "
    "handwriting, or an official stamp NEVER by itself decides the type - "
    "classify by what the content is actually about (rules 0/0b/13 below give "
    "the specifics).\n"
    "III. WHEN NO NAME GENUINELY FITS, SAYING SO IS THE CORRECT ANSWER: "
    "return match=false with a short, specific other_label describing what "
    "the document really is. HR files are full of one-off documents (hospital "
    "letters, internal emails, audit reports, loan agreements, policies) that "
    "belong to no controlled type - filing them as 'Other - <description>' is "
    "a GOOD outcome. A confident but wrong controlled name is the WORST "
    "possible outcome. Never stretch the nearest-looking name over a "
    "document that does not genuinely fit it.\n\n"
    "KEY DISAMBIGUATIONS (read carefully - these are common mistakes):\n"
    "0. STAMPS AND SIGNATURES DO NOT DETERMINE THE DOCUMENT TYPE. Many "
    "unrelated documents carry an official stamp, a signature, a tick or a "
    "date - e.g. tuberculosis/medical certificates, police/criminal-record "
    "certificates, V5C vehicle logbooks, 'use of own car' declarations, "
    "safeguarding questionnaires, reference-consent forms, references, "
    "and college letters. The PRESENCE of a stamp or signature is NOT evidence "
    "that a document is a right-to-work check, a DBS document, or a "
    "'Share Code Check Result'. Classify by the document's actual heading, "
    "issuer and CONTENT - what the document IS - never by the mere fact that it "
    "is signed or stamped. If a document is not clearly a Home Office "
    "right-to-work result, do NOT call it 'Share Code Check Result'.\n"
    "0b. TITLE-WORDS AND LAYOUT DO NOT DETERMINE THE DOCUMENT TYPE. The word "
    "'Declaration', the word 'Notes', a signed layout, or a HANDWRITTEN page "
    "does NOT by itself decide the type. Classify by the document's SUBJECT "
    "MATTER - its headings, its fields, and what the content is actually about "
    "- never by a title word or the fact that it is handwritten/signed. In "
    "particular: a handwritten page is NOT 'DBS Check Notes' unless its text "
    "actually mentions a DBS / Disclosure / Barring check - handwritten "
    "probation feedback is 'Probation Review', supervision notes are "
    "'Supervision', interview notes are 'Interview Notes'; and a form titled "
    "'... Declaration' is NOT a DBS document or a vehicle declaration unless "
    "its subject matches that type.\n"
    "1. NATIONAL INSURANCE vs PASSPORT: If a document shows a National "
    "Insurance number (two letters, six digits, one letter) or the words "
    "'NI NUMBER' / 'National Insurance' - even in small print on the back "
    "of a plain card - it is 'National Insurance Number', NOT 'Passport'. "
    "A passport has a photo page, machine-readable '<<<' lines, and the "
    "word PASSPORT.\n"
    "2. PASSPORT vs VISA VIGNETTE vs POLICE CHECK: 'Passport' is "
    "ONLY the passport identity page (face photo, 'PASSPORT' header, two "
    "machine-readable '<<<' lines starting 'P<'). A 'UK ENTRY CLEARANCE' visa "
    "sticker inside a passport (headed 'UK ENTRY CLEARANCE', with a visa type "
    "such as 'D - SKILLED WORKER MIGRANT' and an immigration-officer stamp) is "
    "'Visa Vignette', NOT 'Passport'. ALWAYS READ THE FIRST CHARACTER OF THE "
    "MACHINE-READABLE ZONE before answering 'Passport': an MRZ beginning 'P<' "
    "belongs to a passport bio page; an MRZ beginning 'V' belongs to a VISA "
    "and the page is 'Visa Vignette' - a photo + crest + MRZ is NOT enough to "
    "be a passport. This includes PHOTOCOPIES: a photocopy or scan of an OPEN "
    "passport whose visible sticker page holds an entry-clearance vignette "
    "was copied to evidence the VISA - it is 'Visa Vignette', not 'Passport', "
    "unless the passport BIO page (headed 'PASSPORT', MRZ 'P<') is what is "
    "actually shown. A police / criminal-record / "
    "ACRO certificate is 'Proof of Police Check', NOT 'Passport' - even if it "
    "quotes a passport number or personal details.\n"
    "3. BRP vs SHARE CODE CHECK RESULT: A physical residence-permit CARD "
    "(photo, card number, visa category, expiry, 'Residence Permit') is "
    "'BRP'. The card itself is never a right-to-work result.\n"
    "4. RIGHT TO WORK = SHARE CODE CHECK RESULT (there is NO 'Proof of Right "
    "to Work' category anymore): ANY employer right-to-work CHECK RESULT or "
    "verification record is 'Share Code Check Result'. This includes the "
    "Home Office 'View a right to work' / 'View immigration status' result "
    "page - worker name + photo, 'They have permission to work in the UK "
    "until <date>', a 'Details of check' box (Company name / Date of check / "
    "Reference number), permitted-work conditions, or the "
    "right-to-work.service.gov.uk layout. Do NOT invent a 'Proof of Right to "
    "Work' label.\n"
    "5. SHARE CODE CHECK RESULT vs SHARE CODE DOCUMENT: The RESULT page "
    "(above, showing the employer's check outcome) is 'Share Code Check "
    "Result'. A page that simply DISPLAYS the share code itself - e.g. the "
    "'Details to give your employer' screen showing a 9-character code like "
    "'WCK 3NN 68S', or an email quoting the code - is 'Share Code Document'. "
    "The distinguishing feature of 'Share Code Document' is the prominent "
    "share CODE; of 'Share Code Check Result' it is the employer's CHECK "
    "OUTCOME / permission-to-work statement.\n"
    "6. DBS DOCUMENTS - FOUR DISTINCT THINGS. Every one of these requires the "
    "document to actually be ABOUT a DBS check - if there is no DBS / "
    "Disclosure / Barring signal in the content, it is NONE of them (it is "
    "probably Probation Review, Supervision, Interview Notes, a vehicle "
    "declaration, or another HR form). Verification-style LAYOUT is not a "
    "DBS signal: 'date checked' columns, yes/no tick rows, and 'office use "
    "only' boxes appear on many HR forms (e.g. a use-of-own-car form) - only "
    "the words DBS / Disclosure / Barring (or a disclosure number etc.) make "
    "a document DBS-related: "
    "(a) Free-text notes/memo/email whose text mentions a DBS/Disclosure/"
    "Barring check = 'DBS Check Notes'. "
    "(b) A DBS CHECK RECORD / RESULT SUMMARY = 'DBS Check' (the verification/"
    "check record - NOT the certificate). This includes a document titled "
    "'DBS Service Check' / 'DBS Update Service' / a status or service-check "
    "result, AND a 'DBS Check list' / results-summary sheet an employer fills "
    "in to record & verify a check - recognisable by workflow fields like "
    "'Result Date', 'Certificate Issue Date', 'Disclosure number', 'Position', "
    "'Disclosure Level', 'Barred List', 'Workforce Type', 'DBS Reference', "
    "'Verification By'. "
    "(c) A formal short 'Adult First' clearance result with a reference "
    "number = 'Adult First Certificate'. "
    "(d) The actual DBS CERTIFICATE = 'DBS Document' - ONLY the GREEN "
    "DBS certificate, a 'DBS Certificate Record' sheet reproducing the "
    "certificate (Name, Date of Birth, Date of original DBS Check, Outcome, "
    "Unique Reference Number, counter-signatory), or a certificate bearing a "
    "'uCheck' logo. "
    "A 'DBS Service Check' or a 'DBS Check list'/results-summary sheet is "
    "'DBS Check', NEVER 'DBS Document'. Only the issued certificate itself is "
    "'DBS Document'. Notes are never an Adult First Certificate.\n"
    "7. EMPLOYMENT CONTRACT vs CONTRACT-PACK PAGES: A new-starter pack often "
    "bundles several documents behind the contract. Only the contract / "
    "statement-of-terms itself is 'Employment Contract'. A 'Deductions from "
    "Pay Agreement', overpayment/pay agreement, policy extract, job "
    "description, privacy notice, or a standalone signature/declaration page "
    "is its own document (usually OTHER) - do NOT call it 'Employment "
    "Contract' just because it sits in the same pack or mentions pay/employment.\n"
    "8. REFERENCE CONSENT vs REFERENCE: A short signed form where the WORKER "
    "gives permission for the employer to obtain a reference ('I <name> give "
    "permission to <company> to apply for and receive a reference...') is "
    "'Reference Consent'. The actual reference letter/email from a previous "
    "employer is 'Reference'. A reference-consent form is NEVER a right-to-"
    "work document.\n"
    "9. MEDICAL / TB AND VEHICLE DOCUMENTS: A tuberculosis or other medical "
    "certificate is 'Proof of Tuberculosis Test' / a medical document. A V5C "
    "registration certificate / logbook is 'Proof of Car Ownership'. Their "
    "stamps and signatures never make them right-to-work documents.\n"
    "10. THE ID / IMMIGRATION DOCUMENT CLUSTER - keep these distinct:\n"
    "   - 'Passport': the passport BOOKLET identity page ONLY (face photo, "
    "'PASSPORT' header, two '<<<' machine-readable lines starting 'P<'). An MRZ "
    "or a passport number appearing on some OTHER document does not make it a "
    "passport.\n"
    "   - 'Visa Vignette': a visa STICKER inside a passport, including a 'UK "
    "ENTRY CLEARANCE' vignette (visa type/category, validity dates, immigration-"
    "officer stamp, and an MRZ that begins with 'V', not 'P<'). The sticker, "
    "not the photo page - and a passport page (or photocopy of one) DOMINATED "
    "by this sticker is 'Visa Vignette', not 'Passport', even though it also "
    "shows a photo and passport number. It is also never 'BRP': a BRP is a "
    "separate credit-card-sized plastic CARD headed 'RESIDENCE PERMIT', while "
    "a vignette is a STICKER on a passport PAGE - wording like 'ENTRY "
    "CLEARANCE', 'SKILLED WORKER MIGRANT' or 'leave to enter' on a passport "
    "page means vignette, not BRP.\n"
    "   - 'BRP': a physical, credit-card-sized Biometric Residence Permit CARD. "
    "BOTH sides are 'BRP': the FRONT (photo, card number, 'Residence Permit', "
    "category, expiry) and the BACK (Date/Place of birth, Sex, Nationality, "
    "'NO PUBLIC FUNDS', an 'NI NUMBER', and a two-line MRZ, with NO face photo). "
    "The BACK of a BRP is 'BRP', NOT 'Passport' and NOT 'National Insurance "
    "Number', despite showing an MRZ and an NI number - it is a small CARD, not "
    "a booklet page.\n"
    "   - 'eVisa Screenshot': a screenshot of the UKVI ONLINE immigration-status "
    "/ eVisa account page (the person's own status), from gov.uk - NOT a card "
    "and NOT the employer's check result.\n"
    "   - 'Share Code Check Result': the EMPLOYER's right-to-work CHECK RESULT "
    "page (Details of check box: company, date of check, reference number).\n"
    "   - 'UK Driving Licence': the PHYSICAL DVLA PHOTOCARD driving licence "
    "(a plastic CARD with the holder's photo, driver number, and entitlement "
    "category codes printed on it, 'DRIVING LICENCE'/'DVLA'). Decide UK vs "
    "non-UK from the ISSUER, field 4c - '4c. DVLA' (with a Union Jack or "
    "'UK'/'GB' panel) is the UK licence. Field 3's place of birth is about the "
    "PERSON: 'PAKISTAN'/'BANGLADESH'/'NIGERIA' there does NOT make the card "
    "foreign, and 'PROVISIONAL DRIVING LICENCE' is still the UK licence.\n"
    "   - 'Non UK Driving Licence': a FOREIGN photocard/paper driving licence "
    "issued by another country (not the DVLA) - the overseas equivalent, often "
    "in another language. Not a UK licence, not a passport, not an ID card. A "
    "UK flag panel ('UK'/'GB'/Union Jack) or a DVLA mark means the card is the "
    "UK one, whatever the holder's nationality or place of birth.\n"
    "   - 'Driving Permit': an INTERNATIONAL DRIVING PERMIT - headed "
    "'INTERNATIONAL DRIVING PERMIT' (often multilingual), issued under the "
    "UN/Geneva conventions as an official translation of a national licence, "
    "listing categories and the countries it is valid in. An IDP is 'Driving "
    "Permit', NEVER 'Non UK Driving Licence' - they are different types with "
    "different upload routes, so read the heading before choosing.\n"
    "   - 'Driving Licence Summary': the DVLA ONLINE 'Licence summary' / "
    "share-code printout (a gov.uk page headed 'Driver & Vehicle Licensing "
    "Agency' / 'Licence summary' with 'Driving status', a CHECK CODE, "
    "endorsements, and 'Can drive'/'Provisionally drive' category tables). A "
    "printed gov.uk licence page with a check code is the Summary; the plastic "
    "photocard is the Licence.\n"
    "   A photo card is NOT a passport; a status screenshot is NOT a card; the "
    "person's own eVisa status is NOT the employer's Share Code Check Result; "
    "and a gov.uk licence printout with a check code is the 'Driving Licence "
    "Summary', not the physical 'UK Driving Licence'.\n"
    "11. USE OF OWN CAR DECLARATION: a signed 'use of own car'/own-vehicle "
    "self-declaration (worker declares valid insurance incl. business use, MOT, "
    "a valid driving licence, and any licence endorsements, with their "
    "signature) is 'Use of Own Car Declaration'. It is NOT a 'DBS Document' or "
    "any DBS document, NOT 'Proof of Car Insurance' (the insurer's certificate), "
    "and NOT a driving licence. Its signature/tick boxes do not make it a DBS or "
    "right-to-work document.\n"
    "12. SAFEGUARDING QUESTIONNAIRE vs HEALTH DECLARATION: a form headed "
    "'Safeguarding Questions' with (often handwritten) answers about "
    "safeguarding, whistleblowing, the Mental Capacity Act, signs of abuse and "
    "reporting = 'Safeguarding Questionnaire'. A 'Health Declaration' is about "
    "the worker's own MEDICAL health (conditions, fitness, medication). A "
    "safeguarding questionnaire is NEVER a 'Health Declaration'.\n"
    "13. READ THE SUBJECT, NOT THE FORM. The word 'Declaration' in a title, or "
    "the fact that a document is signed, tabular, or has a yes/no "
    "acknowledgement layout, does NOT determine its type. Many unrelated HR "
    "forms use a 'Declaration'/acknowledgement framing - uniform & deposit "
    "receipts, intranet/IT-access declarations, emergency-contact forms, "
    "safeguarding questionnaires, health declarations, vehicle-use "
    "declarations, and more. Classify by what the form is actually ABOUT (its "
    "specific subject matter and fields), NEVER by the title word 'Declaration' "
    "or the signed/tabular layout alone. In particular, do NOT default a signed "
    "'Declaration' form to 'Use of Own Car Declaration' unless its subject is "
    "genuinely the worker's use of their own VEHICLE (car insurance, MOT, "
    "driving licence, roadworthiness, travel between calls): a uniform/deposit "
    "receipt is 'Receipt of Uniform and Deposit Declaration'; an emergency-"
    "contact / next-of-kin form is 'Emergency Contact Details'; an intranet/IT "
    "system-access acknowledgement is 'Intranet Access Declaration'; and a "
    "photographed staff photo-ID / lanyard card is 'ID Badge' (not a form at "
    "all).\n"
    "14. CV / RÉSUMÉ IS ALWAYS 'CV'. A curriculum vitae or résumé (a person's "
    "employment history, job timeline, qualifications, education, skills and "
    "contact details) is ALWAYS the controlled type 'CV'. Never label a CV as "
    "'Other - Curriculum Vitae', 'Other - Resume', or any other Other label - "
    "the specific 'CV' type already covers it, so match it to 'CV'. LIKEWISE "
    "a course/training completion certificate of any kind (workshop, e-"
    "learning, first aid, mandatory training, a screenshot or photo of one) "
    "is ALWAYS 'Training Certificate' - never an Other label. That second "
    "half applies ONLY to documents that CERTIFY COMPLETION of a course: a "
    "'Training Repayment Agreement' (a signed contract to repay training "
    "costs) is 'Other - Training Repayment Agreement', and a former "
    "employer's 'experience certificate'/character letter about a person's "
    "job and conduct is 'Reference'. The word 'training', or a "
    "certificate-like border, is not enough.\n"
    "15. THE CERTIFICATE OF SPONSORSHIP FAMILY - only the assigned CoS "
    "details record itself is 'Certificate of Sponsorship'. It must show the "
    "CoS's OWN fields: certificate/CoS number, sponsor (licence) details, the "
    "worker's personal details, the JOB (title/occupation code) and usually "
    "salary/work dates, plus 'Date assigned' / expiry (use by). Documents "
    "that merely relate to a CoS are NOT a CoS: a Sponsorship Management "
    "System screenshot or navigation page (breadcrumbs like 'Sponsorship "
    "management system > Workers', screens titled 'Amend a CoS' / 'Edit "
    "sponsor note' / a small 'CoS summary' box WITHOUT the job/salary "
    "details) is 'CoS Summary'; an email or letter discussing/querying a CoS "
    "is 'CoS Query Email'; a police certificate, training certificate, offer "
    "letter or visa letter that mentions sponsorship/'Tier 2'/a CoS number "
    "is its own type, never 'Certificate of Sponsorship'.\n"
    "16. REPORTS *ABOUT* DOCUMENTS OR CHECKS ARE THEIR OWN TYPE. An identity-"
    "document validation report (e.g. TrustID: 'Document Validation Report', "
    "document checks, MRZ/checksum validation, PASSED/FAILED flags, with "
    "thumbnail images of a BRP/passport) is 'Document Validation Report' - "
    "NEVER 'BRP', 'Passport', 'eVisa Screenshot' or 'Share Code Check "
    "Result', even though it pictures and describes those documents. An "
    "internal audit findings/actions table or email (audit ref, findings, "
    "'Action Required', 'Action By', due dates) is 'Corrective Action "
    "Report' - NEVER a DBS type, 'Probation Review' or 'Supervision', even "
    "when the audit items mention DBS checks, passports or licences.\n"
    "17. ROTATED / SIDEWAYS SCANS: many documents are photographed or "
    "scanned rotated 90 or 180 degrees. Rotation does not change the type - "
    "read the rotated text before deciding. If you cannot actually read the "
    "page well enough to identify it, do NOT guess a controlled name from "
    "its rough shape: return match=false, a LOW confidence, report the "
    "rotation, and describe what little you can see.\n"
    "18. MIXED BUNDLES: one scanned file sometimes contains several distinct "
    "documents. THE IMAGES ARE THE FILE'S PAGES IN ORDER - image 1 is page 1. "
    "Classify the FILE by its SUBSTANTIVE document, applying two "
    "principles in order: (a) a COVERING email/cover sheet never outranks "
    "the document it transmits - a reference-request email whose later pages "
    "contain the completed/written reference is a 'Reference' (only a chain "
    "with NO reference content is 'Reference Request Email'); (b) otherwise "
    "THE FIRST COMPLETE DOCUMENT WINS - an unrelated attachment or appendix "
    "does not override what the file starts with (e.g. a health-declaration "
    "form with an unrelated table appended is still the health "
    "declaration). Apply (b) strictly on stacked ID scans and mixed packs: a "
    "passport bio page on page 1 followed by a BRP on page 3 is 'Passport', "
    "not 'BRP'; a criminal-record declaration on page 1 followed by an "
    "emergency-contact form is 'Other - Criminal Record Check Declaration', "
    "not 'Emergency Contact Details'. Do NOT classify from the most "
    "interesting or most compliance-critical page - classify from the FIRST "
    "one, and put the rest in 'bundle_starts'. If every document in the file "
    "is the SAME type (two right-to-work checks, two insurance "
    "certificates), that shared type is the answer.\n"
    "19. A FILLED-IN FORM IS NEVER A 'TEMPLATE'. When a form's fields are "
    "completed (names, answers, dates, a signature), classify it as the "
    "document its CONTENT makes it - a filled reference check form is a "
    "'Reference', a filled health questionnaire is a 'Health Declaration'. "
    "Only a genuinely BLANK specimen with empty fields may be described as "
    "a template (an Other document).\n"
    "20. FOUR DOCUMENTS THAT ARE OFTEN GIVEN AN 'Other - ...' LABEL WHEN A "
    "CONTROLLED NAME ALREADY COVERS THEM. A council-tax or utility bill is "
    "'Proof of Address', NOT 'Other - council tax bill'. A bank account "
    "statement is 'Bank Statement', NOT 'Other - bank statement'. A signed "
    "staff-handbook receipt is 'Employee Handbook Receipt'. A curriculum "
    "vitae is 'CV'. THIS RULE IS A SHORT LIST, NOT A GENERAL PREFERENCE: it "
    "never overrides rule III. Where no controlled name genuinely fits, "
    "match=false with a specific other_label is still the RIGHT answer, and "
    "stretching the nearest controlled name over the document is still the "
    "worst one.\n"
    "21. UK vs NON-UK DRIVING LICENCE - DECIDE BY THE ISSUER, NOT BY THE "
    "HOLDER. On a photocard licence the numbered fields mean: 3 = the "
    "holder's date and PLACE OF BIRTH, 4c = the ISSUING AUTHORITY. Only 4c "
    "decides the type. '4c. DVLA', a Union Jack, or a blue 'UK'/'GB' panel "
    "means 'UK Driving Licence' - and a line such as '3. 04.12.1998 PAKISTAN' "
    "or a foreign-sounding name changes NOTHING, because it describes the "
    "person, not the card. Care-sector files are full of UK licences held by "
    "workers born abroad; filing them as 'Non UK Driving Licence' is a "
    "recurring and costly error (the two types take different Stage 3 upload "
    "routes). 'PROVISIONAL DRIVING LICENCE' with a UK panel is a UK "
    "provisional licence, still 'UK Driving Licence'. Use 'Non UK Driving "
    "Licence' ONLY when the issuing authority itself is overseas (a foreign "
    "country's name or transport authority printed as the issuer). And if the "
    "document is headed 'INTERNATIONAL DRIVING PERMIT', it is neither: it is "
    "'Driving Permit'.\n"
)


class APIError(Exception):
    """Carries a sanitized message and an HTTP status (0 for non-HTTP errors).
    `detail` holds the API's own human-readable explanation (key-free) for the
    console / failed_files.csv; `message` is the short label for the UI log."""
    def __init__(self, status: int, message: str, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail or message


class CreditExhausted(Exception):
    """Raised when the API reports the account is out of credit / billing is
    blocked. This is NOT a per-document problem, so the whole run must stop
    immediately rather than failing every remaining file. Carries the API's
    own message for display."""
    def __init__(self, detail: str = ""):
        super().__init__(detail or "API credit exhausted")
        self.detail = detail or "API credit exhausted"


class UnreadableDocumentError(Exception):
    """Raised instead of sending a classification/adjudication request when a
    document yields NO page images and NO extracted text (encrypted, corrupt,
    or every page rejected). The provider would otherwise be asked to classify
    nothing and can invent a plausible type: an encrypted PDF really was named
    from empty evidence and then hidden behind the audit's 'Custom Name'
    suppression with 'Pages Examined = 1'. Callers record it as an explicit
    unreadable/error outcome with zero examined pages."""
    def __init__(self, reason: str = ""):
        super().__init__(reason or "document is unreadable (no renderable "
                         "pages or extractable text)")
        self.reason = str(self)


def _sanitize_api_error(status: int, raw_body: str):
    """Return (label, detail) describing an API error.
    label  : short, safe text mapped from the status code (for the activity log).
    detail : the API's own human-readable 'message' plus error 'type' if present
             (for the console / failed_files.csv). The API never echoes the key
             in its response body, so the message is safe to surface and is the
             most useful diagnostic (e.g. it names oversized/blank images)."""
    known = {
        400: "bad request (a document may be unreadable or too large)",
        401: "authentication failed - check the API key",
        403: "forbidden - the API key may lack access",
        404: "endpoint not found",
        413: "payload too large - the document/image is too big",
        429: "rate limited - too many requests, slow down",
        500: "Anthropic server error",
        529: "Anthropic temporarily overloaded",
    }
    etype = ""
    emsg = ""
    try:
        j = json.loads(raw_body)
        err = j.get("error", {}) if isinstance(j, dict) else {}
        etype = str(err.get("type", ""))[:60]
        emsg = str(err.get("message", ""))[:300]
    except Exception:
        etype = ""
        emsg = ""
    base = known.get(status, f"HTTP {status}")
    label = f"{base}" + (f" [{etype}]" if etype else "")
    detail = " | ".join(x for x in (f"type={etype}" if etype else "",
                                    emsg) if x) or label
    return label, detail


def is_credit_error(status: int, detail: str) -> bool:
    """True if an API error indicates the account is out of credit / billing
    is blocked, rather than a problem with the specific document. Anthropic
    typically returns this as a 400 invalid_request_error whose message mentions
    the credit balance, or as a 402 Payment Required."""
    if status == 402:
        return True
    d = (detail or "").lower()
    needles = ("credit balance", "insufficient credit", "insufficient_quota",
               "out of credit", "billing", "purchase credits",
               "too low to", "payment required", "exceeded your current quota",
               "plan and billing")
    return any(n in d for n in needles)


class ClaudeAPI:
    URL = "https://api.anthropic.com/v1/messages"
    ALLOWED_HOST = "api.anthropic.com"
    MAX_RETRIES = 2          # bounded; only retried for transient 429/5xx
    RETRY_BASE_DELAY = 1.5   # seconds, exponential backoff

    def __init__(self, api_key: str, model_id: str):
        self.api_key = api_key.strip()
        self.model_id = model_id
        self.in_tokens = 0
        self.out_tokens = 0
        # Windows uses the operating system's TLS/HTTP stack for paid batch
        # creation. Select before sending; never fall back after a lost response.
        self.batch_transport = "winhttp" if os.name == "nt" else "urllib"

    # ---- low level ----
    def _check_url(self):
        """Defence-in-depth: never POST anywhere other than the Anthropic host,
        even if URL is somehow tampered with. Document content only ever leaves
        the machine through this single, asserted destination."""
        parsed = urllib.parse.urlparse(self.URL)
        if parsed.scheme != "https" or parsed.hostname != self.ALLOWED_HOST:
            raise APIError(0, f"refusing to send data to non-Anthropic host "
                              f"'{parsed.hostname}'")

    @staticmethod
    def _system_field(system: str, cache: bool):
        """Build the request's `system` field. When cache=True the (large, fixed)
        system prompt is wrapped as a content block marked with
        cache_control: ephemeral, so Anthropic prompt-caches it: the first call
        writes the cache and every later call within the cache lifetime reads it
        at ~10% of the input price. Because `system` is always the very start of
        the prompt, this caches cleanly regardless of the per-document images
        that follow. Harmless if the prefix is below the model's minimum
        cacheable size (it simply isn't cached)."""
        if cache and system:
            return [{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}]
        return system

    def _post(self, system: str, content_blocks: list, max_tokens=400,
              cache_system: bool = False):
        self._check_url()
        body = {
            "model": self.model_id,
            "max_tokens": max_tokens,
            # temperature 0: classification/extraction must be deterministic -
            # at the default (1.0) the same document could get different names
            # on different runs, which showed up as run-to-run flips in the
            # regression eval
            "temperature": 0,
            "system": self._system_field(system, cache_system),
            "messages": [{"role": "user", "content": content_blocks}],
        }
        data = json.dumps(body).encode("utf-8")
        attempt = 0
        while True:
            req = urllib.request.Request(self.URL, data=data, method="POST")
            req.add_header("content-type", "application/json")
            req.add_header("x-api-key", self.api_key)
            req.add_header("anthropic-version", "2023-06-01")
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                status = e.code
                try:
                    raw = e.read().decode("utf-8", "ignore")[:1000]
                except Exception:
                    raw = ""
                label, detail = _sanitize_api_error(status, raw)
                # Console-only diagnostic (captured by the dev console / stdout).
                # The API response never contains the key, so the message is safe
                # to surface here and is the key clue for invalid_request errors.
                print(f"[API {status}] {detail}")
                # Out-of-credit / billing-blocked is an ACCOUNT problem, not a
                # per-document one: stop the whole run immediately.
                if is_credit_error(status, detail):
                    raise CreditExhausted(detail)
                # Retry only transient errors, never 4xx auth/validation.
                if status in (429, 500, 502, 503, 529) and attempt < self.MAX_RETRIES:
                    delay = self.RETRY_BASE_DELAY * (2 ** attempt)
                    attempt += 1
                    import time as _t
                    _t.sleep(delay)
                    continue
                raise APIError(status, label, detail)
            except urllib.error.URLError as e:
                # network/DNS/timeout - retry a couple of times, then give up
                if attempt < self.MAX_RETRIES:
                    delay = self.RETRY_BASE_DELAY * (2 ** attempt)
                    attempt += 1
                    import time as _t
                    _t.sleep(delay)
                    continue
                raise APIError(0, f"network error ({getattr(e, 'reason', e)})")
            except TimeoutError as e:
                # A timeout DURING resp.read() is raised as a bare
                # TimeoutError, not URLError, so it used to bypass the retry
                # above and fail the document outright (a readable DBS result
                # became an audit's only error row this way). Same bounded
                # backoff; temperature-0 requests are safe to resend.
                if attempt < self.MAX_RETRIES:
                    delay = self.RETRY_BASE_DELAY * (2 ** attempt)
                    attempt += 1
                    import time as _t
                    _t.sleep(delay)
                    continue
                raise APIError(0, f"network error ({e or 'read timed out'})")
        usage = payload.get("usage", {})
        if _api_usage:
            _api_usage.record_usage(self.model_id, usage)
        # Fold prompt-cache tokens into an equivalent standard-priced input count
        # so the live £ meter stays accurate with caching on: uncached input is
        # full price, cache WRITES cost ~1.25x, cache READS cost ~0.1x.
        cache_write = usage.get("cache_creation_input_tokens", 0) or 0
        cache_read = usage.get("cache_read_input_tokens", 0) or 0
        self.in_tokens += (usage.get("input_tokens", 0)
                           + int(cache_write * 1.25) + int(cache_read * 0.10))
        self.out_tokens += usage.get("output_tokens", 0)
        parts = [b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"]
        return "\n".join(parts).strip()

    @staticmethod
    def _img_block(b64):
        # Detect the actual format from the decoded magic bytes so the media_type
        # is correct whether the renderer produced PNG or (downscaled) JPEG.
        media = "image/png"
        try:
            head = base64.b64decode(b64[:24])
            if head.startswith(b"\xff\xd8\xff"):
                media = "image/jpeg"
            elif head.startswith(b"\x89PNG"):
                media = "image/png"
            elif head[:4] == b"RIFF" and b"WEBP" in head:
                media = "image/webp"
            elif head.startswith(b"GIF8"):
                media = "image/gif"
        except Exception:
            pass
        return {"type": "image",
                "source": {"type": "base64", "media_type": media, "data": b64}}

    @staticmethod
    def _json_from(text: str) -> dict:
        """Parse the model's reply into a dict, tolerating the common ways a
        model wraps JSON: ```json fences (anywhere, not just the ends), leading
        prose ('Here is the classification: {...}'), and trailing commentary
        after the object. Strategy, in order:
          1. strip surrounding whitespace and any ``` fences, try a direct parse;
          2. scan for the FIRST balanced {...} object (brace-matching, so
             trailing prose that contains braces does not break it);
          3. fall back to a greedy first-{ .. last-} slice.
        A malformed / non-JSON reply returns {} and logs a diagnostic to the
        console - it is NEVER allowed to raise (callers treat {} as 'no match')."""
        if not text:
            return {}
        raw = str(text).strip()
        # 1) remove code fences wherever they appear, then try a direct parse
        stripped = re.sub(r"```(?:json)?", "", raw).strip()
        for candidate in (stripped, raw):
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
        # 2) first balanced {...} object via brace matching
        obj = ClaudeAPI._first_json_object(stripped)
        if isinstance(obj, dict):
            return obj
        # 3) greedy fallback: first '{' to last '}'
        m = re.search(r"\{.*\}", stripped, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
        # give up cleanly, but make the failure visible for diagnostics
        try:
            print("[json] could not parse a JSON object from model reply "
                  f"(first 200 chars): {raw[:200]!r}")
        except Exception:
            pass
        return {}

    @staticmethod
    def _first_json_object(text: str):
        """Return the first complete, balanced {...} JSON object found in text
        (respecting quotes/escapes so braces inside strings don't miscount), or
        None. Never raises."""
        start = text.find("{")
        while start != -1:
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = text[start:i + 1]
                        try:
                            return json.loads(chunk)
                        except Exception:
                            break  # malformed; try the next '{'
            start = text.find("{", start + 1)
        return None

    # ---- 1) classify a document into the controlled vocabulary ----
    def classify_payload(self, vocab_block: str, imgs: list,
                         extracted_text: str, max_tokens: int = 500,
                         note: str = "", page_idxs=None, total_pages=None,
                         segment: bool = False):
        """Build the (system, content_blocks, max_tokens) for a classification
        request. Shared by live classify() and Overnight Batch submission so BOTH
        modes send a byte-for-byte identical request (same system prompt, same
        image blocks, same max_tokens). This is the single definition of the
        classification prompt.
        `page_idxs`/`total_pages` label each image with the REAL page it came
        from ('Page 3 of 12'). Long documents are sampled (first two pages plus
        the last), so without labels the model cannot tell 'the third image'
        from 'page 3' - and rule 18(b), first complete document wins, is
        decided on exactly that. Omit them and the images go unlabelled, as
        before.
        `segment=True` (set only when the caller is showing EVERY non-ghost
        page, see segmentation_pages) additionally asks for the per-page
        `documents` map that lets a genuine multi-document file be split
        locally. It is purely ADDITIVE: the top-level match/name/confidence
        answer keeps exactly its old meaning - the whole file's single name
        under rule 18(b) - so a file that is not split is named today's way.
        (max_tokens must comfortably exceed the JSON reply: 320 used to truncate
        replies mid-'features', which parsed as {} and mis-filed the document.)"""
        if segment:
            # the documents map costs output tokens roughly linear in pages
            max_tokens = max(max_tokens, 500 + 60 * max(1, len(imgs)))
        system = (
            "You are a meticulous UK care-sector compliance document classifier. "
            "You are given the controlled vocabulary of allowed document names "
            "(with the features that identify each) and the document itself.\n\n"
            "Pick the ONE name from the vocabulary that best matches the "
            "document, copying the name EXACTLY as it appears in the vocabulary "
            "- never invent, reword, abbreviate or re-spell a name. Accuracy is "
            "critical - never guess. If the document does not clearly match any "
            "name in the vocabulary, return match=false with a short, specific "
            "other_label: that is a CORRECT, useful answer for the many one-off "
            "documents in an HR file, and always better than forcing the "
            "nearest-looking name.\n\n"
            "Report confidence honestly: 90+ only when you have actually READ "
            "the identifying features; 50-80 when the type is probable but key "
            "evidence is unread; below 40 when you are inferring from layout or "
            "general appearance. Pages are often scanned rotated 90 or 180 "
            "degrees - read the rotated text before deciding, and report the "
            "rotation in the 'rotation' field EVEN WHEN you could still read "
            "the content: an UPSIDE-DOWN page (title at the bottom, text only "
            "readable by inverting) is rotation 180, not 0 - being able to "
            "decipher it does not make it upright. If you cannot genuinely "
            "read the page, return match=false with low confidence rather "
            "than a guess.\n\n"
            "HARD-REQUIREMENT CHECK: some definitions state a feature the "
            "document MUST visibly contain (e.g. 'Passport' requires an MRZ "
            "beginning 'P<'; every DBS type requires the words DBS / "
            "Disclosure / Barring or a disclosure number; 'Certificate of "
            "Sponsorship' requires the CoS's own job/sponsor fields). Before "
            "returning match=true, confirm you can actually SEE that required "
            "feature in this document. If you cannot, that name is wrong no "
            "matter how similar the document looks - pick the name whose "
            "required features ARE visible, or return match=false.\n\n"
            "Special rules:\n"
            '- "Employment Contract" applies ONLY to the actual contract / '
            'statement-of-terms document itself - i.e. one whose TITLE is the '
            'contract, such as "Statement of Main Terms of Employment", '
            '"Schedule of Statement of Main Terms and Conditions of Employment", '
            '"Contract of Employment", or "Employment Contract" (any casing). '
            'Do NOT label a document "Employment Contract" merely because it is '
            'filed alongside a contract or repeats employment wording. Separate '
            'documents that often sit in the same contract pack - e.g. '
            '"Deductions from Pay Agreement", a pay/overpayment agreement, a '
            'policy page, a job description, a privacy notice, or a single '
            'signature/declaration page - are NOT the Employment Contract; '
            'classify them on their own title/content (often the OTHER group).\n'
            '- Anything that belongs to the OTHER group is returned with its '
            "OTHER name as given.\n\n"
            + DISAMBIGUATION_RULES +
            "\nBUNDLE CHECK: the images you are given are the file's pages in "
            "order (for long files, a sample). Occasionally one FILE wrongly "
            "contains SEVERAL distinct documents - a different artefact, or a "
            "document belonging to a DIFFERENT PERSON (e.g. a DBS certificate "
            "followed by another worker's passport). The MOST COMMON case is "
            "STACKED ID DOCUMENTS: a passport page followed by a driving "
            "licence, BRP, bank statement or other ID on the next page - each "
            "of those is a SEPARATE document and its page must be reported, "
            "even though they belong to the same person. If you can SEE any of "
            "this among the pages shown, report in 'bundle_starts' the position "
            "(1 = first image shown, 2 = second, ...) of each page that BEGINS "
            "a new, separate document. Continuation pages of the SAME document "
            "- page 2 of a contract, the BACK OF THE SAME card, a letter's "
            "second sheet - are NOT new documents. For a normal "
            "single-document file (the vast majority) bundle_starts is []. "
            "When unsure, leave it [].\n"
            "\nRespond with ONLY a JSON object, no prose:\n"
            '{"match": true|false, "name": "<exact vocabulary name or empty>", '
            '"group": "Crucial|Important|Other|", '
            '"confidence": 0-100, '
            '"rotation": 0|90|180|270 - the clockwise degrees the FIRST page '
            'image must be turned to read upright (0 if already upright), '
            '"rotations": [<one entry PER PAGE IMAGE PROVIDED, in order: 0, '
            "90, 180 or 270 - the clockwise degrees THAT page must be turned "
            "to read upright; scans regularly mix upright and rotated pages, "
            'so judge each page separately>], '
            '"features": "<2-3 concise identifying features you actually observe '
            'in this document: headings, logos, key phrases, layout, numbers - '
            'always fill this in, matched or not>", '
            '"other_label": "<a SHORT descriptive phrase (about 3-6 words) '
            'naming what this document actually is, for filing as Other - be '
            'specific about its purpose, not just its format. '
            'e.g. \'reference request email\', \'bank address screenshot\', '
            '\'audit action request email\', \'employee loan agreement\'. '
            'always provide this>", '
            '"guess": "<your best short description of what this document is, '
            'used only when match=false>", '
            '"bundle_starts": [<positions of shown pages that BEGIN a new '
            'separate document, [] for a single document>]}\n'
            "Set match=true only when confidence is high (>=80)."
        )
        if segment:
            # APPENDED, never woven in: a non-segmenting call keeps the exact
            # system prompt it has always had, so its prompt cache entry and
            # its answers are unchanged.
            system += (
                "\n\nPER-PAGE DOCUMENT MAP (additional task)\n"
                "You are being shown EVERY readable page of this file, each "
                "labelled with its REAL page number. As well as the single "
                "answer above (which must stay the answer for the FIRST "
                "complete document, exactly as instructed), return a "
                "'documents' array that maps EVERY page number from 1 to "
                f"{total_pages or len(imgs)} to the document it belongs to.\n"
                "- Near-blank pages are NOT shown to you. A page number you "
                "were not shown is the blank back of the page before it: put "
                "it in the SAME document as the page before it.\n"
                "- List the documents in page order. The first must start at "
                "page 1. Page ranges must not overlap and must leave no gaps.\n"
                "- A NEW document means a different ARTEFACT (a passport page "
                "after a right-to-work check, a payslip after a contract) or "
                "the same artefact for a DIFFERENT PERSON. Continuation pages "
                "- page 2 of a contract, the BACK of the same card, a "
                "signature page, an appendix - are the SAME document.\n"
                "- TWO COPIES OF THE SAME KIND OF DOCUMENT (two right-to-work "
                "checks, two insurance certificates, two bank statements) are "
                "two documents of the SAME type: report them as separate "
                "entries with the SAME 'type'. Never invent a different type "
                "to make them look distinct.\n"
                "- 'type' must be copied EXACTLY from the controlled "
                "vocabulary, or be a short specific description for an "
                "Other-group document. 'confidence' is per document, judged "
                "only on the pages of THAT document.\n"
                "- WHEN UNSURE, return ONE document covering every page. A "
                "wrong boundary destroys a compliance record; leaving a bundle "
                "whole merely leaves it as it is today.\n"
                "Add to the JSON object:\n"
                '"documents": [{"pages": [<real page numbers>], '
                '"type": "<exact vocabulary name or short Other description>", '
                '"document_date": "DD-MM-YYYY" or null, '
                '"confidence": 0-100}]'
            )
        blocks = []
        # rotation retries send MORE than MAX_PAGES images (one page rendered
        # in several orientations), so the cap is applied by the caller
        shown = imgs[:max(DocRender.MAX_PAGES, len(imgs))]
        # only label when the labels are trustworthy: the rotation retry sends
        # the SAME page four times, and its note explains that itself
        label = (not note and page_idxs is not None
                 and len(page_idxs) == len(shown) and total_pages)
        for k, b in enumerate(shown):
            if label:
                blocks.append({"type": "text", "text":
                               f"Page {page_idxs[k] + 1} of {total_pages}:"})
            blocks.append(self._img_block(b))
        ctx = f"CONTROLLED VOCABULARY:\n{vocab_block}\n\n"
        if extracted_text:
            ctx += f"EXTRACTED TEXT FROM THE DOCUMENT (may help):\n{extracted_text[:6000]}\n\n"
        if note:
            ctx += f"{note}\n\n"
        ctx += "Classify the document now. JSON only."
        blocks.append({"type": "text", "text": ctx})
        return system, blocks, max_tokens

    def classify(self, vocab_block: str, imgs: list, extracted_text: str,
                 note: str = "", page_idxs=None, total_pages=None,
                 segment: bool = False) -> dict:
        """LIVE classify: build the shared request and POST it synchronously.
        The fixed system prompt is prompt-cached (cache_system=True). `note` is
        an optional per-document instruction appended to the (uncached) user
        message - used by the rotation retry. `segment` additionally requests
        the per-page documents map (see classify_payload)."""
        system, blocks, mt = self.classify_payload(
            vocab_block, imgs, extracted_text, note=note,
            page_idxs=page_idxs, total_pages=total_pages, segment=segment)
        raw = self._post(system, blocks, max_tokens=mt, cache_system=True)
        return self._json_from(raw)

    # ---- 1b) ADAPTIVE TRIAGE: look at page 1 only, decide if enough ----
    def triage(self, vocab_block: str, page1_img: str, page1_text: str,
               total_pages: int) -> dict:
        """Cheap first-page-only call. Decides whether page 1 is enough to
        confidently classify, or whether later pages are needed (e.g. the
        identifying detail or a signature/date is likely further in).

        The filename is deliberately NOT given to the model: Stage 2 renames
        files to its own previous guesses, so on any re-run or repair pass a
        wrong filename becomes self-reinforcing evidence (and download names
        like 'CertificateofSponsorship-....pdf' are frequently wrong too).
        Classification must stand on the document's content alone."""
        system = (
            "You are triaging a UK care-sector compliance document using ONLY its "
            "FIRST PAGE, to save cost. Decide whether the first page is enough to "
            "classify it confidently, or whether later pages should be examined.\n\n"
            "Return enough_from_page1=true ONLY when ALL of these hold:\n"
            "  - you can confidently identify the document type from page 1, "
            "having actually READ its heading and key fields (not just its "
            "layout or general appearance), AND\n"
            "  - page 1 is upright and legible (if it is rotated or hard to "
            "read, prefer enough_from_page1=false), AND\n"
            "  - the document is NOT one whose key detail typically appears on a "
            "later page (e.g. a multi-page contract whose signature page is at the "
            "end, a bundle, or a form whose result/date is later).\n"
            "If the document looks important/compliance-critical and you are not "
            "fully certain from page 1, prefer enough_from_page1=false.\n\n"
            + DISAMBIGUATION_RULES +
            "\nRespond with ONLY JSON:\n"
            '{"enough_from_page1": true|false, '
            '"match": true|false, '
            '"name": "<exact vocabulary name if confident, else empty>", '
            '"group": "Crucial|Important|Other|", '
            '"confidence": 0-100, '
            '"rotation": 0|90|180|270 - clockwise degrees the page must be '
            'turned to read upright, '
            '"features": "<identifying features observed on page 1>", '
            '"other_label": "<a SHORT descriptive phrase (about 3-6 words) '
            'naming what this document is, if it is an Other-group doc - be '
            'specific about purpose, e.g. \'reference request email\', '
            '\'bank address screenshot\'>", '
            '"reason": "<short why more pages are/are not needed>"}'
        )
        blocks = [self._img_block(page1_img)] if page1_img else []
        ctx = (f"TOTAL PAGES IN DOCUMENT: {total_pages}\n\n"
               f"CONTROLLED VOCABULARY:\n{vocab_block}\n\n")
        if page1_text:
            ctx += f"PAGE 1 EXTRACTED TEXT:\n{page1_text[:4000]}\n\n"
        ctx += "Triage now. JSON only."
        blocks.append({"type": "text", "text": ctx})
        # 300 truncated the JSON whenever 'features'/'reason' ran long, so the
        # reply failed to parse and the doc fell through to a full re-classify
        # - silently defeating the whole point of adaptive triage. 600 leaves
        # comfortable room for the small JSON object.
        raw = self._post(system, blocks, max_tokens=600, cache_system=True)
        return self._json_from(raw)

    # ---- 2a) Certificate of Sponsorship: issue date ----
    def cos_issue_date(self, imgs: list, text: str) -> str:
        system = (
            "You are reading a UK Certificate of Sponsorship (CoS) details page. "
            "Find its ISSUE/ASSIGNED date - the date the certificate was assigned "
            "or issued. On the standard UKVI layout this is the 'Date assigned' "
            "field (or 'Current certificate status date' when shown as ASSIGNED). "
            "Do NOT return the 'Expiry date (use by)', the employment start date, "
            "or the worker's date of birth. If only a date range is visible, "
            "return the assigned/issue date. "
            "Respond ONLY with JSON: "
            '{"issue_date": "YYYY-MM-DD" or ""}.'
        )
        blocks = [self._img_block(b) for b in imgs[:DocRender.MAX_PAGES]]
        if text:
            blocks.append({"type": "text", "text": f"Extracted text:\n{text[:4000]}"})
        blocks.append({"type": "text", "text": "Return the issue date as JSON only."})
        return self._json_from(self._post(system, blocks, max_tokens=120)).get("issue_date", "")

    # ---- 2b) Employment Contract: signed? ----
    def contract_signed(self, imgs: list, text: str) -> bool:
        system = (
            "You are checking a UK employment contract. Determine whether it is "
            "SIGNED by the employee (a handwritten signature, typed signature, "
            "e-signature mark, or clearly completed signature block with a name "
            "and date). An empty signature line is NOT signed. Respond ONLY with "
            'JSON: {"signed": true|false}.'
        )
        blocks = [self._img_block(b) for b in imgs[:DocRender.MAX_PAGES]]
        if text:
            blocks.append({"type": "text", "text": f"Extracted text:\n{text[:5000]}"})
        blocks.append({"type": "text", "text": "Is it signed? JSON only."})
        return bool(self._json_from(self._post(system, blocks, max_tokens=80)).get("signed", False))

    # ---- 2d) DBS Document: clarity / completeness / relevance score ----
    # ---- general document-quality ranker (any type, for numbered ranking) ----
    def doc_quality(self, imgs: list, text: str, doc_type: str) -> dict:
        """Score ANY document of a given type for overall quality so multiple
        copies can be ranked. Higher = better, judged on: NEWEST (most recent /
        most current / latest valid), CLEAREST (legible, flat, complete, not
        cut-off or blurry), and MOST RELEVANT / COMPLETE (shows the key
        identifying fields for that document type, correct document, right
        person). Returns a 0-100 score and supporting flags."""
        system = (
            f"You are assessing a single UK care-compliance document that has "
            f"been classified as: '{doc_type}'. Multiple copies of this document "
            f"type exist for one worker and must be RANKED so the best copy gets "
            f"the highest rank. Score THIS copy from 0-100 on overall quality, "
            f"where a HIGHER score means a BETTER document, judged on three "
            f"things together:\n"
            f"1) NEWEST / MOST CURRENT - the most recent, most up-to-date, or "
            f"latest-valid version (later issue/expiry/check dates are better; an "
            f"expired or superseded copy is worse). If dates are not applicable, "
            f"ignore this.\n"
            f"2) CLEAREST - fully legible, flat and straight, complete (not "
            f"cut-off, cropped, dark, glare-covered or blurry). A clean scan "
            f"beats a poor phone photo.\n"
            f"3) MOST RELEVANT / COMPLETE - genuinely this document type for the "
            f"right person, showing the key identifying fields you would expect "
            f"for a '{doc_type}' (e.g. names, dates, numbers, photo, issuing "
            f"authority as applicable). A partial, wrong-side, or ambiguous copy "
            f"scores lower.\n\n"
            f"Use the FULL range: give clearly-better copies distinctly higher "
            f"scores than weaker ones so they can be ordered. Respond ONLY with "
            f"JSON: "
            '{"score": 0-100, "legible": true|false, "complete": true|false, '
            '"date": "YYYY-MM-DD" or "", "note": "<=8 words"}.'
        )
        blocks = [self._img_block(b) for b in imgs[:DocRender.MAX_PAGES]]
        if text:
            blocks.append({"type": "text", "text": f"Extracted text:\n{text[:5000]}"})
        blocks.append({"type": "text",
                       "text": f"Score this '{doc_type}' copy. JSON only."})
        d = self._json_from(self._post(system, blocks, max_tokens=160))
        try:
            score = int(float(d.get("score", 0)))
        except Exception:
            score = 0
        return {"score": max(0, min(100, score)),
                "legible": bool(d.get("legible", False)),
                "complete": bool(d.get("complete", False)),
                "date": str(d.get("date", "")).strip(),
                "note": str(d.get("note", "")).strip()}

    def dbs_quality(self, imgs: list, text: str) -> dict:
        """Score how good a DBS Document is, on CLARITY, COMPLETENESS and
        RELEVANCE (no date weighting). The ideal clearly shows the FULL NAME,
        DATE OF BIRTH, and the POSITION for which the check was requested.
        Returns a 0-100 score plus which key fields are present."""
        system = (
            "You are assessing a UK DBS (Disclosure & Barring Service) CERTIFICATE "
            "document to score how good a copy it is. Valid forms of a DBS "
            "Document include: the GREEN DBS certificate; a 'DBS Certificate "
            "Record' sheet (a record of the certificate's details); and documents "
            "bearing a 'uCheck' logo. All are acceptable.\n\n"
            "Score on CLARITY, COMPLETENESS and RELEVANCE only - do NOT reward "
            "newer dates. The IDEAL document clearly and legibly shows ALL of:\n"
            "- the applicant's FULL NAME;\n"
            "- DATE OF BIRTH;\n"
            "- the POSITION / role for which the check was requested (e.g. "
            "'Position applied for', 'Position for which the check was "
            "requested').\n"
            "Supporting details that add confidence: the date of the original/"
            "issued DBS check, a Unique Reference Number / certificate number, "
            "the type of check, and the disclosure outcome (e.g. 'None "
            "Recorded'). Good scan quality (flat, straight, fully legible) beats "
            "a skewed, dark, cut-off or blurry photo.\n\n"
            "Give a high score when full name + date of birth + position are all "
            "clearly present and the document is legible. Give a low score when "
            "key fields are missing/illegible, the page is cut off, or it is a "
            "blurry/partial photo.\n\n"
            "Respond ONLY with JSON: "
            '{"score": 0-100, '
            '"has_full_name": true|false, '
            '"has_dob": true|false, '
            '"has_position": true|false, '
            '"has_reference_number": true|false, '
            '"legible": true|false}.'
        )
        blocks = [self._img_block(b) for b in imgs[:DocRender.MAX_PAGES]]
        if text:
            blocks.append({"type": "text", "text": f"Extracted text:\n{text[:5000]}"})
        blocks.append({"type": "text", "text": "Score this DBS document. JSON only."})
        d = self._json_from(self._post(system, blocks, max_tokens=160))
        try:
            score = int(float(d.get("score", 0)))
        except Exception:
            score = 0
        return {"score": max(0, min(100, score)),
                "has_full_name": bool(d.get("has_full_name", False)),
                "has_dob": bool(d.get("has_dob", False)),
                "has_position": bool(d.get("has_position", False)),
                "has_reference_number": bool(d.get("has_reference_number", False)),
                "legible": bool(d.get("legible", False))}

    # ---- 2e) ECS Notice: check date + outcome ----
    def ecs_check(self, imgs: list, text: str) -> dict:
        """Read an Employer Checking Service (ECS) notice: find the date of the
        check/notice and whether the outcome is a positive right-to-work
        confirmation."""
        system = (
            "You are reading a UK Employer Checking Service (ECS) notice or "
            "letter. Find:\n"
            "1) the DATE of the check or the date on the notice/letter. This "
            "may be labelled 'Date', 'Date of check', or appear at the top of "
            "the letter.\n"
            "2) whether the OUTCOME is POSITIVE - i.e. the letter/notice "
            "confirms the person has a current right to work (e.g. 'has a "
            "current right to work', 'Positive Verification Notice', 'can "
            "continue to work'). Return verified=false if the outcome is "
            "negative, unclear, or not stated.\n"
            "Respond ONLY with JSON: "
            '{"check_date": "YYYY-MM-DD" or "", "verified": true|false}.'
        )
        blocks = [self._img_block(b) for b in imgs[:DocRender.MAX_PAGES]]
        if text:
            blocks.append({"type": "text", "text": f"Extracted text:\n{text[:4000]}"})
        blocks.append({"type": "text", "text": "Return date and outcome. JSON only."})
        d = self._json_from(self._post(system, blocks, max_tokens=100))
        return {"check_date": (d.get("check_date") or "").strip(),
                "verified": bool(d.get("verified", False))}

    # ---- 2f) BRP: expiry date + quality of scan ----
    def brp_details(self, imgs: list, text: str) -> dict:
        """Read a BRP (Biometric Residence Permit) card. Find the expiry date
        and assess whether the front (photo/details) side is visible."""
        system = (
            "You are reading a UK BRP (Biometric Residence Permit) card. "
            "Find:\n"
            "1) the EXPIRY DATE of the card (the date it expires / is valid "
            "until). It is usually on the front of the card, often labelled "
            "'Expiry' or 'Valid until'.\n"
            "2) whether the FRONT of the card is visible - the front has the "
            "holder's PHOTO, their full name, date of birth, nationality, and "
            "visa/leave category. The back has fingerprints and a machine-"
            "readable zone.\n"
            "3) whether the scan is CLEAR and all text is legible.\n"
            "Respond ONLY with JSON: "
            '{"expiry_date": "YYYY-MM-DD" or "", '
            '"front_visible": true|false, '
            '"clear": true|false}.'
        )
        blocks = [self._img_block(b) for b in imgs[:DocRender.MAX_PAGES]]
        if text:
            blocks.append({"type": "text", "text": f"Extracted text:\n{text[:4000]}"})
        blocks.append({"type": "text", "text": "Return BRP details. JSON only."})
        d = self._json_from(self._post(system, blocks, max_tokens=100))
        return {"expiry_date": (d.get("expiry_date") or "").strip(),
                "front_visible": bool(d.get("front_visible", False)),
                "clear": bool(d.get("clear", False))}

    # ---- 2g) eVisa Screenshot: check/screenshot date + validity ----
    def evisa_check(self, imgs: list, text: str) -> dict:
        """Read a UKVI eVisa or 'View and prove your immigration status' "
        "screenshot. Find the date of the check/screenshot and whether the "
        "status shows as current/valid."""
        system = (
            "You are reading a UK eVisa or UKVI 'View and prove your "
            "immigration status' screenshot (from the Home Office online "
            "service). Find:\n"
            "1) the DATE of the screenshot or check - this is often shown as "
            "'Date you viewed this' or similar, or may be handwritten on the "
            "page. Use the most specific date available.\n"
            "2) whether the immigration status appears VALID / CURRENT at the "
            "time of the check (i.e. the visa/leave has not expired and work "
            "is permitted).\n"
            "Respond ONLY with JSON: "
            '{"check_date": "YYYY-MM-DD" or "", "valid": true|false}.'
        )
        blocks = [self._img_block(b) for b in imgs[:DocRender.MAX_PAGES]]
        if text:
            blocks.append({"type": "text", "text": f"Extracted text:\n{text[:4000]}"})
        blocks.append({"type": "text", "text": "Return eVisa check details. JSON only."})
        d = self._json_from(self._post(system, blocks, max_tokens=100))
        return {"check_date": (d.get("check_date") or "").strip(),
                "valid": bool(d.get("valid", False))}

    # ---- 2h) NI Number document: clarity/completeness score ----
    def ni_quality(self, imgs: list, text: str) -> dict:
        """Score a National Insurance Number document on how clearly and
        officially it shows the NI number."""
        system = (
            "You are assessing a UK National Insurance Number document. "
            "Score it on clarity and completeness (0-100). The IDEAL is an "
            "official HMRC letter (headed 'HM Revenue & Customs' or 'HMRC' "
            "or 'Department for Work and Pensions' / 'DWP') that clearly "
            "shows the NI number in the standard format (two letters, six "
            "digits, one letter - e.g. 'AB 12 34 56 C'). Score lower for a "
            "handwritten note, a partial/blurry scan, or a document where the "
            "NI number itself is not clearly legible.\n"
            "Respond ONLY with JSON: "
            '{"score": 0-100, '
            '"ni_number_visible": true|false, '
            '"is_official_letter": true|false, '
            '"has_name": true|false}.'
        )
        blocks = [self._img_block(b) for b in imgs[:DocRender.MAX_PAGES]]
        if text:
            blocks.append({"type": "text", "text": f"Extracted text:\n{text[:4000]}"})
        blocks.append({"type": "text", "text": "Score this NI number document. JSON only."})
        d = self._json_from(self._post(system, blocks, max_tokens=100))
        try:
            score = int(float(d.get("score", 0)))
        except Exception:
            score = 0
        return {"score": max(0, min(100, score)),
                "ni_number_visible": bool(d.get("ni_number_visible", False)),
                "is_official_letter": bool(d.get("is_official_letter", False)),
                "has_name": bool(d.get("has_name", False))}

    # ---- 2i) Share Code Check Result: check date + validity ----
    def share_code_check(self, imgs: list, text: str) -> dict:
        """Read a Home Office 'View a right to work / immigration status' share
        code result page. Find the date of the check and whether the status
        shows work is currently permitted."""
        system = (
            "You are reading a UK Home Office 'View a right to work' or "
            "'View immigration status' share code CHECK RESULT page - the page "
            "produced after an employer enters a share code online. Find:\n"
            "1) the DATE OF CHECK - when the employer performed this check. "
            "This may appear as 'Date of check', a date printed on the result "
            "page, or a handwritten date added by the employer.\n"
            "2) whether work is currently PERMITTED - i.e. the result page "
            "shows the person has a current right to work and work is allowed "
            "(e.g. 'they have the right to work', 'can work', the visa/leave "
            "has not expired at the time of the check).\n"
            "Respond ONLY with JSON: "
            '{"check_date": "YYYY-MM-DD" or "", "work_permitted": true|false}.'
        )
        blocks = [self._img_block(b) for b in imgs[:DocRender.MAX_PAGES]]
        if text:
            blocks.append({"type": "text", "text": f"Extracted text:\n{text[:4000]}"})
        blocks.append({"type": "text", "text": "Return check date and work status. JSON only."})
        d = self._json_from(self._post(system, blocks, max_tokens=100))
        return {"check_date": (d.get("check_date") or "").strip(),
                "work_permitted": bool(d.get("work_permitted", False))}

    # ================================================================
    # MESSAGE BATCHES API  (Overnight Batch mode - 50% cheaper)
    # ----------------------------------------------------------------
    # These call the asynchronous /v1/messages/batches endpoints. The request
    # `params` are the EXACT same body /v1/messages would receive (built via
    # classify_payload), so a batched classification is identical to a live one
    # apart from price and turnaround. All traffic stays on the Anthropic host
    # (asserted by _check_host), the single destination document data leaves by.
    # ================================================================
    BATCH_URL = "https://api.anthropic.com/v1/messages/batches"
    # Per Anthropic limits: a batch may hold up to 100,000 requests / 256 MB.
    # Kept slightly under so encoding overhead never tips us over.
    BATCH_MAX_REQUESTS = 100_000
    BATCH_MAX_BYTES = 256 * 1024 * 1024
    BATCH_SIZE_HEADROOM = 0.95      # aim for <=95% of the byte ceiling per batch

    @classmethod
    def _check_host(cls, url: str):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != cls.ALLOWED_HOST:
            raise APIError(0, f"refusing to contact non-Anthropic host "
                              f"'{parsed.hostname}'")

    def _headers(self, req):
        req.add_header("content-type", "application/json")
        req.add_header("x-api-key", self.api_key)
        req.add_header("anthropic-version", "2023-06-01")

    def _http(self, method: str, url: str, body: dict = None):
        """Generic Anthropic JSON request with the same bounded transient-retry
        policy as _post for safe polling/cancel calls. Batch-creation POSTs are
        never automatically retried: a lost response is ambiguous, and sending
        the same body again could create and bill a duplicate batch. Returns the
        parsed JSON dict. Raises APIError / CreditExhausted like _post."""
        self._check_host(url)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        batch_creation = (method.upper() == "POST"
                          and url.rstrip("/") == self.BATCH_URL)
        if batch_creation:
            transport = getattr(self, "batch_transport",
                                "winhttp" if os.name == "nt" else "urllib")
            if transport == "winhttp":
                from batch_transport import BatchTransportError, winhttp_post_batch
                try:
                    status, raw = winhttp_post_batch(url, {
                        "content-type": "application/json",
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01"}, data)
                except BatchTransportError as exc:
                    raise APIError(0, str(exc)) from None
                if not 200 <= status < 300:
                    safe_body = raw.replace(self.api_key, "[redacted]") if self.api_key else raw
                    label, detail = _sanitize_api_error(status, safe_body[:1000])
                    if is_credit_error(status, detail):
                        raise CreditExhausted(detail)
                    raise APIError(status, label, detail)
                try:
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise ValueError("not an object")
                    return parsed
                except (ValueError, TypeError):
                    raise APIError(0, "Windows batch response was not valid JSON; "
                                   "reconcile the submission before retrying") from None
            if transport != "urllib":
                raise APIError(0, "Unknown batch transport; no request sent")
        attempt = 0
        retry_safe = method.upper() != "POST" or url.rstrip("/").endswith(
            "/cancel")
        while True:
            req = urllib.request.Request(url, data=data, method=method)
            self._headers(req)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                status = e.code
                try:
                    rawb = e.read().decode("utf-8", "ignore")[:1000]
                except Exception:
                    rawb = ""
                label, detail = _sanitize_api_error(status, rawb)
                print(f"[batch API {status}] {method} {url.rsplit('/',2)[-1]}: {detail}")
                if is_credit_error(status, detail):
                    raise CreditExhausted(detail)
                if (retry_safe and status in (429, 500, 502, 503, 529)
                        and attempt < self.MAX_RETRIES):
                    import time as _t
                    _t.sleep(self.RETRY_BASE_DELAY * (2 ** attempt))
                    attempt += 1
                    continue
                raise APIError(status, label, detail)
            except urllib.error.URLError as e:
                if retry_safe and attempt < self.MAX_RETRIES:
                    import time as _t
                    _t.sleep(self.RETRY_BASE_DELAY * (2 ** attempt))
                    attempt += 1
                    continue
                raise APIError(0, f"network error ({getattr(e, 'reason', e)})")
            except TimeoutError as e:
                # read-phase timeout (see _post); retried only for the same
                # idempotent-safe requests as URLError - never a batch POST
                if retry_safe and attempt < self.MAX_RETRIES:
                    import time as _t
                    _t.sleep(self.RETRY_BASE_DELAY * (2 ** attempt))
                    attempt += 1
                    continue
                raise APIError(0, f"network error ({e or 'read timed out'})")

    def build_batch_request(self, custom_id: str, system: str, blocks: list,
                            max_tokens: int, cache_system: bool = True) -> dict:
        """One entry in the batch `requests` array: {custom_id, params}. `params`
        is the normal /v1/messages body, so a batched call is byte-identical to
        the live one built by classify_payload()."""
        return {
            "custom_id": custom_id,
            "params": {
                "model": self.model_id,
                "max_tokens": max_tokens,
                "temperature": 0,   # deterministic, matching live _post
                "system": self._system_field(system, cache_system),
                "messages": [{"role": "user", "content": blocks}],
            },
        }

    def submit_batch(self, requests_list: list) -> dict:
        """POST a list of {custom_id, params} requests as one batch. Returns the
        created batch object (id, processing_status, request_counts, ...)."""
        return self._http("POST", self.BATCH_URL, {"requests": requests_list})

    def get_batch(self, batch_id: str) -> dict:
        """GET a batch's current status object."""
        return self._http("GET", f"{self.BATCH_URL}/{urllib.parse.quote(batch_id)}")

    def list_batches(self, after_id: str = "", limit: int = 100) -> dict:
        """Read one newest-first provider page; never create/retry a batch."""
        query = {"limit": max(1, min(100, int(limit)))}
        if after_id:
            query["after_id"] = after_id
        return self._http("GET", self.BATCH_URL + "?"
                          + urllib.parse.urlencode(query))

    def cancel_batch(self, batch_id: str) -> dict:
        """Request cancellation of a batch still being processed. Anything
        already processed is still billed and its results remain retrievable;
        in-flight/queued requests are cancelled."""
        return self._http("POST",
                          f"{self.BATCH_URL}/{urllib.parse.quote(batch_id)}/cancel")

    def batch_results(self, results_url: str):
        """Download a finished batch's results (JSONL) and yield one parsed dict
        per line. Streams so a large result set is not all held in memory."""
        self._check_host(results_url)
        req = urllib.request.Request(results_url, method="GET")
        self._headers(req)
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    print(f"[batch] skipped an unparseable results line: {line[:120]!r}")


# ====================================================================
# FILESYSTEM HELPERS
# ====================================================================
ILLEGAL = r'[<>:"/\\|?*]'

def safe_stem(name: str) -> str:
    name = re.sub(ILLEGAL, " ", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:150] if name else "document"


def other_name(label: str) -> str:
    """Build a clean 'Other - <descriptor>' name from the AI's short description.
    Keeps a concise descriptive phrase (a few words) and strips junk; falls back
    to 'Other - Unknown' when there's no usable label.  An unmatched document
    must never lose the visible Other prefix.
    e.g. 'Other - reference request email', 'Other - bank address screenshot'."""
    if not label:
        return "Other - Unknown"
    lab = re.sub(ILLEGAL, " ", str(label)).strip()
    lab = re.sub(r"\s+", " ", lab)
    # drop a leading 'other -' if the model echoed it, and any stray dashes
    lab = re.sub(r"^\s*other\s*[-:]\s*", "", lab, flags=re.I).strip(" -")
    if not lab:
        return "Other - Unknown"
    # keep it a concise descriptive phrase: at most the first 6 words / 60 chars
    words = lab.split()
    lab = " ".join(words[:6])[:60].strip()
    return f"Other - {lab}" if lab else "Other - Unknown"


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    """Return a non-colliding path: 'Name.pdf', 'Name (2).pdf', ..."""
    cand = folder / f"{stem}{ext}"
    if not cand.exists():
        return cand
    i = 2
    while True:
        cand = folder / f"{stem} ({i}){ext}"
        if not cand.exists():
            return cand
        i += 1


def unique_dir(parent: Path, name: str) -> Path:
    """Return a non-colliding directory path under `parent`: 'Worker',
    'Worker (2)', ... Never returns an existing path, so a move can never
    overwrite or merge into an existing worker folder."""
    cand = parent / name
    if not cand.exists():
        return cand
    i = 2
    while True:
        cand = parent / f"{name} ({i})"
        if not cand.exists():
            return cand
        i += 1


def move_worker_folder(worker_dir: Path, dest_root: Path):
    """Move a completed worker subfolder into dest_root. Never overwrites: if a
    folder of the same name already exists in the destination, the incoming one
    is placed as 'Name (2)', etc. Returns the final destination Path.
    Raises on failure (caller decides what to do)."""
    dest_root.mkdir(parents=True, exist_ok=True)
    target = unique_dir(dest_root, worker_dir.name)
    shutil.move(str(worker_dir), str(target))
    return target


def parse_date(s: str):
    """Parse a YYYY-MM-DD-ish string to a date; return None on failure."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except Exception:
            continue
    m = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    return None


_ORIENTATION_TEMP_RE = re.compile(
    r"^\.(?P<stem>.+)\.orientation-[a-z0-9_]{8}\.(?P<suffix>pdf|tmp)$",
    re.IGNORECASE)


def is_orientation_temp_file(path: Path) -> bool:
    """True only for the exact same-directory orientation temp name."""
    return bool(_ORIENTATION_TEMP_RE.fullmatch(Path(path).name))


def _orientation_temp_source(path: Path):
    """Return the expected source PDF sibling, or None when it is absent."""
    path = Path(path)
    match = _ORIENTATION_TEMP_RE.fullmatch(path.name)
    if not match:
        return None
    source_suffix = (match.group("suffix")
                     if match.group("suffix").casefold() == "pdf" else "pdf")
    source = path.with_name(f"{match.group('stem')}.{source_suffix}")
    return source if source.is_file() else None


def cleanup_orientation_temp_files(worker_dir: Path,
                                   log=lambda _message: None) -> int:
    """Remove only proven residue from an interrupted atomic PDF rewrite.

    A filename match alone is not enough: the original source PDF must still
    be beside it. This keeps unrelated hidden PDFs untouched.
    """
    removed = 0
    for path in list(Path(worker_dir).rglob("*")):
        if not path.is_file() or _orientation_temp_source(path) is None:
            continue
        try:
            path.unlink()
            removed += 1
            log(f"    x interrupted orientation temp removed: {path.name}")
        except Exception as exc:
            log(f"    ! could not remove orientation temp {path.name}: {exc}")
    return removed


def list_worker_docs(worker_dir: Path):
    """All loose document files directly representing this worker's docs,
    found recursively (so it copes whether or not sub-folders still exist)."""
    cleanup_orientation_temp_files(worker_dir)
    files = []
    for p in worker_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in DOC_EXT and not is_program_file(p):
            files.append(p)
    files.sort(key=lambda p: natural_key(p.name))
    return files


# Hidden / system files that should never block a folder deletion and can be
# safely discarded along with an emptied sub-folder.
JUNK_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", "ehthumbs.db",
              ".dropbox", ".dropbox.attr", "icon\r"}


def _is_junk(p: Path) -> bool:
    n = p.name.lower()
    if n in JUNK_NAMES:
        return True
    # OneDrive/Windows sometimes leave zero-byte hidden placeholders
    if n.startswith("~$") or n.endswith(".tmp"):
        return True
    return False


def is_program_file(p: Path) -> bool:
    """True for internal program artefacts (NOT worker documents) that must
    never be converted, classified, renamed, filed into a sub-folder, counted,
    or uploaded - they are left where they are, untouched. Covers:
      - any file whose name begins with a leading underscore (our convention
        for helper/manifest files, e.g. '_overwrite_order.csv'), and
      - any file whose name contains 'overwrite_order' (legacy manifests left
        behind by a previous version, whatever their extension).
    Real compliance documents never start with '_'."""
    n = p.name.lower()
    return (n.startswith("_") or "overwrite_order" in n
            or is_orientation_temp_file(p))


def _real_files_in(d: Path):
    """Return a list of real (non-junk) files anywhere under d. If a folder
    cannot be listed (e.g. a cloud error), that is surfaced, not swallowed."""
    real = []
    try:
        for child in d.iterdir():
            if child.is_dir():
                real.extend(_real_files_in(child))
            elif child.is_file() and not _is_junk(child):
                real.append(child)
    except Exception:
        # if we cannot even read the folder, treat it as "has something" so we
        # don't delete it blindly
        real.append(d / "<unreadable>")
    return real


def batch_worker_ready_to_move(worker_dir: Path, records) -> bool:
    """True when batch apply may safely relocate a worker folder.

    Batch mode normally proves completion by collecting at least one applied
    record.  A genuinely empty worker has no record to collect, but live mode
    still moves it after its no-op processing pass.  Treat that one case as
    complete as well, while keeping any folder that still contains a real
    (possibly skipped or failed) file in the source for attention.
    """
    return bool(records) or not _real_files_in(worker_dir)


def extract_worker_zips(worker_dir: Path, log=lambda m: None) -> int:
    """Extract every document from .zip archives anywhere under worker_dir
    into the worker folder itself (flat, collision-safe names), so archived
    documents get processed like any other file - and so the end-of-run
    leftover cleanup can safely delete the archives afterwards. Only DOC_EXT
    members are extracted; unreadable/encrypted archives are left alone (and
    are then also NOT deleted by the cleanup). Returns files extracted."""
    import zipfile
    extracted = 0
    for z in list(worker_dir.rglob("*.zip")):
        try:
            with zipfile.ZipFile(z) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    member = Path(info.filename)
                    if member.suffix.lower() not in DOC_EXT:
                        continue
                    dest = unique_path(worker_dir, member.stem, member.suffix)
                    with zf.open(info) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    extracted += 1
                    log(f"    + extracted from {z.name}: {dest.name}")
        except Exception as e:
            log(f"    ! could not extract {z.name}: {e} (archive kept)")
    return extracted


# Files that are pure residue once a worker is fully processed: split-tool
# backups of the original combined PDFs, and source archives whose documents
# were extracted during flatten. Deleted (with logging) at the end of each
# worker by cleanup_leftover_files.
LEFTOVER_EXTS = {".splitbak", ".zip"}


def cleanup_leftover_files(worker_dir: Path, log=lambda m: None) -> int:
    """Delete leftover non-document residue under worker_dir: .splitbak
    backups always; .zip archives only when they can still be opened (an
    unreadable archive might hold unextracted documents, so it is kept and
    reported). Returns the number of files deleted."""
    import zipfile
    deleted = 0
    for p in list(worker_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in LEFTOVER_EXTS:
            continue
        if p.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(p) as zf:
                    zf.infolist()
            except Exception:
                log(f"    ! leftover archive {p.name} is unreadable - kept "
                    f"(check it by hand)")
                continue
        try:
            p.unlink()
            deleted += 1
            log(f"    x leftover removed: {p.name}")
        except Exception as e:
            log(f"    ! could not delete leftover {p.name}: {e}")
    return deleted


def flatten_worker(worker_dir: Path, log=lambda m: None):
    """Move every real file anywhere under worker_dir up into worker_dir itself,
    then delete the sub-folders (Overwrite Documents, Bulk, Bulk/Batch NN, and
    any legacy Compliance Documents / Other / Overwrite / Batch XX folders). A
    sub-folder is removed
    once it holds no real files - any leftover hidden junk (desktop.ini,
    Thumbs.db, .DS_Store) is discarded with it. Real documents are never
    deleted; only emptied/junk-only folders are removed.
    Zip archives are EXTRACTED first (extract_worker_zips), so archived
    documents join the run instead of being silently skipped."""
    extract_worker_zips(worker_dir, log)
    moved = 0
    # 1) move every real (non-junk) file up to the worker folder. Program
    #    artefacts (e.g. '_overwrite_order.csv') are left exactly where they
    #    are - never moved, renamed, or deleted.
    for p in list(worker_dir.rglob("*")):
        if (p.is_file() and p.parent != worker_dir
                and not _is_junk(p) and not is_program_file(p)):
            dest = unique_path(worker_dir, p.stem, p.suffix)
            try:
                shutil.move(str(p), str(dest))
                moved += 1
            except Exception as e:
                log(f"    ! could not move {p.name}: {e}")

    # 2) remove every sub-folder, deepest first. A folder is deleted when no
    #    real files remain inside it; leftover junk is removed with it.
    subdirs = sorted([d for d in worker_dir.rglob("*") if d.is_dir()],
                     key=lambda d: len(d.parts), reverse=True)
    for d in subdirs:
        try:
            if not d.exists():
                continue
            leftover = _real_files_in(d)
            if leftover:
                names = ", ".join(p.name for p in leftover[:4])
                log(f"    ! kept '{d.name}' - still holds: {names}")
                continue
            # no real files: blow the folder away (clears desktop.ini etc.)
            shutil.rmtree(d, ignore_errors=True)
            if d.exists():
                # rmtree can fail silently on Windows if a file is read-only or
                # locked; retry after clearing read-only bits
                _force_rmtree(d, log)
            if d.exists():
                log(f"    ! could not delete '{d.name}' (folder is locked or in use)")
        except Exception as e:
            log(f"    ! could not remove folder '{d.name}': {e}")
    return moved


def _force_rmtree(d: Path, log=lambda m: None):
    """Clear read-only attributes and retry deletion (Windows-friendly)."""
    import stat
    def on_rm_error(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass
    try:
        shutil.rmtree(d, onerror=on_rm_error)
    except Exception as e:
        log(f"    ! force-delete failed for '{d.name}': {e}")


def file_hash(path: Path, chunk=1 << 20) -> str:
    """SHA-256 of a file's bytes (exact-copy detection)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# Magic-byte signatures for the formats we send. Used to verify that a file's
# *content* matches its extension before we render and upload it - so a
# mislabelled or corrupt file is skipped rather than sent.
_MAGIC = {
    b"%PDF": ".pdf",
    b"\x89PNG": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF8": ".gif",
    b"BM": ".bmp",
    b"II*\x00": ".tiff",
    b"MM\x00*": ".tiff",
    b"RIFF": ".webp",   # RIFF....WEBP
    b"PK\x03\x04": ".docx",  # docx/zip container
}

def sniff_kind(path: Path) -> str:
    """Return a coarse content kind from the leading bytes: 'pdf', 'image',
    'docx', 'text', or '' if unknown/unreadable. Defends against files whose
    extension does not match their real content."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except Exception:
        return ""
    if head.startswith(b"%PDF"):
        return "pdf"
    if (head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff")
            or head.startswith(b"GIF8") or head.startswith(b"BM")
            or head.startswith(b"II*\x00") or head.startswith(b"MM\x00*")
            or (head[:4] == b"RIFF" and b"WEBP" in head)):
        return "image"
    if head.startswith(b"PK\x03\x04"):
        return "docx"   # zip container (docx is a zip)
    # plain text heuristic: mostly printable
    try:
        head.decode("utf-8")
        return "text"
    except Exception:
        return ""


def file_too_big(path: Path, max_mb: float) -> bool:
    """True if the file exceeds the configured per-file size ceiling."""
    try:
        return path.stat().st_size > max_mb * 1024 * 1024
    except Exception:
        return False


def content_matches_ext(path: Path) -> bool:
    """True if the file's magic bytes are consistent with its extension family.
    Conservative: unknown kinds (e.g. .doc legacy, .txt) are allowed through."""
    ext = path.suffix.lower()
    kind = sniff_kind(path)
    if not kind:
        return True   # cannot tell - don't block (text/.doc legacy etc.)
    if ext in PDF_EXT:
        return kind == "pdf"
    if ext in IMG_EXT:
        return kind == "image"
    if ext == ".docx":
        return kind == "docx"
    return True


def _base_label(stem: str) -> str:
    """Strip a trailing ' (n)' dedup suffix so 'DBS (2)' -> 'DBS'."""
    return re.sub(r"\s*\(\d+\)\s*$", "", stem).strip().lower()


def dedup_worker(worker_dir: Path, log=lambda m: None):
    """Remove exact-duplicate documents in the worker folder, keeping one of
    each. Files are compared by exact byte content (SHA-256). Only files that
    share the same base name (ignoring a ' (n)' suffix) AND the same content are
    treated as duplicates - so 'DBS', 'DBS (1)', 'DBS (2)' collapse to one if
    their bytes match, while two unrelated files that happen to be identical are
    left alone for safety. Returns the number of files deleted.

    The kept copy is renamed to the clean base label when possible
    (e.g. the surviving 'Passport (2).pdf' becomes 'Passport.pdf')."""
    files = [p for p in worker_dir.iterdir()
             if p.is_file() and p.suffix.lower() in DOC_EXT
             and not is_program_file(p)]
    # group by (base label, extension)
    groups = {}
    for p in files:
        key = (_base_label(p.stem), p.suffix.lower())
        groups.setdefault(key, []).append(p)

    deleted = 0
    for (label, ext), members in groups.items():
        if len(members) < 2:
            continue
        # within the name-group, sub-group by content hash
        by_hash = {}
        for p in members:
            try:
                hsh = file_hash(p)
            except Exception as e:
                log(f"    ! could not read {p.name} for dedup: {e}")
                hsh = f"__unreadable__{p.name}"
            by_hash.setdefault(hsh, []).append(p)

        for hsh, copies in by_hash.items():
            if hsh.startswith("__unreadable__") or len(copies) < 2:
                continue
            # keep the one with the shortest/cleanest name (prefers 'DBS' over 'DBS (2)')
            copies.sort(key=lambda p: (len(p.name), natural_key(p.name)))
            keep = copies[0]
            for dup in copies[1:]:
                try:
                    dup.unlink()
                    deleted += 1
                    log(f"    x duplicate removed: {dup.name}  (kept {keep.name})")
                except Exception as e:
                    log(f"    ! could not delete duplicate {dup.name}: {e}")
    return deleted


def organize_worker(worker_dir: Path, log=lambda m: None, on_move=None):
    """Organise a worker folder after ranking into TWO sub-folders, split by
    whether a document needs Stage 3's individual overwrite-upload flow:
      - 'Overwrite Documents' : every document whose (base) controlled type is
                                in OVERWRITE_TYPES. Left loose inside the folder,
                                keeping any rank/date suffix on the filename (the
                                suffixes encode the upload order). Stage 3
                                uploads these ONE AT A TIME via the "Upload
                                document" + "Overwrite current document" flow.
      - 'Bulk'                : EVERY other document (all non-OVERWRITE_TYPES,
                                including 'Other' and any type not in the set),
                                split into 'Batch 01', 'Batch 02', ...
                                sub-folders of up to BATCH_SIZE (30) files each.

    Program artefacts (a leading-underscore or '..overwrite_order..' file) are
    never filed into either sub-folder - they are left loose and untouched.

    Returns dict: {"overwrite": int, "bulk": int}.
    """
    files = [p for p in worker_dir.iterdir()
             if p.is_file() and p.suffix.lower() in DOC_EXT
             and not is_program_file(p)]
    files.sort(key=lambda p: natural_key(p.name))
    if not files:
        return {"overwrite": 0, "bulk": 0}

    overwrite_files = [p for p in files if is_overwrite_type(p.stem)]
    bulk_files = [p for p in files if not is_overwrite_type(p.stem)]

    moved_overwrite = 0
    if overwrite_files:
        odir = worker_dir / "Overwrite Documents"
        odir.mkdir(exist_ok=True)
        for p in overwrite_files:
            dest = unique_path(odir, p.stem, p.suffix)
            try:
                shutil.move(str(p), str(dest))
                if on_move is not None:
                    on_move(p, dest)
                moved_overwrite += 1
            except Exception as e:
                log(f"    ! could not move {p.name} to Overwrite Documents: {e}")

    moved_bulk = 0
    if bulk_files:
        bdir = worker_dir / "Bulk"
        bdir.mkdir(exist_ok=True)
        for batch_idx, start in enumerate(range(0, len(bulk_files), BATCH_SIZE),
                                          start=1):
            chunk = bulk_files[start:start + BATCH_SIZE]
            batch_dir = bdir / f"Batch {batch_idx:02d}"
            batch_dir.mkdir(exist_ok=True)
            for p in chunk:
                dest = unique_path(batch_dir, p.stem, p.suffix)
                try:
                    shutil.move(str(p), str(dest))
                    if on_move is not None:
                        on_move(p, dest)
                    moved_bulk += 1
                except Exception as e:
                    log(f"    ! could not move {p.name} to {batch_dir.name}: {e}")

    return {"overwrite": moved_overwrite, "bulk": moved_bulk}


# ====================================================================
# PROCESSED-FILE MANIFEST  (skip already-reviewed files; avoid repeat billing)
# --------------------------------------------------------------------
# Keyed by file content hash. For each processed file we remember the model,
# resolution and the resulting name, so a re-run of the same care-home folder
# skips files that have not changed and were processed with the same settings.
# Stored inside the care-home folder itself so it travels with the documents.
# ====================================================================
MANIFEST_NAME = ".docreview_manifest.json"

def _write_hidden_json(path: Path, data: dict):
    """Write JSON to a file that is kept HIDDEN on Windows. Windows refuses to
    overwrite a hidden file via a normal open-for-write (PermissionError), so
    the hidden attribute is cleared first and re-applied after the write. On
    other platforms this is a plain write. Raises on failure (callers decide)."""
    if platform.system() == "Windows" and path.exists():
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)  # NORMAL
        except Exception:
            pass
    # Same-directory temp + os.replace means a crash cannot leave a truncated
    # batch/manifest/orientation state file behind.
    atomic_write_json(path, data)
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 2)  # HIDDEN
        except Exception:
            pass


class ProcessedManifest:
    def __init__(self, care_home_dir: Path):
        self.path = care_home_dir / MANIFEST_NAME
        self.data = {"version": 1, "entries": {}}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and "entries" in loaded:
                    self.data = loaded
        except Exception:
            traceback.print_exc()

    def save(self):
        try:
            # hidden-aware write: a plain write_text on an already-hidden file
            # fails with PermissionError on Windows and the cache would be lost
            _write_hidden_json(self.path, self.data)
        except Exception:
            traceback.print_exc()

    @staticmethod
    def _sig(model_id: str, resolution: float) -> str:
        return f"{model_id}@{float(resolution):.1f}"

    def seen(self, fhash: str, model_id: str, resolution: float) -> dict:
        """Return the stored entry if this exact content was already processed
        with the same model+resolution, else None."""
        e = self.data["entries"].get(fhash)
        if e and e.get("sig") == self._sig(model_id, resolution):
            return e
        return None

    def record(self, fhash: str, model_id: str, resolution: float,
               final_name: str, group: str):
        self.data["entries"][fhash] = {
            "sig": self._sig(model_id, resolution),
            "name": final_name, "group": group,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        }


# ====================================================================
# BATCH STATE FILE  (Overnight Batch mode - durable submit->apply record)
# --------------------------------------------------------------------
# Written into the care-home folder the moment a batch is submitted, so the app
# can be closed and reopened (even on another day) and still know which batch(es)
# are pending and how to apply their results. Records:
#   - the batch id(s) and submit timestamp + model/resolution/settings,
#   - the estimated cost (for reconciliation against real usage),
#   - a mapping custom_id -> {file path at submit, worker folder, content hash,
#     page count}, used to re-locate each file (by hash if it has since moved)
#     and file it through the SAME apply path as live mode.
# The manifest is updated only when a result is APPLIED, never at submit, so a
# crash between download and apply cannot mark files as done.
# ====================================================================
# ====================================================================
# JOB TRACKER + LIVE CHECKPOINT  (processing dashboard / crash recovery)
# ====================================================================
# The Engine (live runs) updates TRACKER as it works; the Jobs dashboard
# reads consistent snapshots. Batch runs are observed separately through
# BatchState + the Anthropic batch-status API (the platform DOES expose job
# status - processing_status + request_counts - so the dashboard uses it).
class JobTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self, mode="", care_home=""):
        with self._lock:
            self.data = {
                "mode": mode, "care_home": care_home,
                "started": time.time() if mode else 0.0,
                "workers_total": 0, "workers_done": 0,
                "current_worker": "", "status": "",
                "stats": {}, "finished": False, "note": "",
            }

    def update(self, **kw):
        with self._lock:
            self.data.update(kw)

    def snapshot(self) -> dict:
        with self._lock:
            snap = dict(self.data)
            snap["stats"] = dict(snap.get("stats") or {})
            return snap

    def eta_seconds(self):
        """Rough remaining time from the per-worker rate so far (None when
        unknowable)."""
        s = self.snapshot()
        done, total = s["workers_done"], s["workers_total"]
        if not s["started"] or done <= 0 or total <= 0 or done >= total:
            return None
        rate = (time.time() - s["started"]) / done
        return max(0.0, rate * (total - done))


TRACKER = JobTracker()

# A tiny per-care-home checkpoint written as each worker completes, so a
# crash / forced close is detectable on the next launch and the run can be
# offered for resume (re-running is cheap: the manifest cache already skips
# every file whose result was applied, so completed work is never re-billed).
LIVE_CHECKPOINT_NAME = ".docreview_live_checkpoint.json"


def write_live_checkpoint(care_dir: Path, done: int, total: int,
                          current: str, errors: int, finished: bool,
                          auto_review_run_id: str = "", audit_worker_dirs=(),
                          processing_complete: bool = False):
    try:
        p = Path(care_dir) / LIVE_CHECKPOINT_NAME
        _write_hidden_json(p, {
            "version": 2,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "workers_done": done, "workers_total": total,
            "current_worker": current, "errors": errors,
            "finished": finished,
            "auto_review_run_id": str(auto_review_run_id or ""),
            "audit_worker_dirs": [str(Path(p).resolve()) for p in audit_worker_dirs],
            "processing_complete": bool(processing_complete),
        })
        return True
    except Exception:
        return False


class LiveCheckpointWriteError(RuntimeError):
    """A review-enabled live run could not save its required restart link."""


def read_live_checkpoint(care_dir: Path):
    try:
        p = Path(care_dir) / LIVE_CHECKPOINT_NAME
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return None


def clear_live_checkpoint(care_dir: Path):
    try:
        (Path(care_dir) / LIVE_CHECKPOINT_NAME).unlink(missing_ok=True)
    except Exception:
        pass


BATCH_STATE_NAME = ".docreview_batch_state.json"
ORIENTATION_STATE_NAME = ".docreview_orientation_state.json"


class OrientationState:
    """Durable local-only page decisions and authorised rewrite hashes."""

    def __init__(self, care_home_dir: Path):
        self.path = Path(care_home_dir) / ORIENTATION_STATE_NAME
        self.data = {"version": 1, "entries": {}, "warnings": []}
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
                    self.data.setdefault("entries", {})
                    self.data.setdefault("warnings", [])
        except Exception:
            traceback.print_exc()

    @staticmethod
    def key(path: Path) -> str:
        try:
            return str(Path(path).resolve()).casefold()
        except Exception:
            return str(path).casefold()

    def entry(self, path: Path) -> dict:
        return self.data.setdefault("entries", {}).get(self.key(path), {})

    def put(self, path: Path, entry: dict):
        value = dict(entry or {})
        value["path"] = str(path)
        value["updated_ts"] = datetime.datetime.now().isoformat(
            timespec="seconds")
        self.data.setdefault("entries", {})[self.key(path)] = value
        self.save()

    def warning(self, path: Path, reason: str):
        item = {"path": str(path), "reason": str(reason),
                "ts": datetime.datetime.now().isoformat(timespec="seconds")}
        warnings = self.data.setdefault("warnings", [])
        if not any(w.get("path") == item["path"]
                   and w.get("reason") == item["reason"] for w in warnings):
            warnings.append(item)
            self.save()

    def move_path(self, old_path: Path, new_path: Path):
        old_key = self.key(old_path)
        entry = self.data.setdefault("entries", {}).pop(old_key, None)
        if entry is None:
            return
        entry["path"] = str(new_path)
        entry["updated_ts"] = datetime.datetime.now().isoformat(
            timespec="seconds")
        self.data["entries"][self.key(new_path)] = entry
        self.save()

    def move_tree(self, old_root: Path, new_root: Path):
        """Update every persisted file path after a completed folder move."""
        old_root = Path(old_root).resolve()
        new_root = Path(new_root).resolve()
        changed = False
        entries = self.data.setdefault("entries", {})
        for old_key, entry in list(entries.items()):
            try:
                relative = Path(entry.get("path", "")).resolve().relative_to(
                    old_root)
            except (OSError, ValueError):
                continue
            new_path = new_root / relative
            entries.pop(old_key, None)
            entry["path"] = str(new_path)
            entry["updated_ts"] = datetime.datetime.now().isoformat(
                timespec="seconds")
            entries[self.key(new_path)] = entry
            changed = True
        for warning in self.data.setdefault("warnings", []):
            try:
                relative = Path(warning.get("path", "")).resolve().relative_to(
                    old_root)
            except (OSError, ValueError):
                continue
            warning["path"] = str(new_root / relative)
            changed = True
        if changed:
            self.save()

    def authorised_hash(self, path: Path, original_hash: str,
                        current_hash: str) -> bool:
        """Narrow fallback for a rewrite explicitly persisted before replace."""
        entry = self.entry(path)
        return bool(entry
                    and entry.get("original_hash") == original_hash
                    and entry.get("current_hash") == current_hash
                    and entry.get("status") in (
                        "complete", "complete_recovered", "rewrite_failed"))

    def audit_rows(self) -> list:
        rows = []
        for entry in self.data.get("entries", {}).values():
            for page in entry.get("pages", []) or []:
                if (page.get("uncertain") or page.get("correction")
                        or page.get("warning")):
                    rows.append({
                        "File": entry.get("path", ""),
                        "Page": int(page.get("page", 0) or 0) + 1,
                        "Predicted orientation": page.get(
                            "predicted_orientation", ""),
                        "Correction indicated": page.get("correction", ""),
                        "Top confidence": page.get("confidence", ""),
                        "Runner-up confidence": page.get(
                            "runner_up_confidence", ""),
                        "Confidence margin": page.get("margin", ""),
                        "Action": ("rotated locally" if page.get("applied")
                                   else "unchanged"),
                        "Reason": page.get("reason", ""),
                    })
        for warning in self.data.get("warnings", []) or []:
            rows.append({"File": warning.get("path", ""), "Page": "",
                         "Predicted orientation": "",
                         "Correction indicated": "",
                         "Top confidence": "", "Runner-up confidence": "",
                         "Confidence margin": "", "Action": "unchanged",
                         "Reason": "local orientation warning: "
                                   + str(warning.get("reason", ""))})
        return rows

    def save(self):
        _write_hidden_json(self.path, self.data)

class BatchState:
    def __init__(self, care_home_dir: Path):
        self.dir = Path(care_home_dir)
        self.path = self.dir / BATCH_STATE_NAME
        self.data = {}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = loaded
        except Exception:
            traceback.print_exc()

    def exists(self) -> bool:
        if not self.data or self.data.get("applied", False):
            return False
        followup = self.data.get("followup") or {}
        primary_submit = self.data.get("primary_submission") or {}
        audit = self.data.get("audit") or {}
        return bool(self.data.get("batches") or followup.get("batches")
                    or followup.get("phase")
                    or primary_submit.get("status") in (
                        "submission_started", "ambiguous")
                    or self.data.get("primary_submission_complete") is False
                    or (self.data.get("processing_complete")
                        and audit.get("status") not in (
                            "complete", "skipped", "disabled")))

    def init(self, care_home: str, model_id: str, resolution: float,
             settings: dict):
        settings = dict(settings or {})
        self.data = {
            "version": 4,
            "care_home": care_home,
            "model_id": model_id,
            "resolution": float(resolution),
            "settings": settings,
            "submitted_ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "batches": [],          # [{"id","n","status_at_submit"}]
            "requests": {},         # custom_id -> {path, worker, worker_dir, fhash, pages}
            "est_gbp": 0.0,
            "est_input_tokens": 0,
            "est_output_tokens": 0,
            "phase": "primary_preparing",
            "primary_submission": {},
            # Added in v1.3.1.  Kept separate so a v1.3.0 state file (which has
            # only batches/requests) remains directly readable.
            "followup": {},
            "costs": {},
            "workers": {},
            "processing_complete": False,
            "audit": {"status": ("pending" if settings.get(
                "post_run_audit") else "disabled")},
            "applied": False,
        }

    def add_request(self, custom_id: str, path: Path, worker_dir: Path,
                    fhash: str, pages: int):
        self.data.setdefault("requests", {})[custom_id] = {
            "path": str(path), "worker": worker_dir.name,
            "worker_dir": str(worker_dir), "fhash": fhash, "pages": int(pages),
        }

    def add_batch(self, batch_id: str, n: int, status: str,
                  phase: str = "primary", request_ids=None):
        target = (self.data.setdefault("followup", {}).setdefault("batches", [])
                  if phase == "followup"
                  else self.data.setdefault("batches", []))
        record = {"id": batch_id, "n": int(n),
                  "status_at_submit": status}
        if request_ids is not None:
            record["request_ids"] = [str(item) for item in request_ids]
        target.append(record)

    def batch_ids(self, phase: str = "primary"):
        batches = ((self.data.get("followup") or {}).get("batches", [])
                   if phase == "followup" else self.data.get("batches", []))
        return [b.get("id") for b in batches if b.get("id")]

    def request_for(self, custom_id: str) -> dict:
        return self.data.get("requests", {}).get(custom_id)

    def followup_request_for(self, custom_id: str) -> dict:
        return ((self.data.get("followup") or {}).get("requests", {})
                .get(custom_id))

    def save(self):
        try:
            _write_hidden_json(self.path, self.data)
            return True
        except Exception:
            traceback.print_exc()
            return False

    def mark_applied(self):
        self.data["applied"] = True
        self.data["applied_ts"] = datetime.datetime.now().isoformat(timespec="seconds")
        self.save()

    def recovery_snapshot(self) -> Path:
        """Keep the exact pre-recovery state in a new, never-overwritten file."""
        if not self.path.is_file():
            raise RuntimeError("No durable batch state is available to back up")
        target = self.path.with_name(
            self.path.name + f".recovery-{time.time_ns()}.bak")
        with self.path.open("rb") as source, target.open("xb") as backup:
            shutil.copyfileobj(source, backup)
            backup.flush()
            os.fsync(backup.fileno())
        return target

    def delete(self):
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            traceback.print_exc()


class BatchWriterBusy(RuntimeError):
    """A different process owns the care-home's mutation lock."""


class CareHomeWriterLock:
    """App-facing wrapper around the shared engine/review-helper lock."""
    NAME = DOCUMENT_WRITER_LOCK

    def __init__(self, care_home_dir):
        self._lock = PathWriterLock(care_home_dir, self.NAME)
        self.path = self._lock.path

    def acquire(self):
        try:
            self._lock.acquire()
            return self
        except WriterLockBusy as exc:
            raise BatchWriterBusy(
                "Another Stage 2 operation is using this care-home folder, or "
                "the folder's writer lock is unavailable. Wait for the current "
                "operation to finish, then check batch status again.") from exc

    def release(self):
        self._lock.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_args):
        self.release()


def _care_home_writer_operation(method):
    """Serialize actual writes, while recovery assessment stays read-only."""
    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        recovery = method.__name__ == "recover_primary_submission"
        authorized = kwargs.get("allow_resubmit", args[0] if args else False)
        if recovery and not authorized:
            return method(self, *args, **kwargs)
        lock = CareHomeWriterLock(self.dir)
        try:
            lock.acquire()
        except BatchWriterBusy as exc:
            message = str(exc)
            self.log(message)
            self.on_done(self.stats, "batch_busy:" + message)
            if recovery:
                return {"status": "blocked", "message": message,
                        "remaining": 0, "busy": True}
            return None
        try:
            return method(self, *args, **kwargs)
        finally:
            lock.release()
    return guarded


def has_pending_batch(care_home_dir: Path) -> dict:
    """Return the pending batch-state dict for this care-home folder, or {} if
    there is none (no file, or it has already been fully applied)."""
    try:
        st = BatchState(care_home_dir)
        return dict(st.data) if st.exists() else {}
    except Exception:
        return {}


def worker_dirs_in(care_home_dir: Path):
    """Immediate sub-folders that look like worker folders. Excludes hidden /
    system folders and symlinked/junction directories to avoid wandering into
    unrelated or linked locations."""
    out = []
    try:
        for x in care_home_dir.iterdir():
            try:
                if not x.is_dir():
                    continue
                if x.is_symlink():
                    continue
                if x.name.startswith(".") or x.name.startswith("$"):
                    continue
                out.append(x)
            except Exception:
                continue
    except Exception:
        pass
    return sorted(out, key=lambda d: natural_key(d.name))


def has_existing_batches(care_home_dir: Path, cancel_event=None) -> int:
    """Detect whether this care-home folder was processed before. Since files
    are no longer batched into 'Batch XX' folders, this now checks the processed
    -file manifest stored in the folder: a non-empty manifest means a previous
    run already classified files here. Returns the number of remembered files
    (0 if the folder has not been processed before)."""
    try:
        mpath = care_home_dir / MANIFEST_NAME
        if not mpath.exists():
            return 0
        loaded = json.loads(mpath.read_text(encoding="utf-8"))
        entries = loaded.get("entries", {}) if isinstance(loaded, dict) else {}
        return len(entries)
    except Exception:
        return 0


def is_cloud_only_placeholder(p: Path) -> bool:
    """True if the file is a OneDrive/cloud 'online-only' placeholder whose bytes
    are not on disk. Reading such a file forces a download, which can hang a
    scan. We detect it via Windows file attributes and skip it during scanning.
    On non-Windows this is always False."""
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
        if attrs == -1:
            return False
        # FILE_ATTRIBUTE_OFFLINE = 0x1000
        # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
        # FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
        RECALL = 0x1000 | 0x400000 | 0x40000
        return bool(attrs & RECALL)
    except Exception:
        return False


def preflight_scan(care_home_dir: Path, max_file_mb: float,
                   cancel_event=None, progress_cb=None, scan_ext=None):
    """Walk the care-home folder WITHOUT calling the API and WITHOUT opening any
    file, returning a summary used for the confirmation screen.

    Cheap and cloud-safe by design: it only uses the path, extension, and
    stat() size. It does NOT sniff file content here (that would force OneDrive
    to download cloud-only files and could hang). Content/extension verification
    still happens later, per file, inside the engine where it actually matters.

    cancel_event : threading.Event - if set, the scan aborts and returns what it
                   has so far with cancelled=True.
    progress_cb  : optional callable(done_workers, total_workers, files_so_far)
                   called as each worker folder is finished, for live feedback.
    """
    workers = worker_dirs_in(care_home_dir)
    total = len(workers)
    eligible = 0
    total_bytes = 0
    oversized = []
    cloud_only = []
    cancelled = False
    # which extensions count as "will be sent to the API". When PDF conversion
    # is on this includes the source types that get converted first.
    exts = scan_ext if scan_ext else DOC_EXT

    for wi, w in enumerate(workers, 1):
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        try:
            for p in w.rglob("*"):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                try:
                    if not p.is_file() or p.is_symlink():
                        continue
                    if (p.suffix.lower() not in exts or _is_junk(p)
                            or is_program_file(p)):
                        continue
                    # size check uses stat() metadata only - does NOT download
                    try:
                        size = p.stat().st_size
                    except Exception:
                        continue
                    if size > max_file_mb * 1024 * 1024:
                        oversized.append(p)
                        continue
                    # cloud-only (OneDrive online-only) files are SKIPPED: we do
                    # not download them. Their paths are recorded so the user can
                    # see/copy them and make them available locally if wanted.
                    if is_cloud_only_placeholder(p):
                        cloud_only.append(p)
                        continue
                    eligible += 1
                    total_bytes += size
                except Exception:
                    continue
        except Exception:
            # a worker folder we cannot even enumerate - skip it, don't hang
            pass
        if progress_cb is not None:
            try:
                progress_cb(wi, total, eligible)
            except Exception:
                pass
        if cancelled:
            break

    return {"workers": total, "files": eligible,
            "bytes": total_bytes, "oversized": oversized,
            "cloud_only": cloud_only, "cancelled": cancelled}


def estimate_cost_gbp(n_files: int, model_id: str) -> float:
    """Very rough pre-flight £ estimate (NOT billing). Assumes ~1 call/file at
    average token sizes; real cost is tracked live from API usage.
    Retained for backward compatibility; the confirmation screen now uses the
    richer page/resolution-aware estimate_run_cost_gbp()."""
    m = MODELS_BY_ID.get(model_id, {"in": 1.0, "out": 5.0})
    usd = (n_files * EST_TOKENS_IN_PER_FILE / 1e6) * m["in"] + \
          (n_files * EST_TOKENS_OUT_PER_FILE / 1e6) * m["out"]
    return usd * FX_RATE[0]


# ====================================================================
# PAGE / RESOLUTION-AWARE COST ESTIMATOR  (Part 2)
# Estimates input tokens per document from the rendered image dimensions at the
# configured resolution (Anthropic vision ~= width*height/750 per image), plus
# the shared system+vocabulary prompt, the extracted-text block, and the small
# JSON output. Produces a Live-vs-Batch comparison for the confirmation screen
# and an accurate per-request figure for the batch submit-time budget gate.
# ====================================================================
def estimate_image_tokens(zoom: float) -> float:
    """Approximate vision tokens for ONE rendered A4 page image at `zoom`,
    honouring the same long-side pixel cap the renderer applies."""
    try:
        z = float(zoom)
    except Exception:
        z = 1.5
    # A4 in PDF points (595 x 842) scaled by the zoom factor.
    w, h = 595.0 * z, 842.0 * z
    cap = float(DocRender._max_px(z))
    longest = max(w, h)
    if longest > cap:
        s = cap / longest
        w *= s
        h *= s
    return (w * h) * VISION_TOKENS_PER_PIXEL


def estimate_classify_prefix_tokens(vocab_block: str) -> int:
    """Rough token count of the FIXED classify prompt prefix (system
    instructions + disambiguation rules + controlled vocabulary) that is
    identical for every document. Counted once when prompt caching is modelled.
    Uses the standard ~4-characters-per-token approximation."""
    chars = len(vocab_block or "") + len(DISAMBIGUATION_RULES) + 2200
    return max(1, chars // 4)


def _img_px_tokens_from_b64(b64: str) -> float:
    """Estimate vision tokens for an already-rendered base64 image, from its
    real pixel dimensions when Pillow is available, else from its byte size."""
    if HAS_PIL:
        try:
            from io import BytesIO
            im = Image.open(BytesIO(base64.b64decode(b64)))
            w, h = im.size
            return (w * h) * VISION_TOKENS_PER_PIXEL
        except Exception:
            pass
    # Fallback: assume PNG ~ 2 bytes/pixel, then apply the token formula.
    approx_px = (len(b64) * 3 / 4) / 2.0
    return approx_px * VISION_TOKENS_PER_PIXEL


def estimate_request_input_tokens(system: str, blocks: list) -> int:
    """Accurate-as-possible input-token estimate for a SINGLE built request
    (the shape ClaudeAPI._post sends). Sums the system prompt, every text block
    (~4 chars/token) and every image block (pixel-area formula). Used for the
    batch submit-time budget gate and end-of-run reconciliation."""
    total = len(system or "") / 4.0
    for b in blocks or []:
        btype = b.get("type")
        if btype == "text":
            total += len(b.get("text", "")) / 4.0
        elif btype == "image":
            data = (b.get("source", {}) or {}).get("data", "")
            total += _img_px_tokens_from_b64(data)
    return int(total)


def estimate_run_cost_gbp(n_files: int, model_id: str, zoom: float,
                          adaptive: bool, vocab_block: str = "",
                          batch: bool = False, include_second_pass: bool = True,
                          cached_prefix: bool = True) -> dict:
    """Page/resolution-aware £ estimate for a whole run. Returns a dict:
        {"gbp": float, "input_tokens": int, "output_tokens": int,
         "second_pass_gbp": float}
    - `batch`   : apply the 50% Message Batches discount.
    - `cached_prefix`: model prompt caching, i.e. charge the big shared
                  system+vocab prefix ONCE rather than per document (Live mode
                  has caching enabled; batch requests carry it harmlessly too).
    Assumptions are intentionally rough - real billing uses the API's own
    token counts."""
    m = MODELS_BY_ID.get(model_id, {"in": 1.0, "out": 5.0})
    n = max(0, int(n_files))
    prefix = estimate_classify_prefix_tokens(vocab_block)
    imgs_per_doc = EST_IMAGES_PER_DOC_ADAPTIVE if adaptive else EST_IMAGES_PER_DOC_FULL
    img_tokens = estimate_image_tokens(zoom) * imgs_per_doc
    per_doc_variable = img_tokens + EST_TEXT_TOKENS_PER_DOC

    if cached_prefix:
        # prefix billed roughly once (cache write ~1x here for simplicity),
        # the rest per document.
        input_tokens = prefix + n * per_doc_variable
    else:
        input_tokens = n * (prefix + per_doc_variable)
    output_tokens = n * EST_OUTPUT_TOKENS_PER_DOC

    discount = BATCH_DISCOUNT if batch else 1.0
    usd = ((input_tokens / 1e6) * m["in"] + (output_tokens / 1e6) * m["out"]) * discount

    # Second-pass reviews always run LIVE (standard price), even in batch mode.
    second_usd = 0.0
    if include_second_pass:
        sp_docs = n * EST_SECOND_PASS_FRACTION
        sp_in = sp_docs * (prefix * 0.0 + per_doc_variable)  # no full vocab in 2nd-pass prompts
        sp_out = sp_docs * 80
        second_usd = (sp_in / 1e6) * m["in"] + (sp_out / 1e6) * m["out"]

    return {
        "gbp": (usd + second_usd) * FX_RATE[0] if include_second_pass else usd * FX_RATE[0],
        "primary_gbp": usd * FX_RATE[0],
        "second_pass_gbp": second_usd * FX_RATE[0],
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
    }


def estimate_pipeline_costs_gbp(n_files: int, primary_model_id: str,
                                followup_model_id: str, zoom: float,
                                vocab_block: str = "", *, batch: bool,
                                include_audit: bool) -> dict:
    """Return separate, cumulative estimates for every enabled run phase.

    Batch classification and its small unresolved follow-up receive the Message
    Batches discount.  Dating/ranking/quality finishing calls and the optional
    accuracy audit remain live-priced.  Keeping these figures separate prevents
    a cheap headline batch estimate from hiding the rest of the run.
    """
    n = max(0, int(n_files))
    primary = estimate_run_cost_gbp(
        n, primary_model_id, zoom, adaptive=not batch,
        vocab_block=vocab_block, batch=batch, include_second_pass=False,
        cached_prefix=not batch)
    finishing = estimate_run_cost_gbp(
        n, primary_model_id, zoom, adaptive=False,
        vocab_block=vocab_block, batch=False, include_second_pass=True,
        cached_prefix=True)["second_pass_gbp"]

    followup_n = (max(1, int(math.ceil(n * EST_BATCH_FOLLOWUP_FRACTION)))
                  if batch and n else 0)
    followup = estimate_run_cost_gbp(
        followup_n, followup_model_id or primary_model_id, zoom,
        adaptive=False, vocab_block=vocab_block, batch=True,
        include_second_pass=False, cached_prefix=False)["primary_gbp"] \
        if followup_n else 0.0

    audit = 0.0
    if include_audit and n:
        audit_primary = estimate_run_cost_gbp(
            n, primary_model_id, zoom, adaptive=False,
            vocab_block=vocab_block, batch=False,
            include_second_pass=False, cached_prefix=True)["primary_gbp"]
        adjudication_n = max(
            1, int(math.ceil(n * EST_AUDIT_ADJUDICATION_FRACTION)))
        audit_adjudication = estimate_run_cost_gbp(
            adjudication_n, followup_model_id or primary_model_id, zoom,
            adaptive=False, vocab_block="", batch=False,
            include_second_pass=False, cached_prefix=True)["primary_gbp"]
        audit = audit_primary + audit_adjudication

    return {
        "primary_gbp": primary["primary_gbp"],
        "finishing_gbp": finishing,
        "followup_reserve_gbp": followup,
        "followup_reserve_files": followup_n,
        "audit_gbp": audit,
        "gbp": primary["primary_gbp"] + finishing + followup + audit,
        "input_tokens": primary["input_tokens"],
        "output_tokens": primary["output_tokens"],
    }


def tokens_cost_gbp(model_id: str, in_tokens: int, out_tokens: int,
                    batch: bool = False) -> float:
    """£ cost of a known number of input/output tokens for a model, optionally
    at the batch (half) price. Central helper so the discount lives in one
    place."""
    m = MODELS_BY_ID.get(model_id, {"in": 1.0, "out": 5.0})
    discount = BATCH_DISCOUNT if batch else 1.0
    usd = ((in_tokens / 1e6) * m["in"] + (out_tokens / 1e6) * m["out"]) * discount
    return usd * FX_RATE[0]


def estimate_runtime_seconds(n_files: int, sec_per_file: float) -> float:
    """Rough wall-clock estimate for the run, in seconds. Purely indicative -
    the real time depends on the machine, connection and (crucially) whether
    files are local or cloud-only on OneDrive."""
    try:
        return max(0.0, n_files * float(sec_per_file))
    except Exception:
        return 0.0


def human_duration(seconds: float) -> str:
    """Format a duration as e.g. '45 sec', '12 min', '2 hr 10 min', '1 day 3 hr'."""
    s = int(round(seconds))
    if s < 60:
        return f"{s} sec"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min" + (f" {s} sec" if s and m < 5 else "")
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} hr" + (f" {m} min" if m else "")
    d, h = divmod(h, 24)
    return f"{d} day" + ("s" if d != 1 else "") + (f" {h} hr" if h else "")


# ====================================================================
# SHARED CLASSIFICATION CORE  (used by the live Engine AND eval_classifier.py)
# --------------------------------------------------------------------
# Behaviour-preserving extraction of live mode's per-document decision:
#   render page 1 -> (adaptive) triage -> maybe render all -> classify.
# Kept as a module-level function so a regression harness can run the EXACT
# production classification path on a single file without the GUI/Engine.
# ====================================================================
# A classification whose reply reports the page as rotated triggers ONE retry
# with the page re-rendered in all four orientations (see below). The order of
# the copies is fixed so the reply's "rotation" field doubles as the exact
# correction to physically apply to the stored file (see fix_file_rotation).
ROTATION_RETRY_NOTE = (
    "NOTE: an earlier view suggested this page may be scanned rotated. The "
    "SAME first page is provided above in four orientations, IN THIS ORDER: "
    "copy 1 = as stored (0), copy 2 = turned 90 degrees clockwise, copy 3 = "
    "turned 180, copy 4 = turned 270 clockwise. Judge from whichever copy is "
    "upright and readable - the duplicates are the same single page, not "
    "extra pages. In the 'rotation' field return the number of the upright "
    "copy's turn (0, 90, 180 or 270): that is the clockwise rotation the "
    "STORED page needs to read upright.")

# ---- second opinion (model escalation) ----
# When the primary (cheap) model can't settle a document - no match, or
# confidence below this threshold - ONE follow-up call is made to a stronger
# model. Only the handful of hard documents per run pay the higher price.
SECOND_OPINION_MODEL_ID = "claude-sonnet-4-6"
SECOND_OPINION_MAX_CONF = 40


def _atomic_pdf_rotation_rewrite(path: Path, rotations: dict) -> int:
    """Apply per-page clockwise rotations through a same-directory temporary.

    The original is replaced only after PyMuPDF has closed a complete output
    file and it has been flushed to disk.  Any render/save/replace failure
    therefore leaves the original bytes intact.
    """
    rotations = {int(index): int(degrees)
                 for index, degrees in (rotations or {}).items()
                 if int(degrees or 0) in (90, 180, 270)}
    if not rotations or path.suffix.lower() not in PDF_EXT or not HAS_FITZ:
        return 0
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.stem}.orientation-", suffix=".tmp",
        dir=str(path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        # fitz.save requires a path that does not already hold a PDF.
        tmp.unlink()
        doc = fitz.open(str(path))
        try:
            changed = 0
            for index, degrees in rotations.items():
                if 0 <= index < len(doc):
                    page = doc[index]
                    page.set_rotation((page.rotation + degrees) % 360)
                    changed += 1
            if not changed:
                return 0
            doc.save(str(tmp))
        finally:
            doc.close()
        with open(tmp, "r+b") as handle:
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                # Some Windows filesystems reject fsync on an otherwise fully
                # closed/readable temp. Atomic replacement still preserves the
                # original on failure.
                pass
        os.replace(str(tmp), str(path))
        return changed
    except Exception:
        traceback.print_exc()
        return 0
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def fix_file_rotation(path: Path, deg_clockwise: int) -> bool:
    """Physically rotate a scanned document so it opens upright, saving the
    corrected file in place. `deg_clockwise` is the clockwise rotation needed
    to make the pages read upright (the classifier's 'rotation' field). All
    pages of a PDF are turned by the same amount - sideways scans are almost
    always uniformly rotated. Returns True when the file was rewritten.
    NOTE: rewriting changes the file's content hash - callers that track the
    hash (manifest/dedup) must recompute it afterwards."""
    if deg_clockwise not in (90, 180, 270):
        return False
    ext = path.suffix.lower()
    try:
        if ext in PDF_EXT and HAS_FITZ:
            total = DocRender.page_count(path)
            return _atomic_pdf_rotation_rewrite(
                path, {index: deg_clockwise for index in range(total)}) == total
        if ext in IMG_EXT and HAS_PIL:
            im = Image.open(path)
            fmt = im.format
            im = im.rotate(-deg_clockwise, expand=True)  # PIL is CCW-positive
            im.save(path, format=fmt, quality=95)
            return True
    except Exception:
        traceback.print_exc()
    return False


def detect_pdf_page_text_rotations(path: Path, max_pages: int = None,
                                   include_upright: bool = False) -> dict:
    """FREE local PER-PAGE orientation check for PDFs that carry a text
    layer: for each page, infer the clockwise rotation needed to make its
    text read upright from the dominant writing direction of the text lines
    (accounting for any /Rotate already set on the page). Returns
    {page_index: fix_degrees} for pages that unambiguously need turning;
    upright, image-only, near-empty or mixed-direction pages are simply
    absent. Per-page (rather than whole-document) because scans regularly
    mix upright and sideways/upside-down pages in one file."""
    out = {}
    if path.suffix.lower() not in PDF_EXT or not HAS_FITZ:
        return out
    try:
        doc = fitz.open(path)
        try:
            pages = list(doc) if max_pages is None else list(doc)[:max_pages]
            for idx, page in enumerate(pages):
                votes = {0: 0, 90: 0, 180: 0, 270: 0}
                prot = page.rotation or 0
                for block in page.get_text("dict").get("blocks", []):
                    for line in block.get("lines", []):
                        dx, dy = line.get("dir", (1, 0))
                        weight = sum(len(s.get("text", "") or "")
                                     for s in line.get("spans", []))
                        # raw-space writing direction -> clockwise fix needed
                        if dx > 0.7:
                            raw_fix = 0
                        elif dx < -0.7:
                            raw_fix = 180
                        elif dy > 0.7:      # text runs down the page: 90 CW
                            raw_fix = 270
                        elif dy < -0.7:     # text runs up the page: 270 CW
                            raw_fix = 90
                        else:
                            continue        # diagonal / decorative text
                        # the page may already carry a /Rotate that corrects
                        # (part of) this in display space
                        votes[(raw_fix - prot) % 360] += weight
                total = sum(votes.values())
                if total < 40:              # too little text to trust
                    continue
                best = max(votes, key=votes.get)
                if votes[best] >= 0.8 * total and (best or include_upright):
                    out[idx] = best
        finally:
            doc.close()
    except Exception:
        return {}
    return out


def detect_pdf_text_rotation(path: Path, max_pages: int = 3) -> int:
    """Whole-document convenience wrapper over the per-page check: the fix to
    apply to EVERY page, or 0 when the early pages don't agree/aren't
    readable. Kept for callers that rotate the file as one unit."""
    per_page = detect_pdf_page_text_rotations(path, max_pages=max_pages)
    if not per_page:
        return 0
    vals = set(per_page.values())
    return per_page[min(per_page)] if len(vals) == 1 else 0


def fix_pdf_page_rotations(path: Path, rotations: dict) -> int:
    """Physically rotate INDIVIDUAL pages of a PDF clockwise by the amounts
    in `rotations` ({page_index: 90|180|270}) and save in place. Returns the
    number of pages turned (0 = nothing done / not a PDF / error). Same
    hash-changes caveat as fix_file_rotation."""
    return _atomic_pdf_rotation_rewrite(path, rotations)


def _conf_int(result) -> int:
    """The reply's confidence as an int (the model occasionally returns it as
    a string or float)."""
    try:
        return int(float((result or {}).get("confidence", 0) or 0))
    except Exception:
        return 0


def _rot_of(result) -> int:
    """The reply's reported page rotation (0 when absent/unparseable)."""
    try:
        rot = int(float((result or {}).get("rotation", 0) or 0))
    except Exception:
        rot = 0
    return rot if rot in (90, 180, 270) else 0


ROTATION_CONFIRM_NOTE = (
    "NOTE: your previous view of this document reported it as scanned "
    "rotated. Every page above has now been TURNED UPRIGHT accordingly and "
    "re-rendered - read it as it now appears and classify it. In the "
    "'rotation'/'rotations' fields report only any turn STILL needed for what "
    "you can see now (0 when a page is now upright).")

# Confidence the corrected-orientation view must reach for its answer to be
# trusted. Below it the document falls through to the old four-orientation
# retry, which is dearer but makes no assumption about which way is up.
ROTATION_CONFIRM_MIN_CONF = 60

# WHICH ROTATION PATH IS PRIMARY - measured, and currently OFF. Read this
# before turning it on.
#
# The corrected-render confirmation (straighten locally from what the model
# just reported, then re-ask ONCE) is about a quarter of the four-orientation
# retry's images and measured 7.9% cheaper per rotated document. It is also
# LESS ACCURATE, and not by a little: over the 130-file Watra ground truth,
# the same 67 rotated documents scored
#     four-orientation retry   64/67
#     corrected-render         60/67
# and the confidence floor above cannot recover the difference, because the
# wrong answers are exactly as confident as the right ones - the seven misses
# came back at 92, 92, 92, 92, 92, 92 and 95, against a correct-answer
# distribution that is almost entirely 92-95. There is no threshold that
# catches the misses without sending correct answers to the dearer path too.
#
# Four compliance documents misnamed is not worth 7.9% of the rotation
# subset, so the proven path stays primary. The cheap path is kept, tested
# and one flag away for anyone who later finds a signal that separates its
# good answers from its bad ones (disagreement with the pre-rotation answer
# is the obvious candidate, and is NOT yet measured).
ROTATION_FAST_CONFIRM = False


def _rotation_retry_four(api, vocab, path, resolution, out, emit_cost=None):
    """LAST RESORT: re-ask with page 1 rendered in all four orientations in a
    single call, letting the model pick the upright copy. Four images for one
    page, so it is the dearest way to settle orientation - used only when the
    cheap corrected-render confirmation below could not."""
    rot_imgs = DocRender.render_page1_rotations(path, zoom=resolution)
    if len(rot_imgs) < 2:
        return out
    retry = api.classify(vocab, rot_imgs, out.get("used_text", ""),
                         note=ROTATION_RETRY_NOTE)
    if emit_cost:
        emit_cost()
    if retry:
        # the retry saw the SAME page in four orientations, so its per-page
        # 'rotations' list is meaningless - strip it so nothing downstream
        # tries to physically rotate pages from it ('rotation' keeps the
        # copy-number semantics defined in ROTATION_RETRY_NOTE)
        retry.pop("rotations", None)
        out["result"] = retry
        out["rotation_retried"] = True
        out["rotation_path"] = "four-orientation"
    return out


def _rotation_retry(api, vocab, path, resolution, out, emit_cost=None):
    """Settle a document the classifier reported as scanned rotated.

    The reply already says WHICH WAY each page is turned (per-page
    'rotations', or the whole-file 'rotation'). So instead of re-sending page 1
    four times and asking the model to pick the upright copy - four images for
    one page - straighten the render LOCALLY using what it just told us and
    re-ask ONCE, at the same page count as the original call. That is roughly a
    quarter of the old retry's images when it fires.

    The confirmation sees pages already turned by `fixes`, so its own rotation
    report is only the RESIDUAL turn still needed; the two are composed before
    anything is physically rotated. If the confirmation comes back unconvinced
    (below ROTATION_CONFIRM_MIN_CONF), the old four-orientation retry still
    runs - a rotated document that cannot be read is worse than a dear one."""
    res = out.get("result") or {}
    rot = _rot_of(res)
    if not rot:
        return out
    if not ROTATION_FAST_CONFIRM:
        # the proven path (see ROTATION_FAST_CONFIRM for the measurement)
        return _rotation_retry_four(api, vocab, path, resolution, out,
                                    emit_cost)
    idxs = list(out.get("page_idxs") or [0])
    # per-page corrections when the reply lines up with the pages shown,
    # otherwise the single whole-file rotation applied to every page
    fixes = {}
    rots = res.get("rotations")
    if isinstance(rots, list) and len(rots) == len(idxs):
        for i, d in zip(idxs, rots):
            try:
                d = int(float(d or 0))
            except Exception:
                continue
            if d in (90, 180, 270):
                fixes[i] = d
    if not fixes:
        fixes = {i: rot for i in idxs}
    # the first render was already straightened by the free text-layer check,
    # so the model's report is a turn ON TOP of that - compose the two or the
    # confirmation would undo the deskew it never saw
    pre = out.get("pre_rotations") or {}
    render_rot = {}
    for i in idxs:
        deg = (int(pre.get(i, 0) or 0) + fixes.get(i, 0)) % 360
        if deg in (90, 180, 270):
            render_rot[i] = deg
    imgs, text = DocRender.render(path, zoom=resolution, pages=idxs,
                                  rotate=render_rot, max_pages=MAX_SEG_PAGES)
    if len(imgs) != len(idxs):
        return _rotation_retry_four(api, vocab, path, resolution, out,
                                    emit_cost)
    confirm = api.classify(vocab, imgs, text, note=ROTATION_CONFIRM_NOTE,
                           page_idxs=idxs,
                           total_pages=out.get("total_pages"),
                           segment=bool(out.get("segment_view")))
    if emit_cost:
        emit_cost()
    if not confirm or _conf_int(confirm) < ROTATION_CONFIRM_MIN_CONF:
        return _rotation_retry_four(api, vocab, path, resolution, out,
                                    emit_cost)
    # compose what we turned with any residual the confirmation still reports,
    # so the stored file ends up upright in ONE physical rotation
    residual = {}
    crots = confirm.get("rotations")
    if isinstance(crots, list) and len(crots) == len(idxs):
        for i, d in zip(idxs, crots):
            try:
                d = int(float(d or 0))
            except Exception:
                continue
            if d in (90, 180, 270):
                residual[i] = d
    else:
        cr = _rot_of(confirm)
        if cr:
            residual = {i: cr for i in idxs}
    final = {}
    for i in idxs:
        deg = (fixes.get(i, 0) + residual.get(i, 0)) % 360
        if deg in (90, 180, 270):
            final[i] = deg
    confirm["rotations"] = [final.get(i, 0) for i in idxs]
    confirm["rotation"] = final.get(idxs[0], 0) if idxs else 0
    out["result"] = confirm
    out["used_imgs"], out["used_text"] = imgs, text
    out["rotation_retried"] = True
    out["rotation_path"] = "corrected-render"
    return out


def _second_opinion(escalation_api, vocab, path, resolution, out,
                    emit_cost=None):
    """If the (cheap) primary model could not settle the document - no match,
    or confidence below SECOND_OPINION_MAX_CONF - ask the stronger model ONCE.
    Its answer replaces the primary's: even when it is also unsure, that is a
    better-informed abstention. No-op when escalation is disabled/pointless
    (escalation_api is None) or the primary answer is confident."""
    if escalation_api is None:
        return out
    res = out.get("result") or {}
    # confidence alone decides: a CONFIDENT match=false ("this is clearly an
    # Other-group document") is a settled answer and must not escalate, or
    # every legitimate Other doc in a care-home folder would pay Sonnet price
    if _conf_int(res) >= SECOND_OPINION_MAX_CONF:
        return out
    note = ""
    rot = _rot_of(res)
    if rot:
        # the primary says the page is sideways: give the stronger model the
        # four-orientation view rather than re-sending the sideways pages
        imgs = DocRender.render_page1_rotations(path, zoom=resolution)
        text = out.get("used_text", "")
        note = ROTATION_RETRY_NOTE
    else:
        imgs, text = DocRender.render(path, zoom=resolution, pages="all",
                                      rotate=out.get("pre_rotations") or {})
    if not imgs and not text:
        return out
    retry = escalation_api.classify(vocab, imgs, text, note=note)
    if emit_cost:
        emit_cost()
    if retry:
        if rot:
            # four-orientation view: per-page 'rotations' would be nonsense
            retry.pop("rotations", None)
        out["result"] = retry
        out["escalated"] = True
    return out


def rescue_result(api, vocab, path, resolution, result, *, emit_cost=None,
                  escalation_api=None):
    """Run the post-classification rescue steps (rotation retry, then
    low-confidence second opinion) on an ALREADY-OBTAINED result. Used by the
    Overnight Batch apply path, where results arrive asynchronously and the
    live-mode rescue inside classify_document_core never had a chance to run.
    Both steps are no-ops on confident, upright results, so this costs nothing
    for the vast majority of documents.
    Returns the same dict shape as classify_document_core."""
    total = DocRender.page_count(path)
    seg_idxs, seg_ghosts, seg_full = segmentation_pages(path, total)
    out = {"result": result or {}, "used_imgs": [], "used_text": "",
           "rotation_retried": False, "escalated": False,
           "total_pages": total, "page_idxs": seg_idxs,
           "ghost_pages": sorted(seg_ghosts), "segment_view": seg_full,
           "possible_bundle": bool(total > 1 and not seg_full)}
    out = _rotation_retry(api, vocab, path, resolution, out, emit_cost)
    return _second_opinion(escalation_api, vocab, path, resolution, out,
                           emit_cost)


# ====================================================================
# GHOST (near-blank) PAGES
# --------------------------------------------------------------------
# Care-home scanners produce a huge number of near-empty versos: the blank
# back of a single-sided sheet, often carrying a faint mirror-image
# bleed-through of its own front. 26% of the pages in the 2026-08-11 Watra
# ground truth are such pages. They are worthless to the classifier, so they
# are dropped from the images sent to the API - which is what pays for the
# extra pages segmentation needs - but they are NEVER dropped from disk: a
# ghost page is attached to the preceding segment when a file is split.
#
# CALIBRATION (measured over all 544 GT pages, threshold = fraction of pixels
# darker than GHOST_INK_LEVEL on a small probe raster):
#   0.00%  - 139 pages, truly blank versos (the dominant cluster)
#   0.08%  - row 62 p2: a mirror-image bleed-through ghost of its own front
#   0.19%  - row 62 p3: a REAL, full Certificate of Sponsorship (faint scan)
#   0.7-6% - ordinary content pages
# The gap between the faintest real page and the densest ghost is only ~2.4x,
# so the threshold sits LOW and deliberately lets some ghosts through: sending
# a blank page wastes a fraction of a penny, dropping a real page loses a
# compliance document. Nothing is split on a ghost page's evidence anyway.
GHOST_INK_LEVEL = 160      # grey level below which a pixel counts as ink
GHOST_INK_FRAC  = 0.0005   # 0.05% of pixels - see calibration above
_GHOST_PROBE_PX = 220      # long side of the throwaway probe raster

# Segmentation only ever looks at a file it can see IN FULL. Beyond this many
# non-ghost pages the file is classified exactly as before and merely flagged
# 'possible bundle' - blind segmentation of a file the model only sampled is
# how a genuine 40-page contract gets cut in half.
#
# 7, not the 12 this started at, and the reason is measured. The only wrong
# boundary the bundle set produced was a 12-page ID bundle (8 non-ghost pages)
# where the model merged a National Insurance letter and a staff ID badge into
# one ten-page "Bank Statement" - at confidence 92, so no confidence floor
# would have caught it. Every bundle that segmented CORRECTLY had at most 5
# non-ghost pages. Past that the page map stops being reliable, and the design
# says a wrong cut is worse than no cut: over the cap the file is classified
# exactly as today and flagged for a human instead.
MAX_SEG_PAGES = 7

# A segment must be at least this confident to take part in a split. Above the
# vocabulary's own AUTO_REVIEW_MATCH_CONF (60) - naming a whole file at 60 is
# recoverable, cutting one is not - but not far above it: at 75 a correctly
# identified 'ID Badge' reported at 70 was blocking an otherwise perfect
# two-document split. Confidence guards the TYPE, not the boundary (see the
# 92-confidence mis-split above), so it does not need to carry more than that.
SEG_MIN_CONF = 70

# The helpers below remain available to the offline diagnostic harness, but the
# production path never invokes them automatically.  A long PDF is classified
# from the ordinary bounded sample and left intact; sampled evidence can only
# flag it for a human.  This restores the v1.2 cost ceiling.
LONG_BUNDLE_SCAN_ZOOM = 1.0
LONG_BUNDLE_SCAN_WINDOW = 10
LONG_BUNDLE_SCAN_OVERLAP = 2
LONG_BUNDLE_CONFIRM_CONF = 80

def _bundle_prone(result: dict) -> bool:
    """Whether sampled evidence explicitly describes multiple artefacts.

    This is intentionally generic: ordinary homogeneous document types are not
    enumerated.  A handbook, contract, statement or any other long single
    document therefore costs no all-page scan merely because of its category.
    """
    result = result if isinstance(result, dict) else {}
    docs = result.get("documents")
    if isinstance(docs, list) and len(docs) > 1:
        return True
    # The normal classification response already includes this boundary hint;
    # using it is free and must never trigger the retired all-page scan.
    starts = result.get("bundle_starts")
    if isinstance(starts, list) and any(
            str(value).strip() not in ("", "0", "1") for value in starts):
        return True
    for key in ("name", "other_label", "guess"):
        value = (result.get(key) or "").strip().lower()
        if not value:
            continue
        if any(term in value for term in (
                "bundle", "mixed documents", "multiple documents",
                "combined documents", "several documents")):
            return True
    return False


LONG_BUNDLE_SCAN_SYSTEM = (
    "You are checking whether one scanned worker-compliance PDF contains "
    "several separate documents concatenated together. Pages are shown in "
    "their original order and labelled with their real page numbers. Report "
    "a page only when it BEGINS a new artefact: for example a visa after a "
    "passport, a BRP after a visa, or a document for a different person. "
    "A continuation page, the reverse of the same card, a signature page, an "
    "appendix, or the next page of one contract is NOT a new document. Use "
    "names, document numbers, headings, page numbering and issuers as evidence. "
    "When uncertain, do not report a boundary; leaving a bundle whole is safer "
    "than cutting a real document. Never report page 1. Respond only as JSON: "
    "{\"multiple_documents\": true|false, \"starts\": [real page numbers "
    "that begin a new document], \"note\": \"short reason\"}."
)


def bundle_page_ranges(total_pages: int, starts) -> list:
    """Convert 1-based boundary pages into complete 0-based page ranges."""
    clean = set()
    for value in starts or []:
        try:
            n = int(value)
        except Exception:
            continue
        if 2 <= n <= total_pages:
            clean.add(n)
    bounds = [1] + sorted(clean) + [total_pages + 1]
    return [list(range(bounds[i] - 1, bounds[i + 1] - 1))
            for i in range(len(bounds) - 1)
            if bounds[i + 1] > bounds[i]]


def detect_long_bundle_starts(api, path: Path, *, emit_cost=None, log=None):
    """Return proposed 1-based boundaries after scanning every PDF page.

    Windows overlap by two pages so a boundary at a window edge is also seen
    with its preceding page.  A failed/misaligned render returns an empty list
    and therefore can never cause a cut.  Proposals are independently checked
    by Engine._confirm_long_bundle_plan before this method's output is acted on.
    """
    total = DocRender.page_count(path)
    if total < 2:
        return []
    rotations = detect_pdf_page_text_rotations(path) or {}
    starts = set()
    stride = max(1, LONG_BUNDLE_SCAN_WINDOW - LONG_BUNDLE_SCAN_OVERLAP)
    window_start = 0
    while window_start < total:
        window_end = min(total, window_start + LONG_BUNDLE_SCAN_WINDOW)
        want = list(range(window_start, window_end))
        imgs = []
        for offset in range(0, len(want), DocRender.MAX_PAGES):
            chunk = want[offset:offset + DocRender.MAX_PAGES]
            chunk_imgs, _ = DocRender.render(
                path, zoom=LONG_BUNDLE_SCAN_ZOOM, pages=chunk,
                rotate=rotations, max_pages=len(chunk))
            imgs.extend(chunk_imgs)
        if len(imgs) != len(want):
            if log:
                log("      long bundle scan skipped: one or more pages could "
                    "not be rendered safely")
            return []
        blocks = []
        for page_idx, b64 in zip(want, imgs):
            blocks.append({"type": "text",
                           "text": f"Page {page_idx + 1} of {total}:"})
            blocks.append(api._img_block(b64))
        blocks.append({
            "type": "text",
            "text": (f"Assess pages {want[0] + 1}-{want[-1] + 1}. "
                     "Only report a start when the immediately preceding page "
                     "is also visible; JSON only."),
        })
        raw = api._post(LONG_BUNDLE_SCAN_SYSTEM, blocks, max_tokens=300,
                        cache_system=True)
        if emit_cost:
            emit_cost()
        data = api._json_from(raw)
        for value in data.get("starts") or []:
            try:
                n = int(value)
            except Exception:
                continue
            # Reject the first page of any later window: there is no visible
            # preceding page in that framing. The overlap ensures it appeared
            # near the end of the previous window instead.
            first_shown = want[0] + 1
            if (2 <= n <= total and first_shown <= n <= want[-1] + 1
                    and not (window_start and n == first_shown)):
                starts.add(n)
        if window_end >= total:
            break
        window_start += stride
    out = sorted(starts)
    if log and out:
        log("      boundary scan proposed new document(s) at page "
            + ", ".join(map(str, out)))
    return out


def page_ink_fractions(path: Path) -> list:
    """Fraction of dark pixels on each page of a PDF (index = page number - 1).
    Cheap: every page is rasterised to a ~220px probe. Returns [] when the file
    is not a readable PDF or Pillow is missing (callers then treat no page as a
    ghost, i.e. today's behaviour)."""
    if path.suffix.lower() not in PDF_EXT or not HAS_FITZ or not HAS_PIL:
        return []
    out = []
    try:
        doc = fitz.open(path)
        try:
            from io import BytesIO
            for page in doc:
                try:
                    r = page.rect
                    if r.width <= 0 or r.height <= 0:
                        out.append(1.0)      # unmeasurable -> treat as content
                        continue
                    z = _GHOST_PROBE_PX / max(r.width, r.height)
                    pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
                    im = Image.open(BytesIO(pix.tobytes("png"))).convert("L")
                    w, h = im.size
                    if not (w and h):
                        out.append(1.0)
                        continue
                    dark = sum(im.point(lambda v: 1 if v < GHOST_INK_LEVEL else 0,
                                        mode="L").tobytes())
                    out.append(dark / float(w * h))
                except Exception:
                    out.append(1.0)          # never ghost a page we failed to read
        finally:
            doc.close()
    except Exception:
        return []
    return out


def ghost_pages(path: Path, inks=None) -> set:
    """0-based indices of the near-blank pages of a PDF. A file is never
    reported as ALL ghost - if every page measures blank the measurement is
    not to be trusted, so none are excluded."""
    inks = page_ink_fractions(path) if inks is None else inks
    if not inks:
        return set()
    g = {i for i, v in enumerate(inks) if v < GHOST_INK_FRAC}
    return set() if len(g) == len(inks) else g


def segmentation_pages(path: Path, total_pages: int, inks=None):
    """Decide which pages the classify call should see.

    Returns (page_idxs, ghosts, full_view):
      page_idxs  0-based pages to render, in order
      ghosts     0-based ghost pages (excluded from page_idxs)
      full_view  True when page_idxs is EVERY non-ghost page, i.e. the model
                 sees the whole file and its answer may be used to split it.
    When the file has more than MAX_SEG_PAGES non-ghost pages, falls back to
    today's sample (first two + last) and full_view is False."""
    if total_pages <= 1 or path.suffix.lower() not in PDF_EXT:
        return [0], set(), total_pages <= 1
    ghosts = ghost_pages(path, inks)
    keep = [i for i in range(total_pages) if i not in ghosts]
    sample = [0, 1, total_pages - 1] if total_pages > DocRender.MAX_PAGES \
        else list(range(total_pages))
    # ONLY change what is sent when segmentation can actually use it. Dropping
    # blank pages is not free: a request that differs from the one v1.1.0 made
    # is a request that can come back with a different name, and on the
    # borderline documents in these folders it measurably does. So a file that
    # cannot be segmented is sent EXACTLY as before.
    #
    # This costs the headline saving. Blank-page exclusion was reducing 47 of
    # the 130 ground-truth files to a SINGLE image - the two-sided sheet with
    # a blank back, which is the commonest shape in a care-home folder and the
    # source of nearly all the saving. It is also where the naming regressions
    # were: a lone page cannot be split, so that request was being perturbed
    # for no segmentation benefit at all.
    if len(keep) < 2 or len(keep) > MAX_SEG_PAGES:
        return sample, ghosts, False
    return keep, ghosts, True


def _seg_type_of(seg) -> str:
    return str((seg or {}).get("type") or "").strip()


def plan_segments(kb, result: dict, page_idxs: list, ghosts: set,
                  total_pages: int):
    """Turn the classifier's per-page `documents` map into a validated list of
    physical page ranges to split into, or None to leave the file whole.

    THE GATES (all must hold - a wrong split is worse than no split):
      1. the model saw the file in full (checked by the caller via full_view);
      2. at least 2 segments;
      3. at least 2 DISTINCT controlled types - a multi-page file of ONE type
         (a contract, an application form, two copies of the same check) is
         never split;
      4. every segment resolves to a real vocabulary type (or a confidently
         described Other document) at >= SEG_MIN_CONF;
      5. every segment holds at least one NON-GHOST page;
      6. no segment STARTS on a ghost page (a bleed-through verso is never
         evidence of a new document);
      7. the segments tile the file exactly once, in order, with no gaps or
         overlaps once ghost pages are attached to the segment before them.
    Returns [{"pages": [0-based...], "type": str, "date": str, "conf": int,
              "group": str, "matched": bool}] or None."""
    docs = result.get("documents")
    if not isinstance(docs, list) or len(docs) < 2:
        return None
    seen = set(page_idxs)
    starts = []      # (0-based first page, segment dict)
    for seg in docs:
        if not isinstance(seg, dict):
            return None
        pages = seg.get("pages")
        if not isinstance(pages, list) or not pages:
            return None
        nums = []
        for v in pages:
            try:
                n = int(v)
            except Exception:
                return None
            if not (1 <= n <= total_pages):
                return None
            nums.append(n - 1)
        # A segment made up ENTIRELY of blank pages is not a document - it is
        # the trailing verso the model listed for completeness because it was
        # asked to account for every page. Absorb it into the segment before
        # it (which is what happens anyway once the ranges are tiled below)
        # rather than refusing the whole split: a real 4-document bundle was
        # being left whole purely because the model politely accounted for its
        # last blank page.
        if all(p in ghosts for p in nums):
            continue
        first = min(nums)
        # gate 6: a segment holding real content may not BEGIN on a ghost page
        if first in ghosts:
            return None
        # gate 5: at least one page the model actually looked at
        if not any(p in seen for p in nums):
            return None
        starts.append((first, seg))
    if len(starts) < 2:
        return None
    starts.sort(key=lambda t: t[0])
    firsts = [f for f, _ in starts]
    if firsts[0] != 0 or len(set(firsts)) != len(firsts):
        return None          # must start at page 1, no duplicate starts

    out = []
    for k, (first, seg) in enumerate(starts):
        end = (starts[k + 1][0] - 1) if k + 1 < len(starts) else total_pages - 1
        if end < first:
            return None
        rng = list(range(first, end + 1))
        if all(p in ghosts for p in rng):
            return None      # gate 5
        conf = 0
        try:
            conf = int(float(seg.get("confidence", 0) or 0))
        except Exception:
            conf = 0
        if conf < SEG_MIN_CONF:
            return None      # gate 4
        raw = _seg_type_of(seg)
        if not raw:
            return None
        canon = kb.canonical_name(raw)
        if canon and canon != "Other":
            matched, name, group = True, canon, kb.group_of(canon)
        else:
            # an Other-group document is a legitimate segment, but only when
            # the model actually described it (never a bare 'Other'/'Unknown')
            low = raw.strip().lower()
            if low in ("", "other", "unknown", "other - unknown"):
                return None
            matched, name, group = False, raw, "Other"
        out.append({"pages": rng, "type": name, "group": group,
                    "matched": matched, "conf": conf,
                    "date": str(seg.get("document_date") or "").strip()})
    # gate 3: at least two DISTINCT types
    if len({_norm_type(s["type"]) for s in out}) < 2:
        return None
    # gate 7: the segments must tile the file exactly
    covered = [p for s in out for p in s["pages"]]
    if covered != list(range(total_pages)):
        return None
    return out


def classify_document_core(api, vocab, path, *, resolution, adaptive_pages,
                           p1_imgs=None, p1_text=None, total_pages=None,
                           emit_cost=None, escalation_api=None,
                           bundle_split=True):
    """Classify ONE document file through the live-mode path.

    p1_imgs/p1_text/total_pages may be passed in when the caller has already
    rendered page 1 (the Engine does, for its preview); otherwise they are
    rendered here. The document's FILENAME is never sent to the model: Stage 2
    renames files to its own previous guesses, so a wrong filename becomes
    self-reinforcing evidence on any re-run.
    `emit_cost` is called after every API call (the Engine uses it to update
    the live £ meter and enforce the budget ceiling). `escalation_api` is an
    optional stronger-model client for the low-confidence second opinion.
    When `bundle_split` is enabled, a short multi-page PDF which can be shown
    in full deliberately bypasses page-one triage.  Otherwise a confident
    passport on page 1 can hide a BRP or visa on page 2 before the bundle map
    is ever requested.

    Returns a dict:
      {"result": <classification dict>, "used_imgs": [...], "used_text": str,
       "total_pages": int, "page1_only": bool, "triaged": bool,
       "triage_reason": str, "rotation_retried": bool, "escalated": bool,
       "page_idxs": [0-based indices of the pages the model saw - lets the
                     caller map the reply's per-page 'rotations' list back to
                     real pages]}
    Raises APIError / CreditExhausted like the underlying calls."""

    def _finish(out):
        out = _rotation_retry(api, vocab, path, resolution, out, emit_cost)
        return _second_opinion(escalation_api, vocab, path, resolution, out,
                               emit_cost)

    def _require_evidence(o):
        # NEVER classify from nothing. With no page images and no extracted
        # text the model can only invent an answer, so the request is refused
        # before any API call and the caller records an explicit unreadable
        # outcome instead (encrypted/corrupt files used to come back with a
        # confident type and 'Pages Examined = 1').
        if not o["used_imgs"] and not (o["used_text"] or "").strip():
            raise UnreadableDocumentError(
                "no renderable pages or extractable text "
                f"(file reports {total_pages} page(s); possibly encrypted "
                "or corrupt)")

    def _all_idxs():
        # mirror of DocRender's pages='all' sampling policy
        if total_pages <= DocRender.MAX_PAGES:
            return list(range(max(total_pages, 1)))
        return [0, 1, total_pages - 1]

    if total_pages is None:
        total_pages = DocRender.page_count(path)
    # PAGE SELECTION. Near-blank versos are dropped (they teach the model
    # nothing) and, when what is left is small enough to show IN FULL, every
    # remaining page is sent instead of the old three-page sample. Measured
    # over the 130-file Watra ground truth that is a NET REDUCTION in images
    # sent (296 -> 281, median per file unchanged), because the ghost pages
    # removed outnumber the extra pages added - and it is what makes the
    # per-page documents map trustworthy enough to split on.
    seg_idxs, seg_ghosts, seg_full = segmentation_pages(path, total_pages)
    # PRE-CLASSIFICATION DESKEW. The free local text-direction check knows,
    # for any PDF carrying a text layer, exactly which pages are stored
    # sideways or upside down. It used to run only AFTER the answer came back
    # (Engine._maybe_fix_rotation), so the model was asked to read a rotated
    # page and only the reactive rotation retry could save it. Straighten the
    # RENDER first instead: it costs no API call, and a page the model can
    # read upright is a page it classifies from the content rather than the
    # shape. The stored file is still physically corrected later, by the
    # existing rotation-fix step.
    pre_rot = detect_pdf_page_text_rotations(path) or {}
    if pre_rot:
        p1_imgs = p1_text = None      # any caller-supplied page 1 is skewed
    if p1_imgs is None and p1_text is None:
        p1_imgs, p1_text = DocRender.render(path, zoom=resolution,
                                            pages="first", rotate=pre_rot)
    ext = path.suffix.lower()
    out = {"result": {}, "used_imgs": p1_imgs, "used_text": p1_text,
           "total_pages": total_pages, "page1_only": False,
           "triaged": False, "triage_reason": "", "rotation_retried": False,
           "escalated": False, "page_idxs": [0], "pre_rotations": pre_rot,
           "ghost_pages": sorted(seg_ghosts), "segment_view": False,
           "possible_bundle": False}

    def _render_selected(o):
        """Render the pages segmentation_pages chose, and decide whether the
        reply may be used to split. Segmentation is abandoned (silently, back
        to today's behaviour) whenever the rendered images do not line up
        one-for-one with the pages we asked for - _pdf drops a page it cannot
        bring under the API's size limits, and a mislabelled page map is
        exactly the evidence that produces a wrong boundary."""
        imgs, text = DocRender.render(path, zoom=resolution, pages=seg_idxs,
                                      rotate=pre_rot, max_pages=MAX_SEG_PAGES)
        aligned = len(imgs) == len(seg_idxs)
        if not aligned and seg_full:
            # fall back to the historic three-page sample
            imgs, text = DocRender.render(path, zoom=resolution, pages="all",
                                          rotate=pre_rot)
            o["page_idxs"] = _all_idxs()
        else:
            o["page_idxs"] = list(seg_idxs)
        o["used_imgs"], o["used_text"] = imgs, text
        # Only ask for the documents map when a split is actually POSSIBLE:
        # two or more pages worth looking at. On a file whose only readable
        # page is page 1 (a one-sided sheet with a blank back is the commonest
        # shape in these folders) the map can only ever say "one document",
        # so the extra instructions buy nothing - and measurably cost
        # something, having changed the answer on several such files.
        o["segment_view"] = bool(seg_full and aligned and total_pages > 1
                                 and len(o["page_idxs"]) >= 2)
        # a file too long to see in full may still be a bundle - say so rather
        # than segmenting on a sample
        o["possible_bundle"] = bool(total_pages > 1 and not o["segment_view"])

    # BUNDLE-SAFE ADAPTIVE PATH.  Page-one triage is an optimisation for a
    # single document; it cannot prove that later pages do not begin a second
    # document.  When the entire useful short PDF fits in one classification
    # call, skip that triage call and ask for the page map immediately.  This
    # both closes the recurring passport+visa+BRP miss and avoids paying for a
    # triage call immediately followed by classification.
    if (bundle_split and adaptive_pages and ext in PDF_EXT
            and total_pages > 1 and seg_full and len(seg_idxs) >= 2):
        _render_selected(out)
        if out["segment_view"]:
            out["triage_reason"] = "short file shown in full for bundle safety"
            _require_evidence(out)
            out["result"] = api.classify(
                vocab, out["used_imgs"], out["used_text"],
                page_idxs=out["page_idxs"], total_pages=total_pages,
                segment=True)
            if emit_cost:
                emit_cost()
            return _finish(out)

    if adaptive_pages and ext in PDF_EXT and total_pages > 1:
        if p1_imgs or (p1_text or "").strip():
            tri = api.triage(vocab, p1_imgs[0] if p1_imgs else "",
                             p1_text, total_pages)
            if emit_cost:
                emit_cost()
            out["triaged"] = True
            enough = bool(tri.get("enough_from_page1"))
            tconf = tri.get("confidence", 0)
            tmatch = bool(tri.get("match"))
            if enough and tmatch and tconf >= 80 and (tri.get("name") or "").strip():
                out["result"] = tri  # page 1 was sufficient
                out["page1_only"] = True
                return _finish(out)
            out["triage_reason"] = str(tri.get("reason", ""))
        else:
            # page 1 alone gave no evidence (bad first page / oversized
            # render): skip the triage call - it would be asked about
            # nothing - and judge readability from the full page selection
            out["triage_reason"] = "page 1 yielded no image or text"
        _render_selected(out)
        _require_evidence(out)
        out["result"] = api.classify(vocab, out["used_imgs"], out["used_text"],
                                     page_idxs=out["page_idxs"],
                                     total_pages=total_pages,
                                     segment=out["segment_view"])
        if emit_cost:
            emit_cost()
        return _finish(out)
    # single page / image / adaptive off: classify page 1
    # (for single-page docs page 1 IS the whole doc)
    if ext in PDF_EXT and total_pages > 1 and not adaptive_pages:
        _render_selected(out)
    _require_evidence(out)
    out["result"] = api.classify(vocab, out["used_imgs"], out["used_text"],
                                 page_idxs=out["page_idxs"],
                                 total_pages=total_pages,
                                 segment=out["segment_view"])
    if emit_cost:
        emit_cost()
    return _finish(out)


def validate_result(kb, result):
    """Validate ONE classification result dict against the live vocabulary.
    Shared by Engine._apply_classification and eval_classifier.py so both
    interpret the model's answer identically.

    The returned name is ALWAYS an exact controlled-vocabulary name: a
    case/spacing variant is snapped to the canonical spelling, and a name that
    is not in the vocabulary at all makes the result unmatched (an earlier,
    permissive version accepted any name as long as `group` looked valid -
    which is how invented names like 'use of own care declaration' ended up
    on real files).
    Returns (matched, name, group, conf, features, other_label)."""
    matched = bool(result.get("match"))
    conf = result.get("confidence", 0)
    name = (result.get("name") or "").strip()
    group = (result.get("group") or "").strip()
    features = (result.get("features") or "").strip()
    other_label = (result.get("other_label") or "").strip()
    if matched and name:
        canon = kb.canonical_name(name)
        if canon:
            name = canon
            group = kb.group_of(canon)
        else:
            # not a controlled name: route through the unknown/Other flow,
            # remembering the model's wording as the descriptive label
            matched = False
            if not other_label:
                other_label = name
    # SNAP-TO-CONTROLLED: when the model answers match=false but DESCRIBES the
    # document with (exactly) a controlled type's name in other_label - e.g.
    # other_label 'Bank statement' while 'Bank Statement' is in the vocabulary
    # - it has identified the type and merely failed to connect it to the
    # list. Confidently described documents are filed under the controlled
    # type instead of a lookalike 'Other - <label>'.
    if (not matched) and other_label \
            and _conf_int(result) >= AUTO_REVIEW_MATCH_CONF:
        canon = kb.canonical_name(other_label)
        if canon and canon != "Other":
            matched = True
            name = canon
            group = kb.group_of(canon)
    return matched, name, group, conf, features, other_label


# Confidence floors for the automatic unknown re-review (no human in the
# loop, so both are deliberately conservative): a controlled name is accepted
# at >= 60; a mere descriptive relabel ('Other - <label>') at >= 40.
AUTO_REVIEW_MATCH_CONF = 60
AUTO_REVIEW_LABEL_CONF = 40

_GENERIC_OTHER_LABELS = {
    "", "unknown", "other", "unidentified", "unclassified", "n/a", "none",
    "document", "other document", "unknown document", "unidentified document",
    # File/container formats are not document identifications. Descriptions
    # that add real subject matter remain meaningful (for example
    # "customer experience email" or "P60 form").
    "email", "e-mail", "letter", "form", "scan", "scanned document",
    "screenshot", "pdf", "pdf document", "image", "photo", "photograph",
}


def meaningful_other_label(result: dict) -> str:
    """Return a specific primary-batch Other description, or an empty string.

    `other_label` is authoritative, with `guess` and an invented unmatched
    `name` as fallbacks.
    Generic abstentions are deliberately unresolved and therefore eligible for
    the single discounted follow-up batch.
    """
    if not isinstance(result, dict):
        return ""
    for value in (result.get("other_label"), result.get("guess"),
                  result.get("name")):
        label = str(value or "").strip()
        label = re.sub(r"^\s*other\s*[-:]\s*", "", label,
                       flags=re.I).strip(" -")
        if label and label.casefold() not in _GENERIC_OTHER_LABELS:
            return label
    return ""


def batch_result_needs_followup(kb, result: dict) -> bool:
    """Whether a first-batch answer is genuinely unresolved.

    Confident canonical matches (including snap-to-controlled matches) and
    confident descriptive Other labels settle immediately.  Malformed,
    generic, blank and low-confidence answers get exactly one discounted
    follow-up request.
    """
    if not isinstance(result, dict) or not result:
        return True
    # A model claiming match=true while inventing a noncanonical name has not
    # made a controlled-vocabulary match. Give it the one discounted follow-up
    # instead of accepting its wording as a descriptive Other.
    raw_name = str(result.get("name") or "").strip()
    if bool(result.get("match")) and raw_name \
            and not kb.canonical_name(raw_name):
        return True
    matched, name, _group, _conf, _features, _other = validate_result(kb, result)
    conf = _conf_int(result)
    if matched and name and name != "Other":
        return conf < AUTO_REVIEW_MATCH_CONF
    return not (conf >= AUTO_REVIEW_LABEL_CONF
                and bool(meaningful_other_label(result)))


def resolve_auto_review(kb, result):
    """Decide the new filename for an automatically re-reviewed
    'Other - Unknown' document. Shared by the batch-apply auto-review pass
    and the standalone rescue script so both apply the same policy.
    Returns (new_name, group) - new_name is a final filename stem (Other
    names already wrapped as 'Other - <x>') - or (None, None) to leave the
    file as 'Other - Unknown'."""
    matched, name, group, conf, features, other_label = \
        validate_result(kb, result)
    conf = _conf_int(result)
    # the literal vocab entry "Other" is not an identification - fall through
    # to the descriptive-label branch (guards against 'Other - Other' names)
    if matched and name and name != "Other" and conf >= AUTO_REVIEW_MATCH_CONF:
        if group == "Other":
            return other_name(name), "Other"
        return name, group
    if conf >= AUTO_REVIEW_LABEL_CONF:
        label = meaningful_other_label(result)
        if label:
            return other_name(label), "Other"
    return None, None


# ====================================================================
# POST-RUN ACCURACY AUDIT  (optional Settings toggle)
# --------------------------------------------------------------------
# After a run finishes, every renamed document can be re-checked: a blind
# re-classification (FULL pages - the first-pass weakness was judging from
# page 1 alone) compared against the filename, with every prospective
# mismatch ADJUDICATED by the stronger model before it is flagged (a
# disagreement between two blind looks is not evidence the filename is
# wrong; the adjudicator sees both names and all the pages). Produces
# Filename_Audit_Report.xlsx in the care-home folder. AUDIT ONLY - no
# renaming; the only physical change is straightening pages it is
# confident are rotated (per page).
# ====================================================================
AUDIT_REPORT_STEM = "Filename_Audit_Report"
AUDIT_COLUMNS = [
    "Current Filename", "Suggested Filename", "Full File Path",
    "Confidence Score", "Confidence Band", "Issue Severity", "Document Type",
    "Property Address", "Client Name", "Project Name", "Document Date",
    "Reason For Concern", "Evidence Found", "Missing Information",
    "Filename Components Compared", "Review Status", "Pages Examined", "Notes",
]


def _confidence_band(score: int) -> str:
    if score >= 95:
        return "Very High"
    if score >= 80:
        return "High"
    if score >= 60:
        return "Medium"
    if score >= 40:
        return "Low"
    return "Very Low"


def _audit_pages_for(path: Path):
    """The page indices DocRender's pages='all' policy renders, so per-page
    rotation reports can be mapped back to real page numbers."""
    total = DocRender.page_count(path)
    if total <= DocRender.MAX_PAGES:
        return list(range(total))
    return [0, 1, total - 1]


def audit_adjudicate(api, vocab, path, resolution, current_base,
                     candidate_base):
    """Adjudicate a prospective audit flag with the stronger model: it sees
    the document's pages (first pages + LAST), the name the file currently
    carries AND the blind re-classification's differing answer, and decides
    which (if either) is right. Returns a dict:
      {"verdict": filename_correct|suggestion_correct|another_type|unsure,
       "correct_name": str, "other_label": str, "confidence": int,
       "rotations": [deg per provided page], "page_idxs": [...],
       "reason": str}"""
    imgs, text = DocRender.render(path, zoom=resolution, pages="all")
    idxs = _audit_pages_for(path)
    system = (
        "You are ADJUDICATING a filename-accuracy dispute for a UK "
        "care-sector compliance document. The file is currently FILED under "
        "one name; a blind re-classification suggested a DIFFERENT name. "
        "Examine every provided page (the last page of the document is "
        "included) and decide which name is right - or that neither is, or "
        "that the pages do not allow a confident call. Judge only from the "
        "document's content and the controlled vocabulary.\n\n"
        + DISAMBIGUATION_RULES +
        "\nRespond with ONLY a JSON object:\n"
        '{"verdict": "filename_correct" | "suggestion_correct" | '
        '"another_type" | "unsure", '
        '"correct_name": "<when another_type: the right vocabulary name, '
        'copied EXACTLY - else empty>", '
        '"other_label": "<when the right answer is an Other document with no '
        'vocabulary name: a SHORT specific description - else empty>", '
        '"confidence": 0-100 - confidence in your verdict, '
        '"rotations": [<one entry PER PROVIDED PAGE IMAGE, in order: 0, 90, '
        "180 or 270 - the clockwise degrees THAT page must be turned to read "
        'upright>], '
        '"reason": "<one short sentence citing the deciding evidence and the '
        'page it is on>"}'
    )
    blocks = [api._img_block(b) for b in imgs[:DocRender.MAX_PAGES]]
    ctx = (f"CONTROLLED VOCABULARY:\n{vocab}\n\n"
           f"CURRENTLY FILED AS: \"{current_base}\"\n"
           f"BLIND RE-CLASSIFICATION SUGGESTED: \"{candidate_base}\"\n"
           f"PAGES PROVIDED (0-based indices of the document): {idxs}\n\n")
    if text:
        ctx += f"EXTRACTED TEXT:\n{text[:5000]}\n\n"
    ctx += "Adjudicate now. JSON only."
    blocks.append({"type": "text", "text": ctx})
    d = api._json_from(api._post(system, blocks, max_tokens=600,
                                 cache_system=True))
    rot = d.get("rotations") or []
    return {
        "verdict": str(d.get("verdict", "unsure")).strip().lower(),
        "correct_name": (d.get("correct_name") or "").strip(),
        "other_label": (d.get("other_label") or "").strip(),
        "confidence": _conf_int(d),
        "rotations": rot if isinstance(rot, list) else [],
        "page_idxs": idxs,
        "reason": (d.get("reason") or "").strip(),
    }


def run_accuracy_audit(api, adjudicator_api, kb, worker_dirs, out_dir, *,
                       resolution, log=lambda m: None,
                       emit_cost=None, check_stop=lambda: None,
                       orientation_rows=None, on_progress=None):
    """Audit every document under `worker_dirs`; write the workbook into
    `out_dir`. Returns (rows, xlsx_path). See the section comment above for
    the method. Flags are conservative: nothing is flagged unless the
    adjudicator confidently agrees the filename is wrong."""
    vocab = kb.vocabulary_block()
    docs = []
    for w in worker_dirs:
        w = Path(w)
        if not w.is_dir():
            continue
        docs += sorted(p for p in w.rglob("*")
                       if p.is_file() and p.suffix.lower() in DOC_EXT
                       and not is_program_file(p))
    log(f"  [audit] re-checking {len(docs)} document(s) - full pages, "
        f"adjudicated flags")
    rows = []
    n_flag = n_rot = 0
    def progress(state, completed, path=None, worker="", report=""):
        if on_progress:
            try:
                on_progress({"kind":"audit_progress", "phase":"audit",
                    "state":state, "completed":completed, "total":len(docs),
                    "needs_review":sum(r["Review Status"] not in ("Correct", "Custom Name") for r in rows),
                    "errors":sum(str(r.get("Notes", "")).startswith("error:") for r in rows),
                    "path":str(path or ""), "worker":worker, "report":str(report or "")})
            except Exception:
                # Presentation/notification failures must never alter results.
                log("  ! Audit progress display could not be refreshed.")
    progress("started", 0)
    for i, p in enumerate(docs, 1):
        check_stop()
        worker = ""
        for w in worker_dirs:
            try:
                p.relative_to(Path(w))
                worker = Path(w).name
                break
            except ValueError:
                continue
        progress("checking", i - 1, p, worker)
        fname_base = _base_label(base_controlled_name(p.stem))
        row = {c: "" for c in AUDIT_COLUMNS}
        row.update({
            "Current Filename": p.name, "Full File Path": str(p),
            "Client Name": worker,
            "Property Address": "N/A (care compliance doc)",
            "Project Name": "N/A",
            "Filename Components Compared": "document type",
            "Review Status": "Correct", "Confidence Score": 0,
            "Confidence Band": "Very Low",
        })
        notes = []
        try:
            core = classify_document_core(
                api, vocab, p, resolution=resolution, adaptive_pages=False,
                emit_cost=emit_cost, escalation_api=None)
            result = core["result"]
            pred_name, pred_group = resolve_auto_review(kb, result)
            pred_base = pred_name or "Other - Unknown"
            row["Document Type"] = pred_base
            row["Evidence Found"] = (result.get("features") or "")[:400]
            row["Pages Examined"] = len(core.get("used_imgs") or []) or 1

            fn_other = fname_base.strip().lower().startswith("other")
            pr_other = (pred_group == "Other") or \
                pred_base.strip().lower().startswith("other")
            same = _norm_type(pred_base) == _norm_type(fname_base)
            # CUSTOM HUMAN FILENAMES: a name that is neither a controlled
            # type nor 'Other - ...' (e.g. 'J Smith - Payslip 31 Jan 2026')
            # was chosen by a person deliberately - it can never 'match' a
            # classification, so flagging it just generates noise (every
            # such flag in real reviews was rejected). Record what the
            # content looks like and move on without flagging.
            custom_name = not (fn_other or kb.canonical_name(fname_base))
            if custom_name and not same:
                row["Review Status"] = "Custom Name"
                row["Notes"] = (f"custom filename (kept); content reads as "
                                f"'{pred_base}'")
                rows.append(row)
                progress("document_done", i, p, worker)
                if i % 25 == 0:
                    log(f"  [audit] {i}/{len(docs)} checked "
                        f"({n_flag} flagged so far)")
                continue
            if not (same or (fn_other and pr_other)):
                # prospective mismatch -> adjudicate before flagging
                progress("adjudicating", i - 1, p, worker)
                adj = audit_adjudicate(adjudicator_api, vocab, p, resolution,
                                       fname_base, pred_base)
                if emit_cost:
                    emit_cost()
                notes.append("adjudicated")
                # Rotation is intentionally NOT applied from this paid audit.
                # The separate local all-page preflight is the sole authority.
                v, conf = adj["verdict"], adj["confidence"]
                if v == "filename_correct" or conf < 60 or v == "unsure":
                    if v == "filename_correct":
                        notes.append("first pass disagreed; adjudicator "
                                     "confirmed the filename - not flagged")
                        row["Document Type"] = fname_base
                    else:
                        row["Review Status"] = "Unable To Determine"
                        row["Confidence Score"] = min(conf, 55)
                        row["Confidence Band"] = _confidence_band(
                            row["Confidence Score"])
                        row["Missing Information"] = (
                            "Two AI passes disagreed but the adjudicator "
                            "could not settle it; needs a human look.")
                        row["Reason For Concern"] = adj["reason"]
                else:
                    if v == "another_type" and (adj["correct_name"]
                                                or adj["other_label"]):
                        canon = kb.canonical_name(adj["correct_name"])
                        best = (other_name(canon) if canon and
                                kb.group_of(canon) == "Other" else canon) \
                            or other_name(adj["other_label"])
                    else:
                        best = pred_base
                    n_flag += 1
                    row["Document Type"] = best
                    row["Suggested Filename"] = f"{best}{p.suffix}"
                    row["Confidence Score"] = conf
                    row["Confidence Band"] = _confidence_band(conf)
                    row["Review Status"] = ("Likely Misnamed" if conf >= 80
                                            else "Possibly Misnamed")
                    crit = (is_overwrite_type(fname_base)
                            or is_overwrite_type(best))
                    row["Issue Severity"] = (
                        "Critical" if crit else
                        ("Major" if conf >= 80 else "Minor"))
                    row["Reason For Concern"] = (
                        f"Filed as '{fname_base}' but two independent AI "
                        f"passes read the content as '{best}'. "
                        f"{adj['reason']}")
        except (StopRequested, LimitReached, CreditExhausted):
            raise
        except UnreadableDocumentError as e:
            # An unreadable file must surface as an explicit zero-page error
            # row - never as 'Custom Name'/'Correct' with invented evidence -
            # so the all-flags review queue always includes it.
            row["Review Status"] = "Unable To Determine"
            row["Pages Examined"] = 0
            row["Missing Information"] = ("Document could not be read; needs "
                                          "a readable source or a password.")
            row["Notes"] = f"error: {e}"
        except Exception as e:
            row["Review Status"] = "Unable To Determine"
            row["Notes"] = f"error: {e}"
        row["Notes"] = "; ".join(n for n in [row.get("Notes", "")] + notes
                                 if n)
        rows.append(row)
        progress("document_done", i, p, worker)
        if i % 25 == 0:
            log(f"  [audit] {i}/{len(docs)} checked "
                f"({n_flag} flagged so far)")
    # Processing reports live in the dedicated C: reports tree
    # (%LOCALAPPDATA%\Lifted\Reports\Processing Reports\<care home>) — NOT in
    # the care-home data folder (2026-07-22 suite modernisation).
    progress("writing_report", len(rows), docs[-1] if docs else None)
    rep_dir = processing_reports_dir(Path(out_dir).name)
    xlsx = unique_path(rep_dir, AUDIT_REPORT_STEM, ".csv")
    _write_audit_workbook(rows, xlsx,
                          orientation_rows=orientation_rows or [])
    # register it so the ribbon's Reports browser can find it later
    record_processing_report(xlsx, Path(out_dir).name)
    progress("complete", len(rows), docs[-1] if docs else None, report=xlsx)
    log(f"  [audit] done: {len(rows)} checked, {n_flag} flagged, "
        f"{n_rot} file(s) straightened -> {xlsx.name}")
    return rows, xlsx


def _write_audit_workbook(rows, out_path: Path, orientation_rows=None):
    """Write the audit rows + a Summary sheet, flagged rows first."""
    if Path(out_path).suffix.lower() == ".csv":
        return _write_audit_csv(rows, Path(out_path), orientation_rows)
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    order = {"Likely Misnamed": 0, "Possibly Misnamed": 1,
             "Unable To Determine": 2, "Correct": 3}
    rows = sorted(rows, key=lambda r: (order.get(r["Review Status"], 9),
                                       -int(r["Confidence Score"] or 0)))
    wb = Workbook()
    ws = wb.active
    ws.title = "Audit"
    fills = {"Likely Misnamed": PatternFill("solid", fgColor="F8CBAD"),
             "Possibly Misnamed": PatternFill("solid", fgColor="FFE699"),
             "Unable To Determine": PatternFill("solid", fgColor="DDEBF7"),
             "Correct": PatternFill("solid", fgColor="E2EFDA")}
    for c, name in enumerate(AUDIT_COLUMNS, 1):
        cell = ws.cell(row=1, column=c, value=name)
        cell.fill = PatternFill("solid", fgColor="1F3864")
        cell.font = Font(bold=True, color="FFFFFF", size=10)
        ws.column_dimensions[get_column_letter(c)].width = \
            {1: 40, 2: 34, 3: 55, 12: 55, 13: 50, 18: 34}.get(c, 15)
    for r, row in enumerate(rows, 2):
        f = fills.get(row["Review Status"])
        for c, name in enumerate(AUDIT_COLUMNS, 1):
            cell = ws.cell(row=r, column=c, value=row.get(name, ""))
            cell.alignment = Alignment(vertical="top",
                                       wrap_text=(c in (1, 2, 3, 12, 13, 18)))
            if f:
                cell.fill = f
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(AUDIT_COLUMNS))}{len(rows)+1}"
    sm = wb.create_sheet("Summary")
    sm.column_dimensions["A"].width = 46
    from collections import Counter
    by = Counter(r["Review Status"] for r in rows)
    flagged = [r for r in rows
               if r["Review Status"] not in ("Correct", "Custom Name")]
    sm.append(["Post-run filename accuracy audit"])
    sm["A1"].font = Font(bold=True, size=13)
    sm.append(["Audit only - no files were renamed. Every flag was confirmed "
               "by a second, stronger model over the full pages."])
    sm.append([])
    for label, v in (("Total files reviewed", len(rows)),
                     ("Correctly named", by.get("Correct", 0)),
                     ("Custom names (kept, not compared)",
                      by.get("Custom Name", 0)),
                     ("Likely misnamed", by.get("Likely Misnamed", 0)),
                     ("Possibly misnamed", by.get("Possibly Misnamed", 0)),
                     ("Unable to determine", by.get("Unable To Determine", 0)),
                     ("Average confidence (flagged rows)",
                      round(sum(int(r["Confidence Score"] or 0)
                                for r in flagged) / len(flagged), 1)
                      if flagged else 0)):
        sm.append([label, v])
    orientation_rows = orientation_rows or []
    if orientation_rows:
        ow = wb.create_sheet("Orientation")
        columns = ["File", "Page", "Predicted orientation",
                   "Correction indicated", "Top confidence",
                   "Runner-up confidence", "Confidence margin", "Action",
                   "Reason"]
        for column, name in enumerate(columns, 1):
            cell = ow.cell(row=1, column=column, value=name)
            cell.fill = PatternFill("solid", fgColor="1F3864")
            cell.font = Font(bold=True, color="FFFFFF", size=10)
            ow.column_dimensions[get_column_letter(column)].width = \
                55 if column == 1 else (40 if column == 9 else 18)
        for row_number, item in enumerate(orientation_rows, 2):
            for column, name in enumerate(columns, 1):
                value = item.get(name, "")
                if name in ("Top confidence", "Runner-up confidence",
                            "Confidence margin") and isinstance(value, float):
                    value = round(value, 6)
                ow.cell(row=row_number, column=column, value=value)
        ow.freeze_panes = "A2"
        ow.auto_filter.ref = (
            f"A1:{get_column_letter(len(columns))}{len(orientation_rows)+1}")
        sm.append(["Local orientation pages flagged", len(orientation_rows)])
    wb.save(out_path)


def _write_audit_csv(rows, out_path, orientation_rows=None):
    """One table per CSV; linked manifest preserves the old workbook's tabs."""
    from collections import Counter
    order = {"Likely Misnamed": 0, "Possibly Misnamed": 1, "Unable To Determine": 2, "Correct": 3}
    rows = sorted(rows, key=lambda r: (order.get(r.get("Review Status"), 9),
                                      -int(r.get("Confidence Score") or 0)))
    pipeline.atomic_csv(out_path, rows, AUDIT_COLUMNS)
    by = Counter(r.get("Review Status", "") for r in rows)
    summary_path = out_path.with_name(out_path.stem + " - Summary.csv")
    flagged = [r for r in rows if r.get("Review Status") not in ("Correct", "Custom Name")]
    summary = [{"metric": "Total files reviewed", "value": len(rows)}]
    summary += [{"metric": key, "value": value} for key, value in sorted(by.items())]
    summary += [{"metric": "Average confidence (flagged rows)", "value":
                 round(sum(int(r.get("Confidence Score") or 0) for r in flagged) / len(flagged), 1) if flagged else 0},
                {"metric": "Local orientation pages flagged", "value": len(orientation_rows or [])}]
    pipeline.atomic_csv(summary_path, summary, ["metric", "value"])
    tables = [{"table": "Audit", "file": out_path.name, "rows": len(rows)},
              {"table": "Summary", "file": summary_path.name, "rows": len(summary)}]
    if orientation_rows:
        orientation_path = out_path.with_name(out_path.stem + " - Orientation.csv")
        columns = ["File", "Page", "Predicted orientation", "Correction indicated", "Top confidence",
                   "Runner-up confidence", "Confidence margin", "Action", "Reason"]
        pipeline.atomic_csv(orientation_path, orientation_rows, columns)
        tables.append({"table": "Orientation", "file": orientation_path.name, "rows": len(orientation_rows)})
    pipeline.atomic_csv(out_path.with_name(out_path.stem + " - Tables.csv"), tables, ["table", "file", "rows"])
    return out_path


def write_orientation_audit(orientation_rows, out_dir: Path):
    """Write the existing audit workbook format without making API calls."""
    rep_dir = processing_reports_dir(Path(out_dir).name)
    xlsx = unique_path(rep_dir, AUDIT_REPORT_STEM, ".csv")
    _write_audit_workbook([], xlsx, orientation_rows=orientation_rows)
    record_processing_report(xlsx, Path(out_dir).name)
    return xlsx


# ====================================================================
# PROCESSING ENGINE
# ====================================================================
class StopRequested(Exception):
    pass


class LimitReached(StopRequested):
    """Raised when a configured hard limit (workers/files/budget) is hit, so the
    run stops cleanly instead of continuing to spend."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class FinishingAmbiguous(StopRequested):
    """A live finishing POST may have completed before its result was saved."""
    pass


class DurableStateError(StopRequested):
    """Required batch checkpoint could not be made safely."""
    pass


class Engine:
    def _record_roster_handover(self, source, destination):
        try:
            if not pipeline.find_roster(Path(source).parent):
                pipeline.refresh_roster(Path(source).parent, stage="stage2")
            pipeline.record_worker_move(source, destination, "stage2")
        except Exception as exc:
            self.log(f"  ! Processing finished, but worker roster needs refresh: {exc}")

    """Runs the whole review on a background thread. Communicates with the UI
    via callbacks (all marshalled back onto the Tk thread by the caller)."""

    def __init__(self, care_home_dir: Path, kb: KnowledgeBase, api: ClaudeAPI,
                 care_home_name: str,
                 log, set_status, set_progress, set_preview,
                 ask_unknown, on_cost, on_done,
                 resolution: float = 1.5, skip_when_clear: bool = False,
                 adaptive_pages: bool = True, auto_other: bool = False,
                 max_workers: int = DEFAULT_MAX_WORKERS,
                 max_files: int = DEFAULT_MAX_FILES,
                 max_budget_gbp: float = DEFAULT_MAX_BUDGET_GBP,
                 max_file_mb: float = DEFAULT_MAX_FILE_MB,
                 redact_logs: bool = False,
                 reprocess: bool = False,
                 convert_pdf: bool = True,
                 move_mode: bool = False,
                 move_dest: Path = None,
                 review_unknowns=None,
                 escalation_api: "ClaudeAPI" = None,
                 orientation_mode: str = "audit",
                 orientation_confidence: float = 0.95,
                 orientation_margin: float = 0.20,
                 orientation_predictor=None,
                 bundle_split: bool = True,
                 cleanup_leftovers: bool = True,
                 post_run_audit: bool = False,
                 on_activity=None):
        self.dir = care_home_dir
        self.kb = kb
        self.api = api
        # optional stronger-model client for the low-confidence second opinion
        # (None = escalation off, or primary model already this strong)
        self.escalation_api = escalation_api
        self.orientation_mode = (orientation_mode if orientation_mode in
                                 ("off", "audit", "automatic") else "audit")
        self.orientation_confidence = max(
            0.5, min(0.999, float(orientation_confidence)))
        self.orientation_margin = max(
            0.0, min(0.999, float(orientation_margin)))
        self._orientation_predictor = orientation_predictor
        self._orientation_model_checked = False
        self.orientation_state = OrientationState(care_home_dir)
        # detect+split files that wrongly contain several documents
        self.bundle_split = bool(bundle_split)
        # delete processing residue (.splitbak/.zip) per worker when done
        self.cleanup_leftovers = bool(cleanup_leftovers)
        # optional second accuracy check over everything once the run ends
        self.post_run_audit = bool(post_run_audit)
        self.on_activity = on_activity
        self._audit_progress_snapshot = {}
        self._audit_worker_dirs = []
        self.care_home = care_home_name
        self.log = log
        self.set_status = set_status
        self.set_progress = set_progress
        self.set_preview = set_preview
        self.ask_unknown = ask_unknown      # blocking call -> returns ("Relevant"/"Other", name, desc) or None
        # Overnight Batch: blocking callback(n_unknowns) -> bool; True to review
        # the queued 'Other - Unknown' documents now. None => never review.
        self.review_unknowns = review_unknowns
        self.on_cost = on_cost
        self.on_done = on_done
        self.resolution = resolution
        self.skip_when_clear = skip_when_clear
        self.adaptive_pages = adaptive_pages
        self.auto_other = auto_other

        # ---- file-movement mode ----
        # When on, each fully-completed worker subfolder is MOVED out of the
        # source care-home folder into move_dest (workers placed directly there).
        # A worker that already exists in the destination is skipped as done.
        self.move_mode = bool(move_mode) and move_dest is not None
        self.move_dest = Path(move_dest) if move_dest is not None else None

        # ---- hard safety limits ----
        self.max_workers = int(max_workers)
        self.max_files = int(max_files)
        self.max_budget_gbp = float(max_budget_gbp)
        self.max_file_mb = float(max_file_mb)
        self.redact_logs = bool(redact_logs)
        self.reprocess = bool(reprocess)   # if True, ignore the manifest cache
        self.convert_pdf = bool(convert_pdf)  # Stage 2: convert files to PDF first
        self.files_sent = 0                # counts files actually sent to the API
        self._current_worker = ""          # worker being processed (for credit-stop marking)
        # Actual already-committed Message Batches spend is folded into the
        # same ceiling as live finishing and audit calls during batch apply.
        self._committed_batch_cost_gbp = 0.0
        self._committed_batch_tokens = 0
        self._persisted_live_cost_gbp = 0.0
        self._persisted_live_tokens = 0
        self._batch_state = None

        # persistent processed-file cache (skip unchanged files on re-runs)
        self.manifest = ProcessedManifest(care_home_dir)

        self.rename_log = RenameLog()
        self.override_log = OverrideLog()
        self.failed_log = FailedLog()
        self._stop = threading.Event()
        self.stats = {"workers": 0, "renamed": 0, "unknown": 0,
                      "cos": 0, "contracts": 0, "rtw": 0, "dbs": 0,
                      "ecs": 0, "brp": 0, "evisa": 0, "ni": 0, "sharecode": 0,
                      "duplicates": 0,
                      "converted": 0, "convert_failed": 0,
                      "batches": 0, "overwrite": 0, "bulk": 0, "ranked": 0, "errors": 0, "moved": 0, "skipped_done": 0,
                      "page1_only": 0, "skipped_api": 0,
                      "skipped_cached": 0, "skipped_oversized": 0,
                      "skipped_cloud": 0,
                      # Overnight Batch mode
                      "batch_requests": 0, "batch_succeeded": 0,
                      "batch_errored": 0, "batch_expired": 0,
                      "batch_canceled": 0, "batch_missing": 0,
                      "batch_in_tokens": 0, "batch_out_tokens": 0,
                      "followup_requests": 0,
                      "followup_in_tokens": 0, "followup_out_tokens": 0,
                      "bundles_split": 0, "possible_bundles": 0}
        # files that look like multi-document bundles but were deliberately
        # NOT split (too long to see in full, or the split gates refused the
        # boundaries). Surfaced in the processing report so they get a human
        # look rather than vanishing into one name.
        self.possible_bundles = []

    def _redact(self, name: str) -> str:
        """When redact_logs is on, mask a filename in log output so personal
        data (worker names embedded in filenames) is not written to the log."""
        if not self.redact_logs or not name:
            return name
        stem, dot, ext = name.rpartition(".")
        base = stem if dot else name
        if len(base) <= 4:
            masked = base[0] + "***"
        else:
            masked = base[:3] + "***" + base[-1:]
        return masked + (("." + ext) if dot else "")

    def stop(self):
        self._stop.set()

    def _check_stop(self):
        if self._stop.is_set():
            raise StopRequested()

    # ---- budget / limit guards ----
    def _current_cost_gbp(self) -> float:
        gbp = (getattr(self, "_committed_batch_cost_gbp", 0.0)
               + getattr(self, "_persisted_live_cost_gbp", 0.0)
               + tokens_cost_gbp(
            self.api.model_id, self.api.in_tokens, self.api.out_tokens)
               )
        if self.escalation_api is not None:
            gbp += tokens_cost_gbp(self.escalation_api.model_id,
                                   self.escalation_api.in_tokens,
                                   self.escalation_api.out_tokens)
        return gbp

    def _session_live_tokens(self):
        total = self.api.in_tokens + self.api.out_tokens
        if self.escalation_api is not None:
            total += (self.escalation_api.in_tokens
                      + self.escalation_api.out_tokens)
        return total

    def _persist_batch_live_cost(self):
        state = getattr(self, "_batch_state", None)
        if state is None:
            return
        session_cost = tokens_cost_gbp(
            self.api.model_id, self.api.in_tokens, self.api.out_tokens)
        if self.escalation_api is not None:
            session_cost += tokens_cost_gbp(
                self.escalation_api.model_id,
                self.escalation_api.in_tokens,
                self.escalation_api.out_tokens)
        costs = state.data.setdefault("costs", {})
        costs["live_actual_gbp"] = round(
            getattr(self, "_persisted_live_cost_gbp", 0.0) + session_cost, 8)
        costs["live_tokens"] = int(
            getattr(self, "_persisted_live_tokens", 0)
            + self._session_live_tokens())
        costs["updated_ts"] = datetime.datetime.now().isoformat(
            timespec="seconds")
        if not state.save():
            raise DurableStateError(
                "could not persist cumulative live finishing cost")

    def _finishing_operation(self, worker_dir: Path, operation_id: str,
                             callback):
        """Run one chargeable finishing operation at most once across restarts."""
        state = getattr(self, "_batch_state", None)
        if state is None:
            result = callback()
            self._emit_cost()
            return result
        worker_key = str(Path(worker_dir).resolve()).casefold()
        worker = state.data.setdefault("workers", {}).setdefault(
            worker_key, {"name": Path(worker_dir).name,
                         "source_path": str(worker_dir)})
        operations = worker.setdefault("finishing_operations", {})
        prior = operations.get(operation_id) or {}
        if prior.get("status") in ("complete", "failed"):
            return prior.get("result")
        if prior.get("status") == "submission_started":
            raise FinishingAmbiguous(
                f"finishing operation '{operation_id}' for "
                f"'{Path(worker_dir).name}' may already have been accepted; "
                "automatic retry is blocked")
        attempt_id = hashlib.sha256(
            f"{time.time_ns()}:{worker_key}:{operation_id}".encode(
                "utf-8")).hexdigest()[:24]
        operations[operation_id] = {
            "status": "submission_started", "attempt_id": attempt_id,
            "started_ts": datetime.datetime.now().isoformat(
                timespec="seconds")}
        worker["finishing_status"] = "in_progress"
        if not state.save():
            raise DurableStateError(
                "finishing marker could not be persisted; no request sent")
        try:
            result = callback()
        except Exception as exc:
            self._persist_batch_live_cost()
            operations[operation_id].update({
                "status": "failed", "result": None,
                "error": f"{type(exc).__name__}: {exc}",
                "completed_ts": datetime.datetime.now().isoformat(
                    timespec="seconds")})
            state.save()
            self.on_cost(self._current_cost_gbp(),
                         getattr(self, "_committed_batch_tokens", 0)
                         + getattr(self, "_persisted_live_tokens", 0)
                         + self._session_live_tokens())
            raise
        self._persist_batch_live_cost()
        operations[operation_id].update({
            "status": "complete", "result": result,
            "completed_ts": datetime.datetime.now().isoformat(
                timespec="seconds")})
        if not state.save():
            raise FinishingAmbiguous(
                "finishing response was received but completion could not be "
                "persisted; automatic retry is blocked")
        self.on_cost(self._current_cost_gbp(),
                     getattr(self, "_committed_batch_tokens", 0)
                     + getattr(self, "_persisted_live_tokens", 0)
                     + self._session_live_tokens())
        self._check_budget()
        return result

    def _check_budget(self):
        """Stop the whole run if the live estimated spend reaches the ceiling."""
        if self.max_budget_gbp > 0 and self._current_cost_gbp() >= self.max_budget_gbp:
            raise LimitReached(
                f"budget limit reached (£{self.max_budget_gbp:.2f})")

    def _check_file_budget(self):
        if self.max_files > 0 and self.files_sent >= self.max_files:
            raise LimitReached(
                f"file limit reached ({self.max_files} files sent)")

    # ---- local page-orientation preflight ----
    def _orientation_move_path(self, old_path: Path, new_path: Path):
        state = getattr(self, "orientation_state", None)
        if state is not None:
            state.move_path(old_path, new_path)

    def _orientation_move_tree(self, old_root: Path, new_root: Path):
        state = getattr(self, "orientation_state", None)
        if state is not None:
            state.move_tree(old_root, new_root)

    def _orientation_signature(self):
        return (f"{ORIENTATION_MODEL_REVISION}:{ORIENTATION_MODEL_SHA256}:"
                f"{self.orientation_mode}:{self.orientation_confidence:.4f}:"
                f"{self.orientation_margin:.4f}")

    def _orientation_predictor_for_run(self):
        if self._orientation_predictor is not None:
            return self._orientation_predictor
        model_path = bundled_resource("assets", "orientation",
                                      "inference.onnx")
        if not self._orientation_model_checked:
            self._orientation_model_checked = True
            if not model_path.is_file():
                raise FileNotFoundError(
                    f"bundled {ORIENTATION_MODEL_NAME} model is missing")
            if file_hash(model_path).lower() != ORIENTATION_MODEL_SHA256.lower():
                raise RuntimeError(
                    "bundled orientation model checksum does not match the "
                    "pinned release checksum")
        self._orientation_predictor = OnnxOrientationPredictor(
            model_path, batch_size=4)
        return self._orientation_predictor

    @staticmethod
    def _consume_rotation_instructions(result: dict):
        """Consume cloud hints after a parent has already been corrected."""
        if isinstance(result, dict):
            result["rotation"] = 0
            rots = result.get("rotations")
            if isinstance(rots, list):
                result["rotations"] = [0 for _ in rots]
            result["rotation_consumed"] = True

    def _inherit_split_orientation(self, parent: Path, parts: list,
                                   plan: list):
        """Persist child page state so a restart cannot turn split pages again."""
        state = getattr(self, "orientation_state", None)
        if state is None:
            return
        parent_entry = state.entry(parent)
        parent_pages = {int(item.get("page", -1)): item
                        for item in parent_entry.get("pages", []) or []}
        if not parent_entry or not parent_pages:
            return
        parent_was_corrected = bool(parent_entry.get("changed"))
        for child, segment in zip(parts, plan):
            child_pages = []
            for child_index, parent_index in enumerate(segment.get("pages", [])):
                inherited = dict(parent_pages.get(parent_index, {}))
                inherited["page"] = child_index
                inherited["parent_page"] = parent_index
                inherited["reason"] = (
                    "inherited from locally corrected parent"
                    if parent_was_corrected else
                    inherited.get("reason", "inherited parent audit"))
                if parent_was_corrected:
                    inherited["source_correction"] = inherited.get(
                        "correction", 0)
                    inherited["predicted_orientation"] = 0
                    inherited["correction"] = 0
                    inherited["rotate"] = 0
                    inherited["uncertain"] = False
                    inherited["eligible"] = False
                    inherited["applied"] = True
                child_pages.append(inherited)
            try:
                child_hash = file_hash(child)
            except Exception:
                continue
            state.put(child, {
                "status": "complete", "signature": self._orientation_signature(),
                "mode": self.orientation_mode, "model": ORIENTATION_MODEL_NAME,
                "model_revision": ORIENTATION_MODEL_REVISION,
                "model_sha256": ORIENTATION_MODEL_SHA256,
                "original_hash": child_hash, "current_hash": child_hash,
                "pages": child_pages, "changed": False,
                "inherited_from": str(parent),
            })

    def _orientation_preflight(self, path: Path) -> dict:
        """Inspect every PDF page locally in bounded thumbnail batches."""
        mode = getattr(self, "orientation_mode", "off")
        if mode == "off" or path.suffix.lower() not in PDF_EXT \
                or not HAS_FITZ or not HAS_PIL or not path.is_file():
            return {"changed": False, "hash": "", "pages": []}
        self._check_stop()
        try:
            current_hash = file_hash(path)
        except Exception as exc:
            return {"changed": False, "hash": "", "pages": [],
                    "warning": str(exc)}

        state = getattr(self, "orientation_state", None)
        if state is None:
            state = OrientationState(self.dir)
            self.orientation_state = state
        signature = self._orientation_signature()
        previous = state.entry(path)
        # Replacement may have completed just before a crash. The persisted
        # authorisation plus changed bytes prove the turns were consumed.
        if (previous.get("status") == "rewrite_authorized"
                and previous.get("original_hash")
                and previous.get("original_hash") != current_hash):
            previous["status"] = "complete_recovered"
            previous["current_hash"] = current_hash
            previous["changed"] = True
            for page in previous.get("pages", []) or []:
                if page.get("rotate"):
                    page["applied"] = True
            state.put(path, previous)
            return {"changed": True, "hash": current_hash,
                    "pages": previous.get("pages", []), "recovered": True}
        if (previous.get("status") in ("complete", "complete_recovered")
                and previous.get("current_hash") == current_hash
                and previous.get("signature") == signature):
            return {"changed": bool(previous.get("changed")),
                    "hash": current_hash,
                    "pages": previous.get("pages", []), "cached": True}

        entry = {"status": "inspection_started", "signature": signature,
                 "mode": mode, "model": ORIENTATION_MODEL_NAME,
                 "model_revision": ORIENTATION_MODEL_REVISION,
                 "model_sha256": ORIENTATION_MODEL_SHA256,
                 "original_hash": current_hash, "current_hash": current_hash,
                 "pages": [], "changed": False}
        state.put(path, entry)
        try:
            predictor = self._orientation_predictor_for_run()
            text_corrections = detect_pdf_page_text_rotations(
                path, include_upright=True)
            doc = fitz.open(str(path))
            decisions = []
            try:
                for start in range(0, len(doc), 4):
                    self._check_stop()
                    images, evidences = [], []
                    for page_index in range(start, min(start + 4, len(doc))):
                        self._check_stop()
                        pix = doc[page_index].get_pixmap(
                            matrix=fitz.Matrix(1.0, 1.0), alpha=False,
                            colorspace=fitz.csRGB)
                        image = Image.frombytes(
                            "RGB", (pix.width, pix.height), pix.samples)
                        images.append(image)
                        evidences.append(thumbnail_evidence(image.copy()))
                    predictions = predictor.predict(images)
                    if len(predictions) != len(images):
                        raise RuntimeError(
                            "orientation model returned the wrong batch size")
                    for offset, (prediction, evidence) in enumerate(
                            zip(predictions, evidences)):
                        page_index = start + offset
                        decision = orientation_decision(
                            prediction, evidence, mode=mode,
                            confidence_threshold=self.orientation_confidence,
                            margin_threshold=self.orientation_margin,
                            text_correction=text_corrections.get(page_index))
                        decision["page"] = page_index
                        decision["ink_fraction"] = evidence.get(
                            "ink_fraction", 0.0)
                        decisions.append(decision)
            finally:
                doc.close()
        except StopRequested:
            raise
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            entry.update({"status": "unavailable", "warning": reason})
            state.put(path, entry)
            state.warning(path, reason)
            self.stats["orientation_warnings"] = \
                self.stats.get("orientation_warnings", 0) + 1
            self.log(f"    ! local orientation unavailable for "
                     f"{self._redact(path.name)}: {reason}; pages unchanged")
            return {"changed": False, "hash": current_hash, "pages": [],
                    "warning": reason}

        entry["pages"] = decisions
        fixes = {int(page["page"]): int(page["rotate"])
                 for page in decisions if page.get("rotate")}
        if fixes:
            self._check_stop()
            entry["status"] = "rewrite_authorized"
            entry["rotations"] = {str(k): v for k, v in fixes.items()}
            state.put(path, entry)
            self._check_stop()
            changed = fix_pdf_page_rotations(path, fixes)
            if changed == len(fixes):
                new_hash = file_hash(path)
                entry.update({"status": "complete", "current_hash": new_hash,
                              "changed": True})
                for page in entry["pages"]:
                    if page.get("rotate"):
                        page["applied"] = True
                # Persist the new hash immediately after atomic replacement.
                state.put(path, entry)
                self.stats["rotated_fixed"] = \
                    self.stats.get("rotated_fixed", 0) + 1
                self.log(f"    · {self._redact(path.name)}: {changed} page(s) "
                         "corrected locally and atomically")
                return {"changed": True, "hash": new_hash,
                        "pages": entry["pages"]}
            reason = "atomic orientation rewrite failed; original preserved"
            entry.update({"status": "rewrite_failed",
                          "current_hash": file_hash(path), "warning": reason})
            state.put(path, entry)
            state.warning(path, reason)
            self.log(f"    ! {self._redact(path.name)}: {reason}")
            return {"changed": False, "hash": entry["current_hash"],
                    "pages": entry["pages"], "warning": reason}

        entry["status"] = "complete"
        state.put(path, entry)
        apparent = sum(1 for page in decisions if page.get("correction"))
        uncertain = sum(1 for page in decisions if page.get("uncertain"))
        if apparent or uncertain:
            self.log(f"    · local orientation audit: {apparent} apparently "
                     f"rotated, {uncertain} uncertain page(s); unchanged")
        return {"changed": False, "hash": current_hash, "pages": decisions}

    # ---- physical rotation compatibility entry point ----
    def _maybe_fix_rotation(self, f: Path, result: dict, page_idxs=None):
        outcome = self._orientation_preflight(f)
        if outcome.get("changed"):
            self._consume_rotation_instructions(result)
            return outcome.get("hash")
        return None

    # ---- cost ----
    def _emit_cost(self):
        self._persist_batch_live_cost()
        gbp = self._current_cost_gbp()
        toks = (getattr(self, "_committed_batch_tokens", 0)
                + getattr(self, "_persisted_live_tokens", 0)
                + self.api.in_tokens
                + self.api.out_tokens)
        if self.escalation_api is not None:
            toks += (self.escalation_api.in_tokens
                     + self.escalation_api.out_tokens)
        self.on_cost(gbp, toks)
        # after every cost update, enforce the budget ceiling
        self._check_budget()

    # ---- optional post-run accuracy audit ----
    def _activity(self, event):
        """Emit structured UI/notification facts without changing processing."""
        if event.get("phase") == "audit":
            self._audit_progress_snapshot = dict(event)
        callback = getattr(self, "on_activity", None)
        if callback:
            try:
                callback(dict(event))
            except Exception:
                self.log("  ! Activity observer unavailable; processing continues.")

    def _phase(self, phase, label):
        seen = getattr(self, "_announced_phases", set())
        if phase not in seen:
            seen.add(phase)
            self._announced_phases = seen
            self._activity({"kind":"phase_started", "phase":phase, "label":label})

    def _phase_progress(self, phase, done, total, **details):
        self._activity({"kind":"run_progress", "phase":phase, "state":"running",
                        "completed":done, "total":total, **details})

    def _record_review_processing(self):
        """Persist the exact post-movement scope before an automatic audit."""
        controller = getattr(self, "_review_controller", None)
        run_id = getattr(self, "_review_run_id", "")
        if controller is not None and run_id:
            # Validate even when a receipt already exists.  A resumed engine
            # must never treat a stale/partial in-memory scope as complete.
            workers = controller.validate_processing_scope(
                run_id, processing_root=self.dir,
                worker_dirs=list(self._audit_worker_dirs))
            self._audit_worker_dirs = [Path(item) for item in workers]
            if controller.get_status(run_id, processing_root=self.dir).get("processing_receipt"):
                return
            controller.record_processing_complete(run_id, processing_root=self.dir,
                worker_dirs=list(self._audit_worker_dirs),
                errors=([f"Processing recorded {self.stats.get('errors', 0)} errors"]
                        if self.stats.get("errors") else ()))

    def _record_review_audit(self, status, report=None):
        controller = getattr(self, "_review_controller", None)
        run_id = getattr(self, "_review_run_id", "")
        if controller is not None and run_id:
            if status == "complete":
                controller.record_audit_receipt(run_id, processing_root=self.dir,
                    report_path=report, status=status, worker_dirs=list(self._audit_worker_dirs))
            else:
                controller.record_audit_status(run_id, status,
                    reason="The accuracy audit is not complete; automatic review has not started.")

    def _run_post_run_audit(self):
        """When the Settings toggle is on, re-check every processed document
        (run_accuracy_audit: full pages, adjudicated flags) and write
        Filename_Audit_Report.xlsx into the care-home folder (the move
        destination when move mode is on). This phase is report-only: nothing
        is renamed or rotated. A failure here never breaks the finished run."""
        orientation_rows = self.orientation_state.audit_rows()
        batch_state = getattr(self, "_batch_state", None)
        audit_state = (batch_state.data.setdefault("audit", {})
                       if batch_state is not None else {})
        if audit_state.get("status") == "complete":
            self.stats["audit_status"] = "complete"
            self.stats["audit_report"] = audit_state.get("report", "")
            self._record_review_audit("complete", audit_state.get("report"))
            return
        out_dir = (self.move_dest if (self.move_mode and self.move_dest)
                   else self.dir)
        if not self.post_run_audit or not self._audit_worker_dirs:
            self.stats["audit_status"] = "disabled" if not self.post_run_audit else "skipped"
            self._record_review_audit(self.stats["audit_status"])
            if orientation_rows:
                xlsx = write_orientation_audit(orientation_rows, out_dir)
                self.stats["orientation_audit_rows"] = len(orientation_rows)
                self.log(f"local orientation audit -> {xlsx}")
            if batch_state is not None:
                audit_state["status"] = "disabled"
                audit_state["orientation_rows"] = len(orientation_rows)
                batch_state.save()
            return
        docs = []
        for worker in self._audit_worker_dirs:
            worker = Path(worker)
            if worker.is_dir():
                docs.extend(p for p in worker.rglob("*")
                            if p.is_file() and p.suffix.lower() in DOC_EXT
                            and not is_program_file(p))
        adjudicator = self.escalation_api or self.api
        expected = estimate_pipeline_costs_gbp(
            len(docs), self.api.model_id, adjudicator.model_id,
            self.resolution, self.kb.vocabulary_block(), batch=False,
            include_audit=True)["audit_gbp"]
        remaining = (float("inf") if self.max_budget_gbp <= 0 else
                     max(0.0, self.max_budget_gbp - self._current_cost_gbp()))
        self.log(f"\n=== POST-RUN ACCURACY AUDIT ESTIMATE: "
                 f"~£{expected:.2f}; remaining cumulative budget "
                 f"{'unlimited' if math.isinf(remaining) else f'£{remaining:.2f}'} ===")
        if expected > remaining:
            self.stats["audit_skipped_budget"] = 1
            self.stats["audit_expected_gbp"] = expected
            if orientation_rows:
                xlsx = write_orientation_audit(orientation_rows, out_dir)
                self.log(f"local orientation audit -> {xlsx}")
            if batch_state is not None:
                audit_state.update({"status": "skipped", "reason": "budget",
                                    "expected_gbp": expected})
                batch_state.save()
            self.log("PROCESSING COMPLETE: post-run accuracy audit skipped "
                     "because the remaining budget cannot cover its separate "
                     f"~£{expected:.2f} expected cost.")
            self.stats["audit_status"] = "skipped"
            self._record_review_audit("skipped")
            self._activity({"phase":"audit", "state":"skipped", "completed":0,
                            "total":len(docs), "reason":"budget"})
            return
        try:
            if batch_state is not None:
                audit_state["status"] = "running"
                audit_state["started_ts"] = datetime.datetime.now().isoformat(
                    timespec="seconds")
                if not batch_state.save():
                    raise RuntimeError(
                        "audit start could not be persisted; audit not started")
            self.set_status("Post-run accuracy audit…")
            self.stats["audit_status"] = "running"
            self.log(f"\n=== POST-RUN ACCURACY AUDIT "
                     f"({len(self._audit_worker_dirs)} worker folder(s)) ===")
            rows, xlsx = run_accuracy_audit(
                self.api, adjudicator, self.kb, self._audit_worker_dirs,
                out_dir, resolution=self.resolution, log=self.log,
                emit_cost=self._emit_cost, check_stop=self._check_stop,
                orientation_rows=orientation_rows, on_progress=self._activity)
            flagged = sum(1 for r in rows
                          if r["Review Status"] not in ("Correct",
                                                        "Custom Name"))
            self.stats["audited"] = len(rows)
            self.stats["audit_flagged"] = flagged
            self.stats["audit_needs_review"] = flagged
            self.stats["audit_status"] = "complete"
            self.stats["audit_report"] = str(xlsx)
            self._record_review_audit("complete", xlsx)
            self.log(f"audit report -> {xlsx}")
            self.manifest.save()
            if batch_state is not None:
                audit_state.update({
                    "status": "complete", "report": str(xlsx),
                    "completed_ts": datetime.datetime.now().isoformat(
                        timespec="seconds")})
                batch_state.save()
        except (StopRequested, LimitReached, CreditExhausted):
            self.stats["audit_status"] = "pending"
            self._record_review_audit("pending")
            self._activity(dict(getattr(self, "_audit_progress_snapshot", {}),
                                phase="audit", state="stopped"))
            if batch_state is not None:
                audit_state["status"] = "pending"
                audit_state["last_stop_ts"] = datetime.datetime.now().isoformat(
                    timespec="seconds")
                batch_state.save()
            raise
        except Exception as e:
            self.stats["audit_status"] = "failed"
            try:
                self._record_review_audit("failed")
            except Exception:
                traceback.print_exc()
            self._activity(dict(getattr(self, "_audit_progress_snapshot", {}),
                                phase="audit", state="failed"))
            if batch_state is not None:
                audit_state["status"] = "pending"
                audit_state["last_error"] = str(e)
                batch_state.save()
            self.log(f"! post-run audit failed: {e} (run itself is complete)")
            traceback.print_exc()

    # ---- run ----
    @_care_home_writer_operation
    def run(self):
        try:
            pending = BatchState(self.dir)
            if pending.exists():
                self.log("LIVE PROCESSING BLOCKED: this folder has a pending "
                         "or ambiguous primary/follow-up batch. Retrieve, "
                         "complete or safely resolve it first. No live API "
                         "request was made.")
                self.on_done(self.stats, "live_blocked_by_batch")
                return
            if getattr(self, "_resume_processing_complete", False):
                self.log("Live processing is already complete; resuming only "
                         "the saved Accuracy Audit for its original review run.")
                self._record_review_processing()
                controller = getattr(self, "_review_controller", None)
                run_id = getattr(self, "_review_run_id", "")
                saved_review = (controller.get_status(
                    run_id, processing_root=self.dir)
                    if controller is not None and run_id else {})
                audit_receipt = saved_review.get("audit_receipt") or {}
                if audit_receipt.get("status") == "complete":
                    self.stats["audit_status"] = "complete"
                    self.stats["audit_report"] = audit_receipt.get("report_path", "")
                    clear_live_checkpoint(self.dir)
                    self.on_done(self.stats, None)
                    return
                self._run_post_run_audit()
                if self.stats.get("audit_status") in ("complete", "skipped", "disabled"):
                    clear_live_checkpoint(self.dir)
                self.on_done(self.stats, None)
                return
            workers = worker_dirs_in(self.dir)
            saved_scope = getattr(self, "_review_scope_worker_names", None)
            if saved_scope is not None:
                workers = [worker for worker in workers if worker.name.casefold() in saved_scope]
            if not workers:
                self.log("No worker sub-folders found in that care-home folder.")
                self.on_done(self.stats, None)
                return
            total = len(workers)
            if self.max_workers > 0 and total > self.max_workers:
                self.log(f"NOTE: {total} worker folders found; this run is capped "
                         f"at {self.max_workers} (change in Settings). The rest "
                         f"will be left untouched.")
                total = self.max_workers
            TRACKER.reset("live", self.care_home)
            TRACKER.update(workers_total=total)
            for idx, w in enumerate(workers[:total], 1):
                self._check_stop()
                self._phase_progress("processing", idx - 1, total)
                self.set_progress(idx - 1, total)
                TRACKER.update(workers_done=idx - 1, current_worker=w.name,
                               status=f"Worker {idx}/{total}",
                               stats=dict(self.stats))
                # checkpoint BEFORE the worker so a crash mid-worker records
                # the exact position for the next launch's resume offer.
                review_run_id = getattr(self, "_review_run_id", "")
                checkpoint_saved = write_live_checkpoint(
                    self.dir, idx - 1, total, w.name,
                    self.stats.get("errors", 0), False,
                    review_run_id, self._audit_worker_dirs)
                if review_run_id and not checkpoint_saved:
                    raise LiveCheckpointWriteError(
                        "The automatic-review live checkpoint could not be saved; "
                        "processing stopped before this worker was sent.")
                # In move mode, a worker already present in the destination has
                # been processed before - skip it entirely (no API calls).
                if self.move_mode and (self.move_dest / w.name).exists():
                    self.stats["skipped_done"] += 1
                    self.log(f"\n=== Worker {idx}/{total}: {w.name} ===")
                    self.log(f"  already in destination — skipped as done")
                    self._emit_cost()
                    continue
                self.log(f"\n=== Worker {idx}/{total}: {w.name} ===")
                self.set_status(f"Worker {idx}/{total}: {w.name}")
                self._current_worker = w.name
                try:
                    self._process_worker(w)
                    self.stats["workers"] += 1
                    final_dir = w
                    # fully completed -> move the whole subfolder to destination
                    if self.move_mode:
                        try:
                            dest = move_worker_folder(w, self.move_dest)
                            self._orientation_move_tree(w, dest)
                            self.stats["moved"] += 1
                            self.log(f"  moved worker folder -> {dest}")
                            final_dir = Path(dest)
                        except Exception as e:
                            self.stats["errors"] += 1
                            self.log(f"  ! could not move {w.name} to destination: "
                                     f"{e} (left in source)")
                            traceback.print_exc()
                    final_key = str(Path(final_dir).resolve()).casefold()
                    if all(str(Path(old).resolve()).casefold() != final_key
                           for old in self._audit_worker_dirs):
                        self._audit_worker_dirs.append(final_dir)
                    self._record_roster_handover(w, final_dir)
                    checkpoint_saved = write_live_checkpoint(
                        self.dir, idx, total, "",
                        self.stats.get("errors", 0), False,
                        review_run_id, self._audit_worker_dirs)
                    if review_run_id and not checkpoint_saved:
                        raise LiveCheckpointWriteError(
                            "The completed worker could not be added to the "
                            "automatic-review checkpoint; processing stopped safely.")
                except (StopRequested, LimitReached, CreditExhausted):
                    raise
                except LiveCheckpointWriteError:
                    raise
                except Exception as e:
                    self.stats["errors"] += 1
                    self.log(f"  ! error on {w.name}: {e} "
                             f"{'(left in source, not moved)' if self.move_mode else ''}")
                    traceback.print_exc()
                self._emit_cost()
            self._phase_progress("processing", total, total)
            self.set_progress(total, total)
            review_run_id = getattr(self, "_review_run_id", "")
            # A worker-level failure leaves its folder out of the final scope.
            # Validate before writing the audit-only resume boundary so Start
            # can retry the outstanding original worker instead.
            if getattr(self, "_review_controller", None) is not None \
                    and review_run_id:
                workers = self._review_controller.validate_processing_scope(
                    review_run_id, processing_root=self.dir,
                    worker_dirs=list(self._audit_worker_dirs))
                self._audit_worker_dirs = [Path(item) for item in workers]
            checkpoint_saved = write_live_checkpoint(
                self.dir, total, total, "", self.stats.get("errors", 0),
                False, review_run_id, self._audit_worker_dirs,
                processing_complete=True)
            if review_run_id and not checkpoint_saved:
                raise LiveCheckpointWriteError(
                    "Processing completed, but its automatic-review checkpoint "
                    "could not be saved; the Accuracy Audit was not started.")
            TRACKER.update(workers_done=total, current_worker="",
                           status="complete", finished=True,
                           stats=dict(self.stats))
            self.manifest.save()
            self._record_review_processing()
            self._run_post_run_audit()
            # Retain a processing-complete checkpoint while an audit is pending
            # or failed, so Start resumes the exact captured review run.
            if self.stats.get("audit_status") in ("complete", "skipped", "disabled"):
                clear_live_checkpoint(self.dir)
            self.on_done(self.stats, None)
        except CreditExhausted as e:
            # Out of API credit: stop immediately, mark the worker we were on,
            # and surface a clear alert. NOT a per-document failure.
            self.manifest.save()
            worker = getattr(self, "_current_worker", "") or "(unknown)"
            self.log(f"\n*** STOPPED: API credit exhausted while processing "
                     f"worker '{worker}'. {e.detail} ***")
            try:
                self.failed_log.record(self.care_home, worker, "(run stopped)",
                                       "API CREDIT EXHAUSTED", e.detail)
            except Exception:
                traceback.print_exc()
            # write a clearly-marked status row into the Excel record workbook
            try:
                write_run_status(self.care_home, worker, "STOPPED - API CREDIT "
                                 "EXHAUSTED", e.detail)
            except Exception:
                traceback.print_exc()
            TRACKER.update(finished=True, status="credit exhausted",
                           stats=dict(self.stats))
            self.on_done(self.stats, f"credit:{worker}|{e.detail}")
        except LimitReached as e:
            self.manifest.save()
            self.log(f"\n*** STOPPED: {e.reason} ***")
            TRACKER.update(finished=True, status="limit reached",
                           stats=dict(self.stats))
            self.on_done(self.stats, f"limit:{e.reason}")
        except StopRequested:
            self.manifest.save()
            self.log("\n*** STOPPED by user ***")
            TRACKER.update(finished=True, status="stopped by user",
                           stats=dict(self.stats))
            self.on_done(self.stats, "stopped")
        except Exception as e:
            self.manifest.save()
            self.log(f"\n! fatal error: {e}")
            traceback.print_exc()
            self.on_done(self.stats, str(e))

    # ---- one worker ----
    def _process_worker(self, worker_dir: Path):
        cleanup_orientation_temp_files(worker_dir, self.log)
        # 0) CONVERT EVERYTHING TO PDF first (Stage 2). Images, Office docs,
        #    .msg/.eml and .txt become PDFs in place; existing PDFs are kept.
        if self.convert_pdf:
            self._phase("converting", "PDF conversion")
            self.set_status(f"{worker_dir.name}  -  converting to PDF")
            self.log("  [convert] converting documents to PDF")
            conv = PdfConverter.convert_worker(
                worker_dir, self.log, self._check_stop)
            self.stats["converted"] += conv["converted"]
            self.stats["convert_failed"] += conv["failed"]
            self.log(f"  converted {conv['converted']} file(s) to PDF, "
                     f"{conv['kept']} already PDF"
                     + (f", {conv['failed']} could not be converted"
                        if conv["failed"] else ""))

        # 1) make sure everything is loose first (handles legacy sub-folders)
        moved = flatten_worker(worker_dir, self.log)
        if moved:
            self.log(f"  flattened {moved} file(s) out of sub-folders")

        files = list_worker_docs(worker_dir)
        if not files:
            self.log("  (no documents)")
            return
        self.log(f"  {len(files)} document(s) to review")

        vocab = self.kb.vocabulary_block()
        # track renamed files for the second pass; cache page images/text so the
        # second pass does not re-render or re-download the same documents
        renamed_records = []   # dicts: path, name, group, original, imgs, text

        for f in files:
            self._check_stop()
            self._phase("processing", "Document classification")
            self.set_status(f"{worker_dir.name}  -  reviewing {self._redact(f.name)}")

            ext = f.suffix.lower()

            # --- SAFETY GUARD 1: per-file size ceiling -----------------
            # Skip very large files BEFORE reading/rendering them, so they
            # never consume memory or inflate the API payload.
            if file_too_big(f, self.max_file_mb):
                self.stats["skipped_oversized"] += 1
                self.log(f"    - {self._redact(f.name)}: skipped "
                         f"(over {self.max_file_mb:.0f} MB)")
                self.failed_log.record(self.care_home, worker_dir.name, f,
                                       "skipped: oversized",
                                       f">{self.max_file_mb:.0f} MB")
                continue

            # --- SAFETY GUARD 1b: cloud-only placeholder ----------------
            # OneDrive online-only files are skipped and NOT downloaded. This
            # check is done before any read (content sniff / render) so we never
            # accidentally trigger a download. Their paths are listed up front.
            if is_cloud_only_placeholder(f):
                self.stats["skipped_cloud"] = self.stats.get("skipped_cloud", 0) + 1
                self.log(f"    - {self._redact(f.name)}: skipped "
                         f"(cloud-only / not downloaded)")
                self.failed_log.record(self.care_home, worker_dir.name, f,
                                       "skipped: cloud-only", "not downloaded")
                continue

            # --- SAFETY GUARD 2: content/extension mismatch ------------
            if not content_matches_ext(f):
                self.stats["errors"] += 1
                self.log(f"    ! {self._redact(f.name)}: content does not match "
                         f"its extension - skipped")
                self.failed_log.record(self.care_home, worker_dir.name, f,
                                       "content/extension mismatch", "")
                continue

            # --- SAFETY GUARD 3: processed-file cache (no repeat billing) --
            # If this exact content was already classified with the same model
            # and resolution, reuse the previous result instead of paying again.
            try:
                fhash = file_hash(f)
            except Exception:
                fhash = ""
            cached_before_orientation = (
                self.manifest.seen(fhash, self.api.model_id, self.resolution)
                if fhash and not self.reprocess else None)
            # Local every-page orientation preflight runs before any paid
            # rendering/classification. Audit mode only records; automatic
            # mode atomically rewrites and immediately returns the new hash.
            orientation = self._orientation_preflight(f)
            if orientation.get("hash"):
                fhash = orientation["hash"]
            if orientation.get("changed") and cached_before_orientation:
                self.manifest.record(
                    fhash, self.api.model_id, self.resolution,
                    cached_before_orientation.get("name") or "Other",
                    cached_before_orientation.get("group") or "Other")
                self.manifest.save()
            if fhash and not self.reprocess:
                cached = (self.manifest.seen(
                    fhash, self.api.model_id, self.resolution)
                          or cached_before_orientation)
                if cached:
                    self.stats["skipped_cached"] += 1
                    self.log(f"    = {self._redact(f.name)}: already processed "
                             f"-> {cached.get('name','?')} (cached, no API call)")
                    # still rename to the cached name so the folder ends up tidy
                    cname = cached.get("name") or "Other"
                    cgroup = cached.get("group") or self.kb.group_of(cname) or "Other"
                    new_path = unique_path(worker_dir, safe_stem(cname), f.suffix)
                    try:
                        f.rename(new_path)
                        self._orientation_move_path(f, new_path)
                        renamed_records.append({"path": new_path, "name": cname,
                                                "group": cgroup, "original": f.name,
                                                "imgs": [], "text": ""})
                    except Exception as e:
                        self.log(f"    ! rename (cached) failed for "
                                 f"{self._redact(f.name)}: {e}")
                    continue

            total_pages = DocRender.page_count(f)

            # Render PAGE 1 first (cheapest). We may add more pages only if needed.
            p1_imgs, p1_text = DocRender.render(f, zoom=self.resolution, pages="first")
            self.set_preview(p1_imgs[0] if p1_imgs else None, f.name)

            if not p1_imgs and not p1_text:
                self.log(f"    - {self._redact(f.name)}: cannot render, left unchanged")
                continue

            # We are about to send this file to the API: enforce the file budget.
            self._check_file_budget()
            self.files_sent += 1

            # these hold whatever we end up using (page 1, or all pages)
            used_imgs, used_text = p1_imgs, p1_text
            features = ""
            other_label = ""
            source = "AI"

            # --- (a) special title rule: no API needed ---------------
            # Only fire for a filename whose title clearly IS the contract /
            # statement-of-terms itself - and never for a related-but-separate
            # document whose name merely CONTAINS 'employment contract' (e.g. an
            # amendment/variation, a deductions page, an application form). These
            # are distinct documents and must be classified on their own.
            low = f.stem.lower()
            forced = None
            _contract_titles = (
                "schedule of statement of main terms and conditions of employment",
                "statement of main terms of employment",
                "statement of main terms and conditions",
                "contract of employment",
                "employment contract",
            )
            _not_contract = (
                "deduction", "pay agreement", "overpayment",
                "job description", "privacy notice", "policy",
                # amendments / variations to a contract are NOT the contract
                "amendment", "amend", "variation", "addendum", "addenda",
                "annex", "appendix", "schedule of changes", "change to",
                "changes to", "extension", "renewal",
                # other things that often carry employment wording in the name
                "application", "offer letter", "reference", "checklist",
                "acknowledg", "declaration", "consent",
            )
            if (any(t in low for t in _contract_titles)
                    and not any(b in low for b in _not_contract)):
                forced = ("Employment Contract", "Crucial")

            # Each branch below produces a `result` dict (identical shape to the
            # API's own reply) plus the pages actually used. A single SHARED
            # helper, _apply_classification(), then turns that into the final
            # filename + rename + record. Overnight Batch mode calls the SAME
            # helper when applying downloaded results, so both modes file
            # documents through byte-for-byte identical logic.
            result = None
            core_page_idxs = None   # pages the model saw (per-page rotations)
            default_source = "AI"
            if forced:
                name, group = forced
                default_source = "title-rule"
                self.log(f"    - {f.name}: title rule -> {name}")
                # for the second pass we still want full pages of a contract
                used_imgs, used_text = DocRender.render(f, zoom=self.resolution, pages="all")
                result = {"match": True, "name": name, "group": group,
                          "confidence": 100}
            else:
                # --- (b) optional: skip the API when filename/text is clearly
                #         one specific type (off by default) -----------
                skipped = None
                if self.skip_when_clear:
                    skipped = self._quick_match(f, p1_text)
                if skipped:
                    name, group = skipped
                    default_source = "filename/text"
                    self.stats["skipped_api"] += 1
                    self.log(f"    - {f.name}: matched without API -> {name}")
                    used_imgs, used_text = DocRender.render(f, zoom=self.resolution, pages="all")
                    result = {"match": True, "name": name, "group": group,
                              "confidence": 100}
                else:
                    # --- (c) ADAPTIVE triage on page 1, then maybe full ---
                    # (decision logic lives in classify_document_core, shared
                    # with eval_classifier.py so both run the identical path)
                    try:
                        core = classify_document_core(
                            self.api, vocab, f,
                            resolution=self.resolution,
                            adaptive_pages=self.adaptive_pages,
                            p1_imgs=p1_imgs, p1_text=p1_text,
                            total_pages=total_pages,
                            emit_cost=self._emit_cost,
                            escalation_api=self.escalation_api,
                            bundle_split=self.bundle_split)
                        result = core["result"]
                        used_imgs = core["used_imgs"]
                        used_text = core["used_text"]
                        core_page_idxs = core.get("page_idxs")
                        if core["page1_only"]:
                            self.stats["page1_only"] += 1
                            self.log(f"    · {f.name}: identified from page 1 "
                                     f"(saved {total_pages-1} page(s))")
                        elif core["triaged"]:
                            self.log(f"    · {f.name}: page 1 not enough "
                                     f"({core['triage_reason'][:60]}) - sent {len(used_imgs)} pages")
                        if core.get("escalated"):
                            self.stats["second_opinion"] = \
                                self.stats.get("second_opinion", 0) + 1
                            self.log(f"    · {self._redact(f.name)}: low "
                                     f"confidence - second opinion from "
                                     f"{self.escalation_api.model_id}")
                    except APIError as e:
                        # Sanitized message in the activity log; full path + the
                        # raw API detail go to failed_files.csv and the console.
                        self.log(f"    ! API error on {worker_dir.name}/"
                                 f"{self._redact(f.name)}: {e.message}")
                        self.failed_log.record(self.care_home, worker_dir.name, f,
                                               f"API error {e.status}", e.detail)
                        self.stats["errors"] += 1
                        self._emit_cost()
                        continue
                    except (StopRequested, LimitReached, CreditExhausted):
                        raise
                    except Exception as e:
                        self.log(f"    ! API call failed on {worker_dir.name}/"
                                 f"{self._redact(f.name)}: {e}")
                        self.failed_log.record(self.care_home, worker_dir.name, f,
                                               "exception", str(e))
                        self.stats["errors"] += 1
                        traceback.print_exc()
                        continue

            # physically fix a sideways scan before it is renamed/filed
            new_hash = self._maybe_fix_rotation(f, result or {},
                                                page_idxs=core_page_idxs)
            if new_hash:
                fhash = new_hash
            # Orientation decisions are local-only in v1.3.1. Never let the
            # paid classifier's incidental fields rotate this parent or its
            # split children.
            self._consume_rotation_instructions(result)

            # SHARED apply path (live + batch): resolve unknowns (asking the
            # user in live mode), name, rename, log and record for the 2nd pass.
            vocab = self._apply_classification(
                worker_dir, f, result or {}, fhash, vocab, renamed_records,
                interactive=True, page1_img=(p1_imgs[0] if p1_imgs else None),
                used_imgs=used_imgs, used_text=used_text,
                default_source=default_source)

        # 2-4) DEDUP -> SECOND PASS -> ORGANISE (shared tail, used by batch too).
        self._finish_worker(worker_dir, renamed_records)

    # ---- multi-document bundle split, from the classify call's page map ----
    BUNDLE_ARCHIVE_DIRNAME = "Original Bundles"

    def _bundle_archive_dir(self, worker_dir: Path) -> Path:
        """Where a split file's ORIGINAL is kept.

        Deliberately OUTSIDE the worker folder. The old '<name>.splitbak'
        sitting next to the parts was not the durable archive its name implied:
        flatten_worker() moves every file under a worker back up into the
        processing root, and cleanup_leftover_files() deletes '.splitbak'
        outright (Settings 'clean up leftovers', on by default). Under APP_DIR
        the original is out of reach of both and sits with the other audit
        artefacts, so 'a split never destroys a document' is actually true."""
        d = (APP_DIR / self.BUNDLE_ARCHIVE_DIRNAME
             / safe_stem(self.care_home or "care home")
             / safe_stem(worker_dir.name))
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _confirm_long_bundle_plan(self, f: Path, starts, vocab: str,
                                  total: int):
        """Independently classify every proposed child before allowing a cut.

        The boundary scan and the normal document classifier use different
        prompts and views.  Requiring both to agree makes an accidental break
        in a long contract fail closed.  Every adjacent child must resolve to
        a different controlled type at high confidence; same-type packs are
        left whole and surfaced for review because distinguishing two people
        of the same document type needs identity-specific evidence.
        """
        ranges = bundle_page_ranges(total, starts)
        if len(ranges) < 2:
            return None
        plan = []
        try:
            with tempfile.TemporaryDirectory(prefix="lifted-bundle-check-") as td:
                temp_paths = []
                src = fitz.open(str(f))
                try:
                    for idx, pages in enumerate(ranges, 1):
                        tmp = Path(td) / f"segment-{idx}.pdf"
                        child = fitz.open()
                        try:
                            child.insert_pdf(src, from_page=pages[0],
                                             to_page=pages[-1])
                            child.save(str(tmp))
                        finally:
                            child.close()
                        temp_paths.append((tmp, pages))
                finally:
                    src.close()

                for tmp, pages in temp_paths:
                    core = classify_document_core(
                        self.api, vocab, tmp,
                        resolution=self.resolution,
                        adaptive_pages=False,
                        emit_cost=self._emit_cost,
                        escalation_api=self.escalation_api,
                        bundle_split=False)
                    result = core.get("result") or {}
                    matched, name, group, conf, _features, _other = \
                        validate_result(self.kb, result)
                    try:
                        conf_n = int(float(conf or 0))
                    except Exception:
                        conf_n = 0
                    if (not matched or not name
                            or conf_n < LONG_BUNDLE_CONFIRM_CONF):
                        return None

                    # Model rotations are relative to this temporary child;
                    # translate them back to the original PDF's page indexes.
                    rotations = {}
                    local_idxs = list(core.get("page_idxs") or [0])
                    reported = result.get("rotations")
                    if (isinstance(reported, list)
                            and len(reported) == len(local_idxs)):
                        for local, deg in zip(local_idxs, reported):
                            try:
                                deg = int(float(deg or 0))
                            except Exception:
                                continue
                            if deg in (90, 180, 270) and local < len(pages):
                                rotations[pages[local]] = deg
                    elif _rot_of(result):
                        for local in local_idxs:
                            if local < len(pages):
                                rotations[pages[local]] = _rot_of(result)

                    plan.append({
                        "pages": pages,
                        "type": name,
                        "group": group,
                        "matched": True,
                        "conf": conf_n,
                        "date": str(result.get("document_date") or "").strip(),
                        "rotations": rotations,
                    })
        except (StopRequested, CreditExhausted):
            raise
        except Exception:
            traceback.print_exc()
            return None

        # Independent confirmation is boundary-specific: each pair on either
        # side of a proposed cut must be confidently different.
        if any(_norm_type(a["type"]) == _norm_type(b["type"])
               for a, b in zip(plan, plan[1:])):
            return None
        return plan

    def _maybe_split_bundle(self, worker_dir: Path, f: Path, result: dict,
                            vocab: str, records: list, *, interactive: bool,
                            default_source: str, unknown_queue, depth: int):
        """Split a confidently verified short multi-document file before filing.

        Files with no more than MAX_SEG_PAGES useful pages use the single
        classification call's full `documents` page map.  Long files retain the
        v1.2 bounded sample and are never scanned or child-classified
        automatically; explicit sampled bundle evidence only creates a flag.

        Returns the updated vocabulary block when the file WAS split, or None
        when it is a normal single document."""
        if f.suffix.lower() not in PDF_EXT or not HAS_FITZ or not f.exists():
            return None
        total = DocRender.page_count(f)
        if total < 2:
            return None
        # parts of an earlier split are never re-split
        if " [doc " in f.stem:
            return None
        inks = page_ink_fractions(f)
        seg_idxs, ghosts, full_view = segmentation_pages(f, total, inks)
        if not full_view:
            if _bundle_prone(result):
                self._flag_possible_bundle(
                    worker_dir, f, total,
                    "sampled evidence suggests a possible bundle; long file "
                    "left intact")
            return None
        else:
            plan = plan_segments(self.kb, result, seg_idxs, ghosts, total)
            if not plan:
                if isinstance(result.get("documents"), list) \
                        and len(result.get("documents") or []) > 1:
                    # the model saw more than one document but the gates refused
                    # the cut - the safest outcome, still worth a human look
                    self._flag_possible_bundle(
                        worker_dir, f, total,
                        "segments failed the split gates")
                return None

        label = self._redact(f.name)
        # The local all-page preflight has already made the only permitted
        # orientation decision. Splitting copies those parent bytes verbatim.

        # ---- write the parts, then archive the original (never deleted) --
        parts = []
        try:
            src = fitz.open(str(f))
            try:
                for i, seg in enumerate(plan, 1):
                    a, b = seg["pages"][0], seg["pages"][-1]
                    out_path = unique_path(worker_dir,
                                           f"{f.stem} [doc {i}]", f.suffix)
                    w = fitz.open()
                    w.insert_pdf(src, from_page=a, to_page=b)
                    w.save(str(out_path))
                    w.close()
                    parts.append(out_path)
            finally:
                src.close()
        except Exception as e:
            for p in parts:
                try:
                    p.unlink()
                except Exception:
                    pass
            self.log(f"    ! bundle split failed on {label}: {e} - left intact.")
            return None

        archived = None
        try:
            dest = self._bundle_archive_dir(worker_dir)
            archived = unique_path(dest, f.stem, f.suffix)
            shutil.move(str(f), str(archived))
        except Exception as e:
            for p in parts:
                try:
                    p.unlink()
                except Exception:
                    pass
            self.log(f"    ! could not archive original {label}: {e} - "
                     f"left intact.")
            return None

        self._inherit_split_orientation(f, parts, plan)
        orientation_state = getattr(self, "orientation_state", None)
        if orientation_state is not None:
            orientation_state.move_path(f, archived)
        self.stats["bundles_split"] = self.stats.get("bundles_split", 0) + 1
        self.log(f"    SPLIT {label}: contained {len(plan)} documents "
                 + ", ".join(f"p{s['pages'][0] + 1}-{s['pages'][-1] + 1} "
                             f"{s['type']}" for s in plan)
                 + f" - split; original archived to {archived.parent}")
        # parent -> children mapping, so the audit tools can trace a split
        for i, (p, seg) in enumerate(zip(parts, plan), 1):
            try:
                self.rename_log.record(
                    self.care_home, worker_dir.name, f.name, p.name,
                    f"pages {seg['pages'][0] + 1}-{seg['pages'][-1] + 1} of "
                    f"{total}; original archived: {archived}",
                    "bundle-split")
            except Exception:
                pass

        # ---- apply each part through the normal shared path --------------
        # The segment already carries the model's own type/confidence for
        # exactly these pages, so no part is re-classified. From here a part is
        # an ordinary document: same naming, same duplicate ranking, same
        # OVERWRITE_TYPES placement.
        for p, seg in zip(parts, plan):
            self._check_stop()
            try:
                presult = {
                    "match": bool(seg["matched"]),
                    "name": seg["type"] if seg["matched"] else "",
                    "group": seg["group"],
                    "confidence": seg["conf"],
                    "features": f"page {seg['pages'][0] + 1}-"
                                f"{seg['pages'][-1] + 1} of a {total}-page "
                                f"bundle",
                    "other_label": "" if seg["matched"] else seg["type"],
                    "guess": seg["type"],
                    "document_date": seg.get("date", ""),
                    # already applied to the part's pages above
                    "rotation": 0, "rotations": [],
                }
                vocab = self._apply_classification(
                    worker_dir, p, presult, file_hash(p), vocab, records,
                    interactive=interactive,
                    used_imgs=[], used_text="",
                    default_source="bundle-split",
                    unknown_queue=unknown_queue,
                    _bundle_depth=depth + 1)
            except (StopRequested, CreditExhausted):
                raise
            except Exception as e:
                self.log(f"    ! failed on bundle part "
                         f"{self._redact(p.name)}: {e}")
                traceback.print_exc()
                self.stats["errors"] += 1
        return vocab

    def _flag_possible_bundle(self, worker_dir: Path, f: Path, total: int,
                              why: str):
        """Record that a file looks like a bundle that was NOT split, so it
        reaches the processing report and the recheck queue instead of
        disappearing silently into a single name."""
        self.stats["possible_bundles"] = \
            self.stats.get("possible_bundles", 0) + 1
        self.possible_bundles.append(
            {"worker": worker_dir.name, "file": f.name, "pages": total,
             "reason": why})
        self.log(f"    ? {self._redact(f.name)}: possible multi-document "
                 f"bundle ({total} pages, {why}) - left whole, flagged for "
                 f"review")

    # ---- shared apply of one classification result (live + batch) ----
    def _apply_classification(self, worker_dir: Path, f: Path, result: dict,
                              fhash: str, vocab: str, records: list, *,
                              interactive: bool, page1_img=None,
                              used_imgs=None, used_text=None,
                              default_source: str = "AI", unknown_queue=None,
                              _bundle_depth: int = 0):
        """Turn ONE classification result dict into a final filename: resolve
        the controlled type (or handle an unmatched document), rename the file,
        update stats / rename log / manifest, and append a record for the
        second pass. This is the single code path both modes use to apply a
        result, so live and Overnight Batch behave identically.

          interactive=True  (live)  : an unmatched doc opens the Define-document
                                       dialog unless Auto-Other is on; may define
                                       a new vocabulary type; may raise
                                       StopRequested if the user stops.
          interactive=False (batch) : a confident specific `other_label`/guess
                                       becomes 'Other - <description>'; only a
                                       genuinely unresolved follow-up result is
                                       filed as 'Other - Unknown'.

        The manifest is recorded ONLY here, at the moment a result is applied, so
        a crash before this point never marks a file as done.
        Returns the (possibly updated) vocabulary block."""
        used_imgs = used_imgs or []
        used_text = used_text or ""

        # ---- BUNDLE INTERCEPT: a file that wrongly contains several -------
        # documents is split FIRST; its parts are then classified and
        # applied individually through this same method (depth-limited).
        if self.bundle_split and _bundle_depth < 2:
            try:
                new_vocab = self._maybe_split_bundle(
                    worker_dir, f, result, vocab, records,
                    interactive=interactive, default_source=default_source,
                    unknown_queue=unknown_queue, depth=_bundle_depth)
            except (StopRequested, CreditExhausted):
                raise
            except Exception:
                traceback.print_exc()
                new_vocab = None
            if new_vocab is not None:
                return new_vocab        # handled as a bundle - original filed


        # validate against the live vocabulary (shared with eval_classifier.py)
        matched, name, group, conf, features, other_label = \
            validate_result(self.kb, result)
        source = default_source

        if not matched or not name:
            guess = (result.get("guess") or "").strip()
            if interactive and not self.auto_other:
                # ---- PAUSE AND ASK (pre-filled with observed features) ----
                self.log(f"    ? {f.name}: not matched (conf {conf}). Asking you to define it.")
                self.set_status(f"Awaiting your decision on: {f.name}")
                answer = self.ask_unknown(f.name, guess, features, page1_img)
                if answer is None:
                    raise StopRequested()
                decision, new_name, desc = answer
                self.kb.add(new_name, desc, "Other" if decision == "Other" else "Relevant")
                vocab = self.kb.vocabulary_block()  # known henceforth
                self.stats["unknown"] += 1
                if decision == "Other":
                    name, group = "Other", "Other"
                    other_label = new_name or other_label or guess
                else:
                    name, group = new_name, "Important"
                self.log(f"      -> classified as {decision}: \"{name}\"")
                source = "user-defined"
            elif interactive:
                # ---- AUTO-OTHER (live): no prompt, file straight to Other ----
                # The filename gets a descriptive 'Other - <phrase>' from
                # other_label, but we deliberately do NOT add each one-off
                # description to the Other vocabulary tab (that would bloat the
                # prompt). The vocabulary only grows when YOU define an unknown.
                if not other_label:
                    other_label = guess   # fall back to the AI's guess
                if not meaningful_other_label({"other_label": other_label}):
                    other_label = "Unknown"
                self.stats["unknown"] += 1
                name, group = "Other", "Other"
                source = "auto-other"
                self.log(f"    ? {f.name}: not matched (conf {conf}) "
                         f"-> auto-filed as Other "
                         f"({other_label or 'unlabelled'})")
            else:
                # ---- BATCH: preserve a confident descriptive Other answer. --
                # The primary batch's other_label/guess is already a useful
                # identification.  Do not replace it with Unknown or add it to
                # the persistent workbook.  Only a generic/low-confidence
                # follow-up result reaches the unresolved branch.
                self.stats["unknown"] += 1
                name, group = "Other", "Other"
                specific = meaningful_other_label(result)
                if _conf_int(result) >= AUTO_REVIEW_LABEL_CONF and specific:
                    other_label = specific
                    source = "batch-descriptive-other"
                    self.log(f"    ? {f.name}: not matched (conf {conf}) "
                             f"-> filed as '{other_name(other_label)}'")
                else:
                    other_label = "Unknown"
                    source = "batch-auto-other"
                    self.log(f"    ? {f.name}: not matched (conf {conf}) "
                             f"-> filed as 'Other - Unknown'")

        # OTHER group is named "Other - <descriptor>". A document matched to a
        # CONTROLLED Other-tab name (e.g. 'CoS Summary') uses that name as the
        # descriptor, so recurring Other types get one consistent filename;
        # otherwise the AI's concise label is used. Unresolved values retain the
        # explicit 'Other - Unknown' prefix.
        if group == "Other":
            if matched and name and name != "Other":
                name = other_name(name)
            else:
                name = other_name(other_label)

        # perform the rename ------------------------------------
        original = f.name
        new_path = unique_path(worker_dir, safe_stem(name), f.suffix)
        try:
            f.rename(new_path)
        except Exception as e:
            self.log(f"    ! rename failed for {original}: {e}")
            self.stats["errors"] += 1
            return vocab
        orientation_state = getattr(self, "orientation_state", None)
        if orientation_state is not None:
            orientation_state.move_path(f, new_path)
        self.stats["renamed"] += 1
        self.rename_log.record(self.care_home, worker_dir.name, original,
                               new_path.name, group, source)
        # remember it so a future re-run of this folder skips it (no re-billing).
        # Recorded here, at apply time, so nothing is marked done prematurely.
        if fhash:
            self.manifest.record(fhash, self.api.model_id, self.resolution,
                                 name, group)
        self.log(f"    + {self._redact(original)}  ->  {self._redact(new_path.name)}")
        records.append({"path": new_path, "name": name,
                        "group": group, "original": original,
                        "imgs": used_imgs, "text": used_text})
        # Queue genuine batch unknowns ('Other - Unknown') for the review pass.
        if (not interactive) and unknown_queue is not None \
                and source == "batch-auto-other":
            unknown_queue.append({"worker_dir": str(worker_dir),
                                  "path": str(new_path),
                                  "worker_name": worker_dir.name,
                                  "guess": (result.get("guess") or "").strip(),
                                  "features": features,
                                  "fhash": fhash})
        return vocab

    # ---- shared worker tail: dedupe -> second pass -> organise ----
    def _records_from_manifest(self, worker_dir: Path):
        """Rebuild finishing inputs after an interrupted batch-apply restart."""
        records = []
        for path in list_worker_docs(worker_dir):
            try:
                fhash = file_hash(path)
            except Exception:
                continue
            cached = self.manifest.seen(
                fhash, self.api.model_id, self.resolution)
            if not cached:
                continue
            records.append({"path": path,
                            "name": cached.get("name") or path.stem,
                            "group": cached.get("group") or "Other",
                            "original": path.name, "imgs": [], "text": ""})
        return records

    def _finish_worker(self, worker_dir: Path, renamed_records: list):
        """Steps 2-4 shared by live and batch modes: remove exact duplicates,
        run the second-pass reviews (dating / ranking / signed checks - these
        always run LIVE, even after a batch), then organise into 'Overwrite
        Documents' and 'Bulk' sub-folders."""
        # 2) DEDUP FIRST - remove exact-copy duplicates, keep one of each.
        #    Done before the second pass so identical copies are collapsed
        #    before the (costly) CoS/contract/RTW analysis runs on them, and
        #    so the survivor is the one that gets any suffix tag.
        self._check_stop()
        self._phase("deduplicating", "Exact-duplicate review")
        self.log("  [review] scanning for duplicate documents")
        removed = dedup_worker(worker_dir, self.log)
        self.stats["duplicates"] += removed
        if removed:
            self.log(f"  removed {removed} duplicate file(s)")
            # rebuild the record list to reflect deletions/renames
            renamed_records = self._rebuild_records(worker_dir, renamed_records)
        else:
            self.log("  no duplicates found")

        # 3) SECOND PASS - special reviews -------------------------
        self._phase("ranking", "Dating, signed checks and ranking")
        self._second_pass(worker_dir, renamed_records)

        # 4) ORGANISE into two sub-folders: 'Overwrite Documents' (only the
        #    OVERWRITE_TYPES, loose, for Stage 3's individual overwrite flow)
        #    and 'Bulk' (everything else, split into Batch NN folders of up to
        #    30 for bulk upload).
        self._phase("organising", "Organising processed files")
        org = organize_worker(
            worker_dir, log=self.log,
            on_move=self._orientation_move_path)
        self.stats["overwrite"] = self.stats.get("overwrite", 0) + org["overwrite"]
        self.stats["bulk"] = self.stats.get("bulk", 0) + org["bulk"]
        self.log(f"  organised: {org['overwrite']} -> 'Overwrite Documents', "
                 f"{org['bulk']} -> 'Bulk' (batches of {BATCH_SIZE})")

        # 5) LEFTOVER CLEANUP: processing residue serves no further purpose -
        #    .splitbak backups from the split tools, and .zip archives whose
        #    documents were extracted at flatten time (unreadable archives are
        #    kept and reported, never silently lost).
        if self.cleanup_leftovers:
            n = cleanup_leftover_files(worker_dir, self.log)
            if n:
                self.stats["leftovers_removed"] = \
                    self.stats.get("leftovers_removed", 0) + n
                self.log(f"  removed {n} leftover file(s) (.splitbak/.zip)")

    # ================================================================
    # OVERNIGHT BATCH MODE  (Message Batches API - 50% cheaper)
    # ----------------------------------------------------------------
    # Phase A (run_batch_submit): convert + flatten locally, build the SAME
    # classification request live mode would send for every eligible document,
    # and submit them all as one or more Message Batches. State is written to
    # a hidden file in the care-home folder so the app can be closed.
    # Phase B (run_batch_apply): poll the primary batch(es), settle confident
    # controlled/descriptive-Other answers, and send only genuinely unresolved
    # documents to one discounted follow-up batch.  Application, live finishing
    # and worker movement wait until that follow-up ends.  The manifest is only
    # updated when a result is APPLIED.
    # ================================================================

    # keep each submitted batch comfortably inside the API's 256 MB / 100k caps
    BATCH_SUBMIT_MAX_BYTES = 100 * 1024 * 1024   # per-chunk memory/network cap
    BATCH_SUBMIT_MAX_REQUESTS = 10_000
    FOLLOWUP_CHUNK_TARGET_BYTES = 90 * 1024 * 1024
    FOLLOWUP_LARGE_MIN_REQUESTS = 25
    FOLLOWUP_LARGE_RATIO = 0.25
    PRIMARY_RECOVERY_CHUNK_BYTES = 40 * 1024 * 1024
    PRIMARY_RECOVERY_TARGET_BYTES = 10 * 1024 * 1024
    PRIMARY_RECOVERY_GRACE_SECONDS = 15 * 60
    PRIMARY_RECOVERY_RECHECK_SECONDS = 10

    def _batch_skip_file(self, worker_dir: Path, f: Path) -> str:
        """Apply live mode's pre-API skip rules to one file. Returns a reason
        string when the file must NOT be sent to the batch, else ''."""
        if file_too_big(f, self.max_file_mb):
            self.stats["skipped_oversized"] += 1
            self.failed_log.record(self.care_home, worker_dir.name, f,
                                   "skipped: oversized",
                                   f">{self.max_file_mb:.0f} MB")
            return "oversized"
        if is_cloud_only_placeholder(f):
            self.stats["skipped_cloud"] = self.stats.get("skipped_cloud", 0) + 1
            self.failed_log.record(self.care_home, worker_dir.name, f,
                                   "skipped: cloud-only", "not downloaded")
            return "cloud-only"
        if not content_matches_ext(f):
            self.stats["errors"] += 1
            self.failed_log.record(self.care_home, worker_dir.name, f,
                                   "content/extension mismatch", "")
            return "content/extension mismatch"
        return ""

    @staticmethod
    def _primary_time(value):
        """Provider times are UTC; old local state timestamps have no offset."""
        if not value:
            raise RuntimeError("Submission timestamp is missing")
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp()

    def _primary_provider_evidence(self, state):
        """Read-only reconciliation. An incomplete/uncertain listing fails closed."""
        marker = state.data.get("primary_submission") or {}
        ambiguous = marker.get("status") in ("submission_started", "ambiguous")
        expected = set(marker.get("request_identities") or []) if ambiguous else set()
        known = set(state.batch_ids())
        accepted, matched = set(), []
        for batch in state.data.get("batches", []):
            ids = batch.get("request_ids")
            if ids is None:
                remote = self.api.get_batch(batch["id"])
                if remote.get("processing_status") != "ended" or not remote.get("results_url"):
                    raise RuntimeError("An earlier accepted batch has not finished; check again later")
                rows = list(self.api.batch_results(remote["results_url"]))
                ids = [row.get("custom_id") for row in rows]
                if (len(ids) != int(batch.get("n", -1)) or len(set(ids)) != len(ids)
                        or any(not cid or cid not in state.data.get("requests", {}) for cid in ids)):
                    raise RuntimeError("Earlier batch results do not exactly match the saved requests")
            if (not isinstance(ids, list) or len(ids) != int(batch.get("n", -1))
                    or len(set(ids)) != len(ids)
                    or any(not cid or cid not in state.data.get("requests", {}) for cid in ids)):
                raise RuntimeError("Saved accepted-batch identities are incomplete or invalid")
            if accepted.intersection(ids):
                raise RuntimeError("Accepted primary batches overlap; independent review is required")
            accepted.update(ids)
        if not ambiguous:
            return {"accepted_ids": sorted(accepted), "matched_batches": [],
                    "checked_ts": datetime.datetime.now().isoformat(timespec="seconds")}
        if not expected or expected.intersection(accepted):
            raise RuntimeError("The uncertain chunk's request identities are missing or overlap accepted work")
        started = self._primary_time(marker.get("started_ts"))
        window_start, window_end = started - 600, started + 600
        after_id, seen, covered = "", set(), False
        candidates = []
        for _page in range(1000):
            self._check_stop()
            page = self.api.list_batches(after_id=after_id, limit=100)
            rows = page.get("data")
            if not isinstance(rows, list) or not isinstance(page.get("has_more"), bool):
                raise RuntimeError("Provider batch listing was incomplete or malformed")
            timestamps = []
            for row in rows:
                bid = str(row.get("id") or "")
                if not bid or bid in seen:
                    raise RuntimeError("Provider pagination repeated or omitted a batch identity")
                seen.add(bid)
                created = self._primary_time(row.get("created_at"))
                timestamps.append(created)
                if window_start <= created <= window_end and bid not in known:
                    candidates.append(row)
            if timestamps != sorted(timestamps, reverse=True):
                raise RuntimeError("Provider batch listing was not newest-first")
            if not page["has_more"] or (timestamps and min(timestamps) < window_start):
                covered = True
                break
            cursor = str(page.get("last_id") or (rows[-1].get("id") if rows else ""))
            if not cursor or cursor == after_id:
                raise RuntimeError("Provider batch listing did not provide a usable next page")
            after_id = cursor
        if not covered:
            raise RuntimeError("Provider listing did not cover the original submission window")
        for candidate in candidates:
            remote = self.api.get_batch(candidate["id"])
            if remote.get("processing_status") != "ended" or not remote.get("results_url"):
                raise RuntimeError("A possible matching provider batch is still running or has no results; wait before recovery")
            rows = list(self.api.batch_results(remote["results_url"]))
            ids = [row.get("custom_id") for row in rows]
            counts = remote.get("request_counts") or {}
            total = sum(int(counts.get(key, 0) or 0) for key in (
                "processing", "succeeded", "errored", "canceled", "expired"))
            if not ids or len(set(ids)) != len(ids) or any(not cid for cid in ids) or total != len(ids):
                raise RuntimeError("A candidate batch's results are incomplete; no resubmission is safe")
            if set(ids) == expected:
                matched.append({"id": remote["id"], "n": len(ids),
                                "status_at_submit": "ended", "request_ids": ids})
            elif expected.intersection(ids):
                raise RuntimeError("A provider batch only partially overlaps the uncertain chunk; independent review is required")
        if len(matched) > 1:
            raise RuntimeError("More than one provider batch matches; possible duplicate submission needs review")
        if matched:
            accepted.update(expected)
        return {"accepted_ids": sorted(accepted), "matched_batches": matched,
                "no_match": not matched, "attempt_started": started,
                "checked_ts": datetime.datetime.now().isoformat(timespec="seconds")}

    def _primary_recovery_inventory(self, state):
        """Validate saved fingerprints and reconstruct a legacy tail without prep."""
        saved = state.data.get("primary_inventory")
        requests = state.data.get("requests") or {}
        inventory = {cid: dict(meta) for cid, meta in
                     (saved if saved is not None else requests).items()}
        root = self.dir.resolve()
        paths = {}
        for cid, meta in inventory.items():
            path = Path(meta["path"])
            if (not path.is_file() or path.is_symlink() or root not in path.resolve().parents
                    or is_cloud_only_placeholder(path) or file_hash(path) != meta.get("fhash")):
                raise RuntimeError("A saved source document is missing, moved or changed; restore or review it before recovery")
            key = os.path.normcase(str(path.resolve()))
            if key in paths:
                raise RuntimeError("Saved inventory contains duplicate paths")
            paths[key] = cid
        if saved is not None:
            return inventory, 0
        # v1.3.2 persisted only files rendered before the failed POST. Discover
        # the remaining original files, preserving existing path-specific IDs.
        cutoff = self._primary_time((state.data.get("primary_submission") or {}).get("started_ts")
                                    or state.data.get("submitted_ts")) + 2
        settings = state.data.get("settings") or {}
        max_workers = int(settings.get("max_workers", self.max_workers))
        max_files = int(settings.get("max_files", self.max_files))
        max_file_mb = float(settings.get("max_file_mb", self.max_file_mb))
        reprocess = bool(settings.get("reprocess", self.reprocess))
        workers = worker_dirs_in(self.dir)
        if max_workers > 0:
            workers = workers[:max_workers]
        discovered = []
        for worker in workers:
            if self.move_mode and self.move_dest and (self.move_dest / worker.name).exists():
                if any(meta.get("worker_dir") == str(worker) for meta in requests.values()):
                    raise RuntimeError("A previously submitted worker now exists in the processed destination; review before recovery")
                continue
            # Do NOT call list_worker_docs: it removes orientation temp files.
            documents = [p for p in worker.rglob("*") if p.is_file()
                         and p.suffix.lower() in DOC_EXT and not is_program_file(p)]
            documents.sort(key=lambda p: natural_key(p.name))
            for path in documents:
                self._check_stop()
                key = os.path.normcase(str(path.resolve()))
                if key in paths:
                    discovered.append((paths[key], inventory[paths[key]]))
                else:
                    if file_too_big(path, max_file_mb) or is_cloud_only_placeholder(path) or not content_matches_ext(path):
                        continue
                    if path.is_symlink() or root not in path.resolve().parents:
                        raise RuntimeError("Legacy recovery found a source link outside the care-home folder")
                    digest = file_hash(path)
                    if not reprocess and self.manifest.seen(digest, self.api.model_id, self.resolution):
                        continue
                    stat = path.stat()
                    if max(stat.st_mtime, stat.st_ctime) > cutoff:
                        raise RuntimeError("New or modified documents exist after the submission; review the legacy inventory before recovery")
                    index = 0
                    while f"{digest[:56]}-{index:03d}" in inventory:
                        index += 1
                    cid = f"{digest[:56]}-{index:03d}"
                    meta = {"path": str(path), "worker": worker.name,
                            "worker_dir": str(worker), "fhash": digest,
                            "pages": DocRender.page_count(path)}
                    inventory[cid] = meta
                    paths[key] = cid
                    discovered.append((cid, meta))
                if max_files > 0 and len(discovered) >= max_files:
                    break
            if max_files > 0 and len(discovered) >= max_files:
                break
        if not set(requests).issubset({cid for cid, _meta in discovered}):
            raise RuntimeError("Current folder/worker limits exclude previously submitted documents")
        return dict(discovered), len(discovered) - len(requests)

    def _resume_primary_inventory(self, state, inventory, accepted):
        """Submit only proven-unsubmitted IDs; every POST has a durable marker."""
        vocab = state.data.get("primary_vocabulary") or self.kb.vocabulary_block()
        remaining = [cid for cid in inventory if cid not in accepted]
        chunk, chunk_ids = [], []
        envelope_bytes = len(json.dumps({"requests": []}).encode("utf-8"))
        chunk_bytes = envelope_bytes
        chunk_no = len(state.data.get("primary_chunks") or [])
        expected_disk = json.loads(json.dumps(state.data))

        def submit():
            nonlocal chunk_no, expected_disk, chunk_bytes
            if not chunk:
                return
            self._check_stop()
            for cid in chunk_ids:
                meta = inventory[cid]
                path = Path(meta["path"])
                if not path.is_file() or file_hash(path) != meta["fhash"]:
                    raise RuntimeError("Source changed while preparing recovery; no request sent for this chunk")
            wire_bytes = len(json.dumps({"requests": chunk}).encode("utf-8"))
            if wire_bytes > self.PRIMARY_RECOVERY_CHUNK_BYTES:
                raise RuntimeError("One rendered request exceeds the recovery upload limit; reduce that document separately")
            if BatchState(self.dir).data != expected_disk:
                raise RuntimeError("Another process changed the batch state while preparing recovery; no request sent for this chunk")
            chunk_no += 1
            self.set_status(f"Uploading recovery chunk {chunk_no}: {len(chunk)} document(s), "
                            f"{wire_bytes / 1048576:.1f} MiB…")
            marker = {"status": "submission_started", "submission_started": True,
                      "planned_chunk_id": f"primary-recovery-{chunk_no:04d}",
                      "request_identities": list(chunk_ids),
                      "attempt_id": hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:24],
                      "started_ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                      "wire_bytes": wire_bytes}
            state.data["primary_submission"] = marker
            state.data["phase"] = "primary_submission_started"
            if not state.save():
                raise RuntimeError("Recovery submission marker could not be saved; no request sent")
            try:
                created = self.api.submit_batch(list(chunk))
                bid = str(created.get("id") or "")
                if not bid:
                    raise RuntimeError("Provider returned no batch ID")
            except Exception:
                marker["status"] = "ambiguous"
                state.data["phase"] = "primary_submission_ambiguous"
                state.save()
                raise
            state.add_batch(bid, len(chunk), created.get("processing_status", ""),
                            request_ids=chunk_ids)
            marker.update(status="accepted", batch_id=bid)
            state.data.setdefault("primary_chunks", []).append(dict(marker))
            state.data["phase"] = "primary_preparing"
            if not state.save():
                raise RuntimeError("Accepted recovery batch ID could not be saved; stop and reconcile before retrying")
            expected_disk = json.loads(json.dumps(state.data))
            accepted.update(chunk_ids)
            self.log(f"  > recovered submission {bid} with {len(chunk)} request(s)")
            chunk.clear()
            chunk_ids.clear()
            chunk_bytes = envelope_bytes

        for index, cid in enumerate(remaining, 1):
            self._check_stop()
            meta = inventory[cid]
            path = Path(meta["path"])
            self.set_status(f"Preparing remaining primary document {index}/{len(remaining)}")
            self._phase_progress("recovery", index - 1, len(remaining))
            self.set_progress(index - 1, len(remaining))
            imgs, text, page_idxs, total_pages, segment_view = self._batch_classification_view(path)
            if not imgs and not text:
                raise RuntimeError("A remaining document cannot be rendered; review it before recovery")
            system, blocks, mt = self.api.classify_payload(vocab, imgs, text,
                page_idxs=page_idxs, total_pages=total_pages, segment=segment_view)
            req = self.api.build_batch_request(cid, system, blocks, mt)
            req_size = len(json.dumps(req).encode("utf-8"))
            if req_size + envelope_bytes > self.PRIMARY_RECOVERY_CHUNK_BYTES:
                raise RuntimeError("One rendered request exceeds the 40 MiB recovery upload limit")
            # Aim for short network writes. A single request may exceed the
            # target (without lowering document quality), but never the hard cap.
            if chunk and (chunk_bytes + req_size + 2 > self.PRIMARY_RECOVERY_TARGET_BYTES
                          or len(chunk) >= self.BATCH_SUBMIT_MAX_REQUESTS):
                submit()
            chunk_bytes += req_size + (2 if chunk else 0)
            chunk.append(req)
            chunk_ids.append(cid)
        submit()
        if BatchState(self.dir).data != expected_disk:
            raise RuntimeError("Batch state changed before marking primary submission complete")
        state.data["primary_submission_complete"] = True
        state.data["phase"] = "primary_pending"
        if not state.save():
            raise RuntimeError("Primary completion could not be saved; resume recovery before applying")

    @_care_home_writer_operation
    def recover_primary_submission(self, allow_resubmit=False):
        """Assess read-only, or explicitly authorize snapshot + reconciled resume.

        False returns status/message/remaining without file writes or callbacks.
        True calls on_done on every outcome, never applies results automatically.
        """
        try:
            state = BatchState(self.dir)
            if not state.exists() or state.data.get("applied") or state.data.get("followup"):
                raise RuntimeError("This is not an interrupted primary submission")
            self.api = self._api_for_model(state.data.get("model_id") or self.api.model_id)
            self.resolution = float(state.data.get("resolution", self.resolution))
            settings = state.data.get("settings") or {}
            for name in ("bundle_split", "adaptive_pages", "post_run_audit"):
                if name in settings:
                    setattr(self, name, bool(settings[name]))
            budgets = [value for value in (self.max_budget_gbp,
                       float(settings.get("max_budget_gbp", 0) or 0)) if value > 0]
            budget = min(budgets) if budgets else 0
            inventory, tail_count = self._primary_recovery_inventory(state)
            evidence = self._primary_provider_evidence(state)
            accepted = set(evidence["accepted_ids"])
            if not accepted.issubset(inventory):
                raise RuntimeError("Accepted provider results contain IDs outside the source inventory")
            remaining = len(set(inventory) - accepted)
            vocab = state.data.get("primary_vocabulary") or self.kb.vocabulary_block()
            estimate = estimate_pipeline_costs_gbp(len(inventory), self.api.model_id,
                state.data.get("followup_model_id") or self.api.model_id,
                self.resolution, vocab, batch=True, include_audit=self.post_run_audit)
            if budget and estimate["gbp"] > budget:
                raise RuntimeError(f"Full recovered pipeline estimate £{estimate['gbp']:.2f} exceeds the £{budget:.2f} budget")
            if evidence.get("no_match") and time.time() - evidence["attempt_started"] < self.PRIMARY_RECOVERY_GRACE_SECONDS:
                raise RuntimeError("The uncertain upload is too recent; wait at least 15 minutes before checking recovery")
            report = {"status": "needs_authorization", "remaining": remaining,
                      "accepted": len(accepted), "legacy_tail": tail_count,
                      "estimated_remaining_gbp": round(estimate["primary_gbp"] * remaining / max(1, len(inventory)), 4),
                      "message": f"Verified {len(accepted)} accepted request(s); {remaining} remain. "
                                 f"Reconstructed {tail_count} legacy tail document(s) without changing files. "
                                 "Recovery backs up the state and submits only the verified remaining IDs."}
            if not allow_resubmit:
                return report
            if evidence.get("no_match"):
                self.set_status("Rechecking provider before authorized recovery…")
                time.sleep(self.PRIMARY_RECOVERY_RECHECK_SECONDS)
                self._check_stop()
                evidence = self._primary_provider_evidence(state)
                accepted = set(evidence["accepted_ids"])
            # Revalidate every source after the provider check and before state mutation.
            verified_inventory, _tail_count = self._primary_recovery_inventory(state)
            if verified_inventory != inventory:
                raise RuntimeError("Source inventory changed during recovery checks; nothing submitted")
            if BatchState(self.dir).data != state.data:
                raise RuntimeError("Another process changed the batch state; close that operation before recovery")
            backup = state.recovery_snapshot()
            state.data.setdefault("recovery_history", []).append({
                "snapshot": str(backup), "evidence": evidence,
                "authorized_ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "legacy_tail_count": tail_count})
            for batch in evidence["matched_batches"]:
                state.data.setdefault("batches", []).append(batch)
            state.data["primary_inventory"] = inventory
            if getattr(self, "_review_run_id", ""):
                state.data["auto_review_run_id"] = self._review_run_id
            state.data["requests"] = {cid: dict(meta) for cid, meta in inventory.items()}
            state.data["primary_vocabulary"] = vocab
            state.data["primary_submission_complete"] = False
            state.data["primary_submission"] = {"status": "reconciled"}
            state.data["phase"] = "primary_preparing"
            state.data["est_gbp"] = round(estimate["gbp"], 4)
            if not state.save():
                raise RuntimeError("Reconciled state could not be saved; no remaining requests submitted")
            self._resume_primary_inventory(state, inventory, accepted)
            self.on_done(self.stats, f"batch_submitted:{len(inventory)}|{len(state.batch_ids())}|"
                         f"{estimate['primary_gbp']:.2f}|{estimate['gbp']:.2f}")
            report.update(status="submitted", message="Primary submission recovered; use Check batch status to apply when ready.",
                          remaining=0, snapshot=str(backup))
            return report
        except Exception as exc:
            message = str(exc)
            self.log(f"Primary recovery blocked: {message}")
            if allow_resubmit:
                self.on_done(self.stats, "batch_recovery_blocked:" + message)
            return {"status": "blocked", "message": message, "remaining": 0}

    def _batch_classification_view(self, path: Path):
        """Render the same bounded classification view used by live mode.

        A short PDF that can be shown in full keeps the one-request documents
        map used for safe bundle splitting.  A long/non-full-view PDF keeps the
        ordinary first-two-plus-last sample and never enters the long scanner.
        Returns (images, text, page_indices, total_pages, segment_view).
        """
        total = DocRender.page_count(path)
        page_idxs = _audit_pages_for(path)
        segment_view = False
        pages = "all"
        max_pages = None
        if (getattr(self, "bundle_split", True)
                and path.suffix.lower() in PDF_EXT
                and total > 1):
            seg_idxs, _ghosts, full_view = segmentation_pages(path, total)
            if full_view:
                page_idxs = seg_idxs
                segment_view = True
                pages = seg_idxs
                max_pages = MAX_SEG_PAGES
        imgs, text = DocRender.render(
            path, zoom=self.resolution, pages=pages, max_pages=max_pages)
        return imgs, text, page_idxs, total, segment_view

    @staticmethod
    def _batch_worker_state(state: "BatchState", source: Path):
        """Return only the durable worker record for one exact source path."""
        key = str(Path(source).resolve()).casefold()
        direct = state.data.get("workers", {}).get(key)
        if direct is not None:
            return direct
        for worker in state.data.get("workers", {}).values():
            try:
                if str(Path(worker.get("source_path") or "").resolve()).casefold() == key:
                    return worker
            except Exception:
                continue
        return None

    def _batch_submitted_worker_scope(self, state: "BatchState"):
        """Resolve the immutable worker cohort selected at batch submission.

        Version-5 states persist this list before the first provider request.
        Older states may be inferred only from their saved request inventory;
        the current Files root is never used as a fallback because it may now
        contain later, unsubmitted workers.
        """
        raw = state.data.get("submitted_worker_scope")
        inferred = raw is None
        if inferred:
            inventory = state.data.get("primary_inventory")
            evidence = (inventory if isinstance(inventory, dict) and inventory
                        else state.data.get("requests"))
            if not isinstance(evidence, dict) or not evidence:
                raise RuntimeError(
                    "The legacy batch has no saved worker-scope evidence. "
                    "Apply is blocked rather than including the current Files root.")
            raw = []
            seen = set()
            for meta in evidence.values():
                if not isinstance(meta, dict):
                    raise RuntimeError("The saved batch worker-scope evidence is invalid.")
                source = str(meta.get("worker_dir") or "").strip()
                name = str(meta.get("worker") or Path(source).name).strip()
                key = source.casefold()
                if not source or not name:
                    raise RuntimeError("The saved batch worker-scope evidence is incomplete.")
                if key not in seen:
                    seen.add(key)
                    raw.append({"name": name, "source_path": source})

        if not isinstance(raw, list) or not raw:
            raise RuntimeError("The submitted batch worker scope is missing or empty.")
        root = self.dir.resolve()
        scope = []
        names = set()
        paths = set()
        for item in raw:
            if not isinstance(item, dict):
                raise RuntimeError("The submitted batch worker scope is invalid.")
            name = str(item.get("name") or "").strip()
            source_text = str(item.get("source_path") or "").strip()
            if not name or not source_text:
                raise RuntimeError("The submitted batch worker scope is incomplete.")
            source = Path(source_text).resolve()
            name_key = name.casefold()
            path_key = str(source).casefold()
            if (source.parent != root or source.name.casefold() != name_key
                    or name_key in names or path_key in paths):
                raise RuntimeError(
                    "The submitted batch worker scope does not match unique "
                    "immediate folders under its original Files root.")
            names.add(name_key)
            paths.add(path_key)
            if source.exists():
                if not source.is_dir() or source.is_symlink():
                    raise RuntimeError("A submitted worker path is no longer a safe folder.")
            else:
                worker = self._batch_worker_state(state, source)
                final_text = str((worker or {}).get("final_path")
                                 or (worker or {}).get("movement_target") or "")
                final = Path(final_text) if final_text else None
                if (not worker or worker.get("movement_status") not in
                        ("started", "complete") or final is None
                        or not final.is_dir()):
                    raise RuntimeError(
                        f"Submitted worker '{name}' is missing and has no "
                        "durable completed-move record.")
            scope.append(source)

        # Every saved request must belong to the selected cohort.  This catches
        # both a damaged new state and an unsafe legacy inference.
        for collection_name in ("primary_inventory", "requests"):
            collection = state.data.get(collection_name)
            if not isinstance(collection, dict):
                continue
            for meta in collection.values():
                try:
                    key = str(Path(meta["worker_dir"]).resolve()).casefold()
                except Exception as exc:
                    raise RuntimeError(
                        "A saved batch request has invalid worker-scope metadata.") from exc
                if key not in paths:
                    raise RuntimeError(
                        "A saved batch request falls outside the submitted worker scope.")

        if inferred:
            state.data["version"] = max(5, int(state.data.get("version", 1)))
            state.data["submitted_worker_scope"] = [
                {"name": source.name, "source_path": str(source)}
                for source in scope]
            if not state.save():
                raise DurableStateError(
                    "the inferred legacy worker scope could not be persisted")
        return scope

    @_care_home_writer_operation
    def run_batch_submit(self):
        """Phase A: collect & submit. Runs on the background thread like run().
        Finishes by calling on_done(stats, 'batch_submitted:<n>|<m>|<est£>') or
        an error status; never proceeds to classification locally."""
        try:
            state = BatchState(self.dir)
            if state.exists():
                self.log("A batch is already pending for this folder - "
                         "check its status instead of submitting a new one.")
                self.on_done(self.stats, "batch_already_pending")
                return

            workers = worker_dirs_in(self.dir)
            if not workers:
                self.log("No worker sub-folders found in that care-home folder.")
                self.on_done(self.stats, None)
                return
            total = len(workers)
            if self.max_workers > 0 and total > self.max_workers:
                self.log(f"NOTE: {total} worker folders found; this submission is "
                         f"capped at {self.max_workers} (change in Settings).")
                total = self.max_workers
            workers = workers[:total]

            # ---- local prep: convert to PDF + flatten (no API) ----
            self._phase("preparing", "Preparing worker folders")
            self._phase_progress("preparing", 0, total)
            for idx, w in enumerate(workers, 1):
                self._check_stop()
                self.set_progress(idx - 1, total)
                # move mode: a worker already in the destination is done
                if self.move_mode and (self.move_dest / w.name).exists():
                    self.stats["skipped_done"] += 1
                    self.log(f"\n=== Worker {idx}/{total}: {w.name} — already "
                             f"in destination, skipped ===")
                    self._phase_progress("preparing", idx, total)
                    continue
                self.set_status(f"Preparing {idx}/{total}: {w.name}")
                self.log(f"\n=== Preparing worker {idx}/{total}: {w.name} ===")
                if self.convert_pdf:
                    conv = PdfConverter.convert_worker(w, self.log, self._check_stop)
                    self.stats["converted"] += conv["converted"]
                    self.stats["convert_failed"] += conv["failed"]
                moved = flatten_worker(w, self.log)
                if moved:
                    self.log(f"  flattened {moved} file(s)")
                # A preparation pass finished, not a guarantee every conversion
                # succeeded. Conversion failures retain their existing counters.
                self._phase_progress("preparing", idx, total)
            self._phase_progress("preparing", total, total, state="complete")

            # ---- enumerate eligible documents (same skip rules as live) ----
            self.set_status("Scanning documents for the batch…")
            self._phase("scanning", "Scanning documents and local orientation checks")
            scanned_workers = scanned_documents = 0
            self._phase_progress("scanning", 0, total, state="scanning", documents=0)
            eligible = []   # (worker_dir, path, fhash, pages)
            limit_reached = limit_leaves_scope = False
            for worker_index, w in enumerate(workers):
                self._check_stop()
                if self.move_mode and (self.move_dest / w.name).exists():
                    scanned_workers += 1
                    self._phase_progress("scanning", scanned_workers, total,
                                         state="scanning", documents=scanned_documents)
                    continue
                documents = list(list_worker_docs(w))
                for document_index, f in enumerate(documents):
                    scanned_documents += 1
                    self._phase_progress("scanning", scanned_workers, total,
                                         state="scanning", documents=scanned_documents,
                                         operation=f"scan:{scanned_documents}")
                    reason = self._batch_skip_file(w, f)
                    if reason:
                        self.log(f"    - {self._redact(f.name)}: skipped ({reason})")
                        continue
                    try:
                        fhash = file_hash(f)
                    except Exception as e:
                        self.stats["errors"] += 1
                        self.log(f"    ! could not hash {self._redact(f.name)}: {e}")
                        continue
                    cached_before_orientation = (
                        self.manifest.seen(
                            fhash, self.api.model_id, self.resolution)
                        if fhash and not self.reprocess else None)
                    self._phase_progress("scanning", scanned_workers, total,
                                         state="orienting" if self.orientation_mode != "off" else "scanning",
                                         documents=scanned_documents,
                                         operation=f"orientation:{scanned_documents}")
                    orientation = self._orientation_preflight(f)
                    self._phase_progress("scanning", scanned_workers, total,
                                         state="scanning", documents=scanned_documents,
                                         operation=f"scan-returned:{scanned_documents}")
                    if orientation.get("hash"):
                        fhash = orientation["hash"]
                    if orientation.get("changed") \
                            and cached_before_orientation:
                        self.manifest.record(
                            fhash, self.api.model_id, self.resolution,
                            cached_before_orientation.get("name") or "Other",
                            cached_before_orientation.get("group") or "Other")
                        self.manifest.save()
                    if fhash and not self.reprocess and (
                            self.manifest.seen(
                                fhash, self.api.model_id, self.resolution)
                            or cached_before_orientation):
                        self.stats["skipped_cached"] += 1
                        self.log(f"    = {self._redact(f.name)}: already processed "
                                 f"(cached - will be applied without the API)")
                        continue
                    eligible.append((w, f, fhash, DocRender.page_count(f)))
                    if self.max_files > 0 and len(eligible) >= self.max_files:
                        limit_reached = True
                        limit_leaves_scope = (
                            document_index + 1 < len(documents)
                            or worker_index + 1 < total)
                        if limit_leaves_scope:
                            self.log(f"NOTE: file limit reached ({self.max_files}); "
                                     f"remaining documents are left for a later run.")
                        if document_index + 1 == len(documents):
                            # The allowed final document completed this worker,
                            # even if a later worker remains outside the limit.
                            scanned_workers += 1
                            self._phase_progress("scanning", scanned_workers, total,
                                                 state="scanning", documents=scanned_documents)
                        break
                if limit_reached:
                    break
                scanned_workers += 1
                self._phase_progress("scanning", scanned_workers, total,
                                     state="scanning", documents=scanned_documents)
            # A configured limit is limited only while input remains unvisited;
            # retain the original denominator and never manufacture 100%.
            self._phase_progress("scanning", scanned_workers, total,
                                 state="limited" if limit_leaves_scope else "complete",
                                 documents=scanned_documents)

            if not eligible:
                self.log("Nothing to submit - every document is cached, skipped "
                         "or missing.")
                self.on_done(self.stats, "batch_nothing_to_submit")
                return

            # ---- BUDGET GATE (submit time - a batch cannot be stopped later) --
            vocab = self.kb.vocabulary_block()
            followup_api = self.escalation_api or self.api
            est = estimate_pipeline_costs_gbp(
                len(eligible), self.api.model_id, followup_api.model_id,
                self.resolution, vocab, batch=True,
                include_audit=self.post_run_audit)
            if self.max_budget_gbp > 0 and est["gbp"] > self.max_budget_gbp:
                over = est["gbp"] - self.max_budget_gbp
                self.log(f"\n*** NOT SUBMITTED: estimated batch cost "
                         f"£{est['gbp']:.2f} exceeds the £{self.max_budget_gbp:.2f} "
                         f"budget by £{over:.2f}. Raise the budget in Settings or "
                         f"reduce the files/resolution. ***")
                self.on_done(self.stats,
                             f"batch_over_budget:{est['gbp']:.2f}|{self.max_budget_gbp:.2f}")
                return

            # ---- initialise durable state BEFORE anything is submitted ----
            state.init(self.care_home, self.api.model_id, self.resolution,
                       {"adaptive_pages": self.adaptive_pages,
                         "convert_pdf": self.convert_pdf,
                         "post_run_audit": self.post_run_audit,
                         "orientation_mode": self.orientation_mode,
                         "bundle_split": self.bundle_split,
                         "max_workers": self.max_workers,
                         "max_files": self.max_files,
                         "max_file_mb": self.max_file_mb,
                         "max_budget_gbp": self.max_budget_gbp,
                         "reprocess": self.reprocess,
                         "move_mode": self.move_mode,
                         "move_dest": str(self.move_dest) if self.move_dest else ""})
            # Bind both the automatic review and the exact selected cohort in
            # the very first durable state, before any provider POST.  A later
            # restart must not infer either from current settings/root content.
            state.data["version"] = max(5, int(state.data.get("version", 1)))
            state.data["auto_review_run_id"] = str(
                getattr(self, "_review_run_id", "") or "")
            state.data["submitted_worker_scope"] = [
                {"name": worker.name,
                 "source_path": str(worker.resolve())}
                for worker in workers]
            inventory_counts = {}
            inventory = {}
            for worker, path, digest, pages in eligible:
                suffix = inventory_counts.get(digest, 0)
                inventory_counts[digest] = suffix + 1
                custom_id = f"{digest[:56]}-{suffix:03d}"
                inventory[custom_id] = {
                    "path": str(path), "worker": worker.name,
                    "worker_dir": str(worker), "fhash": digest, "pages": int(pages)}
            state.data["primary_inventory"] = inventory
            state.data["primary_vocabulary"] = vocab
            state.data["primary_submission_complete"] = False
            state.data["primary_chunks"] = []
            state.data["est_gbp"] = round(est["gbp"], 4)
            state.data["est_primary_gbp"] = round(est["primary_gbp"], 4)
            state.data["est_finishing_gbp"] = round(est["finishing_gbp"], 4)
            state.data["est_followup_reserve_gbp"] = round(
                est["followup_reserve_gbp"], 4)
            state.data["est_audit_gbp"] = round(est["audit_gbp"], 4)
            state.data["followup_model_id"] = followup_api.model_id
            state.data["est_input_tokens"] = est["input_tokens"]
            state.data["est_output_tokens"] = est["output_tokens"]
            # the state file is the only durable record of what was submitted:
            # refuse to submit anything if it cannot be written
            if not state.save() or not state.path.exists():
                self.log("*** NOT SUBMITTED: could not write the batch state "
                         f"file ({state.path}). Check the folder is writable "
                         "and not read-only/cloud-locked. ***")
                self.on_done(self.stats, "batch_submit_failed:state file "
                                         "not writable")
                return

            # ---- build + submit in chunks (render as we go, cap memory) ----
            chunk, chunk_bytes = [], 0
            hash_counts = {}
            n_built = 0
            n_accepted = 0
            chunk_number = 0
            self._phase("batch", "Preparing and submitting batch requests")

            def _submit_chunk():
                nonlocal chunk_number, n_accepted
                if not chunk:
                    return
                wire_bytes = len(json.dumps({"requests": chunk}).encode("utf-8"))
                if wire_bytes > self.BATCH_SUBMIT_MAX_BYTES:
                    raise RuntimeError("Rendered primary chunk exceeds the 100 MiB upload guard; "
                                       "recover with smaller chunks before applying")
                self.set_status(f"Submitting a batch of {len(chunk)} request(s)…")
                chunk_number += 1
                self._phase_progress("batch", n_built, len(eligible),
                                     state="submitting", accepted=n_accepted,
                                     operation=f"submit:{chunk_number}")
                chunk_id = f"primary-{chunk_number:04d}"
                identities = [str(req.get("custom_id") or "")
                              for req in chunk]
                attempt_id = hashlib.sha256(
                    (f"{time.time_ns()}:{os.getpid()}:{chunk_id}:"
                     + "|".join(identities)).encode("utf-8")).hexdigest()[:24]
                state.data["primary_submission"] = {
                    "status": "submission_started",
                    "submission_started": True,
                    "planned_chunk_id": chunk_id,
                    "request_identities": identities,
                    "attempt_id": attempt_id,
                    "started_ts": datetime.datetime.now().isoformat(
                        timespec="seconds"),
                }
                state.data["phase"] = "primary_submission_started"
                if not state.save():
                    raise RuntimeError(
                        "primary submission marker could not be persisted; "
                        "no request sent")
                try:
                    created = self.api.submit_batch(chunk)
                    bid = str(created.get("id") or "").strip()
                    if not bid:
                        raise APIError(
                            0, "primary batch submission returned no batch id")
                except Exception:
                    marker = state.data["primary_submission"]
                    marker["status"] = "ambiguous"
                    marker["ambiguous_ts"] = datetime.datetime.now().isoformat(
                        timespec="seconds")
                    state.data["phase"] = "primary_submission_ambiguous"
                    state.save()
                    raise
                state.add_batch(bid, len(chunk),
                                created.get("processing_status", ""),
                                request_ids=identities)
                state.data["primary_submission"].update({
                    "status": "accepted", "batch_id": bid,
                    "accepted_ts": datetime.datetime.now().isoformat(
                        timespec="seconds")})
                state.data["phase"] = "primary_pending"
                state.data["primary_chunks"].append(dict(state.data["primary_submission"]))
                if not state.save():
                    raise RuntimeError(
                        "primary batch id could not be persisted; automatic "
                        "resubmission is blocked by the durable started marker")
                self.log(f"  > submitted batch {bid} with {len(chunk)} request(s)")
                self.stats["batches"] += 1
                n_accepted += len(chunk)
                self._phase_progress("batch", n_built, len(eligible),
                                     state="submitted", accepted=n_accepted,
                                     operation=f"accepted:{chunk_number}")
                chunk.clear()

            for i, (w, f, fhash, pages) in enumerate(eligible, 1):
                self._check_stop()
                self.set_status(f"Rendering {i}/{len(eligible)}: "
                                f"{self._redact(f.name)}")
                self._phase_progress("batch", n_built, len(eligible),
                                     state="rendering", accepted=n_accepted,
                                     operation=f"render:{i}")
                if not f.exists():
                    continue
                imgs, text, page_idxs, total_pages, segment_view = \
                    self._batch_classification_view(f)
                self.set_preview(imgs[0] if imgs else None, f.name)
                if not imgs and not text:
                    self.log(f"    - {self._redact(f.name)}: cannot render - "
                             f"left unchanged")
                    self.failed_log.record(self.care_home, w.name, f,
                                           "skipped: unrenderable", "")
                    continue
                system, blocks, mt = self.api.classify_payload(
                    vocab, imgs, text, page_idxs=page_idxs,
                    total_pages=total_pages, segment=segment_view)
                # custom_id: stable, unique, derived from the content hash
                # (sha-256 hex truncated + a per-hash counter for exact copies).
                cid = next(cid for cid, meta in inventory.items()
                           if meta["path"] == str(f))
                req = self.api.build_batch_request(cid, system, blocks, mt)
                # rough serialized size (b64 data dominates)
                req_bytes = sum(len(b.get("source", {}).get("data", ""))
                                for b in blocks if b.get("type") == "image")
                req_bytes += sum(len(b.get("text", "")) for b in blocks
                                 if b.get("type") == "text") + len(system) + 2048
                if chunk and (chunk_bytes + req_bytes > self.BATCH_SUBMIT_MAX_BYTES
                              or len(chunk) >= self.BATCH_SUBMIT_MAX_REQUESTS):
                    _submit_chunk()
                    chunk_bytes = 0
                state.add_request(cid, f, w, fhash, pages)
                chunk.append(req)
                chunk_bytes += req_bytes
                n_built += 1
                self._phase_progress("batch", n_built, len(eligible),
                                     state="rendering", accepted=n_accepted,
                                     operation=f"built:{i}")
            _submit_chunk()

            # Eligibility was decided before this bounded rendering pass.  If
            # every source disappeared or proved unrenderable here, no provider
            # request exists to wait for or apply.  Do not retain the initial
            # inventory as an interrupted submission: that would force the
            # recovery flow to treat a local file problem as a possible billed
            # upload.  Save a non-pending terminal marker first, so a failed
            # delete cannot revive the old incomplete state; then remove the
            # empty batch state and leave the source files untouched for retry.
            if not n_built:
                state.data.pop("primary_submission_complete", None)
                state.data["primary_submission"] = {
                    "status": "nothing_renderable",
                    "eligible": len(eligible),
                }
                state.data["phase"] = "primary_nothing_renderable"
                if not state.save():
                    self.log("*** NOT SUBMITTED: no request was rendered, but "
                             "the empty batch state could not be cleared. "
                             "No provider request was sent. ***")
                    self.on_done(self.stats, "batch_state_write_failed")
                    return
                state.delete()
                self._phase_progress("batch", 0, len(eligible),
                                     state="attention", accepted=0,
                                     operation="no-renderable")
                self.log("\n=== NO BATCH REQUESTS SUBMITTED: every eligible "
                         "document was missing or could not be rendered; no "
                         "provider classification result was submitted or applied ===")
                self.on_done(self.stats,
                             f"batch_no_renderable:{len(eligible)}")
                return

            state.data["phase"] = "primary_pending"
            state.data["primary_submission_complete"] = True
            state.save()
            self.stats["batch_requests"] = n_built
            self._phase_progress("batch", n_built, len(eligible),
                                 state="submitted", accepted=n_accepted)
            self.set_progress(total, total)
            n_batches = len(state.batch_ids())
            self.log(f"\n=== BATCH SUBMITTED: {n_built} document(s) in "
                     f"{n_batches} batch(es) ===")
            self.log(f"  primary batch classification : ~£{est['primary_gbp']:.2f}")
            self.log(f"  required live finishing      : ~£{est['finishing_gbp']:.2f}")
            self.log(f"  optional follow-up reserve   : ~£{est['followup_reserve_gbp']:.2f}")
            self.log(f"  optional accuracy audit      : ~£{est['audit_gbp']:.2f}")
            self.log(f"  cumulative enabled estimate  : ~£{est['gbp']:.2f}")
            self.log("You can close this app now. Results are usually ready "
                     "within an hour (up to 24h). Re-open the folder and press "
                     "'Check batch status' to fetch and apply them.")
            self.on_done(self.stats,
                         f"batch_submitted:{n_built}|{n_batches}|"
                         f"{est['primary_gbp']:.2f}|{est['gbp']:.2f}")
        except CreditExhausted as e:
            self.log(f"\n*** STOPPED: API credit exhausted during submission. "
                     f"{e.detail} ***")
            self.on_done(self.stats, f"credit:(batch submit)|{e.detail}")
        except (LimitReached,) as e:
            self.log(f"\n*** STOPPED: {e.reason} ***")
            self.on_done(self.stats, f"limit:{e.reason}")
        except StopRequested:
            self.log("\n*** STOPPED by user (submission incomplete; any "
                     "already-submitted batches remain pending) ***")
            self.on_done(self.stats, "stopped")
        except APIError as e:
            self.log(f"\n! batch submission failed: {e.message}")
            traceback.print_exc()
            self.on_done(self.stats, f"batch_submit_failed:{e.message}")
        except Exception as e:
            self.log(f"\n! fatal error during batch submission: {e}")
            traceback.print_exc()
            self.on_done(self.stats, str(e))

    # ---- Phase B ----
    def _api_for_model(self, model_id: str):
        for candidate in (self.api, self.escalation_api):
            if candidate is not None and candidate.model_id == model_id:
                return candidate
        # The model is persisted in batch state, so a Settings change between
        # submit and resume cannot silently change the paid follow-up model.
        return ClaudeAPI(self.api.api_key, model_id)

    def poll_batches(self, state: "BatchState", phase: str = "primary"):
        """Fetch current status for every batch in the state file. Returns a
        list of the raw batch objects (id, processing_status, request_counts,
        results_url...)."""
        api = (self._api_for_model(
            (state.data.get("followup") or {}).get("model_id")
            or state.data.get("followup_model_id") or self.api.model_id)
            if phase == "followup" else self.api)
        out = []
        for bid in state.batch_ids(phase):
            out.append(api.get_batch(bid))
        return out

    @staticmethod
    def _batch_counts(batches: list) -> dict:
        counts = {"processing": 0, "succeeded": 0, "errored": 0,
                  "canceled": 0, "expired": 0}
        for batch in batches:
            rc = batch.get("request_counts", {}) or {}
            for key in counts:
                counts[key] += int(rc.get(key, 0) or 0)
        return counts

    def _download_batch_results(self, api, batches: list) -> dict:
        results = {}
        for batch in batches:
            url = batch.get("results_url")
            if not url:
                continue
            for line in api.batch_results(url):
                cid = line.get("custom_id")
                if cid:
                    results[cid] = line.get("result", {}) or {}
        return results

    @staticmethod
    def _parse_batch_result(api, result: dict) -> dict:
        if (result or {}).get("type") != "succeeded":
            return {}
        msg = result.get("message", {}) or {}
        raw = "\n".join(blk.get("text", "")
                         for blk in msg.get("content", [])
                         if blk.get("type") == "text").strip()
        parsed = api._json_from(raw)
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _batch_usage(results: dict) -> tuple:
        in_tokens = out_tokens = 0
        for result in results.values():
            if (result or {}).get("type") != "succeeded":
                continue
            usage = ((result.get("message") or {}).get("usage") or {})
            in_tokens += int(usage.get("input_tokens", 0) or 0)
            out_tokens += int(usage.get("output_tokens", 0) or 0)
        return in_tokens, out_tokens

    def _find_batch_file(self, meta: dict):
        """Locate a submitted document without trusting its old path alone."""
        expected = str(meta.get("fhash") or "")
        candidates = []
        old = Path(meta.get("path") or "")
        if old.is_file():
            candidates.append(old)
        root = Path(meta.get("worker_dir") or "")
        if root.is_dir():
            candidates.extend(p for p in root.rglob("*")
                              if p.is_file() and p.suffix.lower() in DOC_EXT
                              and not is_program_file(p) and p not in candidates)
        for path in candidates:
            try:
                actual = file_hash(path)
                if not expected or actual == expected:
                    return path
                # Narrow crash-recovery exception: the exact old request path
                # may carry bytes from a locally authorised atomic orientation
                # rewrite. Both old and new hashes must match its durable state.
                orientation_state = getattr(self, "orientation_state", None)
                if (path == old and orientation_state is not None
                        and orientation_state.authorised_hash(
                            path, expected, actual)):
                    return path
            except Exception:
                continue
        return None

    def approve_followup_warning(self):
        state = BatchState(self.dir)
        followup = state.data.get("followup") or {}
        if followup.get("phase") == "prepared":
            followup["large_warning_acknowledged"] = True
            state.data["followup"] = followup
            state.save()

    @staticmethod
    def _serialized_request_bytes(request: dict) -> int:
        return len(json.dumps(request, ensure_ascii=False).encode("utf-8"))

    def _partition_followup_request_ids(self, sized_requests: list):
        """Return guarded request-id chunks without holding their payloads.

        The 90 MB target leaves headroom below the hard 100 MB transport guard.
        A single request above the hard guard is returned separately so nothing
        is submitted before the operator receives an actionable explanation.
        """
        chunks, current = [], []
        current_bytes = 2  # surrounding JSON list brackets
        oversized = []
        for custom_id, request_bytes in sized_requests:
            request_bytes = int(request_bytes)
            if request_bytes + 2 > self.BATCH_SUBMIT_MAX_BYTES:
                oversized.append((custom_id, request_bytes))
                continue
            addition = request_bytes + (2 if current else 0)
            if current and (current_bytes + addition
                            > self.FOLLOWUP_CHUNK_TARGET_BYTES
                            or len(current) >= self.BATCH_SUBMIT_MAX_REQUESTS):
                chunks.append(current)
                current = []
                current_bytes = 2
                addition = request_bytes
            current.append(custom_id)
            current_bytes += addition
        if current:
            chunks.append(current)
        return chunks, oversized

    def _build_followup_request(self, followup_api, vocab: str,
                                custom_id: str, meta: dict):
        path = self._find_batch_file(meta)
        if path is None:
            self.log(f"  ! follow-up source missing for {custom_id}; "
                     "the primary result will be retained")
            return None
        imgs, text, page_idxs, total_pages, segment_view = \
            self._batch_classification_view(path)
        if not imgs and not text:
            self.log(f"  ! follow-up source unrenderable: "
                     f"{self._redact(path.name)}; the primary result will be retained")
            return None
        system, blocks, max_tokens = followup_api.classify_payload(
            vocab, imgs, text, page_idxs=page_idxs,
            total_pages=total_pages, segment=segment_view)
        request = followup_api.build_batch_request(
            custom_id, system, blocks, max_tokens)
        return request, estimate_request_input_tokens(system, blocks)

    def _followup_progress(self, phase, done, total, *, state="rendering",
                           operation="", prepared=0, accepted=0):
        """Aggregate UI facts only; rendering is not provider acceptance."""
        if phase == "followup_plan":
            message = (f"Sizing follow-up requests: {done}/{total} candidates checked; "
                       f"{prepared} requests sized. Nothing submitted by this planning pass.")
        else:
            action = "Sending" if state == "submitting" else "Submitted" if state == "submitted" else "Preparing"
            message = (f"{action} follow-up requests: {done}/{total} remaining requests prepared; "
                       f"{accepted} accepted overall. Provider processing is separate.")
        status_callback = getattr(self, "set_status", None)
        if status_callback:
            try:
                status_callback(message)
            except Exception:
                pass  # An optional observer must never change processing.
        self._phase_progress(phase, done, total, state=state,
                             operation=operation, prepared=prepared, accepted=accepted)

    def _submit_followup_batch(self, state: "BatchState", unresolved: list,
                               vocab: str, primary_actual_gbp: float) -> bool:
        """Persist and submit one discounted request per unresolved document.

        Oversized follow-up work is partitioned into guarded chunks. Each POST
        has its own durable started/accepted marker and accepted request IDs, so
        a clean restart can continue with the next chunk while an ambiguous POST
        still fails closed instead of risking duplicate billing.
        """
        followup = state.data.get("followup") or {}
        phase = followup.get("phase")
        submission = followup.get("submission") or {}
        if phase == "pending":
            return True
        if phase == "ended":
            return False
        if (phase in ("submission_started", "ambiguous")
                or submission.get("status") in
                ("submission_started", "ambiguous")):
            self.log("*** FOLLOW-UP NOT RESUBMITTED: a previous submission may "
                     "have reached Anthropic but its batch id was not saved. "
                     "Automatic retry is blocked to prevent duplicate billing. ***")
            self.on_done(self.stats, "batch_followup_ambiguous")
            return True

        followup_api = self._api_for_model(
            followup.get("model_id") or state.data.get("followup_model_id")
            or (self.escalation_api or self.api).model_id)
        if "est_finishing_gbp" not in state.data:
            # v1.3.0 state compatibility: reconstruct the newly-separated
            # remaining phases before applying the cumulative budget gate.
            phases = estimate_pipeline_costs_gbp(
                len(state.data.get("requests", {})),
                state.data.get("model_id") or self.api.model_id,
                followup_api.model_id, self.resolution, vocab, batch=True,
                include_audit=bool(getattr(self, "post_run_audit", False)))
            state.data["est_finishing_gbp"] = round(
                phases["finishing_gbp"], 4)
            state.data["est_audit_gbp"] = round(phases["audit_gbp"], 4)
            state.save()
        if not followup:
            requests = {}
            for index, primary_cid in enumerate(unresolved):
                meta = state.request_for(primary_cid) or {}
                fhash = str(meta.get("fhash") or "")
                custom_id = f"fu-{fhash[:55]}-{index:03d}"[:64]
                requests[custom_id] = {
                    "primary_custom_id": primary_cid,
                    "path": meta.get("path", ""),
                    "worker": meta.get("worker", ""),
                    "worker_dir": meta.get("worker_dir", ""),
                    "fhash": fhash,
                    "pages": int(meta.get("pages", 0) or 0),
                }
            rough = estimate_run_cost_gbp(
                len(requests), followup_api.model_id, self.resolution,
                adaptive=False, vocab_block=vocab, batch=True,
                include_second_pass=False, cached_prefix=False)
            primary_n = max(1, len(state.data.get("requests", {})))
            followup = {
                "phase": "prepared", "model_id": followup_api.model_id,
                "prepared_ts": datetime.datetime.now().isoformat(
                    timespec="seconds"),
                "requests": requests, "batches": [],
                "est_gbp": round(rough["primary_gbp"], 4),
                "large": (len(requests) >= self.FOLLOWUP_LARGE_MIN_REQUESTS
                          and len(requests) / primary_n
                          >= self.FOLLOWUP_LARGE_RATIO),
            }
            state.data["version"] = max(4, int(state.data.get("version", 1)))
            state.data["phase"] = "followup_prepared"
            state.data["followup"] = followup
            state.save()

        if followup.get("large") and not followup.get(
                "large_warning_acknowledged"):
            self.log("*** WARNING: unexpectedly large follow-up batch: "
                     f"{len(followup.get('requests', {}))} request(s), expected "
                     f"~£{float(followup.get('est_gbp', 0)):.2f}. "
                     "Explicit confirmation is required before submission. ***")
            self.on_done(
                self.stats,
                "batch_followup_warning:"
                f"{len(followup.get('requests', {}))}|"
                f"{float(followup.get('est_gbp', 0)):.2f}|"
                f"{followup_api.model_id}")
            return True

        finishing = float(state.data.get("est_finishing_gbp", 0) or 0)
        audit = float(state.data.get("est_audit_gbp", 0) or 0)
        expected_total = (primary_actual_gbp
                          + float(followup.get("est_gbp", 0) or 0)
                          + finishing + audit)
        if self.max_budget_gbp > 0 and expected_total > self.max_budget_gbp:
            self.log("*** FOLLOW-UP NOT SUBMITTED: cumulative expected cost "
                     f"£{expected_total:.2f} exceeds the £{self.max_budget_gbp:.2f} "
                     "budget. State is retained; raise the limit and use Check "
                     "batch status to continue. ***")
            self.on_done(self.stats,
                         f"batch_followup_over_budget:{expected_total:.2f}|"
                         f"{self.max_budget_gbp:.2f}")
            return True

        submitted_ids = set(str(item) for item in
                            followup.get("submitted_request_ids", []))
        for batch in followup.get("batches", []):
            submitted_ids.update(str(item) for item in
                                 batch.get("request_ids", []))

        # First pass: establish the complete chunk plan and exact cost before
        # any POST. Payloads are discarded between documents, keeping memory
        # bounded; the planned request IDs and hashes are the durable contract.
        chunk_plan = followup.get("chunk_plan") or []
        if not chunk_plan:
            self._phase("followup_plan", "Sizing stronger-model follow-up requests")
            self.log("Preparing the follow-up chunk plan locally; rendering candidates to measure size and cost. No follow-up request is sent by this pass.")
            sized_requests = []
            actual_requests = {}
            input_tokens = 0
            candidates = list(followup.get("requests", {}).items())
            for index, (custom_id, meta) in enumerate(candidates, 1):
                self._followup_progress("followup_plan", index - 1, len(candidates),
                                        operation=f"size:{index}", prepared=len(actual_requests))
                built_item = self._build_followup_request(
                    followup_api, vocab, custom_id, meta)
                if built_item is not None:
                    request, request_tokens = built_item
                    sized_requests.append((
                        custom_id, self._serialized_request_bytes(request)))
                    input_tokens += request_tokens
                    actual_requests[custom_id] = meta
                self._followup_progress("followup_plan", index, len(candidates),
                                        operation=f"sized:{index}", prepared=len(actual_requests))
            self._followup_progress("followup_plan", len(candidates), len(candidates),
                                    state="complete", prepared=len(actual_requests))

            chunk_plan, oversized = self._partition_followup_request_ids(
                sized_requests)
            if oversized:
                custom_id, size = oversized[0]
                followup["oversized_request"] = {
                    "custom_id": custom_id, "bytes": size}
                state.data["followup"] = followup
                state.save()
                self.log("*** FOLLOW-UP NOT SUBMITTED: one document creates a "
                         f"{size / 1048576:.1f} MB request, above the 100 MB "
                         "per-batch guard. No request was sent; lower the "
                         "resolution or prepare that file separately. ***")
                self.on_done(
                    self.stats,
                    f"batch_followup_too_large:{size / 1048576:.1f}")
                return True
            if not chunk_plan:
                followup["phase"] = ("pending" if submitted_ids else "ended")
                followup["requests"] = {
                    cid: meta for cid, meta in actual_requests.items()
                    if cid in submitted_ids}
                state.data["phase"] = ("followup_pending" if submitted_ids
                                       else "followup_ended")
                state.data["followup"] = followup
                state.save()
                return bool(submitted_ids)

            exact_est = tokens_cost_gbp(
                followup_api.model_id, input_tokens,
                len(actual_requests) * EST_OUTPUT_TOKENS_PER_DOC, batch=True)
            expected_total = primary_actual_gbp + exact_est + finishing + audit
            followup.update({
                "phase": "submission_prepared",
                "requests": actual_requests,
                "chunk_plan": chunk_plan,
                "planned_chunks": len(chunk_plan),
                "planned_request_count": len(actual_requests),
                "planned_input_tokens": input_tokens,
                "est_gbp": round(exact_est, 4),
            })
            state.data["version"] = max(
                4, int(state.data.get("version", 1)))
            state.data["phase"] = "followup_submission_prepared"
            state.data["followup"] = followup
            if not state.save():
                raise RuntimeError(
                    "follow-up chunk plan could not be persisted; no request sent")
            if self.max_budget_gbp > 0 and expected_total > self.max_budget_gbp:
                self.log("*** FOLLOW-UP NOT SUBMITTED after exact planning: "
                         f"cumulative expected cost £{expected_total:.2f} exceeds "
                         f"the £{self.max_budget_gbp:.2f} budget. ***")
                self.on_done(
                    self.stats,
                    f"batch_followup_over_budget:{expected_total:.2f}|"
                    f"{self.max_budget_gbp:.2f}")
                return True
        else:
            exact_est = float(followup.get("est_gbp", 0) or 0)
            expected_total = primary_actual_gbp + exact_est + finishing + audit

        total_planned = sum(len(chunk) for chunk in chunk_plan)
        self.log(f"Preparing {total_planned} unresolved document(s) for the "
                 f"discounted {followup_api.model_id} follow-up in "
                 f"{len(chunk_plan)} guarded batch chunk(s); expected follow-up "
                 f"cost ~£{exact_est:.2f}, cumulative enabled estimate "
                 f"~£{expected_total:.2f}.")

        self._phase("followup_upload", "Preparing and submitting stronger-model follow-up")
        remaining_total = sum(str(cid) not in submitted_ids for chunk in chunk_plan for cid in chunk)
        prepared_now = attempted_now = 0
        self._followup_progress("followup_upload", 0, remaining_total,
                                operation="upload:start", accepted=len(submitted_ids))
        for chunk_index, planned_ids in enumerate(chunk_plan, 1):
            remaining_ids = [str(cid) for cid in planned_ids
                             if str(cid) not in submitted_ids]
            if not remaining_ids:
                continue
            built = []
            actual_ids = []
            for custom_id in remaining_ids:
                attempted_now += 1
                self._followup_progress("followup_upload", prepared_now, remaining_total,
                                        operation=f"render:{attempted_now}", accepted=len(submitted_ids))
                meta = followup.get("requests", {}).get(custom_id) or {}
                built_item = self._build_followup_request(
                    followup_api, vocab, custom_id, meta)
                if built_item is None:
                    followup.get("requests", {}).pop(custom_id, None)
                    self._followup_progress("followup_upload", prepared_now, remaining_total,
                                            operation=f"skipped:{attempted_now}", accepted=len(submitted_ids))
                    continue
                request, _request_tokens = built_item
                built.append(request)
                actual_ids.append(custom_id)
                prepared_now += 1
                self._followup_progress("followup_upload", prepared_now, remaining_total,
                                        operation=f"built:{attempted_now}", accepted=len(submitted_ids))
            if not built:
                continue
            payload_bytes = len(json.dumps(
                built, ensure_ascii=False).encode("utf-8"))
            if (len(built) > self.BATCH_SUBMIT_MAX_REQUESTS
                    or payload_bytes > self.BATCH_SUBMIT_MAX_BYTES):
                followup["oversized_chunk"] = {
                    "chunk": chunk_index, "bytes": payload_bytes,
                    "request_ids": actual_ids}
                state.data["followup"] = followup
                state.save()
                self.log("*** FOLLOW-UP CHUNK NOT SUBMITTED: rendered payload "
                         f"{chunk_index}/{len(chunk_plan)} is "
                         f"{payload_bytes / 1048576:.1f} MB, above the 100 MB "
                         "hard guard. Earlier accepted chunks are retained; "
                         "nothing was sent for this chunk. ***")
                self.on_done(
                    self.stats,
                    f"batch_followup_too_large:{payload_bytes / 1048576:.1f}")
                return True

            attempt_id = hashlib.sha256(
                (f"{time.time_ns()}:{os.getpid()}:followup-{chunk_index}:"
                 + "|".join(actual_ids)).encode("utf-8")).hexdigest()[:24]
            followup["phase"] = "submitting"
            followup["submission"] = {
                "status": "submission_started",
                "planned_chunk": chunk_index,
                "request_ids": actual_ids,
                "attempt_id": attempt_id,
                "started_ts": datetime.datetime.now().isoformat(
                    timespec="seconds"),
            }
            state.data["phase"] = "followup_submitting"
            state.data["followup"] = followup
            if not state.save():
                raise RuntimeError(
                    "follow-up submission marker could not be persisted; "
                    "no request sent")
            self._followup_progress("followup_upload", prepared_now, remaining_total,
                                    state="submitting", operation=f"submit:{chunk_index}",
                                    accepted=len(submitted_ids))
            try:
                created = followup_api.submit_batch(built)
                batch_id = str(created.get("id") or "").strip()
                if not batch_id:
                    raise APIError(
                        0, "follow-up batch submission returned no batch id")
            except Exception:
                followup["phase"] = "ambiguous"
                followup["submission"]["status"] = "ambiguous"
                followup["submission"]["ambiguous_ts"] = \
                    datetime.datetime.now().isoformat(timespec="seconds")
                state.data["phase"] = "followup_submission_ambiguous"
                state.data["followup"] = followup
                state.save()
                raise

            state.add_batch(
                batch_id, len(built), created.get("processing_status", ""),
                phase="followup", request_ids=actual_ids)
            submitted_ids.update(actual_ids)
            followup = state.data["followup"]
            followup["submitted_request_ids"] = sorted(submitted_ids)
            followup["submission"].update({
                "status": "accepted", "batch_id": batch_id,
                "accepted_ts": datetime.datetime.now().isoformat(
                    timespec="seconds")})
            followup["phase"] = "submitting"
            state.data["phase"] = "followup_submitting"
            if not state.save():
                raise RuntimeError(
                    "follow-up batch id could not be persisted; automatic retry "
                    "is blocked by the durable started marker")
            self._followup_progress("followup_upload", prepared_now, remaining_total,
                                    state="submitted", operation=f"accepted:{chunk_index}",
                                    accepted=len(submitted_ids))
            self.log(f"  > submitted follow-up chunk "
                     f"{chunk_index}/{len(chunk_plan)} as {batch_id} with "
                     f"{len(built)} request(s) "
                     f"({payload_bytes / 1048576:.1f} MB)")

        if not submitted_ids:
            followup["phase"] = "ended"
            state.data["phase"] = "followup_ended"
            state.data["followup"] = followup
            state.save()
            self._followup_progress("followup_upload", prepared_now, remaining_total,
                                    state="complete", accepted=0)
            return False
        followup["phase"] = "pending"
        followup["submitted_ts"] = datetime.datetime.now().isoformat(
            timespec="seconds")
        followup["submitted_request_ids"] = sorted(submitted_ids)
        state.data["phase"] = "followup_pending"
        state.data["followup"] = followup
        if not state.save():
            raise RuntimeError(
                "follow-up completion marker could not be persisted; accepted "
                "batch ids remain protected by per-chunk markers")
        self.stats["followup_requests"] = len(submitted_ids)
        self.stats["followup_batches"] = len(followup.get("batches", []))
        self._followup_progress("followup_upload", prepared_now, remaining_total,
                                state="submitted", accepted=len(submitted_ids))
        self.on_done(
            self.stats,
            f"batch_followup_submitted:{len(submitted_ids)}|"
            f"{exact_est:.2f}|{followup_api.model_id}|"
            f"{len(followup.get('batches', []))}")
        return True

    def _rescue_batch_result(self, f: Path, parsed: dict, vocab: str) -> dict:
        """Batch results are produced asynchronously, so the live-mode rescue
        steps (rotation retry, low-confidence second opinion) could never run
        at submit time. Run them here at apply time instead: a rotated or
        unsettled result gets one live-priced follow-up call rather than
        falling straight to 'Other - Unknown'. Both steps are no-ops for
        confident, upright results, so almost every document costs nothing
        extra. Any rescue failure falls back to the original batch result -
        it must never abort the apply pass."""
        try:
            needs = (_rot_of(parsed) != 0
                     or (self.escalation_api is not None
                         and _conf_int(parsed) < SECOND_OPINION_MAX_CONF))
            if not needs:
                return parsed
            out = rescue_result(self.api, vocab, f, self.resolution, parsed,
                                emit_cost=self._emit_cost,
                                escalation_api=self.escalation_api)
            if out.get("rotation_retried") or out.get("escalated"):
                how = " + ".join(
                    s for s, on in (("rotation retry", out.get("rotation_retried")),
                                    ("second opinion", out.get("escalated"))) if on)
                self.stats["batch_rescued"] = \
                    self.stats.get("batch_rescued", 0) + 1
                self.log(f"    · {self._redact(f.name)}: batch result "
                         f"rescued live ({how})")
            return out["result"]
        except (LimitReached, StopRequested):
            raise
        except Exception as e:
            self.log(f"    ! rescue pass failed for {self._redact(f.name)}: "
                     f"{e} - using the batch result as-is")
            return parsed

    def _dated_overwrite_label(self, name: str, core: dict) -> str:
        """CoS / Share Code Check Result carry their date in the filename in
        Overwrite Documents (matching the second pass); read it live from the
        pages the double-check already rendered."""
        imgs = core.get("used_imgs") or []
        text = core.get("used_text") or ""
        try:
            if name == "Certificate of Sponsorship":
                d = parse_date(self.api.cos_issue_date(imgs, text))
                self._emit_cost()
                if d:
                    return f"{name} - ({d.strftime('%d-%m-%Y')})"
            elif name == "Share Code Check Result":
                d = parse_date(self.api.share_code_check(imgs, text)
                               .get("check_date", ""))
                self._emit_cost()
                if d:
                    return f"{name} - ({d.strftime('%d-%m-%Y')})"
        except Exception as e:
            self.log(f"      (could not read date: {e})")
        return name

    def _recheck_leftover_unknowns(self, worker_dir: Path, vocab: str,
                                   exclude_hashes=frozenset()):
        """Final double-check for one worker, mirroring the standalone
        'Re-check Unknowns & Tidy' tool: find every document still named
        'Other - Unknown' ANYWHERE under the worker folder (including Bulk
        batches left by earlier runs), re-classify each individually through
        the shared core (rotation retry + second opinion included) and file
        it under what it turns out to be - overwrite types move (dated) to
        'Overwrite Documents'. Documents this run's auto-review pass already
        examined are excluded by content hash, so nothing is billed twice."""
        targets = [p for p in worker_dir.rglob("*")
                   if p.is_file() and p.suffix.lower() in DOC_EXT
                   and not is_program_file(p)
                   and base_controlled_name(p.stem).strip().lower()
                   == "other - unknown"]
        targets.sort(key=lambda p: natural_key(p.name))
        if not targets:
            return
        self.log(f"  [double-check] re-examining {len(targets)} leftover "
                 f"'Other - Unknown' document(s)")
        for f in targets:
            self._check_stop()
            if not f.exists():
                continue
            try:
                fhash = file_hash(f)
            except Exception:
                fhash = ""
            if fhash and fhash in exclude_hashes:
                continue   # examined moments ago by the auto-review pass
            try:
                core = classify_document_core(
                    self.api, vocab, f, resolution=self.resolution,
                    adaptive_pages=False, emit_cost=self._emit_cost,
                    escalation_api=self.escalation_api)
            except (StopRequested, LimitReached, CreditExhausted):
                raise
            except Exception as e:
                self.log(f"    ! double-check failed for "
                         f"{self._redact(f.name)}: {e} - left unchanged")
                self.stats["errors"] += 1
                continue
            new_hash = self._maybe_fix_rotation(f, core["result"],
                                                page_idxs=core.get("page_idxs"))
            if new_hash:
                fhash = new_hash
            name, group = resolve_auto_review(self.kb, core["result"])
            if not name:
                self.log(f"    = {self._redact(f.name)}: still unrecognised "
                         f"- stays 'Other - Unknown'")
                continue
            if is_overwrite_type(name):
                label = self._dated_overwrite_label(name, core)
                dest_dir = worker_dir / "Overwrite Documents"
                dest_dir.mkdir(exist_ok=True)
            else:
                label = name
                dest_dir = f.parent
            new_path = unique_path(dest_dir, safe_stem(label), f.suffix)
            try:
                if dest_dir == f.parent:
                    f.rename(new_path)
                else:
                    shutil.move(str(f), str(new_path))
                self._orientation_move_path(f, new_path)
            except Exception as e:
                self.log(f"    ! rename failed for {f.name}: {e}")
                self.stats["errors"] += 1
                continue
            self.stats["rechecked_fixed"] = \
                self.stats.get("rechecked_fixed", 0) + 1
            self.rename_log.record(self.care_home, worker_dir.name, f.name,
                                   new_path.name, group, "batch-double-check")
            if fhash:
                self.manifest.record(fhash, self.api.model_id,
                                     self.resolution, name, group)
            self.log(f"    + double-check: {self._redact(f.name)} -> "
                     f"{self._redact(new_path.name)}")

    @_care_home_writer_operation
    def run_batch_apply(self):
        """Poll/apply the primary and, when required, discounted follow-up.

        Primary results are first divided into settled canonical/descriptive
        answers and genuinely unresolved answers.  Only the latter are sent in
        one persisted Message Batches follow-up.  No worker is finalised or
        moved until that follow-up has ended.
        """
        try:
            state = BatchState(self.dir)
            if not state.exists():
                self.log("No pending batch found for this folder.")
                self.on_done(self.stats, "batch_none_pending")
                return

            self._batch_state = state
            saved_costs = state.data.get("costs") or {}
            self._committed_batch_cost_gbp = (
                float(saved_costs.get("primary_actual_gbp", 0) or 0)
                + float(saved_costs.get("followup_actual_gbp", 0) or 0))
            self._persisted_live_cost_gbp = float(
                saved_costs.get("live_actual_gbp", 0) or 0)
            self._persisted_live_tokens = int(
                saved_costs.get("live_tokens", 0) or 0)

            followup = state.data.get("followup") or {}
            primary_submit = state.data.get("primary_submission") or {}
            if primary_submit.get("status") in (
                    "submission_started", "ambiguous"):
                self.log("*** PRIMARY BATCH SUBMISSION STATE IS AMBIGUOUS. "
                         "The planned chunk and attempt are retained; automatic "
                         "resubmission and live processing are blocked to "
                         "prevent duplicate billing. ***")
                self.on_done(self.stats, "batch_primary_ambiguous")
                return
            if state.data.get("primary_submission_complete") is False:
                self.log("*** PRIMARY SUBMISSION IS INCOMPLETE. Recover the remaining "
                         "inventory before applying results or moving workers. ***")
                self.on_done(self.stats, "batch_primary_incomplete")
                return
            followup_submission = followup.get("submission") or {}
            if (followup.get("phase") in ("submission_started", "ambiguous")
                    or followup_submission.get("status") in
                    ("submission_started", "ambiguous")):
                self.log("*** FOLLOW-UP SUBMISSION STATE IS AMBIGUOUS. "
                         "Automatic resubmission is blocked to prevent duplicate "
                         "billing. ***")
                self.on_done(self.stats, "batch_followup_ambiguous")
                return

            try:
                submitted_scope = self._batch_submitted_worker_scope(state)
            except Exception as exc:
                self.log("*** BATCH WORKER SCOPE IS UNVERIFIABLE. Apply, "
                         "movement, audit and automatic review are blocked; "
                         "the saved state is retained. ***")
                self.on_done(self.stats, "batch_scope_invalid:" + str(exc))
                return

            # Classification, finishing and movement were durably completed
            # before the optional audit began. A restart resumes only audit.
            if state.data.get("processing_complete"):
                self._audit_worker_dirs = []
                for source in submitted_scope:
                    worker = self._batch_worker_state(state, source)
                    if not worker or not worker.get("completed"):
                        self.on_done(
                            self.stats,
                            "batch_scope_invalid:Processing was marked complete "
                            f"without a completed record for '{source.name}'.")
                        return
                    final_path = Path(worker.get("final_path")
                                      or worker.get("source_path") or "")
                    if final_path.is_dir():
                        self._audit_worker_dirs.append(final_path)
                    else:
                        self.on_done(
                            self.stats,
                            "batch_scope_invalid:The completed final folder for "
                            f"'{source.name}' is unavailable.")
                        return
                self.log("Batch processing is already complete; resuming only "
                         "the separate post-run audit phase.")
                self._record_review_processing()
                self._run_post_run_audit()
                audit_status = (state.data.get("audit") or {}).get("status")
                if audit_status in ("complete", "skipped", "disabled"):
                    state.mark_applied()
                    state.delete()
                    self.on_done(self.stats, "batch_audit_complete")
                return

            # ---- poll the currently active phase ----
            self.set_status("Checking batch status…")
            followup_incomplete = followup.get("phase") in (
                "prepared", "submission_prepared", "submitting")
            active_phase = ("followup"
                            if state.batch_ids("followup")
                            and not followup_incomplete else "primary")
            batches = self.poll_batches(state, active_phase)
            pending = [b for b in batches
                       if b.get("processing_status") != "ended"]
            counts = self._batch_counts(batches)
            counts["phase"] = active_phase
            self.log(f"{active_phase.title()} batch status: "
                     f"{len(batches) - len(pending)}/{len(batches)} "
                     f"ended - {counts['succeeded']} succeeded, "
                     f"{counts['errored']} errored, {counts['processing']} still "
                     f"processing, {counts['canceled']} canceled, "
                     f"{counts['expired']} expired.")
            if pending:
                self.on_done(self.stats,
                             "batch_pending:" + json.dumps(counts))
                return

            if active_phase == "followup":
                followup["phase"] = "ended"
                state.data["phase"] = "followup_ended"
                state.data["followup"] = followup
                if not state.save():
                    raise RuntimeError(
                        "follow-up completion phase could not be persisted")

            # ---- download primary results (v1.3.0 state compatible) ----
            self.set_status("Downloading batch results…")
            primary_batches = (batches if active_phase == "primary"
                               else self.poll_batches(state, "primary"))
            results = self._download_batch_results(self.api, primary_batches)
            self.log(f"Downloaded {len(results)} primary result(s).")
            self.set_status("Checking primary batch answers for required follow-up…")

            vocab = self.kb.vocabulary_block()
            parsed_primary = {
                cid: self._parse_batch_result(self.api, result)
                for cid, result in results.items()
            }
            unresolved = []
            for cid in state.data.get("requests", {}):
                result = results.get(cid, {})
                parsed = parsed_primary.get(cid, {})
                if ((result or {}).get("type") != "succeeded"
                        or batch_result_needs_followup(self.kb, parsed)):
                    unresolved.append(cid)

            primary_in, primary_out = self._batch_usage(results)
            primary_cost = tokens_cost_gbp(
                state.data.get("model_id") or self.api.model_id,
                primary_in, primary_out, batch=True)
            state.data.setdefault("costs", {})["primary_actual_gbp"] = round(
                primary_cost, 6)

            # No first-pass result is applied yet when follow-up is required;
            # this keeps restart/resume simple and prevents worker movement.
            if unresolved:
                if self._submit_followup_batch(
                        state, unresolved, vocab, primary_cost):
                    return

            followup_results = {}
            followup_api = None
            followup = state.data.get("followup") or {}
            if state.batch_ids("followup"):
                followup_api = self._api_for_model(
                    followup.get("model_id") or self.api.model_id)
                followup_batches = (batches if active_phase == "followup"
                                    else self.poll_batches(state, "followup"))
                if any(b.get("processing_status") != "ended"
                       for b in followup_batches):
                    counts = self._batch_counts(followup_batches)
                    counts["phase"] = "followup"
                    self.on_done(self.stats,
                                 "batch_pending:" + json.dumps(counts))
                    return
                followup_results = self._download_batch_results(
                    followup_api, followup_batches)
                self.log(f"Downloaded {len(followup_results)} follow-up "
                         "result(s).")

            followup_by_primary = {}
            followup_raw_by_primary = {}
            for followup_cid, meta in followup.get("requests", {}).items():
                raw_result = followup_results.get(followup_cid, {})
                primary_cid = meta.get("primary_custom_id")
                outcome = {"status": "failed", "parsed": {}}
                if raw_result.get("type") == "succeeded":
                    try:
                        parsed = (self._parse_batch_result(
                            followup_api, raw_result)
                            if followup_api is not None else {})
                    except Exception:
                        parsed = {}
                    if parsed:
                        # A completed generic answer has consumed the one
                        # discounted opinion and remains explicitly unresolved.
                        status = ("generic" if batch_result_needs_followup(
                            self.kb, parsed) else "meaningful")
                        outcome = {"status": status, "parsed": parsed}
                    else:
                        outcome = {"status": "no_result", "parsed": {}}
                elif not raw_result:
                    outcome = {"status": "no_result", "parsed": {}}
                followup_by_primary[primary_cid] = outcome
                followup_raw_by_primary[primary_cid] = raw_result

            followup_in, followup_out = self._batch_usage(followup_results)
            followup_cost = (tokens_cost_gbp(
                followup.get("model_id") or self.api.model_id,
                followup_in, followup_out, batch=True)
                if followup_results else 0.0)
            state.data.setdefault("costs", {})["followup_actual_gbp"] = round(
                followup_cost, 6)
            state.save()
            self._committed_batch_cost_gbp = primary_cost + followup_cost
            self._committed_batch_tokens = (primary_in + primary_out
                                            + followup_in + followup_out)
            self.stats["batch_in_tokens"] = primary_in + followup_in
            self.stats["batch_out_tokens"] = primary_out + followup_out
            self.stats["followup_in_tokens"] = followup_in
            self.stats["followup_out_tokens"] = followup_out
            self.on_cost(self._current_cost_gbp(),
                         self._committed_batch_tokens)

            # reverse index: content hash -> [custom_ids] (for moved files)
            by_hash = {}
            for cid, meta in state.data.get("requests", {}).items():
                by_hash.setdefault(meta.get("fhash", ""), []).append(cid)

            matched_cids = set()

            # Recover the narrow crash window after shutil.move succeeded but
            # before the worker completion record was saved.
            for source in submitted_scope:
                worker = self._batch_worker_state(state, source)
                if worker is None:
                    continue
                target = Path(worker.get("movement_target") or "")
                if (worker.get("movement_status") == "started"
                        and str(target) and target.is_dir()
                        and not source.is_dir()):
                    self._orientation_move_tree(source, target)
                    worker.update({"movement_status": "complete",
                                   "final_path": str(target),
                                   "completed": True,
                                   "completed_ts": datetime.datetime.now()
                                   .isoformat(timespec="seconds")})
                    state.save()
                if worker.get("completed"):
                    source_key = str(worker.get("source_path") or "").casefold()
                    for cid, meta in state.data.get("requests", {}).items():
                        if str(meta.get("worker_dir") or "").casefold() == \
                                source_key:
                            matched_cids.add(cid)
                    final_path = Path(worker.get("final_path")
                                      or worker.get("source_path") or "")
                    if final_path.is_dir():
                        self._audit_worker_dirs.append(final_path)

            # Never re-enumerate the current Files root here. It may contain a
            # later cohort that was not part of this paid submission.
            workers = [worker for worker in submitted_scope if worker.is_dir()]
            total = len(workers)
            for idx, w in enumerate(workers, 1):
                self._check_stop()
                self._phase_progress("processing", idx - 1, total)
                self.set_progress(idx - 1, total)
                self.log(f"\n=== Applying results {idx}/{total}: {w.name} ===")
                self.set_status(f"Applying {idx}/{total}: {w.name}")
                self._current_worker = w.name
                worker_key = str(w.resolve()).casefold()
                worker_state = state.data.setdefault("workers", {}).setdefault(
                    worker_key, {"name": w.name, "source_path": str(w),
                                 "classification_status": "pending",
                                 "finishing_status": "pending",
                                 "movement_status": "pending",
                                 "completed": False})
                if worker_state.get("completed"):
                    for cid, meta in state.data.get("requests", {}).items():
                        if str(meta.get("worker_dir", "")).casefold() == \
                                str(w).casefold():
                            matched_cids.add(cid)
                    final_path = Path(worker_state.get("final_path") or w)
                    if final_path.is_dir():
                        self._audit_worker_dirs.append(final_path)
                    self.log("  finishing already completed on an earlier "
                             "apply pass - skipped without API calls")
                    continue
                try:
                    classification_done = (
                        worker_state.get("classification_status") == "complete")
                    records = (self._records_from_manifest(w)
                               if classification_done else [])
                    files_to_apply = ([] if classification_done
                                      else list_worker_docs(w))
                    worker_state["classification_status"] = (
                        "complete" if classification_done else "in_progress")
                    state.save()
                    for f in files_to_apply:
                        self._check_stop()
                        if is_cloud_only_placeholder(f) or \
                                file_too_big(f, self.max_file_mb):
                            continue
                        try:
                            fhash = file_hash(f)
                        except Exception:
                            continue
                        # (a) cached from an earlier run OR already applied on a
                        #     previous (interrupted) apply pass -> use the
                        #     remembered name and claim any matching results so
                        #     they are never re-applied or counted as missing.
                        cached = (self.manifest.seen(fhash, self.api.model_id,
                                                     self.resolution)
                                  if fhash and not self.reprocess else None)
                        if cached:
                            for c in by_hash.get(fhash, []):
                                if c in results:
                                    matched_cids.add(c)
                        # (b) a batch result for this content?
                        cid = None
                        if not cached:
                            for c in by_hash.get(fhash, []):
                                if c in results and c not in matched_cids:
                                    cid = c
                                    break
                        if cid is None and cached:
                            self.stats["skipped_cached"] += 1
                            cname = cached.get("name") or "Other"
                            cgroup = (cached.get("group")
                                      or self.kb.group_of(cname) or "Other")
                            # cached files never see the model, but the local
                            # text-direction check is free - fix sideways
                            # scans here too, re-recording the manifest under
                            # the rewritten file's new hash
                            new_hash = self._maybe_fix_rotation(f, {})
                            if new_hash:
                                self.manifest.record(new_hash,
                                                     self.api.model_id,
                                                     self.resolution,
                                                     cname, cgroup)
                            desired_stem = safe_stem(cname)
                            new_path = (f if f.stem == desired_stem else
                                        unique_path(w, desired_stem, f.suffix))
                            try:
                                if new_path != f:
                                    f.rename(new_path)
                                    self._orientation_move_path(f, new_path)
                                records.append({"path": new_path, "name": cname,
                                                "group": cgroup,
                                                "original": f.name,
                                                "imgs": [], "text": ""})
                                self.log(f"    = {self._redact(f.name)} -> "
                                         f"{self._redact(new_path.name)} (cached)")
                            except Exception as e:
                                self.log(f"    ! rename (cached) failed for "
                                         f"{self._redact(f.name)}: {e}")
                            continue
                        if cid is None:
                            # not part of this batch (e.g. added after submit)
                            continue
                        matched_cids.add(cid)
                        res = results[cid]
                        rtype = res.get("type")
                        primary_parsed = parsed_primary.get(cid, {})
                        followup_outcome = followup_by_primary.get(cid)
                        followup_completed = bool(
                            followup_outcome
                            and followup_outcome.get("status") in
                            ("meaningful", "generic"))
                        primary_usable = bool(
                            rtype == "succeeded" and primary_parsed)
                        parsed = ((followup_outcome or {}).get("parsed")
                                  if followup_completed else primary_parsed)
                        if followup_completed or primary_usable:
                            msg = res.get("message", {}) or {}
                            usage = msg.get("usage", {}) or {}
                            if _api_usage:
                                _api_usage.record_usage(
                                    self.api.model_id, usage, batch=True)
                                if followup_completed:
                                    followup_usage = (((followup_raw_by_primary
                                        .get(cid) or {}).get("message") or {})
                                        .get("usage") or {})
                                    _api_usage.record_usage(
                                        followup.get("model_id")
                                        or self.api.model_id,
                                        followup_usage, batch=True)
                            self.stats["batch_succeeded"] += 1
                            source = ("batch-followup" if followup_completed
                                      else ("batch-AI-fallback"
                                            if followup_outcome else "batch-AI"))
                            # Both batch phases use the normal bounded
                            # pages='all' policy (first two + last for long PDFs).
                            new_hash = self._maybe_fix_rotation(
                                f, parsed, page_idxs=_audit_pages_for(f))
                            if new_hash:
                                fhash = new_hash
                            self._consume_rotation_instructions(parsed)
                            vocab = self._apply_classification(
                                w, f, parsed, fhash, vocab, records,
                                interactive=False,
                                used_imgs=[], used_text="",
                                default_source=source,
                                unknown_queue=None)
                            # Apply-time checkpoint: a restart sees the manifest
                            # immediately and cannot apply the same paid result
                            # to the same content twice.
                            self.manifest.save()
                        elif rtype == "errored":
                            self.stats["batch_errored"] += 1
                            self.stats["errors"] += 1
                            err = json.dumps(res.get("error", {}))[:300]
                            self.log(f"    ! {self._redact(f.name)}: batch "
                                     f"request errored - left unchanged")
                            self.failed_log.record(self.care_home, w.name, f,
                                                   "batch: errored", err)
                        elif rtype in ("canceled", "expired"):
                            key = ("batch_canceled" if rtype == "canceled"
                                   else "batch_expired")
                            self.stats[key] += 1
                            self.log(f"    ! {self._redact(f.name)}: batch "
                                     f"request {rtype} - left unchanged")
                            self.failed_log.record(self.care_home, w.name, f,
                                                   f"batch: {rtype}", "")
                    # save what has been applied so far (crash-safe resume)
                    self.manifest.save()
                    worker_state["classification_status"] = "complete"
                    worker_state["classification_completed_ts"] = \
                        datetime.datetime.now().isoformat(timespec="seconds")
                    worker_state["applied_records"] = [
                        {"path": str(record.get("path", "")),
                         "name": record.get("name", ""),
                         "group": record.get("group", "")}
                        for record in records]
                    if not state.save():
                        raise DurableStateError(
                            "worker classification completion could not be "
                            "persisted; finishing was not started")

                    # ---- shared tail: dedupe -> LIVE second pass -> organise --
                    if worker_state.get("finishing_status") != "complete":
                        worker_state["finishing_status"] = "in_progress"
                        worker_state["finishing_started_ts"] = \
                            datetime.datetime.now().isoformat(
                                timespec="seconds")
                        if not state.save():
                            raise DurableStateError(
                                "worker finishing start could not be persisted")
                        if records:
                            self._finish_worker(w, records)
                        worker_state["finishing_status"] = "complete"
                        worker_state["finishing_completed_ts"] = \
                            datetime.datetime.now().isoformat(
                                timespec="seconds")
                        self._persist_batch_live_cost()
                        if not state.save():
                            raise FinishingAmbiguous(
                                "worker finishing completed but its durable "
                                "completion marker could not be written")
                    self.stats["workers"] += 1
                    final_dir = w
                    # move mode: relocate the fully-processed worker (as live)
                    if self.move_mode and batch_worker_ready_to_move(w, records):
                        if not records:
                            self.log("  no documents found; moving empty worker "
                                     "folder unchanged")
                        try:
                            dest = unique_dir(self.move_dest, w.name)
                            worker_state["movement_status"] = "started"
                            worker_state["movement_target"] = str(dest)
                            if not state.save():
                                raise DurableStateError(
                                    "worker move marker could not be persisted; "
                                    "folder was not moved")
                            self.move_dest.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(w), str(dest))
                            final_dir = Path(dest)
                            try:
                                self._orientation_move_tree(w, dest)
                            except Exception as exc:
                                raise DurableStateError(
                                    "worker folder moved, but orientation paths "
                                    "could not be checkpointed; restart will "
                                    "recover the persisted move marker") from exc
                            self.stats["moved"] += 1
                            self.log(f"  moved worker folder -> {dest}")
                        except DurableStateError:
                            raise
                        except Exception as e:
                            worker_state["movement_status"] = "failed"
                            worker_state["movement_error"] = str(e)
                            self.stats["errors"] += 1
                            self.log(f"  ! could not move {w.name} to "
                                     f"destination: {e} (left in source)")
                            traceback.print_exc()
                    if not self.move_mode:
                        worker_state["movement_status"] = "disabled"
                    elif final_dir != w:
                        worker_state["movement_status"] = "complete"
                    elif worker_state.get("movement_status") == "pending":
                        worker_state["movement_status"] = "not_ready"
                    worker_state["final_path"] = str(final_dir)
                    # In move mode, a source-resident folder is not at its
                    # captured final document root yet. Retain it as incomplete
                    # so Check batch status can retry movement/attention rather
                    # than producing a partial processing receipt.
                    worker_complete = not self.move_mode or final_dir != w
                    worker_state["completed"] = worker_complete
                    if worker_complete:
                        worker_state["completed_ts"] = \
                            datetime.datetime.now().isoformat(timespec="seconds")
                    else:
                        worker_state.pop("completed_ts", None)
                    if not state.save():
                        raise DurableStateError(
                            "worker completion could not be persisted")
                    if worker_complete:
                        self._audit_worker_dirs.append(final_dir)
                        self._record_roster_handover(w, final_dir)
                except (StopRequested, LimitReached, CreditExhausted):
                    raise
                except Exception as e:
                    self.stats["errors"] += 1
                    self.log(f"  ! error on {w.name}: {e}")
                    traceback.print_exc()

            # results whose file no longer exists anywhere
            for cid, res in results.items():
                if cid in matched_cids:
                    continue
                meta = state.request_for(cid) or {}
                if res.get("type") == "succeeded":
                    self.stats["batch_missing"] += 1
                    self.log(f"  ! result for '{meta.get('path','?')}' could not "
                             f"be matched to any file (moved/deleted) - skipped")

            incomplete_workers = []
            for source in submitted_scope:
                worker = self._batch_worker_state(state, source)
                if not worker or not worker.get("completed"):
                    incomplete_workers.append(
                        worker or {"name": source.name,
                                   "source_path": str(source)})
            if incomplete_workers:
                state.data["phase"] = "processing_incomplete"
                state.save()
                self.log("Batch apply remains incomplete for: "
                         + ", ".join(worker.get("name", "?")
                                     for worker in incomplete_workers)
                         + ". Use Check batch status to resume; completed "
                           "finishing operations will be skipped.")
                self.on_done(self.stats, "batch_apply_incomplete")
                return

            self._phase_progress("processing", total, total)
            self.set_progress(total, total)
            self.manifest.save()
            # Rebuild the final scope in the immutable submitted order. This
            # also excludes any stale worker records from older buggy builds.
            self._audit_worker_dirs = [
                Path(self._batch_worker_state(state, source).get("final_path")
                     or source)
                for source in submitted_scope]
            review_run_id = getattr(self, "_review_run_id", "")
            if getattr(self, "_review_controller", None) is not None \
                    and review_run_id:
                workers = self._review_controller.validate_processing_scope(
                    review_run_id, processing_root=self.dir,
                    worker_dirs=list(self._audit_worker_dirs))
                self._audit_worker_dirs = [Path(item) for item in workers]
            # This is the durable boundary between paid classification/
            # finishing/movement and the optional audit. It is persisted before
            # the audit starts, so a stop/crash can never repeat finishing.
            state.data["processing_complete"] = True
            state.data["processing_completed_ts"] = \
                datetime.datetime.now().isoformat(timespec="seconds")
            state.data["phase"] = "processing_complete"
            if not state.save():
                raise RuntimeError(
                    "processing completion could not be persisted; audit was "
                    "not started")
            unique_audit_dirs = []
            seen_audit_dirs = set()
            for path in self._audit_worker_dirs:
                key = str(Path(path).resolve()).casefold()
                if key not in seen_audit_dirs:
                    seen_audit_dirs.add(key)
                    unique_audit_dirs.append(Path(path))
            self._audit_worker_dirs = unique_audit_dirs
            self._record_review_processing()
            self._run_post_run_audit()
            audit_status = (state.data.get("audit") or {}).get("status")
            if audit_status in ("complete", "skipped", "disabled"):
                state.mark_applied()
                state.delete()
            else:
                self.on_done(self.stats, "batch_processing_complete_audit_pending")
                return
            actual_batch_gbp = self._committed_batch_cost_gbp
            self.log(f"\n=== BATCH APPLIED: {self.stats['batch_succeeded']} "
                     f"succeeded, {self.stats['batch_errored']} errored, "
                     f"{self.stats['batch_expired']} expired, "
                     f"{self.stats['batch_canceled']} canceled, "
                     f"{self.stats['batch_missing']} unmatched ===")
            live_cost = max(0.0, self._current_cost_gbp() - actual_batch_gbp)
            self.log(f"Batch cost (primary + follow-up actual @ 50%): "
                     f"~£{actual_batch_gbp:.2f}  (cumulative estimate at submit: "
                     f"£{state.data.get('est_gbp', 0):.2f}); live finishing/audit: "
                     f"~£{live_cost:.2f}; cumulative actual: "
                     f"~£{self._current_cost_gbp():.2f}")
            self.on_done(self.stats,
                         f"batch_applied:{actual_batch_gbp:.4f}|"
                         f"{state.data.get('est_gbp', 0)}")
        except CreditExhausted as e:
            self.manifest.save()
            worker = getattr(self, "_current_worker", "") or "(unknown)"
            self.log(f"\n*** STOPPED: API credit exhausted while applying "
                     f"batch results (worker '{worker}'). {e.detail} ***")
            try:
                write_run_status(self.care_home, worker,
                                 "STOPPED - API CREDIT EXHAUSTED (batch apply)",
                                 e.detail)
            except Exception:
                traceback.print_exc()
            self.on_done(self.stats, f"credit:{worker}|{e.detail}")
        except LimitReached as e:
            self.manifest.save()
            self.log(f"\n*** STOPPED: {e.reason} (batch state kept; run "
                     f"'Check batch status' again to continue) ***")
            self.on_done(self.stats, f"limit:{e.reason}")
        except FinishingAmbiguous as e:
            self.manifest.save()
            self.log(f"\n*** FINISHING BLOCKED: {e}. The durable state is "
                     "kept and the operation will not be automatically "
                     "repeated. ***")
            self.on_done(self.stats, "batch_finishing_ambiguous")
        except DurableStateError as e:
            self.manifest.save()
            self.log(f"\n*** BATCH CHECKPOINT FAILED: {e}. Processing stopped "
                     "before the next filesystem or paid operation. ***")
            self.on_done(self.stats, "batch_state_write_failed")
        except StopRequested:
            self.manifest.save()
            self.log("\n*** STOPPED by user (batch state kept; run 'Check batch "
                     "status' again to continue applying) ***")
            self.on_done(self.stats, "stopped")
        except APIError as e:
            self.manifest.save()
            self.log(f"\n! batch polling/apply failed: {e.message}")
            traceback.print_exc()
            self.on_done(self.stats, f"batch_apply_failed:{e.message}")
        except Exception as e:
            self.manifest.save()
            self.log(f"\n! fatal error applying batch results: {e}")
            traceback.print_exc()
            self.on_done(self.stats, str(e))

    def _auto_review_unknowns(self, worker_dir: Path, unknowns: list,
                              records: list, vocab: str):
        """Automatically re-examine each queued 'Other - Unknown' from this
        worker with an INDIVIDUAL live classification - fresh full-page
        render, rotation retry and second opinion included - and rename the
        file when the closer look settles on a name (resolve_auto_review
        decides; unsettled files stay 'Other - Unknown'). No dialogs: this
        replaces the old ask-the-user review pass after a batch. Failures
        never abort the apply - the file just keeps its Unknown name."""
        self.log(f"  [auto-review] re-examining {len(unknowns)} unknown "
                 f"document(s) individually (live)")
        for item in unknowns:
            self._check_stop()
            p = Path(item["path"])
            if not p.exists():
                continue
            self.set_status(f"Auto-review unknown: {p.name}")
            try:
                core = classify_document_core(
                    self.api, vocab, p,
                    resolution=self.resolution,
                    adaptive_pages=False,   # small files; look at everything
                    emit_cost=self._emit_cost,
                    escalation_api=self.escalation_api)
            except (LimitReached, StopRequested):
                raise
            except Exception as e:
                self.log(f"    ! auto-review failed for "
                         f"{self._redact(p.name)}: {e} - stays as is")
                continue
            new_hash = self._maybe_fix_rotation(p, core["result"],
                                                page_idxs=core.get("page_idxs"))
            if new_hash:
                item["fhash"] = new_hash
            name, group = resolve_auto_review(self.kb, core["result"])
            if not name:
                self.log(f"    · {self._redact(p.name)}: still unidentified "
                         f"- stays 'Other - Unknown'")
                continue
            new_path = unique_path(worker_dir, safe_stem(name), p.suffix)
            try:
                p.rename(new_path)
                self._orientation_move_path(p, new_path)
            except Exception as e:
                self.log(f"    ! rename failed for {p.name}: {e}")
                self.stats["errors"] += 1
                continue
            self.stats["auto_reviewed"] = \
                self.stats.get("auto_reviewed", 0) + 1
            self.rename_log.record(self.care_home, worker_dir.name, p.name,
                                   new_path.name, group, "batch-auto-review")
            if item.get("fhash"):
                self.manifest.record(item["fhash"], self.api.model_id,
                                     self.resolution, name, group)
            # update the second-pass record so ranking/organising sees the
            # document under its real type
            for r in records:
                if str(r.get("path")) == str(p):
                    r["path"] = new_path
                    r["name"] = name
                    r["group"] = group
                    break
            self.log(f"    + auto-reviewed: {self._redact(p.name)} -> "
                     f"{self._redact(new_path.name)}")

    def _review_batch_unknowns(self, worker_dir: Path, unknowns: list,
                               records: list, vocab: str) -> str:
        """(Manual variant - no longer wired in; kept in case the dialog-based
        review is ever wanted again.) Re-open the existing Define-document
        dialog for each queued 'Other - Unknown' from this worker. A 'Stop
        run' in the dialog ends the REVIEW quietly (remaining files stay
        'Other - Unknown'); it does not abort applying. Returns the (possibly
        grown) vocabulary block."""
        for item in unknowns:
            p = Path(item["path"])
            if not p.exists():
                continue
            imgs, _txt = DocRender.render(p, zoom=self.resolution, pages="first")
            self.set_status(f"Review unknown: {p.name}")
            answer = self.ask_unknown(p.name, item.get("guess", ""),
                                      item.get("features", ""),
                                      imgs[0] if imgs else None)
            if answer is None:
                self.log("    review pass ended by user - remaining unknowns "
                         "stay as 'Other - Unknown'")
                self._review_unknowns_answer = False
                break
            decision, new_name, desc = answer
            if decision == "Other":
                name, group = "Other", "Other"
                # One-off Other descriptions are filenames, not persistent
                # controlled-vocabulary entries.
                name = other_name(new_name or "Unknown")
            else:
                self.kb.add(new_name, desc, "Relevant")
                vocab = self.kb.vocabulary_block()
                name, group = new_name, "Important"
            new_path = unique_path(worker_dir, safe_stem(name), p.suffix)
            try:
                p.rename(new_path)
                self._orientation_move_path(p, new_path)
            except Exception as e:
                self.log(f"    ! rename failed for {p.name}: {e}")
                self.stats["errors"] += 1
                continue
            self.rename_log.record(self.care_home, worker_dir.name, p.name,
                                   new_path.name, group, "batch-review")
            if item.get("fhash"):
                self.manifest.record(item["fhash"], self.api.model_id,
                                     self.resolution, name, group)
            # update the second-pass record for this file
            for r in records:
                if str(r.get("path")) == str(p):
                    r["path"] = new_path
                    r["name"] = name
                    r["group"] = group
                    break
            self.log(f"    reviewed: {self._redact(p.name)} -> "
                     f"{self._redact(new_path.name)}")
        return vocab

    def cancel_pending_batches(self):
        """Request cancellation of every pending batch for this folder. Anything
        already processed stays billed and downloadable; the state file is kept
        so those partial results can still be applied."""
        state = BatchState(self.dir)
        out = []
        for phase in ("primary", "followup"):
            api = (self._api_for_model(
                (state.data.get("followup") or {}).get("model_id")
                or self.api.model_id) if phase == "followup" else self.api)
            for bid in state.batch_ids(phase):
                try:
                    r = api.cancel_batch(bid)
                    out.append((bid, r.get("processing_status", "canceling")))
                    self.log(f"  cancel requested for {phase} batch {bid}")
                except Exception as e:
                    out.append((bid, f"cancel failed: {e}"))
                    self.log(f"  ! cancel failed for {bid}: {e}")
        return out

    def _quick_match(self, path: Path, page1_text: str):
        """Cheap LOCAL match (no API) used only when the 'skip API when clear'
        setting is on. Returns (name, group) only for a confident, unambiguous
        hit; otherwise None so the API is used. Conservative by design - it must
        not introduce the kind of errors the API disambiguations fix."""
        hay = f"{path.stem}\n{page1_text}".lower()

        # Strong, low-ambiguity signals only. Anything in the commonly-confused
        # families (passport/NI, BRP/RTW/share-code, DBS variants) is left to the
        # API so the disambiguation rules apply.
        rules = [
            ("Payslip", ["payslip", "pay slip", "net pay", "gross pay"]),
            ("CV", ["curriculum vitae", "\ncv\n"]),
            ("Bank Statement", ["bank statement", "sort code", "iban"]),
            ("Working Time Directive Waiver", ["working time", "48 hour", "48-hour", "opt-out", "opt out"]),
            ("Health Declaration", ["health declaration"]),
            ("PPE Declaration", ["ppe declaration", "personal protective equipment declaration"]),
            ("Birth Certificate", ["certificate of birth", "birth certificate"]),
            ("Employee Handbook", ["employee handbook", "staff handbook"]),
            ("Job Description", ["job description", "person specification"]),
            ("Job Advert", ["job advert", "vacancy", "we are hiring", "job advertisement"]),
            ("Interview Notes", ["interview notes", "interview assessment"]),
            ("Driving Theory Test Certificate", ["theory test pass", "theory test certificate"]),
            ("Driving Practical Test Certificate", ["practical test pass", "driving test pass certificate"]),
        ]
        hits = [name for name, kws in rules if any(k in hay for k in kws)]
        if len(hits) == 1:
            name = hits[0]
            g = self.kb.group_of(name) or "Important"
            return (name, g)
        return None

    def _rebuild_records(self, worker_dir: Path, records: list):
        """After dedup, some recorded paths may be gone (deleted) or the kept
        copy may have been renamed to the clean base name. Re-map records to the
        files that actually exist now, matching by base label + extension.
        Carries the cached page images/text forward so the second pass need not
        re-render or re-download."""
        existing = [p for p in worker_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in DOC_EXT
                    and not is_program_file(p)]
        by_label = {}
        for p in existing:
            by_label.setdefault((_base_label(p.stem), p.suffix.lower()), []).append(p)
        new_records = []
        seen = set()
        for r in records:
            key = (_base_label(r["name"]), r["path"].suffix.lower())
            candidates = by_label.get(key, [])
            chosen = None
            for c in candidates:
                if c not in seen:
                    chosen = c
                    break
            if chosen is None:
                continue  # this record's file was a deleted duplicate
            seen.add(chosen)
            new_records.append({"path": chosen, "name": r["name"],
                                "group": r["group"], "original": r["original"],
                                "imgs": r.get("imgs", []), "text": r.get("text", "")})
        return new_records

    # ---- second pass ----
    def _pages_for_review(self, rec, path: Path):
        """Return (imgs, text) for a second-pass review. Reuses the cached
        render when it already covers multiple pages; if only page 1 was cached
        (because the doc was identified early), re-render the full set so a
        signature/date on a later page is not missed. Single-page docs reuse
        the cache directly."""
        cached_imgs = rec.get("imgs") or []
        cached_text = rec.get("text") or ""
        total = DocRender.page_count(path)
        if total <= 1 or len(cached_imgs) >= min(total, DocRender.MAX_PAGES):
            if cached_imgs or cached_text:
                return cached_imgs, cached_text
        imgs, text = DocRender.render(path, zoom=self.resolution, pages="all")
        rec["imgs"], rec["text"] = imgs, text
        return imgs, text

    def _second_pass(self, worker_dir: Path, records: list):
        """After every document is renamed, rank duplicates and date the dated
        types. Behaviour:
          - Certificate of Sponsorship -> 'Certificate of Sponsorship - (date)'.
          - Share Code Check Result    -> 'Share Code Check Result - (date)'.
          - Employment Contract        -> signed copies preferred when ranking.
          - EVERY type with 2+ copies is RANKED worst -> best with a numbered
            suffix: bare name (worst), then ' (01)', ' (02)', ... highest = best
            (newest / clearest / most relevant). Single copies keep the bare
            name. Dated types (CoS / Share Code) keep their date AND, when there
            are multiples, also get the rank number: 'Name - (date) (NN)'.
        """
        self._check_stop()

        def current(rec):
            return rec["path"] if rec["path"].exists() else None

        def finishing_key(kind, path):
            try:
                identity = file_hash(path)
            except Exception:
                identity = str(path).casefold()
            return f"{kind}:{identity}"

        # ---- date helpers for the two dated types ----
        def cos_date_for(r, p):
            imgs, text = self._pages_for_review(r, p)
            try:
                raw = self._finishing_operation(
                    worker_dir, finishing_key("cos-date", p),
                    lambda: self.api.cos_issue_date(imgs, text))
                return parse_date(raw)
            except (StopRequested, LimitReached, CreditExhausted):
                raise
            except Exception as e:
                self.stats["errors"] += 1
                self.log(f"      ! {self._redact(p.name)}: CoS date unreadable ({e})")
                return None

        def sc_date_for(r, p):
            imgs, text = self._pages_for_review(r, p)
            try:
                d = self._finishing_operation(
                    worker_dir, finishing_key("share-code-date", p),
                    lambda: self.api.share_code_check(imgs, text)) or {}
                return parse_date(d.get("check_date", ""))
            except (StopRequested, LimitReached, CreditExhausted):
                raise
            except Exception as e:
                self.stats["errors"] += 1
                self.log(f"      ! {self._redact(p.name)}: Share Code date "
                         f"unreadable ({e})")
                return None

        def quality_for(r, p, doc_type):
            imgs, text = self._pages_for_review(r, p)
            try:
                q = self._finishing_operation(
                    worker_dir, finishing_key(f"quality:{doc_type}", p),
                    lambda: self.api.doc_quality(imgs, text, doc_type))
                if not isinstance(q, dict):
                    raise ValueError("quality result unavailable")
            except (StopRequested, LimitReached, CreditExhausted):
                raise
            except Exception as e:
                self.stats["errors"] += 1
                self.log(f"      ! {self._redact(p.name)}: quality unreadable ({e})")
                q = {"score": 0, "legible": False, "complete": False,
                     "date": "", "note": "score failed"}
            return q

        # ---- group records by their (base) controlled name ----
        groups = {}
        for r in records:
            groups.setdefault(r["name"], []).append(r)

        # types that carry a date in the final name
        DATED = {"Certificate of Sponsorship": cos_date_for,
                 "Share Code Check Result": sc_date_for}
        # stat keys for a few headline types (others fall under 'ranked')
        STAT_KEY = {"Certificate of Sponsorship": "cos",
                    "Share Code Check Result": "sharecode",
                    "DBS Document": "dbs", "BRP": "brp",
                    "eVisa Screenshot": "evisa",
                    "National Insurance Number": "ni"}

        for base_name, recs in groups.items():
            # only rank genuine compliance items; skip the catch-all 'Other'
            if base_name == "Other" or base_name.lower().startswith("other"):
                continue
            live = [(r, current(r)) for r in recs]
            live = [(r, p) for r, p in live if p]
            if not live:
                continue

            n = len(live)
            self.log(f"  [review] {base_name}: {n} cop{'y' if n == 1 else 'ies'}")

            # gather a date for dated types (needed for naming + as rank signal)
            date_of = {}
            if base_name in DATED:
                getter = DATED[base_name]
                for r, p in live:
                    self._check_stop()
                    date_of[id(r)] = getter(r, p)

            # ----- single copy: just (date) if dated, else leave as-is -----
            if n == 1:
                r, p = live[0]
                if base_name in DATED:
                    d = date_of.get(id(r))
                    if d is not None:
                        self._rename_suffix(
                            worker_dir, p,
                            f"{base_name} - ({d.strftime('%d-%m-%Y')})",
                            r, "Crucial")
                if base_name in STAT_KEY:
                    self.stats[STAT_KEY[base_name]] = \
                        self.stats.get(STAT_KEY[base_name], 0) + 1
                continue

            # ----- multiple copies: score each, then rank worst -> best -----
            scored = []
            for r, p in live:
                self._check_stop()
                q = quality_for(r, p, base_name)
                # for dated types, fold the date into the ranking signal so the
                # newest tends to rank highest, with quality as a tie-breaker
                d = date_of.get(id(r))
                date_rank = d.toordinal() if d else 0
                signed_bonus = 0
                if base_name == "Employment Contract":
                    imgs, text = self._pages_for_review(r, p)
                    try:
                        signed = self._finishing_operation(
                            worker_dir, finishing_key("contract-signed", p),
                            lambda: self.api.contract_signed(imgs, text))
                        signed_bonus = 1 if signed else 0
                    except (StopRequested, LimitReached, CreditExhausted):
                        raise
                    except Exception:
                        signed_bonus = 0
                scored.append({
                    "r": r, "p": p, "q": q,
                    "sort_key": (date_rank, signed_bonus, q["score"],
                                 1 if q["legible"] else 0,
                                 1 if q["complete"] else 0),
                })
                extra = []
                if d:
                    extra.append(d.strftime("%d-%m-%Y"))
                if signed_bonus:
                    extra.append("signed")
                if q.get("note"):
                    extra.append(q["note"])
                self.log(f"      {self._redact(p.name)}: score {q['score']}"
                         + (f"  [{', '.join(extra)}]" if extra else ""))

            # worst first, best last -> index 0 gets bare name, last gets (NN)
            scored.sort(key=lambda s: s["sort_key"])
            top = len(scored) - 1
            # PHASE 1: park every copy under a temporary name first. Assigning
            # final names directly used to collide with group members that had
            # not been renamed yet (e.g. the worst copy's bare-name target was
            # still occupied by a later-ranked file), which made unique_path
            # silently mint out-of-sequence names like 'Health Declaration (5)'
            # alongside '(01)/(02)/(03)'. With every member parked, the final
            # names can never collide with the group itself.
            for i, s in enumerate(scored):
                r = s["r"]
                p_now = current(r)
                if not p_now:
                    continue
                s["orig_display"] = p_now.name
                tmp = unique_path(worker_dir, f"__rank_tmp_{i}", p_now.suffix)
                try:
                    p_now.rename(tmp)
                    self._orientation_move_path(p_now, tmp)
                    r["path"] = tmp
                except Exception:
                    pass   # keep its current name; phase 2 renames from there
            for idx, s in enumerate(scored):
                r, p = s["r"], s["p"]
                p_now = current(r)
                if not p_now:
                    continue
                # base label, with date for dated types
                if base_name in DATED:
                    d = date_of.get(id(r))
                    label = (f"{base_name} - ({d.strftime('%d-%m-%Y')})"
                             if d is not None else base_name)
                else:
                    label = base_name
                # numbered rank suffix: worst = none, then (01), (02), ...
                if idx == 0:
                    new_name = label
                else:
                    new_name = f"{label} ({idx:02d})"
                group = "Crucial" if base_name in DATED or base_name in (
                    "DBS Document", "BRP", "eVisa Screenshot") else "Important"
                self._rename_suffix(worker_dir, p_now, new_name, r, group,
                                    display_from=s.get("orig_display"))
                if idx == top:
                    self.log(f"      -> best: {Path(new_name).name}")
            if base_name in STAT_KEY:
                self.stats[STAT_KEY[base_name]] = \
                    self.stats.get(STAT_KEY[base_name], 0) + 1
            else:
                self.stats["ranked"] = self.stats.get("ranked", 0) + 1

    def _rename_suffix(self, worker_dir, path: Path, new_name, rec, group,
                       display_from: str = None):
        new_path = unique_path(worker_dir, safe_stem(new_name), path.suffix)
        try:
            path.rename(new_path)
            self._orientation_move_path(path, new_path)
            rec["path"] = new_path
            rec["name"] = new_name
            self.rename_log.record(self.care_home, worker_dir.name,
                                   display_from or path.name,
                                   new_path.name, group, "second-pass")
            self.log(f"      -> {new_path.name}")
        except Exception as e:
            self.log(f"      ! could not rename to {new_name}: {e}")
            self.stats["errors"] += 1


# ====================================================================
# GUI HELPERS
# ====================================================================
# ====================================================================
# BUILT-IN USER GUIDE  (integrated PDF viewer for the Stage 2 guide)
# ====================================================================
GUIDES_DIR = LIFTED_APPDATA_DIR / "Guides"


def guide_dirs():
    """Everywhere a guide PDF may live, in priority order. The installers put
    the guides in the SHARED app-data folder (and a copy beside the exe), so a
    fresh install on any machine finds them — the old Documents location is
    kept last for backwards compatibility."""
    dirs = []
    try:
        exe_dir = Path(sys.executable).resolve().parent if getattr(
            sys, "frozen", False) else Path(__file__).resolve().parent
        dirs += [exe_dir / "Guides", exe_dir]
    except Exception:
        pass
    dirs.append(GUIDES_DIR)                                        # shared
    dirs.append(Path.home() / "Documents" / "Lifted" / "Guides")   # legacy
    return dirs


def find_guide_pdf(prefix="stage 2"):
    """First guide PDF matching `prefix` across all candidate folders."""
    bundled = bundled_resource("docs", "USER_GUIDE.pdf")
    if prefix == "stage 2" and bundled.is_file():
        return bundled
    for d in guide_dirs():
        try:
            if not d.is_dir():
                continue
            cands = sorted(p for p in d.glob("*.pdf")
                           if p.name.lower().startswith(prefix))
            if cands:
                return cands[0]
        except Exception:
            continue
    return None


def open_stage_guide(parent):
    """Open the Stage 2 user guide in an integrated viewer window (page
    navigation, zoom, and an 'Open in PDF app' button that hands the file
    to Adobe / the system default). Falls back to the system PDF app when
    the render engine isn't available."""
    try:
        pdf = find_guide_pdf("stage 2")
    except Exception:
        pdf = None
    if pdf is None:
        messagebox.showinfo(
            "Guide not found",
            f"No Stage 2 guide PDF was found in:\n{GUIDES_DIR}", parent=parent)
        return
    try:
        import fitz
    except ImportError:
        os.startfile(str(pdf))
        return

    win = tk.Toplevel(parent)
    win.title("Stage 2 — User guide")
    win.configure(bg=BG)
    App._set_icon(win)
    win.after(0, lambda: style_titlebar_black(win))
    win.geometry("980x760")
    win.minsize(700, 520)
    try:
        doc = fitz.open(pdf)
    except Exception as e:
        messagebox.showerror("Could not open guide", str(e), parent=parent)
        win.destroy()
        return
    state = {"i": 0, "zoom": 1.25, "img": None}

    bar = tk.Frame(win, bg=PANEL2, height=44)
    bar.pack(fill="x")
    bar.pack_propagate(False)

    def mk(txt, cmd, w=None):
        b = tk.Button(bar, text=txt, command=cmd, bg=PANEL2, fg=FG,
                      activebackground=PANEL, activeforeground=FG,
                      relief="flat", font=("Segoe UI", 10), cursor="hand2",
                      padx=10)
        if w:
            b.configure(width=w)
        return b

    wrap = tk.Frame(win, bg=BG)
    canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
    vs = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vs.set)
    page_lbl = tk.Label(bar, text="", bg=PANEL2, fg=FG,
                        font=("Segoe UI", 10, "bold"), width=12)

    def render():
        try:
            pm = doc[state["i"]].get_pixmap(
                matrix=fitz.Matrix(state["zoom"], state["zoom"]))
            state["img"] = tk.PhotoImage(data=pm.tobytes("png"))
            canvas.delete("all")
            canvas.create_image(12, 12, anchor="nw", image=state["img"])
            canvas.configure(scrollregion=(0, 0, pm.width + 24,
                                           pm.height + 24))
            canvas.yview_moveto(0)
            page_lbl.configure(text=f"Page {state['i'] + 1} / {doc.page_count}")
        except Exception as e:
            page_lbl.configure(text=f"error: {e}")

    def go(d):
        j = state["i"] + d
        if 0 <= j < doc.page_count:
            state["i"] = j
            render()

    def zoom(f):
        state["zoom"] = max(0.4, min(4.0, state["zoom"] * f))
        render()

    def fit_width():
        try:
            avail = canvas.winfo_width() - 28
            pw = doc[state["i"]].rect.width
            if avail > 100 and pw:
                state["zoom"] = avail / pw
        except Exception:
            pass
        render()

    mk("◀", lambda: go(-1), 3).pack(side="left", padx=(12, 2), pady=7)
    page_lbl.pack(side="left")
    mk("▶", lambda: go(1), 3).pack(side="left", padx=2, pady=7)
    tk.Frame(bar, bg=BORDER, width=1, height=22).pack(side="left", padx=10)
    mk("−", lambda: zoom(1 / 1.2), 3).pack(side="left", padx=2, pady=7)
    mk("+", lambda: zoom(1.2), 3).pack(side="left", padx=2, pady=7)
    mk("Fit width", fit_width).pack(side="left", padx=(8, 0), pady=7)
    ob = tk.Button(bar, text="⧉  Open in PDF app",
                   command=lambda: os.startfile(str(pdf)),
                   bg=ACCENT, fg="#04101c", activebackground="#6fb6ff",
                   activeforeground="#04101c", relief="flat",
                   font=("Segoe UI", 10, "bold"), cursor="hand2", padx=14)
    ob.pack(side="right", padx=12, pady=7)

    wrap.pack(fill="both", expand=True)
    vs.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    win.bind(
        "<MouseWheel>",
        lambda e: (zoom(1.1 if e.delta > 0 else 1 / 1.1) if e.state & 0x0004
                   else canvas.yview_scroll(-1 * int(e.delta / 100), "units")))
    win.bind("<Prior>", lambda e: go(-1))
    win.bind("<Next>", lambda e: go(1))
    win.bind("<Destroy>", lambda e: (doc.close()
                                     if e.widget is win else None))
    win.after(60, fit_width)


def style_button(btn, base, hover):
    foreground = resolve_palette(getattr(btn.winfo_toplevel(), "cfg", {}).get("ui_palette")).primary_text if base in (ACCENT, GREEN, GREEN_HI) else FG
    btn.configure(bg=base, fg=foreground, activebackground=hover,
                  activeforeground=foreground, relief="flat", bd=0,
                  highlightthickness=1, highlightbackground=BORDER,
                  highlightcolor=ACCENT, disabledforeground=FG_DIM, takefocus=True,
                  font=UI_B, cursor="hand2", padx=14, pady=7)
    btn.bind("<Enter>", lambda e: btn.configure(bg=hover) if str(btn.cget("state")) != "disabled" else None)
    btn.bind("<Leave>", lambda e: btn.configure(bg=base))


def style_titlebar_black(win):
    """Paint a window's WINDOWS TITLE BAR (the OS caption strip with the
    minimise / maximise / close buttons) black, with white caption text.
    Windows 11 build 22000+ via the DWM caption-colour attributes; on older
    Windows it falls back to immersive dark mode (dark grey), and is a silent
    no-op off Windows. Safe to call after the window is realised."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        from ctypes import wintypes
        win.update_idletasks()
        # the decorated top-level (the frame that actually owns the caption)
        # is the PARENT of the Tk client HWND
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                              ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_CAPTION_COLOR = 35   # Win11 22000+ ; COLORREF 0x00BBGGRR
        DWMWA_TEXT_COLOR = 36      # Win11 22000+
        dark = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                                  ctypes.byref(dark), ctypes.sizeof(dark))
        black = ctypes.c_uint(0x00000000)   # black caption background
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR,
                                  ctypes.byref(black), ctypes.sizeof(black))
        white = ctypes.c_uint(0x00FFFFFF)   # white caption text
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_TEXT_COLOR,
                                  ctypes.byref(white), ctypes.sizeof(white))
        # No translucent/Mica caption: the approved Obsidian ribbon is black.
        backdrop_none = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop_none),
                                  ctypes.sizeof(backdrop_none))
    except Exception:
        pass


# ====================================================================
# SETTINGS DIALOG
# ====================================================================
class SettingsDialog(tk.Toplevel):
    def __init__(self, master, cfg, on_save):
        super().__init__(master)
        self.cfg = cfg
        self.on_save = on_save
        self.title("Settings")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.transient(master)
        self.grab_set()
        self.after(0, lambda: style_titlebar_black(self))
        _ui = float(getattr(master, "_ui_scale", 1.0) or 1.0)
        self.geometry(f"{min(int(790*_ui), self.winfo_screenwidth()-80)}x{min(int(820*_ui), self.winfo_screenheight()-90)}")
        self.minsize(min(int(700*_ui), self.winfo_screenwidth()-80), 440)

        # ---- scrollable body so all controls fit on small screens ----
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.body = tk.Frame(canvas, bg=BG)
        self.body.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        body_window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(body_window, width=event.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.bind("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        b = self.body
        b.columnconfigure(1, weight=1)
        pad = {"padx": 16, "pady": 6}
        settings_head = tk.Frame(b, bg=BG)
        settings_head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(14, 8))
        settings_head.columnconfigure(1, weight=1)
        tk.Label(settings_head, text="Settings", bg=BG, fg=FG, font=UI_H).grid(row=0, column=0, sticky="w")
        notifications_btn = tk.Button(settings_head, text="Notifications…",
            command=lambda:master._open_notification_settings())
        style_button(notifications_btn, PANEL2, BORDER)
        notifications_btn.grid(row=0, column=1, sticky="e", padx=(12, 0))
        appearance = tk.Frame(settings_head, bg=BG)
        appearance.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        tk.Label(appearance, text="Colour palette", bg=BG, fg=FG_DIM, font=UI).pack(side="left")
        self._palette_labels = {f"{key} · {value.name}": key for key, value in PALETTES.items()}
        selected_palette = resolve_palette(cfg.get("ui_palette"))
        self.palette_var = tk.StringVar(value=f"{selected_palette.key} · {selected_palette.name}")
        self.palette_box = ttk.Combobox(appearance, textvariable=self.palette_var,
            values=list(self._palette_labels), state="readonly", width=25)
        self.palette_box.pack(side="right")
        self.palette_box.bind("<<ComboboxSelected>>", self._preview_palette)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # ---------------- API KEY (stored securely) ----------------
        tk.Label(b, text="API key & security", bg=BG, fg=ACCENT, font=UI_B).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=16, pady=(6, 2))
        tk.Label(b, text="Anthropic API key", bg=BG, fg=FG_DIM, font=UI).grid(
            row=2, column=0, sticky="w", **pad)
        # NOTE: we never pre-fill the real key. We show a placeholder if one is
        # already stored. Leaving the field blank keeps the existing key.
        self._has_existing_key = bool(get_api_key())
        self.key_var = tk.StringVar(value="")
        self.key_entry = tk.Entry(b, textvariable=self.key_var, width=42,
                                  show="*", bg=PANEL2, fg=FG, insertbackground=FG,
                                  relief="flat", font=MONO)
        self.key_entry.grid(row=2, column=1, sticky="w", **pad)
        ph = ("•••• stored - leave blank to keep"
              if self._has_existing_key else "paste key here")
        self.key_entry.insert(0, "")
        self.key_hint = tk.Label(b, text=f"Currently: {api_key_source_label()}   "
                                         f"({'set' if self._has_existing_key else 'none'})",
                                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8))
        self.key_hint.grid(row=3, column=1, sticky="w", padx=16)
        self.show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(b, text="show", variable=self.show_var,
                       command=self._toggle_show, bg=BG, fg=FG_DIM,
                       selectcolor=PANEL2, activebackground=BG,
                       activeforeground=FG, font=UI).grid(row=2, column=1, sticky="e", padx=16)
        store_txt = ("Stored in the OS credential store (keyring)."
                     if HAS_KEYRING else
                     "keyring not installed: will use the ANTHROPIC_API_KEY "
                     "environment variable, or a protected local file as a last "
                     "resort. Run 'pip install keyring' for OS-level storage.")
        tk.Label(b, text=store_txt, bg=BG, fg=FG_DIM, font=("Segoe UI", 8),
                 wraplength=360, justify="left").grid(
            row=4, column=1, sticky="w", padx=16, pady=(0, 2))
        clr = tk.Button(b, text="Clear stored key", command=self._clear_key)
        style_button(clr, PANEL2, BORDER)
        clr.grid(row=4, column=0, sticky="w", padx=16)

        # redact logs
        self.redact_var = tk.BooleanVar(value=bool(cfg.get("redact_logs", False)))
        tk.Checkbutton(b, text="Redact filenames in the activity log (hides worker "
                              "names that appear in filenames)",
                       variable=self.redact_var, bg=BG, fg=FG, selectcolor=PANEL2,
                       activebackground=BG, activeforeground=FG, font=UI,
                       wraplength=380, justify="left", anchor="w").grid(
            row=5, column=0, columnspan=2, sticky="w", padx=16, pady=(4, 0))

        # ---------------- MODEL ----------------
        tk.Label(b, text="Model", bg=BG, fg=ACCENT, font=UI_B).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 2))
        self.model_var = tk.StringVar(value=cfg.get("model", DEFAULT_MODEL))
        self.mframe = tk.Frame(b, bg=BG)
        self.mframe.grid(row=7, column=0, columnspan=2, sticky="w", padx=16)
        self.advanced_var = tk.BooleanVar(value=bool(cfg.get("advanced_models", False)))
        self._render_model_choices()
        tk.Checkbutton(b, text="Show advanced (expensive) models — e.g. Opus",
                       variable=self.advanced_var, command=self._render_model_choices,
                       bg=BG, fg=AMBER, selectcolor=PANEL2, activebackground=BG,
                       activeforeground=AMBER, font=UI, anchor="w").grid(
            row=8, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 0))

        # ---------------- FX ----------------
        tk.Label(b, text="USD → GBP rate", bg=BG, fg=FG_DIM, font=UI).grid(
            row=9, column=0, sticky="w", **pad)
        self.fx_var = tk.StringVar(value=str(cfg.get("fx", 0.79)))
        tk.Entry(b, textvariable=self.fx_var, width=10, bg=PANEL2, fg=FG,
                 insertbackground=FG, relief="flat", font=MONO).grid(
            row=9, column=1, sticky="w", **pad)

        # ---------------- SPENDING / SAFETY LIMITS ----------------
        tk.Label(b, text="Spending & safety limits", bg=BG, fg=ACCENT, font=UI_B).grid(
            row=10, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 2))
        self.maxw_var = tk.StringVar(value=str(cfg.get("max_workers", DEFAULT_MAX_WORKERS)))
        self.maxf_var = tk.StringVar(value=str(cfg.get("max_files", DEFAULT_MAX_FILES)))
        self.maxb_var = tk.StringVar(value=str(cfg.get("max_budget_gbp", DEFAULT_MAX_BUDGET_GBP)))
        self.maxmb_var = tk.StringVar(value=str(cfg.get("max_file_mb", DEFAULT_MAX_FILE_MB)))
        self.secpf_var = tk.StringVar(value=str(cfg.get("sec_per_file", DEFAULT_SEC_PER_FILE)))
        for i, (lbl, var, hint) in enumerate([
                ("Max worker folders / run", self.maxw_var, "run stops after this many workers"),
                ("Max files / run", self.maxf_var, "run stops after this many files are sent"),
                ("Max spend / run (£)", self.maxb_var, "run stops when estimated £ reaches this"),
                ("Skip files larger than (MB)", self.maxmb_var, "oversized files are skipped, not sent"),
                ("Est. seconds / file", self.secpf_var, "used only for the run-time estimate")]):
            r = 11 + i
            tk.Label(b, text=lbl, bg=BG, fg=FG_DIM, font=UI).grid(
                row=r, column=0, sticky="w", padx=16, pady=4)
            row = tk.Frame(b, bg=BG); row.grid(row=r, column=1, sticky="w", padx=16)
            tk.Entry(row, textvariable=var, width=10, bg=PANEL2, fg=FG,
                     insertbackground=FG, relief="flat", font=MONO).pack(side="left")
            tk.Label(row, text=hint, bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))

        # ---------------- COST / ACCURACY ----------------
        tk.Label(b, text="Cost & accuracy", bg=BG, fg=ACCENT, font=UI_B).grid(
            row=16, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 2))
        tk.Label(b, text="Image resolution", bg=BG, fg=FG_DIM, font=UI).grid(
            row=17, column=0, sticky="w", **pad)
        rframe = tk.Frame(b, bg=BG)
        rframe.grid(row=17, column=1, sticky="w", **pad)
        self.res_var = tk.DoubleVar(value=float(cfg.get("resolution", 1.5)))
        self.res_label = tk.Label(rframe, text="", bg=BG, fg=FG, font=UI_B,
                                  anchor="w")
        scale = tk.Scale(rframe, from_=1.0, to=3.0, resolution=0.1,
                         orient="horizontal", variable=self.res_var,
                         command=self._on_res, length=200, bg=BG, fg=FG,
                         troughcolor=PANEL2, highlightthickness=0, bd=0,
                         activebackground=ACCENT, showvalue=False)
        scale.pack(side="left")
        self.res_label.pack(side="left", padx=(8, 0))
        self._on_res(self.res_var.get())
        tk.Label(b, text="Lower = cheaper, but text must stay readable. "
                         "1.5 is a good balance; 1.0 is the cheapest.",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8), wraplength=300,
                 justify="left").grid(row=18, column=1, sticky="w", padx=16)

        self.adapt_var = tk.BooleanVar(value=bool(cfg.get("adaptive_pages", True)))
        tk.Checkbutton(b, text="Adaptive pages: check page 1 first; send more "
                              "only if needed",
                       variable=self.adapt_var, bg=BG, fg=FG, selectcolor=PANEL2,
                       activebackground=BG, activeforeground=FG, font=UI,
                       wraplength=380, justify="left", anchor="w").grid(
            row=19, column=0, columnspan=2, sticky="w", padx=16, pady=(8, 0))

        self.skip_var = tk.BooleanVar(value=bool(cfg.get("skip_when_clear", False)))
        tk.Checkbutton(b, text="Skip the API when the filename/text already "
                              "clearly identifies the document (cheaper; "
                              "small accuracy risk)",
                       variable=self.skip_var, bg=BG, fg=FG, selectcolor=PANEL2,
                       activebackground=BG, activeforeground=FG, font=UI,
                       wraplength=380, justify="left", anchor="w").grid(
            row=20, column=0, columnspan=2, sticky="w", padx=16, pady=(6, 0))

        self.auto_other_var = tk.BooleanVar(value=bool(cfg.get("auto_other", False)))
        tk.Checkbutton(b, text="Auto-file unrecognised documents as Other "
                              "(no prompt) — named \"Other - <brief description>\", "
                              "e.g. \"Other - reference request email\"",
                       variable=self.auto_other_var, bg=BG, fg=FG, selectcolor=PANEL2,
                       activebackground=BG, activeforeground=FG, font=UI,
                       wraplength=380, justify="left", anchor="w").grid(
            row=21, column=0, columnspan=2, sticky="w", padx=16, pady=(6, 0))

        # ---------------- OPTIONAL LOCAL AI ----------------
        tk.Label(b, text="Local page orientation (private, no API)", bg=BG, fg=ACCENT,
                 font=UI_B).grid(row=22, column=0, columnspan=2, sticky="w",
                                 padx=16, pady=(12, 2))
        self.orientation_mode_var = tk.StringVar(
            value=cfg.get("orientation_mode", "audit"))
        orientation_frame = tk.Frame(b, bg=BG)
        orientation_frame.grid(row=23, column=0, columnspan=2, sticky="w",
                               padx=16, pady=(2, 0))
        for label, value in (
                ("Off", "off"),
                ("Audit only / shadow mode (default)", "audit"),
                ("Automatic high-confidence correction", "automatic")):
            tk.Radiobutton(
                orientation_frame, text=label,
                variable=self.orientation_mode_var, value=value,
                bg=BG, fg=FG, selectcolor=PANEL2, activebackground=BG,
                activeforeground=FG, font=UI, anchor="w").pack(anchor="w")
        local_row = tk.Frame(b, bg=BG)
        local_row.grid(row=24, column=0, columnspan=2, sticky="w", padx=34,
                       pady=(2, 2))
        self.local_ai_setup_btn = tk.Button(
            local_row, text="Bundled CPU ONNX model", state="disabled")
        style_button(self.local_ai_setup_btn, PANEL2, BORDER)
        self.local_ai_setup_btn.pack(side="left")
        self.orientation_conf_var = tk.StringVar(
            value=str(cfg.get("orientation_confidence", 0.95)))
        self.orientation_margin_var = tk.StringVar(
            value=str(cfg.get("orientation_margin", 0.20)))
        tk.Label(local_row, text="confidence", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 3))
        tk.Entry(local_row, textvariable=self.orientation_conf_var, width=5,
                 bg=PANEL2, fg=FG, insertbackground=FG,
                 relief="flat", font=MONO).pack(side="left")
        tk.Label(local_row, text="margin", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(side="left", padx=(8, 3))
        tk.Entry(local_row, textvariable=self.orientation_margin_var, width=5,
                 bg=PANEL2, fg=FG, insertbackground=FG,
                 relief="flat", font=MONO).pack(side="left")
        self.local_ai_status = tk.Label(
            local_row, text="  every PDF page, small CPU batches", bg=BG,
            fg=FG_DIM,
            font=("Segoe UI", 8), wraplength=315, justify="left")
        self.local_ai_status.pack(side="left", padx=(10, 0))

        # ---------------- CONVERSION (Stage 2) ----------------
        tk.Label(b, text="PDF conversion", bg=BG, fg=ACCENT, font=UI_B).grid(
            row=25, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 2))
        self.convert_var = tk.BooleanVar(value=bool(cfg.get("convert_pdf", True)))
        tk.Checkbutton(b, text="Convert every document to PDF first "
                              "(images, Office files, .msg/.eml and .txt). "
                              "Existing PDFs are left unchanged. Office files "
                              "need LibreOffice installed for full conversion; "
                              "without it, Word falls back to text-only and other "
                              "Office files are left as-is.",
                       variable=self.convert_var, bg=BG, fg=FG, selectcolor=PANEL2,
                       activebackground=BG, activeforeground=FG, font=UI,
                       wraplength=400, justify="left", anchor="w").grid(
            row=26, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 0))
        lo_state = ("LibreOffice: found"
                    if PdfConverter.has_libreoffice()
                    else "LibreOffice: NOT found (text-only fallback for Office)")
        tk.Label(b, text=lo_state, bg=BG,
                 fg=(GREEN_HI if PdfConverter.has_libreoffice() else AMBER),
                 font=("Segoe UI", 8)).grid(row=27, column=0, columnspan=2,
                                            sticky="w", padx=34, pady=(0, 0))

        # ---------------- FILE MOVEMENT ----------------
        tk.Label(b, text="File movement", bg=BG, fg=ACCENT, font=UI_B).grid(
            row=28, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 2))
        self.move_var = tk.BooleanVar(value=bool(cfg.get("move_mode", False)))
        tk.Checkbutton(b, text="Move processed workers to a destination folder "
                              "instead of renaming in place. You choose a SOURCE "
                              "and a DESTINATION; each worker subfolder is moved "
                              "into the destination once it has been fully "
                              "processed. (A worker already in the destination is "
                              "skipped as done.)",
                       variable=self.move_var, bg=BG, fg=FG, selectcolor=PANEL2,
                       activebackground=BG, activeforeground=FG, font=UI,
                       wraplength=400, justify="left", anchor="w").grid(
            row=29, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 0))

        # ---------------- POST-RUN CHECKS ----------------
        tk.Label(b, text="Post-run checks", bg=BG, fg=ACCENT, font=UI_B).grid(
            row=30, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 2))
        self.audit_var = tk.BooleanVar(
            value=bool(cfg.get("post_run_audit", False)))
        tk.Checkbutton(b, text="Accuracy audit after processing: re-check "
                              "every renamed document with the AI (full "
                              "pages, second-model confirmed) and write "
                              "Filename_Audit_Report.csv in Reports > Audit "
                              "reports. Audit only - nothing is renamed. "
                              "Costs roughly as much as classifying the "
                              "folder a second time.",
                       variable=self.audit_var, bg=BG, fg=FG,
                       selectcolor=PANEL2, activebackground=BG,
                       activeforeground=FG, font=UI,
                       wraplength=400, justify="left", anchor="w").grid(
            row=31, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 0))

        # buttons
        review_settings = tk.Frame(b, bg=BG)
        review_settings.grid(row=32, column=0, columnspan=2, sticky="ew", padx=16, pady=(12, 0))
        tk.Label(review_settings, text="AI Document Review after the audit", bg=BG,
                 fg=ACCENT, font=UI_B).pack(anchor="w")
        tk.Label(review_settings, text="Choose model, account, effort and corrections before Start.\n"
                 "Changes to defaults affect the next run only.", bg=BG, fg=FG_DIM,
                 font=UI, justify="left").pack(anchor="w", pady=5)
        self.review_setup_btn = tk.Button(review_settings, text="Configure AI Document Review…",
                                         command=self._configure_review)
        style_button(self.review_setup_btn, PANEL2, BORDER)
        self.review_setup_btn.pack(anchor="w")
        self.cancel_queued_review_btn = tk.Button(review_settings, text="Cancel queued automatic review",
                                                  command=self.master._cancel_queued_automatic_review)
        style_button(self.cancel_queued_review_btn, PANEL2, BORDER)
        self.cancel_queued_review_btn.pack(anchor="w", pady=(6, 0))
        bframe = tk.Frame(b, bg=BG)
        bframe.grid(row=33, column=0, columnspan=2, sticky="e", padx=16, pady=(12, 16))
        cancel = tk.Button(bframe, text="Cancel", command=self._cancel)
        style_button(cancel, PANEL2, BORDER)
        cancel.pack(side="right", padx=(8, 0))
        save = tk.Button(bframe, text="Save", command=self._save)
        style_button(save, GREEN, GREEN_HI)
        save.pack(side="right")

        tk.Label(b, text=f"Settings saved to  {CONFIG_PATH}\n"
                         f"(the API key is NOT stored in this file)",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8), justify="left").grid(
            row=34, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 10))

    def _configure_review(self):
        dialog = self.master._configure_ai_review()
        if dialog is not None:
            self.wait_window(dialog)
        self.cfg["ai_workflows"] = copy.deepcopy(self.master.cfg.get("ai_workflows", {}))
        self.audit_var.set(bool(self.master.cfg.get("post_run_audit", False)))

    def _preview_palette(self, _event=None):
        value = self._palette_labels.get(self.palette_var.get(), "C")
        self.master._set_palette_globals(value)
        self.master.dashboard.apply_palette(value)

    def _cancel(self):
        value = self.master.cfg.get("ui_palette", "C")
        self.master._set_palette_globals(value)
        self.master.dashboard.apply_palette(value)
        self.destroy()

    def _render_model_choices(self):
        """(Re)build the model radio list, including Opus only when advanced is on."""
        for w in self.mframe.winfo_children():
            w.destroy()
        choices = dict(SAFE_MODELS)
        if self.advanced_var.get():
            choices.update(ADVANCED_MODELS)
        # if the saved model is now hidden, fall back to the default
        if self.model_var.get() not in choices:
            self.model_var.set(DEFAULT_MODEL)
        for name, meta in choices.items():
            tk.Radiobutton(
                self.mframe,
                text=f"{name}   (${meta['in']:.2f}/M in, ${meta['out']:.2f}/M out)",
                variable=self.model_var, value=name, bg=BG, fg=FG,
                selectcolor=PANEL2, activebackground=BG, activeforeground=FG,
                font=UI, anchor="w").pack(anchor="w")

    def _clear_key(self):
        if messagebox.askyesno("Clear API key",
                               "Remove the stored Anthropic API key?", parent=self):
            set_api_key("")
            self._has_existing_key = False
            self.key_hint.configure(text=f"Currently: {api_key_source_label()} (none)")
            messagebox.showinfo("Settings", "Stored key cleared.", parent=self)

    def _on_res(self, val):
        v = float(val)
        tag = "cheapest" if v <= 1.1 else ("balanced" if v <= 1.7 else
              ("sharp" if v <= 2.3 else "max"))
        self.res_label.configure(text=f"{v:.1f}x  ({tag})")

    def _toggle_show(self):
        self.key_entry.configure(show="" if self.show_var.get() else "*")

    def _save(self):
        # FX
        try:
            fx = float(self.fx_var.get())
            if fx <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Settings", "USD→GBP rate must be a positive number.",
                                 parent=self)
            return
        # numeric limits
        def _num(var, name, lo, integer=False):
            try:
                v = float(var.get())
                if v < lo:
                    raise ValueError
                return int(v) if integer else v
            except ValueError:
                messagebox.showerror("Settings",
                                     f"{name} must be a number ≥ {lo}.", parent=self)
                return None
        maxw = _num(self.maxw_var, "Max worker folders", 1, integer=True)
        maxf = _num(self.maxf_var, "Max files", 1, integer=True)
        maxb = _num(self.maxb_var, "Max spend", 0.0)
        maxmb = _num(self.maxmb_var, "Max file size", 0.1)
        secpf = _num(self.secpf_var, "Seconds per file", 0.1)
        orientation_conf = _num(
            self.orientation_conf_var, "Orientation confidence", 0.5)
        orientation_margin = _num(
            self.orientation_margin_var, "Orientation confidence margin", 0.0)
        if None in (maxw, maxf, maxb, maxmb, secpf,
                    orientation_conf, orientation_margin):
            return
        if orientation_conf > 0.999 or orientation_margin > 0.999:
            messagebox.showerror(
                "Settings", "Orientation confidence and margin must be "
                "decimal values below 1.0.", parent=self)
            return

        review_defaults = ai_workflows.auto_review_defaults(self.cfg)
        if review_defaults.get("enabled", True) and not bool(self.audit_var.get()):
            messagebox.showerror(
                "Accuracy Audit required",
                "Automatic AI Document Review can run only after a complete Accuracy Audit. "
                "Keep the post-run accuracy audit enabled, or disable automatic review in Configure AI Document Review.",
                parent=self)
            return

        chosen_model = self.model_var.get()
        # Opus guard: require an explicit extra confirmation before saving it.
        if chosen_model in ADVANCED_MODELS:
            if not messagebox.askyesno(
                    "Expensive model selected",
                    f"'{chosen_model}' is much more expensive than Haiku/Sonnet and "
                    f"is rarely more accurate for document classification.\n\n"
                    f"Are you sure you want to use it?", parent=self, icon="warning"):
                return

        # API key: only update if the user typed something; blank keeps existing.
        typed = self.key_var.get().strip()
        key_msg = ""
        if typed:
            where = set_api_key(typed)
            key_msg = f"\nAPI key stored in: {where}."
        elif not self._has_existing_key:
            # no existing key and none typed - allow saving other settings but warn
            key_msg = "\nNo API key set yet — add one before running."

        self.cfg["model"] = chosen_model
        self.cfg["advanced_models"] = bool(self.advanced_var.get())
        self.cfg["fx"] = fx
        self.cfg["resolution"] = max(1.0, min(3.0, float(self.res_var.get())))
        self.cfg["adaptive_pages"] = bool(self.adapt_var.get())
        self.cfg["skip_when_clear"] = bool(self.skip_var.get())
        self.cfg["auto_other"] = bool(self.auto_other_var.get())
        self.cfg["orientation_mode"] = self.orientation_mode_var.get()
        self.cfg["orientation_confidence"] = orientation_conf
        self.cfg["orientation_margin"] = orientation_margin
        self.cfg["redact_logs"] = bool(self.redact_var.get())
        self.cfg["move_mode"] = bool(self.move_var.get())
        self.cfg["convert_pdf"] = bool(self.convert_var.get())
        self.cfg["post_run_audit"] = bool(self.audit_var.get())
        self.cfg["ui_palette"] = self._palette_labels.get(self.palette_var.get(), "C")
        self.cfg["max_workers"] = maxw
        self.cfg["max_files"] = maxf
        self.cfg["max_budget_gbp"] = maxb
        self.cfg["max_file_mb"] = maxmb
        self.cfg["sec_per_file"] = secpf
        self.cfg.pop("api_key", None)  # belt-and-braces: never persist the key here

        if save_config(self.cfg):
            FX_RATE[0] = fx
            self.on_save(self.cfg)
            messagebox.showinfo("Settings", "Settings saved." + key_msg, parent=self)
            self.destroy()
        else:
            messagebox.showerror("Settings",
                                 "Could not save settings (check folder permissions).",
                                 parent=self)


# ====================================================================
# UNKNOWN-DOCUMENT DIALOG  (Relevant / Other)
# ====================================================================
# ====================================================================
# PROCESSING-REPORT REGISTRY + ARCHIVE
# ------------------------------------------------------------------
# Every audit report written by run_accuracy_audit is registered here so the
# ribbon's Reports browser can list them later regardless of which care-home
# folder they were written into. Archived reports move to a dedicated folder
# (per-care-home subfolders) under Documents\Lifted.
# Dedicated app-data reports location on C: (2026-07-22 suite modernisation —
# reports no longer live in Documents). The old Documents folder is migrated
# on startup and no longer written to.
LIFTED_REPORTS_ROOT = LIFTED_APPDATA_DIR / "Reports"
PROCESSING_REPORTS_ROOT = LIFTED_REPORTS_ROOT / "Processing Reports"
ARCHIVED_PROCESSING_REPORTS = (LIFTED_REPORTS_ROOT
                               / "Archived Processing Reports")


def processing_reports_dir(care_home: str) -> Path:
    """Per-care-home folder for NEW audit/processing reports, created on
    demand: %LOCALAPPDATA%\\Lifted\\Reports\\Processing Reports\\<care home>."""
    safe = re.sub(r'[<>:"/\\|?*]+', "_", (care_home or "Unknown")).strip() \
        or "Unknown"
    d = PROCESSING_REPORTS_ROOT / safe
    d.mkdir(parents=True, exist_ok=True)
    return d
_LEGACY_ARCHIVED_PROCESSING = (Path.home() / "Documents" / "Lifted"
                               / "Archived Processing Reports")


def migrate_legacy_processing_archive() -> int:
    """One-time move of the old Documents archive into the app-data root.
    Existing/locked files stay behind (still readable). Never raises."""
    moved = 0
    try:
        if not _LEGACY_ARCHIVED_PROCESSING.exists():
            return 0
        for item in list(_LEGACY_ARCHIVED_PROCESSING.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(_LEGACY_ARCHIVED_PROCESSING)
            dest = ARCHIVED_PROCESSING_REPORTS / rel
            if dest.exists():
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(dest))
                moved += 1
            except Exception:
                pass
        try:
            if not any(_LEGACY_ARCHIVED_PROCESSING.rglob("*")):
                shutil.rmtree(_LEGACY_ARCHIVED_PROCESSING, ignore_errors=True)
        except Exception:
            pass
    except Exception:
        pass
    return moved


def _processing_reports_log() -> Path:
    return APP_DIR / "processing_reports.json"


def record_processing_report(path, care_home=""):
    """Append one written report to the registry. Never raises (a registry
    failure must not fail the run that produced the report)."""
    try:
        log_p = _processing_reports_log()
        data = []
        if log_p.exists():
            data = json.loads(log_p.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                data = []
        data.append({"path": str(path),
                     "care_home": care_home or Path(path).parent.name,
                     "when": time.strftime("%Y-%m-%d %H:%M:%S")})
        log_p.parent.mkdir(parents=True, exist_ok=True)
        log_p.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception:
        pass


def load_processing_reports() -> list:
    """Registry entries whose file still exists, newest-first."""
    try:
        log_p = _processing_reports_log()
        if not log_p.exists():
            return []
        data = json.loads(log_p.read_text(encoding="utf-8"))
        rows = [(i, d) for i, d in enumerate(data)
                if isinstance(d, dict) and Path(d.get("path", "")).is_file()]
        # newest first; same-second entries tie-break on insertion order
        # (later in the file = written later).
        rows.sort(key=lambda t: (t[1].get("when", ""), t[0]), reverse=True)
        return [d for _i, d in rows]
    except Exception:
        return []


def archive_processing_report(path, care_home="") -> Path:
    """Move a report into 'Archived Processing Reports\\<care home>\\'
    (collision-safe). Raises on a real move failure so the dialog can say so."""
    src = Path(path)
    sub = re.sub(r'[<>:"/\\|?*]+', "_",
                 (care_home or src.parent.name or "Unknown")).strip() or "Unknown"
    dest_dir = ARCHIVED_PROCESSING_REPORTS / sub
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest = src.with_name(src.stem + " - Tables.csv")
    if src.suffix.lower() == ".csv" and manifest.exists():
        # Keep linked CSV tables together without renaming their references.
        group = dest_dir / src.stem
        n = 1
        while group.exists():
            n += 1
            group = dest_dir / f"{src.stem} (archived {n})"
        companions = [src, manifest]
        for suffix in (" - Summary.csv", " - Orientation.csv"):
            candidate = src.with_name(src.stem + suffix)
            if candidate.exists():
                companions.append(candidate)
        group.mkdir()
        moved = []
        try:
            for member in companions:
                target = group / member.name
                shutil.move(str(member), str(target))
                moved.append((member, target))
        except Exception:
            for original, target in reversed(moved):
                shutil.move(str(target), str(original))
            raise
        return group / src.name
    dest = dest_dir / src.name
    n = 1
    while dest.exists():
        n += 1
        dest = dest_dir / f"{src.stem} (archived {n}){src.suffix}"
    shutil.move(str(src), str(dest))
    return dest


# ====================================================================
# TOOLS PANEL  (closed by default; opened from the ribbon's Tools button)
# --------------------------------------------------------------------
# Quick access to the small stand-alone utilities used for one-off jobs
# around the processing workflow (converting stray files, rotating or
# splitting a PDF by hand, re-checking unknowns, opening the records).
# ====================================================================
def _first_existing(*paths):
    for p in paths:
        p = Path(p)
        if p.exists():
            return p
    return None


TOOLS_DIR = LIFTED_APPDATA_DIR / "Tools"


def tool_dirs():
    """Everywhere a helper tool may live, in priority order. The installers
    put the tools in the SHARED app-data folder (and a copy beside the exe),
    so a fresh install on ANY machine finds them — previously these were
    hard-coded to one developer's Desktop, which is why every Tools button
    read "(NOT FOUND)" on a colleague's PC. Legacy dev locations stay last."""
    dirs = []
    try:
        exe_dir = Path(sys.executable).resolve().parent if getattr(
            sys, "frozen", False) else Path(__file__).resolve().parent
        dirs += [exe_dir / "Tools", exe_dir]
    except Exception:
        pass
    dirs.append(TOOLS_DIR)                       # shared, installer-managed
    # legacy developer locations (this machine only)
    for d in (Path.home() / "OneDrive" / "Desktop", Path.home() / "Desktop"):
        dirs.append(d)
    dirs.append(Path.home() / "OneDrive" / "Desktop" / "Lifted"
                / "Lifted (to be sorted)")
    return dirs


def find_tool(*filenames):
    """Resolve a helper tool by FILENAME across every candidate folder (first
    hit wins, in tool_dirs() order). Accepts several names so a tool can be
    shipped as an .exe but still resolve to a .pyw in the dev checkout."""
    for d in tool_dirs():
        for name in filenames:
            try:
                p = d / name
                if p.exists():
                    return p
            except Exception:
                continue
    return None


# (label, description, filename candidates - resolved via find_tool)
EXTERNAL_TOOLS = [
    ("\U0001F4C4  Convert to PDF",
     "Convert images / Office files to PDF for processing",
     ["Convert to PDF.exe"]),
    ("\U0001F504  PDF Rotator",
     "Manually rotate the pages of a PDF and save it upright",
     ["PDF Rotator.exe"]),
    ("✂  PDF Splitter",
     "Split a multi-document scan into separate PDFs",
     ["PDF Splitter.exe"]),
    ("\U0001F916  AI Document Splitter",
     "Several documents on ONE page (stacked/side-by-side ID scans): "
     "AI detects each one, you review the boxes, it splits into named PDFs",
     ["AI Document Splitter.exe"]),
    ("\U0001F50E  Re-check Unknowns & Tidy",
     "Second pass over 'Other - Unknown' files in a processed folder",
     # shipped frozen for machines without Python; the .pyw is the dev copy
     ["Re-check Unknowns.exe", "Stage2_Recheck_Unknowns.pyw"]),
]


class ToolsDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Tools")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.after(0, lambda: style_titlebar_black(self))

        head = tk.Frame(self, bg=RIBBON)
        head.pack(fill="x")
        tk.Label(head, text="\U0001F9F0  Tools", bg=RIBBON, fg=RIBBON_FG,
                 font=("Segoe UI", 13, "bold")).pack(side="left",
                                                     padx=14, pady=10)
        tk.Label(head, text="one-off helpers for the processing workflow",
                 bg=RIBBON, fg=FG_DIM, font=("Segoe UI", 9)).pack(
            side="left", padx=(0, 14))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        for label, desc, candidates in EXTERNAL_TOOLS:
            target = find_tool(*candidates)
            row = tk.Frame(body, bg=PANEL)
            row.pack(fill="x", pady=3)
            b = tk.Button(row, text=label, width=26, anchor="w",
                          command=(lambda t=target: self._launch(t)))
            style_button(b, PANEL2, BORDER)
            b.pack(side="left", padx=10, pady=8)
            note = desc if target else f"{desc}   (NOT FOUND)"
            tk.Label(row, text=note, bg=PANEL,
                     fg=(FG_DIM if target else RED_HI),
                     font=("Segoe UI", 9), anchor="w", justify="left",
                     wraplength=330).pack(side="left", padx=(4, 10))
            if not target:
                b.configure(state="disabled")

        # quick-open links for the records this workflow maintains
        links = tk.Frame(body, bg=BG)
        links.pack(fill="x", pady=(10, 0))
        tk.Label(links, text="Records", bg=BG, fg=FG_DIM,
                 font=UI_B).pack(anchor="w")
        for text, path in (
                ("\U0001F4D6  Vocabulary workbook (Filename Identification Record)",
                 APP_DIR / "Filename Identification Record.xlsx"),
                ("\U0001F4CB  Misnaming Record",
                 APP_DIR / "Misnaming Record.xlsx")):
            lb = tk.Label(links, text=text, bg=BG, fg=ACCENT,
                          font=("Segoe UI", 9, "underline"), cursor="hand2",
                          anchor="w")
            lb.pack(anchor="w", pady=2)
            lb.bind("<Button-1>",
                    lambda e, p=path: self._launch(p))

        self.update_idletasks()
        try:
            self.geometry(f"+{master.winfo_rootx()+120}+{master.winfo_rooty()+80}")
        except Exception:
            pass

    def _launch(self, target):
        if not target or not Path(target).exists():
            messagebox.showwarning(
                "Tool not found",
                "That tool/file could not be found on this machine.",
                parent=self)
            return
        try:
            os.startfile(str(target))   # Windows: open with the default app
        except Exception as e:
            messagebox.showerror("Could not launch", str(e), parent=self)


class ReportsDialog(tk.Toplevel):
    """Browser for the processing reports this app writes (the post-run
    Filename_Audit_Report workbooks). Lists every registered report plus any
    found in the currently-selected care-home folder; open a report, open its
    folder, archive it (moved to 'Archived Processing Reports\\<care home>'
    under Documents\\Lifted), or browse the archive."""
    def __init__(self, master):
        super().__init__(master)
        self.title("Processing reports")
        self.configure(bg=BG)
        self.transient(master)
        self.after(0, lambda: style_titlebar_black(self))
        self.geometry("760x420")
        self.minsize(620, 320)
        self._master = master
        self._rows = []          # [(Path, care_home)]

        head = tk.Frame(self, bg=RIBBON)
        head.pack(fill="x")
        tk.Label(head, text="\U0001F4D1  Processing reports", bg=RIBBON,
                 fg=RIBBON_FG, font=("Segoe UI", 13, "bold")).pack(
            side="left", padx=14, pady=10)
        tk.Label(head, text="audit reports from previous runs — double-click "
                            "to open", bg=RIBBON, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 14))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(12, 6))
        self.lb = tk.Listbox(body, bg=PANEL, fg=FG, bd=0,
                             highlightthickness=1,
                             highlightbackground=BORDER,
                             selectbackground=ACCENT,
                             selectforeground="#ffffff",
                             font=("Consolas", 10), activestyle="none")
        sb = tk.Scrollbar(body, command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.lb.bind("<Double-1>", lambda e: self._open_report())

        self.status = tk.Label(self, text="", bg=BG, fg=FG_DIM,
                               font=("Segoe UI", 9), anchor="w")
        self.status.pack(fill="x", padx=14)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=14, pady=(4, 12))
        for text, cmd, side in (
                ("Open report", self._open_report, "right"),
                ("Open folder", self._open_folder, "right"),
                ("Archive", self._archive, "right"),
                ("Browse archive", self._browse_archive, "left"),
                ("Refresh", self.refresh, "left")):
            b = tk.Button(bar, text=text, command=cmd)
            style_button(b, PANEL2, BORDER)
            b.pack(side=side, padx=4)

        self.refresh()

    # ---- data -------------------------------------------------------- #
    def refresh(self):
        self.lb.delete(0, "end")
        self._rows = []
        seen = set()
        entries = []          # (mtime, Path, care_home)
        for d in load_processing_reports():
            p = Path(d["path"])
            key = str(p.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                entries.append((p.stat().st_mtime, p,
                                d.get("care_home") or p.parent.name))
            except Exception:
                continue
        # every report in the central C: tree (covers pre-registry files that
        # were migrated in), organised per care home
        try:
            if PROCESSING_REPORTS_ROOT.exists():
                for sub in PROCESSING_REPORTS_ROOT.iterdir():
                    if not sub.is_dir():
                        continue
                    for p in sub.iterdir():
                        if p.suffix.lower() not in {".xlsx", ".csv"}:
                            continue
                        if p.stem.endswith((" - Summary", " - Orientation", " - Tables")):
                            continue
                        key = str(p.resolve()).lower()
                        if key in seen or p.name.startswith("~$"):
                            continue
                        seen.add(key)
                        entries.append((p.stat().st_mtime, p, sub.name))
        except Exception:
            pass
        # also pick up unregistered LEGACY reports still sitting in the
        # currently-selected care-home folder
        folder = getattr(self._master, "care_home_dir", None)
        if folder and Path(folder).is_dir():
            try:
                for p in Path(folder).rglob(AUDIT_REPORT_STEM + "*.xlsx"):
                    key = str(p.resolve()).lower()
                    if key in seen or p.name.startswith("~$"):
                        continue
                    seen.add(key)
                    entries.append((p.stat().st_mtime, p, Path(folder).name))
            except Exception:
                pass
        entries.sort(key=lambda t: t[0], reverse=True)
        for mt, p, care in entries:
            when = time.strftime("%d/%m/%y %H:%M", time.localtime(mt))
            self.lb.insert("end", f" {when}   {care:<28.28}  {p.name}")
            self._rows.append((p, care))
        self.status.configure(
            text=f"{len(entries)} report(s).  Archive folder: "
                 f"{ARCHIVED_PROCESSING_REPORTS}")

    def _selected(self):
        sel = self.lb.curselection()
        if not sel:
            messagebox.showinfo("Processing reports",
                                "Select a report first.", parent=self)
            return None, ""
        return self._rows[sel[0]]

    # ---- actions ------------------------------------------------------ #
    def _open_report(self):
        p, _care = self._selected()
        if not p:
            return
        try:
            os.startfile(str(p))
        except Exception as e:
            messagebox.showerror("Could not open report", str(e), parent=self)

    def _open_folder(self):
        p, _care = self._selected()
        if not p:
            return
        try:
            subprocess.Popen(["explorer", "/select,", str(p)])
        except Exception as e:
            messagebox.showerror("Could not open folder", str(e), parent=self)

    def _archive(self):
        p, care = self._selected()
        if not p:
            return
        if not messagebox.askyesno(
                "Archive report",
                f"Move this report to the archive?\n\n{p.name}\n\n"
                f"It will move to:\n{ARCHIVED_PROCESSING_REPORTS / care}",
                parent=self):
            return
        try:
            dest = archive_processing_report(p, care)
        except Exception as e:
            messagebox.showerror("Archive failed",
                                 f"Could not move the report:\n{e}",
                                 parent=self)
            return
        self.refresh()
        self.status.configure(text=f"Archived -> {dest}")

    def _browse_archive(self):
        ARCHIVED_PROCESSING_REPORTS.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(ARCHIVED_PROCESSING_REPORTS))
        except Exception as e:
            messagebox.showerror("Could not open folder", str(e), parent=self)


class JobsDialog(tk.Toplevel):
    """Processing dashboard: live-run progress (from TRACKER, self-refreshing)
    plus the Anthropic batch queue for the selected care-home folder (real
    processing_status / request_counts fetched on demand). Observability
    only — resuming/applying uses the existing Start / batch buttons."""
    def __init__(self, master):
        super().__init__(master)
        self.title("Processing jobs")
        self.configure(bg=BG)
        self.transient(master)
        self.after(0, lambda: style_titlebar_black(self))
        self.geometry("720x480")
        self.minsize(600, 380)
        self._master = master
        self._alive = True
        self.protocol("WM_DELETE_WINDOW", self._close)

        head = tk.Frame(self, bg=RIBBON)
        head.pack(fill="x")
        tk.Label(head, text="\U0001F4C8  Processing jobs", bg=RIBBON,
                 fg=RIBBON_FG, font=("Segoe UI", 13, "bold")).pack(
            side="left", padx=14, pady=10)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        # ---- live run ----
        tk.Label(body, text="LIVE RUN", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        live = tk.Frame(body, bg=PANEL)
        live.pack(fill="x", pady=(4, 12))
        self.live_lbl = tk.Label(live, text="", bg=PANEL, fg=FG,
                                 font=("Consolas", 10), justify="left",
                                 anchor="w")
        self.live_lbl.pack(fill="x", padx=12, pady=10)

        # ---- batch queue ----
        tk.Label(body, text="BATCH QUEUE (Anthropic)", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        batch = tk.Frame(body, bg=PANEL)
        batch.pack(fill="both", expand=True, pady=(4, 8))
        self.batch_txt = tk.Text(batch, bg=PANEL, fg=FG, bd=0, height=9,
                                 font=("Consolas", 9), wrap="word",
                                 state="disabled")
        self.batch_txt.pack(fill="both", expand=True, padx=12, pady=10)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=14, pady=(0, 12))
        b = tk.Button(bar, text="Refresh batch status",
                      command=self._refresh_batches)
        style_button(b, PANEL2, BORDER)
        b.pack(side="left")
        self.bar_note = tk.Label(bar, text="", bg=BG, fg=FG_DIM,
                                 font=("Segoe UI", 9))
        self.bar_note.pack(side="left", padx=10)
        c = tk.Button(bar, text="Close", command=self._close)
        style_button(c, PANEL2, BORDER)
        c.pack(side="right")

        self._render_batches_from_state()
        self._tick()

    def _close(self):
        self._alive = False
        self.destroy()

    # ---- live section: refreshes itself every second ------------------- #
    def _tick(self):
        if not self._alive or not self.winfo_exists():
            return
        s = TRACKER.snapshot()
        if not s["mode"]:
            txt = "No run in this session yet.\nPick a folder and press Start."
        else:
            done, total = s["workers_done"], s["workers_total"]
            pct = (100.0 * done / total) if total else 0.0
            eta = TRACKER.eta_seconds()
            eta_txt = (f"{int(eta // 60)}m {int(eta % 60)}s remaining"
                       if eta is not None else "—")
            st = s.get("stats") or {}
            txt = (f"Care home : {s['care_home']}\n"
                   f"Status    : {s['status'] or 'running'}"
                   f"{'  (finished)' if s['finished'] else ''}\n"
                   f"Workers   : {done} / {total}   ({pct:.0f}%)\n"
                   f"Current   : {s['current_worker'] or '—'}\n"
                   f"Renamed   : {st.get('renamed', 0)}    "
                   f"Errors: {st.get('errors', 0)}    "
                   f"Skipped(cached): {st.get('skipped_cached', 0)}\n"
                   f"ETA       : {eta_txt}")
        self.live_lbl.configure(text=txt)
        self.after(1000, self._tick)

    # ---- batch section -------------------------------------------------- #
    def _set_batch_text(self, text):
        try:
            self.batch_txt.configure(state="normal")
            self.batch_txt.delete("1.0", "end")
            self.batch_txt.insert("end", text)
            self.batch_txt.configure(state="disabled")
        except Exception:
            pass

    def _render_batches_from_state(self):
        folder = getattr(self._master, "care_home_dir", None)
        if not folder:
            self._set_batch_text("No care-home folder selected.")
            return None
        state = BatchState(Path(folder))
        if not state.data or not state.data.get("batches"):
            self._set_batch_text("No batch submission recorded for this "
                                 "folder.")
            return None
        lines = [f"Submitted : {state.data.get('submitted_ts', '?')}   "
                 f"applied: {state.data.get('applied', False)}",
                 f"Requests  : {len(state.data.get('requests', {}))} "
                 f"document(s) across "
                 f"{len(state.data.get('batches', []))} batch(es)", ""]
        for b in state.data.get("batches", []):
            lines.append(f"  {b.get('id', '?')}   "
                         f"last known: {b.get('status_at_submit', '?')}")
        lines.append("")
        lines.append("Press 'Refresh batch status' for live Anthropic status.")
        self._set_batch_text("\n".join(lines))
        return state

    def _refresh_batches(self):
        folder = getattr(self._master, "care_home_dir", None)
        if not folder:
            self.bar_note.configure(text="Pick a care-home folder first.")
            return
        state = BatchState(Path(folder))
        ids = [b.get("id") for b in state.data.get("batches", []) if b.get("id")]
        if not ids:
            self.bar_note.configure(text="No batches recorded for this folder.")
            return
        key = get_api_key()
        if not key:
            self.bar_note.configure(text="No API key configured (Settings).")
            return
        self.bar_note.configure(text="Fetching…")

        def work():
            api = ClaudeAPI(key, DEFAULT_MODEL)
            rows, tot = [], {"succeeded": 0, "errored": 0, "processing": 0,
                             "canceled": 0, "expired": 0}
            pending = 0
            for bid in ids:
                try:
                    b = api.get_batch(bid)
                    st = b.get("processing_status", "?")
                    rc = b.get("request_counts", {}) or {}
                    for k in tot:
                        tot[k] += int(rc.get(k, 0) or 0)
                    if st != "ended":
                        pending += 1
                    rows.append(f"  {bid}\n    {st}   " + "  ".join(
                        f"{k}={rc.get(k, 0)}" for k in
                        ("processing", "succeeded", "errored")))
                except Exception as e:
                    rows.append(f"  {bid}\n    (status fetch failed: {e})")
            n_req = sum(tot.values())
            done_pct = (100.0 * tot["succeeded"] / n_req) if n_req else 0.0
            summary = (f"Queue: {pending} batch(es) still processing of "
                       f"{len(ids)}.\n"
                       f"Documents: {tot['succeeded']} done, "
                       f"{tot['errored']} failed, "
                       f"{tot['processing']} in flight "
                       f"({done_pct:.0f}% complete).\n")
            if pending == 0 and not state.data.get("applied"):
                summary += ("\nAll batches ENDED and not yet applied — use "
                            "'⏳ Check batch status' on the main screen to "
                            "apply the results.\n")
            text = summary + "\n" + "\n".join(rows)

            def show():
                if self._alive and self.winfo_exists():
                    self._set_batch_text(text)
                    self.bar_note.configure(text="Live status fetched.")
            try:
                self.after(0, show)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()


class UnknownDialog(tk.Toplevel):
    """Blocking dialog shown when the API can't match a document.
    Returns (decision, name, description) or None (=> stop)."""
    def __init__(self, master, filename, guess, features, b64img):
        super().__init__(master)
        self.result = None
        self.title("Define this document")
        self.configure(bg=BG)
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._stop)

        left = tk.Frame(self, bg=BG)
        left.pack(side="left", padx=16, pady=16, fill="y")
        self._img_ref = None
        if b64img and HAS_PIL:
            try:
                from io import BytesIO
                im = Image.open(BytesIO(base64.b64decode(b64img)))
                im.thumbnail((360, 480))
                self._img_ref = ImageTk.PhotoImage(im)
                tk.Label(left, image=self._img_ref, bg=BG).pack()
            except Exception:
                tk.Label(left, text="(preview unavailable)", bg=BG, fg=FG_DIM).pack()
        else:
            tk.Label(left, text="(no preview)", bg=BG, fg=FG_DIM,
                     width=30, height=20).pack()

        right = tk.Frame(self, bg=BG)
        right.pack(side="left", padx=(0, 16), pady=16, fill="both", expand=True)

        tk.Label(right, text="Unrecognised document", bg=BG, fg=AMBER_HI,
                 font=UI_H).pack(anchor="w")
        tk.Label(right, text=f"File:  {filename}", bg=BG, fg=FG, font=UI,
                 wraplength=380, justify="left").pack(anchor="w", pady=(6, 2))
        if guess:
            tk.Label(right, text=f"AI's best guess:  {guess}", bg=BG, fg=FG_DIM,
                     font=UI, wraplength=380, justify="left").pack(anchor="w", pady=(0, 8))

        tk.Label(right, text="Filename to use:", bg=BG, fg=FG_DIM, font=UI).pack(
            anchor="w", pady=(8, 2))
        self.name_var = tk.StringVar(value=guess if guess else "")
        tk.Entry(right, textvariable=self.name_var, width=40, bg=PANEL2, fg=FG,
                 insertbackground=FG, relief="flat", font=MONO).pack(anchor="w")

        tk.Label(right, text="Identifying features (saved to the record - edit if needed):",
                 bg=BG, fg=FG_DIM, font=UI).pack(anchor="w", pady=(10, 2))
        self.desc_text = tk.Text(right, width=42, height=4, bg=PANEL2, fg=FG,
                                 insertbackground=FG, relief="flat", font=UI,
                                 wrap="word")
        self.desc_text.pack(anchor="w")
        # pre-fill with the AI's observed features so the record gets richer
        if features:
            self.desc_text.insert("1.0", features)

        tk.Label(right,
                 text="Relevant  → added to the Important tab of the record.\n"
                      "Other     → added to the Other tab; file is named \"Other\".\n"
                      "The features above are stored so the AI recognises this "
                      "next time.",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8), justify="left").pack(
            anchor="w", pady=(10, 6))

        bframe = tk.Frame(right, bg=BG)
        bframe.pack(anchor="w", pady=(6, 0))
        rel = tk.Button(bframe, text="Relevant", command=lambda: self._choose("Relevant"))
        style_button(rel, GREEN, GREEN_HI)
        rel.pack(side="left", padx=(0, 8))
        oth = tk.Button(bframe, text="Other", command=lambda: self._choose("Other"))
        style_button(oth, AMBER, AMBER_HI)
        oth.pack(side="left", padx=(0, 8))
        stop = tk.Button(bframe, text="Stop run", command=self._stop)
        style_button(stop, RED, RED_HI)
        stop.pack(side="left")

        self.update_idletasks()
        self._center(master)

    def _center(self, master):
        try:
            self.geometry(f"+{master.winfo_rootx()+80}+{master.winfo_rooty()+60}")
        except Exception:
            pass

    # Longest name the dialog will accept. Names become both FILENAMES and
    # controlled-vocabulary entries; in the past whole descriptions were
    # pasted into this field by mistake, polluting the record with
    # paragraph-length "names" (which also bloated every API prompt).
    MAX_NAME_LEN = 80

    def _choose(self, decision):
        name = " ".join(self.name_var.get().split())
        if decision == "Relevant" and not name:
            messagebox.showerror("Define document",
                                 "Please enter a filename for this document.",
                                 parent=self)
            return
        if len(name) > self.MAX_NAME_LEN:
            messagebox.showerror(
                "Define document",
                f"That name is {len(name)} characters long - it looks like a "
                f"description, not a name.\n\n"
                f"This field becomes the FILENAME (and the vocabulary entry), "
                f"so keep it under {self.MAX_NAME_LEN} characters, e.g. "
                f"'Loan Agreement'. Put the detail in the identifying-"
                f"features box below instead.",
                parent=self)
            return
        if not name:
            name = "Other"
        desc = self.desc_text.get("1.0", "end").strip()
        self.result = (decision, name, desc)
        self.destroy()

    def _stop(self):
        self.result = None
        self.destroy()


# ====================================================================
# CONFIRM-REVIEW DIALOG  (pre-flight overview + skipped-file lists)
# --------------------------------------------------------------------
# Replaces the plain messagebox so we can show the run overview AND one or more
# scrollable, copyable lists of files that will be SKIPPED (oversized files and
# cloud-only files), each with their full paths. Each list has its own "Copy
# paths" button; if there's more than one list, a "Copy ALL paths" button copies
# everything together.
# ====================================================================
class ConfirmReviewDialog(tk.Toplevel):
    def __init__(self, master, overview_text: str, sections, warn: bool,
                 mode_choice: str = None, batch_blocked: str = ""):
        """sections: list of (header_label:str, colour:str, paths:list[Path]).
        Empty path lists are ignored.
        mode_choice : when given ("live"/"batch"), show the processing-mode
                      radio buttons pre-selected to this value; the user's pick
                      is exposed as self.mode after the dialog closes.
        batch_blocked : when non-empty, the Overnight Batch option is disabled
                      and this text explains why (e.g. a batch is pending)."""
        super().__init__(master)
        self.result = False
        self.mode = mode_choice or "live"
        self.title("Confirm review")
        self.configure(bg=BG)
        self.transient(master)
        self.grab_set()
        self.resizable(False, True)
        _ui = float(getattr(master, "_ui_scale", 1.0) or 1.0)
        self.minsize(int(560*_ui), int(360*_ui))

        pad = {"padx": 16}
        sections = [(lbl, col, list(paths)) for (lbl, col, paths) in sections if paths]
        # combined text used by the "Copy ALL" button (and the test harness)
        self._all_text = "\n".join(
            "\n".join(str(p) for p in paths) for _lbl, _col, paths in sections)

        tk.Label(self, text=("\u26A0  Confirm review" if warn else "Confirm review"),
                 bg=BG, fg=(AMBER if warn else FG), font=UI_H).pack(
            anchor="w", padx=16, pady=(14, 6))

        # overview block (monospace so the aligned ' : ' columns line up)
        ov = tk.Text(self, bg=PANEL2, fg=FG, font=MONO, relief="flat",
                     wrap="word", height=overview_text.count("\n") + 1,
                     padx=12, pady=10)
        ov.pack(fill="x", **pad)
        ov.insert("1.0", overview_text)
        ov.configure(state="disabled")

        # ---- processing-mode selection (Live vs Overnight Batch) ----
        self._mode_var = None
        if mode_choice is not None:
            mframe = tk.Frame(self, bg=PANEL)
            mframe.pack(fill="x", padx=16, pady=(10, 0))
            tk.Label(mframe, text="Processing mode", bg=PANEL, fg=ACCENT,
                     font=UI_B).pack(anchor="w", padx=12, pady=(8, 2))
            self._mode_var = tk.StringVar(value=self.mode)
            tk.Radiobutton(
                mframe,
                text="Live mode - classify now, one document at a time "
                     "(app must stay open and online)",
                variable=self._mode_var, value="live", bg=PANEL, fg=FG,
                selectcolor=PANEL2, activebackground=PANEL, activeforeground=FG,
                font=UI, anchor="w", wraplength=520,
                justify="left").pack(anchor="w", padx=18)
            b_state = "disabled" if batch_blocked else "normal"
            tk.Radiobutton(
                mframe,
                text="Overnight Batch mode - submit everything now at 50% of "
                     "the price; results are ready within ~1 hour (up to 24h). "
                     "You can close the app after submitting. Unknown documents "
                     "are auto-filed as 'Other - Unknown' for later review; the "
                     "second pass (dating/ranking) runs LIVE at standard price "
                     "when results are applied.",
                variable=self._mode_var, value="batch", bg=PANEL,
                fg=(FG_DIM if batch_blocked else FG), selectcolor=PANEL2,
                activebackground=PANEL, activeforeground=FG, font=UI,
                anchor="w", wraplength=520, justify="left",
                state=b_state).pack(anchor="w", padx=18, pady=(4, 2))
            if batch_blocked:
                self._mode_var.set("live")
                tk.Label(mframe, text=f"⚠ {batch_blocked}", bg=PANEL,
                         fg=AMBER, font=("Segoe UI", 8), wraplength=520,
                         justify="left").pack(anchor="w", padx=18, pady=(0, 8))
            else:
                tk.Label(mframe, text="", bg=PANEL).pack(pady=(0, 4))

        self._copy_status = None
        if sections:
            # a small toolbar with a "Copy ALL paths" button when >1 section
            if len(sections) > 1:
                bar = tk.Frame(self, bg=BG)
                bar.pack(fill="x", padx=16, pady=(10, 0))
                tk.Label(bar, text="Skipped files (not sent):", bg=BG, fg=FG,
                         font=UI_B).pack(side="left")
                allbtn = tk.Button(bar, text="\U0001F4CB  Copy ALL paths",
                                   command=self._copy_all)
                style_button(allbtn, PANEL2, BORDER)
                allbtn.pack(side="right")

            # each section: header (+ its own copy button) and a scrollable box
            for lbl, col, paths in sections:
                text = "\n".join(str(p) for p in paths)
                header = tk.Frame(self, bg=BG)
                header.pack(fill="x", padx=16, pady=(10, 2))
                tk.Label(header, text=lbl, bg=BG, fg=col, font=UI_B,
                         wraplength=440, justify="left").pack(side="left")
                cbtn = tk.Button(header, text="\U0001F4CB  Copy",
                                 command=lambda t=text: self._copy_text(t))
                style_button(cbtn, PANEL2, BORDER)
                cbtn.pack(side="right")

                listwrap = tk.Frame(self, bg=BG)
                listwrap.pack(fill="both", expand=True, padx=16, pady=(0, 2))
                box = tk.Text(listwrap, bg="#07090c", fg=FG_DIM,
                              font=("Consolas", 9), relief="flat", wrap="none",
                              height=6, insertbackground=FG)
                box.pack(side="left", fill="both", expand=True)
                ysb = ttk.Scrollbar(listwrap, command=box.yview)
                ysb.pack(side="right", fill="y")
                box.configure(yscrollcommand=ysb.set)
                box.insert("1.0", text)
                box.configure(state="disabled")

            self._copy_status = tk.Label(self, text="", bg=BG, fg=GREEN_HI,
                                         font=("Segoe UI", 8), anchor="w")
            self._copy_status.pack(fill="x", padx=16)

        # ---- prompt + buttons ----
        tk.Label(self, text="Send these documents to the Anthropic API?",
                 bg=BG, fg=FG, font=UI).pack(anchor="w", padx=16, pady=(10, 4))

        bframe = tk.Frame(self, bg=BG)
        bframe.pack(anchor="e", padx=16, pady=(4, 16))
        no = tk.Button(bframe, text="Cancel", command=self._cancel)
        style_button(no, PANEL2, BORDER)
        no.pack(side="right", padx=(8, 0))
        yes = tk.Button(bframe, text="Start review", command=self._confirm)
        style_button(yes, GREEN, GREEN_HI)
        yes.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda e: self._cancel())
        try:
            self.update_idletasks()
            self.focus_force()
        except Exception:
            pass

    def _copy_text(self, text):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            if self._copy_status:
                self._copy_status.configure(text="Copied file paths to clipboard.")
        except Exception as e:
            if self._copy_status:
                self._copy_status.configure(text=f"Copy failed: {e}")

    def _copy_all(self):
        self._copy_text(self._all_text)

    def _confirm(self):
        self.result = True
        if self._mode_var is not None:
            self.mode = self._mode_var.get()
        self.destroy()

    def _cancel(self):
        self.result = False
        self.destroy()


# ====================================================================
# DEVELOPER CONSOLE  (Ctrl+Shift+I)
# --------------------------------------------------------------------
# A diagnostics window that captures everything the app prints to stdout/stderr
# (including traceback.print_exc() and the standard `logging` module) plus a
# mirror of the activity-log lines, with timestamps. Useful for diagnosing the
# "keyring not installed" / wrong-interpreter situations and any API/file
# errors without having to launch the app from a terminal.
#
# Nothing sensitive is added here that the app didn't already emit; in
# particular the API key is never printed. If you have "Redact filenames in
# logs" enabled, the mirrored activity-log lines are already redacted.
# ====================================================================
CONSOLE_BUFFER_MAX = 5000   # keep at most this many lines in memory


class ConsoleBuffer:
    """Thread-safe ring buffer of console lines. Writers (any thread) append;
    the DevConsole drains new lines on the Tk thread via a periodic poll."""
    def __init__(self, maxlen=CONSOLE_BUFFER_MAX):
        self._lines = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0            # monotonically increasing line counter

    def add(self, text: str, level: str = "log"):
        if text is None:
            return
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with self._lock:
            for line in str(text).splitlines() or [""]:
                self._seq += 1
                self._lines.append((self._seq, ts, level, line))

    def since(self, seq: int):
        """Return (new_lines, last_seq) for everything after `seq`."""
        with self._lock:
            new = [t for t in self._lines if t[0] > seq]
            last = self._lines[-1][0] if self._lines else seq
        return new, last

    def snapshot_text(self) -> str:
        with self._lock:
            return "\n".join(f"[{ts}] {line}" for _, ts, _lvl, line in self._lines)

    def clear(self):
        with self._lock:
            self._lines.clear()
            # keep _seq increasing so the console's cursor stays valid


# Single global buffer, created before the GUI so early start-up output (and
# import-time messages) are captured.
CONSOLE = ConsoleBuffer()


class _StreamTee:
    """File-like wrapper that forwards writes to the original stream AND mirrors
    them into the global CONSOLE buffer. Installed over sys.stdout/sys.stderr."""
    def __init__(self, original, level: str):
        self._orig = original
        self._level = level

    def write(self, s):
        try:
            if self._orig is not None:
                self._orig.write(s)
        except Exception:
            pass
        try:
            if s and s.strip():
                CONSOLE.add(s.rstrip("\n"), self._level)
        except Exception:
            pass
        return len(s) if s else 0

    def flush(self):
        try:
            if self._orig is not None:
                self._orig.flush()
        except Exception:
            pass

    # some libraries probe these
    def isatty(self):
        try:
            return bool(self._orig and self._orig.isatty())
        except Exception:
            return False

    def fileno(self):
        if self._orig is None or not hasattr(self._orig, "fileno"):
            raise io.UnsupportedOperation("fileno")
        return self._orig.fileno()


class _ConsoleLogHandler(logging.Handler):
    """Routes standard `logging` records into the console buffer too."""
    def emit(self, record):
        try:
            CONSOLE.add(self.format(record), level=record.levelname.lower())
        except Exception:
            pass


def install_console_capture():
    """Tee stdout/stderr and attach a logging handler. Safe to call once."""
    if getattr(install_console_capture, "_done", False):
        return
    # Note: when launched as a .pyw on Windows there is no real console, so
    # sys.stdout/stderr may be None - the tee handles that gracefully.
    sys.stdout = _StreamTee(sys.stdout, "out")
    sys.stderr = _StreamTee(sys.stderr, "err")
    handler = _ConsoleLogHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    if root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    root.addHandler(handler)
    install_console_capture._done = True
    CONSOLE.add(f"Console capture started — {APP_NAME} on "
                f"{platform.system()} {platform.release()}, "
                f"Python {platform.python_version()}", "info")


class DevConsole(tk.Toplevel):
    """The diagnostics window. Created lazily on first Ctrl+Shift+I, then
    re-shown/hidden on subsequent presses. Drains the global CONSOLE buffer."""
    POLL_MS = 250

    LEVEL_COLOURS = {
        "err":     "#ff6b6b",
        "error":   "#ff6b6b",
        "critical":"#ff6b6b",
        "warning": "#f0b429",
        "warn":    "#f0b429",
        "info":    "#4da3ff",
        "out":     "#e8eaed",
        "log":     "#9aa4b0",
        "debug":   "#6b7785",
    }

    def __init__(self, master):
        super().__init__(master)
        self.master_app = master
        self.title("Developer Console — Doc Review (AI) Station")
        self.configure(bg=BG)
        _ui = float(getattr(master, "_ui_scale", 1.0) or 1.0)
        self.geometry(f"{int(900*_ui)}x{int(460*_ui)}")
        self.minsize(int(560*_ui), int(280*_ui))
        self._cursor = 0          # last buffer seq we've displayed
        self._autoscroll = True
        self._poll_id = None

        # ---- top bar ----
        bar = tk.Frame(self, bg=PANEL)
        bar.pack(side="top", fill="x")
        tk.Label(bar, text="\u2328  Console", bg=PANEL, fg=FG,
                 font=("Segoe UI", 12, "bold")).pack(side="left", padx=12, pady=8)

        for text, cmd, base, hi in [
                ("Copy all", self._copy_all, PANEL2, BORDER),
                ("Save to file…", self._save, PANEL2, BORDER),
                ("Clear", self._clear, PANEL2, BORDER)]:
            b = tk.Button(bar, text=text, command=cmd)
            style_button(b, base, hi)
            b.pack(side="right", padx=(0, 8), pady=6)

        self.autoscroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="Auto-scroll", variable=self.autoscroll_var,
                       command=self._toggle_autoscroll, bg=PANEL, fg=FG_DIM,
                       selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=FG, font=("Segoe UI", 9)).pack(
            side="right", padx=(0, 12))

        # ---- text area ----
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(side="top", fill="both", expand=True)
        self.text = tk.Text(wrap, bg="#07090c", fg=FG, font=("Consolas", 9),
                            relief="flat", wrap="none", state="disabled",
                            insertbackground=FG)
        self.text.pack(side="left", fill="both", expand=True)
        ysb = ttk.Scrollbar(wrap, command=self.text.yview)
        ysb.pack(side="right", fill="y")
        xsb = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        xsb.pack(side="bottom", fill="x")
        self.text.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)

        # colour tags per level
        for lvl, col in self.LEVEL_COLOURS.items():
            self.text.tag_configure(lvl, foreground=col)
        self.text.tag_configure("ts", foreground="#56606b")

        # status line
        self.status = tk.Label(self, text="", bg=PANEL, fg=FG_DIM,
                               font=("Segoe UI", 8), anchor="w")
        self.status.pack(side="bottom", fill="x")

        # close button hides instead of destroying, so history is kept
        self.protocol("WM_DELETE_WINDOW", self.hide)
        # let Ctrl+Shift+I / F12 toggle from within the console too
        self.bind("<Control-Shift-KeyPress-I>", lambda e: self.hide())
        self.bind("<Control-Shift-KeyPress-i>", lambda e: self.hide())
        self.bind("<F12>", lambda e: self.hide())
        self.bind("<Escape>", lambda e: self.hide())

        self._render_all()
        self._schedule_poll()

    # ---- drain / render ----
    def _schedule_poll(self):
        self._poll_id = self.after(self.POLL_MS, self._poll)

    def _poll(self):
        self._drain()
        self._schedule_poll()

    def _render_all(self):
        self._cursor = 0
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._drain()

    def _drain(self):
        new, last = CONSOLE.since(self._cursor)
        if not new:
            return
        self.text.configure(state="normal")
        for _seq, ts, level, line in new:
            tag = level if level in self.LEVEL_COLOURS else "log"
            self.text.insert("end", f"[{ts}] ", ("ts",))
            self.text.insert("end", line + "\n", (tag,))
        self._cursor = last
        self.text.configure(state="disabled")
        if self.autoscroll_var.get():
            self.text.see("end")
        # trim displayed widget if it grows huge (buffer is already capped)
        try:
            total = int(self.text.index("end-1c").split(".")[0])
            if total > CONSOLE_BUFFER_MAX + 200:
                self.text.configure(state="normal")
                self.text.delete("1.0", f"{total - CONSOLE_BUFFER_MAX}.0")
                self.text.configure(state="disabled")
        except Exception:
            pass
        self.status.configure(text=f"{last} lines captured")

    # ---- buttons ----
    def _toggle_autoscroll(self):
        if self.autoscroll_var.get():
            self.text.see("end")

    def _copy_all(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(CONSOLE.snapshot_text())
            self.status.configure(text="Copied console to clipboard.")
        except Exception as e:
            self.status.configure(text=f"Copy failed: {e}")

    def _save(self):
        try:
            default = (APP_DIR / f"console_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
            path = filedialog.asksaveasfilename(
                parent=self, title="Save console log",
                defaultextension=".log",
                initialdir=str(APP_DIR), initialfile=default.name,
                filetypes=[("Log files", "*.log"), ("Text files", "*.txt"),
                           ("All files", "*.*")])
            if not path:
                return
            Path(path).write_text(CONSOLE.snapshot_text(), encoding="utf-8")
            self.status.configure(text=f"Saved to {path}")
        except Exception as e:
            self.status.configure(text=f"Save failed: {e}")

    def _clear(self):
        CONSOLE.clear()
        self._cursor = CONSOLE.since(0)[1]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.status.configure(text="Console cleared.")

    # ---- show / hide ----
    def show(self):
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
        except Exception:
            pass
        self._drain()

    def hide(self):
        self.withdraw()


# ====================================================================
# MAIN APPLICATION
# ====================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        # capture stdout/stderr/logging into the dev-console buffer as early as
        # possible so start-up diagnostics are not lost
        install_console_capture()
        ensure_app_dir()
        # one-time migration of archived processing reports out of Documents
        # into %LOCALAPPDATA%\Lifted\Reports (no-op once done)
        try:
            migrate_legacy_processing_archive()
        except Exception:
            pass
        self.cfg = load_config()
        self._set_palette_globals(self.cfg.get("ui_palette"))
        FX_RATE[0] = self.cfg.get("fx", 0.79)
        self.kb = KnowledgeBase()

        self.title(f"Stage 2 — Processing  v{APP_VERSION} | {APP_BUILD}")
        self.configure(bg=BG)
        ui = 1.0
        if _api_usage is not None:
            try:
                ui = _api_usage.tk_scale(self)
            except Exception:
                ui = 1.0
        self._ui_scale = ui
        self.geometry(f"{min(int(1180*ui), self.winfo_screenwidth()-24)}"
                      f"x{min(int(760*ui), self.winfo_screenheight()-80)}")
        self.minsize(min(int(1000*ui), self.winfo_screenwidth()-24),
                     min(int(640*ui), self.winfo_screenheight()-80))
        self._set_icon()

        self.engine = None
        self.worker_thread = None
        self.care_home_dir = None
        self.move_dest = None     # destination root when File movement is on
        self._est_at_start = None  # Part-2 estimates captured at confirmation
        self._console = None      # lazily created DevConsole window
        self._scanning = False    # True while a pre-flight scan is running
        self._scan_cancel = None  # threading.Event set when scan is cancelled
        self._latest_audit_report = ""
        self._latest_audit_completed = False
        self._notification_run_id = ""
        self._last_notification_worker = 0
        self._last_notification_phase = ""
        self._notification_workers_total = 0
        self._last_cost_gbp = 0.0
        self._closing = False
        self._run_config = None
        self._review_snapshot_draft = None
        self._review_run_id = ""
        self._review_controller = None
        self._review_busy = False
        self._review_check_thread = None
        self._review_events = collections.deque()
        self._review_notification_run_id = ""
        self._review_notification_category = ""
        self.notification_service = NotificationService(self.cfg.get("notifications"))

        # thread-safe bridge for blocking unknown-dialog
        self._unknown_event = threading.Event()
        self._unknown_result = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close_app)
        self.after(1000, self._poll_notifications)
        if self.cfg.get("_budget_safety_reset"):
            backup = self.cfg.pop("_budget_safety_reset")
            self.after(0, lambda: messagebox.showwarning(
                "Spend limit corrected",
                "The unsafe £2,500,000 local spend limit was backed up and "
                "reset to £35 for v1.3.1.\n\nBackup:\n" + backup))
        self._refresh_env_banner()
        # paint the Windows title bar (the OS caption strip with the
        # minimise/close buttons) black to match the app
        self.after(0, lambda: style_titlebar_black(self))

        # Developer console toggle: Ctrl+Shift+I (and F12 as a convenience).
        # Bound on the class so the chord works regardless of focused widget.
        self.bind_all("<Control-Shift-KeyPress-I>", self._toggle_console)
        self.bind_all("<Control-Shift-KeyPress-i>", self._toggle_console)
        self.bind_all("<F12>", self._toggle_console)
        CONSOLE.add("Application window ready. Press Ctrl+Shift+I to "
                    "open/close this console.", "info")

    # ---------------- developer console ----------------
    def _toggle_console(self, event=None):
        if self._console is None or not self._console.winfo_exists():
            self._console = DevConsole(self)
            self._console.show()
        elif self._console.state() == "withdrawn":
            self._console.show()
        else:
            self._console.hide()
        return "break"

    # ---------------- window icon ----------------
    def _set_icon(self):
        """Set the window/taskbar icon. Prefers a 'stage2.ico' file sitting next
        to the script (best on Windows: gives a crisp title-bar + taskbar icon);
        falls back to the embedded PNG via iconphoto so an icon always shows even
        if the .ico is missing."""
        # On Windows, give the app its own taskbar identity so it uses our icon
        # instead of the generic pythonw icon.
        try:
            if os.name == "nt":
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "Stage2.Processing.DocReviewAI")
        except Exception:
            pass
        # 1) .ico file alongside the script
        try:
            here = Path(__file__).resolve().parent
        except Exception:
            here = Path.cwd()
        ico = here / "stage2.ico"
        if ico.exists():
            try:
                self.iconbitmap(default=str(ico))
                return
            except Exception:
                pass
        # 2) embedded PNG fallback
        try:
            self._icon_img = tk.PhotoImage(data=APP_ICON_B64)
            self.iconphoto(True, self._icon_img)
        except Exception:
            traceback.print_exc()

    # ---------------- UI ----------------
    def _build_ui(self):
        self.dashboard = CompactDashboard(self, globals())

    def _open_ai_workflow(self, role):
        from stage2_workflow_ui import open_ai_workflow
        open_ai_workflow(self, globals(), role)

    @staticmethod
    def _set_palette_globals(value):
        palette = resolve_palette(value)
        globals().update(BG=palette.bg, PANEL=palette.panel, PANEL2=palette.raised,
                         BORDER=palette.line, FG=palette.text, FG_DIM=palette.muted,
                         ACCENT=palette.primary, GREEN=palette.primary, GREEN_HI=palette.primary)

    def _configure_ai_review(self):
        from stage2_workflow_ui import configure_auto_review
        return configure_auto_review(self, globals())

    def _refresh_ai_review_summary(self):
        if getattr(self, "dashboard", None):
            self.dashboard._refresh_review_summary()

    def _show_ai_review_state(self, state, reason=""):
        """Render a real review state without inventing document progress."""
        dashboard = getattr(self, "dashboard", None)
        if not dashboard:
            return
        active = state in ("preparing", "launching", "launched", "running", "verifying")
        attention = state in ("needs-attention", "outputs-awaiting-verification", "failed")
        labels = {"preparing": "Preparing", "launching": "Launching", "launched": "Launched",
                  "running": "Running", "verifying": "Verifying results",
                  "outputs-awaiting-verification": "Needs attention", "needs-attention": "Needs attention",
                  "failed": "Needs attention", "completed": "Complete",
                  "completed-with-unresolved": "Completed with unresolved", "no-candidates": "No candidates",
                  "cancelled": "Cancelled"}
        dashboard.phase_label.configure(text="AI Document Review")
        dashboard.state_label.configure(text=labels.get(state, state.replace("-", " ").title()))
        # A provider session reports no trustworthy document count here.  Keep
        # the native bar neutral rather than drawing a misleading percentage.
        self.progress.configure(mode="determinate", maximum=1, value=0)
        if attention:
            message = "The AI session needs review before its result can be accepted."
        elif active:
            message = "AI Document Review is working in its visible session; no document percentage is inferred."
        elif state == "completed":
            message = "AI Document Review and ledger reconciliation completed."
        elif state == "no-candidates":
            message = "Accuracy audit found no review candidates; no paid AI review was started."
        elif state == "cancelled":
            message = "Queued automatic AI Document Review was cancelled; processing and audit records remain available."
        else:
            message = "AI Document Review is not running."
        if reason:
            message += " " + reason
        dashboard.progress_label.configure(text=message)
        dashboard.wait_label.configure(text=message)

    def _notify_review_transition(self, run_id, state):
        """Emit only state changes that alter a user's next action."""
        if run_id != self._review_notification_run_id:
            self._review_notification_run_id = run_id
            self._review_notification_category = ""
        if state in ("launched", "running"):
            category, event = "started", "review_started"
        elif state in ("completed", "completed-with-unresolved", "no-candidates"):
            category, event = "complete", "review_complete"
        elif state in ("needs-attention", "outputs-awaiting-verification", "failed"):
            category, event = "attention", "blocked"
        else:
            return
        if category == self._review_notification_category:
            return
        self._review_notification_category = category
        if event == "blocked":
            self._notify(event, phase="audit_review", reason="review")
        else:
            self._notify(event, phase="audit_review")

    def _ai_review_summary(self):
        active = getattr(self, "_review_run_id", "")
        if active and getattr(self, "_review_controller", None):
            try:
                record = self._review_controller.get_status(active)
                settings = record.get("snapshot", {}).get("review", {})
                if settings and record.get("state") not in ("completed", "completed-with-unresolved", "no-candidates", "cancelled", "disabled"):
                    return self._format_review_summary(settings, captured=True)
            except Exception:
                pass
        return self._format_review_summary(ai_workflows.auto_review_defaults(self.cfg))

    @staticmethod
    def _format_review_summary(settings, captured=False):
        if not settings.get("enabled", True):
            return "Automatic AI Document Review is off · Configure before Start"
        model = str(settings.get("model_key", "sol")).title()
        effort = str(settings.get("effort", "high")).replace("xhigh", "Extra High").title()
        email = settings.get("expected_email") or "Selected account verified before Start"
        corrections = "Apply supported corrections" if settings.get("allow_document_changes", True) else "Propose corrections only"
        return f"{'This run' if captured else 'After audit'}: {model} / {effort} · {email} · {corrections}"

    def _view_ai_session(self):
        controller = self._get_review_controller()
        run_id = self._review_run_id
        if not run_id and self.care_home_dir:
            records = controller.find_runs(self.care_home_dir)
            run_id = records[0].get("run_id", "") if records else ""
        if not run_id:
            messagebox.showinfo("AI session", "No automatic review session has started for this care home yet.", parent=self)
            return
        try:
            controller.view_existing(run_id)
        except Exception as exc:
            messagebox.showinfo("AI session", str(exc), parent=self)

    def _cancel_queued_automatic_review(self):
        """Cancel only an unclaimed queued review; never stop a launched CLI."""
        controller = self._get_review_controller()
        run_id = self._review_run_id
        if not run_id and self.care_home_dir:
            records = controller.find_runs(self.care_home_dir)
            run_id = records[0].get("run_id", "") if records else ""
        if not run_id:
            messagebox.showinfo("AI Document Review", "No queued automatic review was found for this care home.", parent=self)
            return
        try:
            record = controller.get_status(run_id)
            if record.get("launch_claim") or record.get("state") not in ("waiting-processing", "waiting-audit", "ready"):
                messagebox.showinfo("AI Document Review", "Only an unclaimed queued review can be cancelled. A launched session remains available through View AI session.", parent=self)
                return
            if not messagebox.askyesno("Cancel queued AI review", "Cancel the queued automatic AI Document Review? Processing and the Accuracy Audit report are kept.", parent=self):
                return
            result = controller.cancel_queued(run_id)
        except Exception as exc:
            messagebox.showerror("AI Document Review", str(exc), parent=self)
            return
        self._review_run_id = run_id
        self._review_busy = False
        self._review_events.append((run_id, result))
        self._poll_ai_review()

    def _restore_ai_review(self, pending=None):
        """Resume supervision of a saved run, never infer a new review request."""
        if not self.care_home_dir:
            return
        controller = self._get_review_controller()
        if pending:
            self._review_run_id = str(pending.get("auto_review_run_id") or "")
            self._refresh_ai_review_summary()
            return
        try:
            records = controller.find_runs(self.care_home_dir)
            if not records:
                self._review_run_id = ""
                self._refresh_ai_review_summary()
                return
            record = records[0]
            self._review_run_id = record["run_id"]
            self._refresh_ai_review_summary()
            if record["state"] == "ready":
                self._begin_automatic_review({"audit_status": "complete"}, None)
            elif record.get("request_dir"):
                self._review_busy = True
                self._review_events.append((record["run_id"], record))
            elif record.get("processing_receipt") and not record.get("audit_receipt"):
                self.set_status("Saved processing is complete; its accuracy audit needs attention. "
                                "Automatic AI Document Review has not started.")
        except Exception as exc:
            self.log("Saved AI review needs attention: " + str(exc))

    def _get_review_controller(self):
        if self._review_controller is None:
            from stage2_autoreview import Stage2AutoReviewController
            self._review_controller = Stage2AutoReviewController(APP_DIR / "AI Review Runs")
        return self._review_controller

    def _begin_automatic_review(self, stats, status):
        """Called only after the engine thread and its writer lock have ended."""
        run_id = getattr(self, "_review_run_id", "")
        if not run_id:
            return False
        kind = str(status or "").partition(":")[0]
        if kind not in ("", "batch_applied", "batch_audit_complete"):
            return False
        if stats.get("audit_status") != "complete":
            return False
        if self._review_check_thread and self._review_check_thread.is_alive():
            return True
        controller = self._get_review_controller()
        self._review_busy = True
        self._refresh_run_controls()
        self.set_status("Accuracy audit complete · preparing AI Document Review…")
        self._show_ai_review_state("preparing")
        def work():
            try:
                result = controller.maybe_launch(run_id)
            except Exception as exc:
                result = {"state": "needs-attention", "reason": str(exc)}
            self._review_events.append((run_id, result))
        self._review_check_thread = threading.Thread(target=work, daemon=True)
        self._review_check_thread.start()
        return True

    def _poll_ai_review(self):
        while self._review_events:
            run_id, result = self._review_events.popleft()
            if run_id != self._review_run_id:
                continue
            state = str(result.get("state", "needs-attention"))
            active = state in ("preparing", "launching", "launched", "running", "verifying")
            self._review_busy = active
            self._refresh_run_controls()
            reason = str(result.get("reason") or result.get("message") or result.get("summary") or "")
            self.set_status(f"AI Document Review · {state.replace('-', ' ')}" + (f" · {reason}" if reason else ""))
            self._show_ai_review_state(state, reason)
            self._refresh_ai_review_summary()
            self._notify_review_transition(run_id, state)
        if self._review_busy and not (self._review_check_thread and self._review_check_thread.is_alive()):
            run_id = self._review_run_id
            def check():
                try:
                    result = self._get_review_controller().reconcile(run_id)
                except Exception as exc:
                    result = {"state": "needs-attention", "reason": str(exc)}
                self._review_events.append((run_id, result))
            self._review_check_thread = threading.Thread(target=check, daemon=True)
            self._review_check_thread.start()

    def _open_notification_settings(self):
        from stage2_workflow_ui import open_notification_settings
        open_notification_settings(self, globals())

    def _close_app(self):
        if self._review_busy:
            if not messagebox.askyesno("Close the dashboard?",
                    "AI Document Review is running in its own process. Closing "
                    "Stage 2 does not cancel that review, but pauses dashboard "
                    "monitoring. Its saved session can be checked when you reopen "
                    "this care home.\n\nKeep Stage 2 open for live completion "
                    "updates. Close the dashboard anyway?", parent=self):
                return
            self._closing = True
            self.notification_service.close()
            self.destroy()
            return
        if self.dashboard.is_busy():
            if not messagebox.askyesno("Stop and close?",
                    "A Stage 2 operation is running. Stop safely after the current "
                    "request, then close?\n\nAn interrupted accuracy audit must be "
                    "run again; this is not pause/resume.", parent=self):
                return
            self._closing = True
            if self._scan_cancel is not None:
                self._scan_cancel.set()
            if self.engine is not None:
                self.engine.stop()
            self._unknown_result = None
            self._unknown_event.set()
            self.after(500, self._close_when_idle)
            return
        self.notification_service.close()
        self.destroy()

    def _close_when_idle(self):
        if self.dashboard.is_busy():
            self.after(500, self._close_when_idle)
        else:
            self.notification_service.close()
            self.destroy()

    def _notify(self, event, **data):
        self.notification_service.emit(event, run_id=self._notification_run_id or "stage2", **data)

    def _poll_notifications(self):
        self._poll_ai_review()
        for status in self.notification_service.drain_statuses():
            CONSOLE.add(str(status), "info")
        if (self.dashboard.progress.phase in ("audit", "preparing", "scanning", "batch", "followup_plan", "followup_upload")
                and self.dashboard.is_busy()):
            progress = self.dashboard.progress
            self._notify("long_wait", phase=progress.phase, wait_seconds=progress.wait_seconds(),
                         completed=progress.completed, total=progress.total)
        self.after(1000, self._poll_notifications)

    def on_activity(self, event):
        self.after(0, self._activity_main, dict(event))

    def _activity_main(self, event):
        self.dashboard.activity_event(event)
        if event.get("phase") != "audit":
            if event.get("kind") == "phase_started":
                self._notify("phase_started", phase=event.get("phase", "processing"))
            return
        state = event.get("state")
        data = {"phase":"audit", "completed":event.get("completed", 0),
                "total":event.get("total", 0), "needs_review":event.get("needs_review", 0),
                "errors":event.get("errors", 0)}
        if state == "started":
            self._latest_audit_completed = False
            self._notify("audit_started", **data)
        elif state == "document_done":
            self._notify("progress", **data)
        elif state == "complete":
            self._latest_audit_report = event.get("report", "")
            self._latest_audit_completed = bool(self._latest_audit_report and Path(self._latest_audit_report).is_file())
            self._notify("audit_complete", **data)
        elif state == "skipped":
            self._notify("audit_skipped", reason=event.get("reason", "general"), **data)
        elif state in ("failed", "stopped"):
            self._latest_audit_completed = False
            self._notify("error" if state == "failed" else "stopped", **data)

    def _notify_done(self, stats, status):
        kind, _, payload = str(status or "").partition(":")
        if kind in ("batch_submitted", "batch_followup_submitted"):
            values = payload.split("|")
            try:
                documents = int(values[0])
                batches = int(values[1] if kind == "batch_submitted" else values[3])
            except (ValueError, IndexError):
                documents, batches = 0, 0
            self._notify("batch_submitted" if kind == "batch_submitted" else "followup_submitted",
                         phase="batch", documents=documents, batches=batches)
        elif kind == "batch_pending":
            self._notify("phase_started", phase="batch")
        elif kind in ("batch_primary_ambiguous", "batch_primary_incomplete", "batch_followup_ambiguous"):
            self._notify("blocked", reason="batch_ambiguous")
        elif kind in ("limit", "credit", "batch_over_budget", "batch_followup_over_budget"):
            self._notify("blocked", reason="budget")
        elif kind == "stopped":
            self._notify("stopped", phase="audit" if stats.get("audit_status") == "pending" else "processing")
        elif not kind or kind in ("batch_applied", "batch_audit_complete"):
            self._notify("run_complete", workers=stats.get("workers", 0),
                         needs_review=stats.get("audit_needs_review", stats.get("audit_flagged", 0)),
                         cost_gbp=self._last_cost_gbp, audit_status=stats.get("audit_status", "unknown"))
        elif kind not in ("batch_none", "batch_empty", "batch_none_pending",
                          "batch_nothing_to_submit"):
            self._notify("blocked", reason="general")

    # ---------------- env / settings ----------------
    def _refresh_env_banner(self):
        warns = []
        if not HAS_FITZ:
            warns.append("PyMuPDF missing (PDFs won't render) — pip install pymupdf")
        if not HAS_PIL:
            warns.append("Pillow missing (image previews limited) — pip install pillow")
        if not HAS_XLSX:
            warns.append("openpyxl missing (record/override Excel disabled) — pip install openpyxl")
        if not HAS_KEYRING:
            warns.append("keyring not installed (API key uses env var / protected "
                         "file) — pip install keyring for OS-level storage")
        if not get_api_key():
            warns.append("No API key set — open Settings and add your Anthropic key.")
        if self.cfg.get("convert_pdf", True) and not PdfConverter.has_libreoffice():
            warns.append("LibreOffice not found — Office files (.docx/.xlsx/.pptx) "
                         "use a text-only fallback. Install LibreOffice for full "
                         "PDF conversion.")
        self.banner.configure(text="   ".join(f"⚠ {w}" for w in warns) if warns else "")

    def _open_api_analytics(self):
        """API usage page: combined total (default) with scope buttons for
        core processing and each workflow tool. Only Stage 2 + its tools
        are included - other apps show their own usage on their own page."""
        if _api_usage is None:
            messagebox.showinfo(
                "API Usage",
                "The shared usage module (api_usage.py) is not available "
                "in this build.", parent=self)
            return
        tools = list(getattr(_api_usage, "STAGE2_TOOL_APPS",
                             ("PDF Splitter", "PDF Rotator",
                              "AI Document Splitter", "Re-check Unknowns")))
        analytics = _api_usage.open_analytics_window(self, "Stage 2 Processing",
                                                     tool_apps=tools)
        if analytics and getattr(analytics, "win", None):
            analytics.win.after(0, lambda:style_titlebar_black(analytics.win))

    def _open_jobs(self):
        """Open (or focus) the Processing-jobs dashboard."""
        if getattr(self, "_jobs_win", None) is not None \
                and self._jobs_win.winfo_exists():
            self._jobs_win.deiconify()
            self._jobs_win.lift()
            return
        self._jobs_win = JobsDialog(self)

    def _open_reports(self):
        """Choose audit output or the reviewers' cumulative correction records."""
        from stage2_workflow_ui import open_reports_menu
        open_reports_menu(self, globals())

    def _open_audit_reports(self):
        """Preserve the existing history/archive browser behind the chooser."""
        if getattr(self, "_reports_win", None) is not None \
                and self._reports_win.winfo_exists():
            self._reports_win.refresh()
            self._reports_win.deiconify()
            self._reports_win.lift()
            return
        self._reports_win = ReportsDialog(self)

    def _open_tools(self):
        """Open (or focus) the Tools panel - closed by default, opened only
        from the ribbon button."""
        if getattr(self, "_tools_win", None) is not None \
                and self._tools_win.winfo_exists():
            self._tools_win.deiconify()
            self._tools_win.lift()
            return
        self._tools_win = ToolsDialog(self)

    def _open_settings(self):
        SettingsDialog(self, copy.deepcopy(self.cfg), self._on_settings_saved)

    def _on_settings_saved(self, cfg):
        self.cfg = cfg
        FX_RATE[0] = cfg.get("fx", 0.79)
        self.notification_service.configure(cfg.get("notifications"))
        self._set_palette_globals(cfg.get("ui_palette"))
        self.dashboard.apply_palette(cfg.get("ui_palette"))
        self._refresh_ai_review_summary()
        self._refresh_env_banner()

    # ---------------- folder ----------------
    @staticmethod
    def _primary_recovery_needed(pending):
        if not pending or pending.get("processing_complete"):
            return False
        marker = pending.get("primary_submission") or {}
        if marker.get("status") in ("submission_started", "ambiguous"):
            return True
        if pending.get("primary_submission_complete") is False:
            return True
        if (pending.get("followup") or {}).get("phase"):
            return False
        batches = pending.get("batches") or []
        return bool(pending.get("requests")) and sum(
            int(batch.get("n", 0) or 0) for batch in batches
        ) < len(pending["requests"])

    def _refresh_folder_state(self):
        """Re-read saved state after operations; the header is not a snapshot."""
        if not self.care_home_dir:
            self.folder_lbl.configure(text="No folder selected.")
            return {}, {}
        pending = has_pending_batch(self.care_home_dir)
        checkpoint = read_live_checkpoint(self.care_home_dir)
        label = (f"Care home:  {self.care_home_dir.name}\n"
                 f"Path:  {self.care_home_dir}\n"
                 f"{len(worker_dirs_in(self.care_home_dir))} worker sub-folder(s) found.")
        if self.cfg.get("move_mode") and self.move_dest:
            label += f"\nMove mode: processed workers → {self.move_dest}"
        if pending:
            if pending.get("processing_complete"):
                audit_status = (pending.get("audit") or {}).get("status", "pending")
                label += (f"\nPROCESSING COMPLETE — accuracy audit {audit_status}. "
                          "Use Check batch status to continue.")
            elif self._primary_recovery_needed(pending):
                label += ("\nPRIMARY SUBMISSION NEEDS RECOVERY: saved request "
                          "and batch IDs are retained. Use Check batch status "
                          "to verify what Anthropic accepted.")
            else:
                followup = pending.get("followup") or {}
                active = followup if followup.get("phase") else pending
                phase = "FOLLOW-UP" if active is followup else "PRIMARY"
                label += (f"\nPENDING {phase} BATCH: "
                          f"{len(active.get('requests', {}))} document(s) in "
                          f"{len(active.get('batches', []))} batch(es). "
                          "Use Check batch status to continue.")
        elif checkpoint and not checkpoint.get("finished"):
            label += (f"\nUNFINISHED RUN: worker "
                      f"{checkpoint.get('workers_done', 0) + 1}/"
                      f"{checkpoint.get('workers_total', '?')}. "
                      "Press Start to resume completed-file skipping.")
        self.folder_lbl.configure(text=label)
        return pending, checkpoint

    def _refresh_run_controls(self, *, busy=False, pending=None):
        if pending is None:
            pending = (has_pending_batch(self.care_home_dir)
                       if self.care_home_dir else {})
        review_busy = bool(getattr(self, "_review_busy", False))
        processing_busy = bool(busy or getattr(self, "_recovery_busy", False)
                               or getattr(self, "_scanning", False)
                               or (getattr(self, "worker_thread", None)
                                   and self.worker_thread.is_alive()))
        busy = processing_busy or review_busy
        self.pick_btn.configure(state="disabled" if busy else "normal")
        self.start_btn.configure(state=("normal" if self.care_home_dir
            and not busy and not pending else "disabled"))
        self.flatten_btn.configure(state=("disabled" if busy or pending
                                          else "normal"))
        self.batch_btn.configure(state=("normal" if pending and not busy
                                        else "disabled"))
        # Stop controls processing/audit only.  The external review session is
        # deliberately not cancelled by this control; use its viewer or cancel
        # the queue before it launches.
        self.stop_btn.configure(state="normal" if processing_busy else "disabled")

    def _pick_folder(self):
        if self._batch_busy_guard():
            return
        start = str(desktop_path())
        title = ("Choose the SOURCE care-home folder"
                 if self.cfg.get("move_mode") else "Choose the care-home folder")
        d = filedialog.askdirectory(title=title, initialdir=start)
        if not d:
            return
        self.care_home_dir = Path(d)
        self.move_dest = None

        # In File-movement mode, also choose where completed workers are moved to.
        if self.cfg.get("move_mode"):
            dd = filedialog.askdirectory(
                title="Choose the DESTINATION folder (processed workers move here)",
                initialdir=str(self.care_home_dir.parent))
            if not dd:
                self.folder_lbl.configure(
                    text="File movement is ON — pick a destination folder too "
                         "(press Choose folder again).")
                self.start_btn.configure(state="disabled")
                return
            dest = Path(dd)
            # guard: destination must not be the source or inside it (that would
            # move a worker into its own subtree)
            try:
                same = dest.resolve() == self.care_home_dir.resolve()
                inside = self.care_home_dir.resolve() in dest.resolve().parents
            except Exception:
                same = (str(dest) == str(self.care_home_dir))
                inside = False
            if same or inside:
                messagebox.showerror(
                    "Invalid destination",
                    "The destination can't be the source folder or a folder "
                    "inside it. Pick a separate destination.")
                self.start_btn.configure(state="disabled")
                return
            self.move_dest = dest

        pend, ckpt = self._refresh_folder_state()
        self._refresh_run_controls(pending=pend)
        self._restore_ai_review(pend)

        if ckpt and not ckpt.get("finished") and not pend:
            if messagebox.askyesno(
                    "Resume unfinished run?",
                    f"A previous processing run of\n\n"
                    f"{self.care_home_dir.name}\n\n"
                    f"did not finish (position: worker "
                    f"{ckpt.get('workers_done', 0) + 1} of "
                    f"{ckpt.get('workers_total', '?')} — "
                    f"'{ckpt.get('current_worker', '?')}', "
                    f"{ckpt.get('ts', '?')}).\n\n"
                    f"Resume now? Already-processed documents are skipped "
                    f"automatically (nothing is billed twice). You will see "
                    f"the normal scan/confirm step first."):
                self.after(100, self._start)

        if pend and self._primary_recovery_needed(pend):
            self.set_status("Primary submission needs recovery — use Check batch status.")
            if messagebox.askyesno(
                    "Primary batch submission needs recovery",
                    "This folder has an interrupted or uncertain primary "
                    "submission. The saved request list is retained.\n\n"
                    "Check Anthropic's batch records now? This check will not "
                    "submit documents. You will see the recovery plan before "
                    "any remaining requests are submitted."):
                self._batch_check_status()
        elif pend:
            choice = messagebox.askyesnocancel(
                "Pending overnight batch found",
                f"This folder has a batch submitted on "
                f"{pend.get('submitted_ts', '?')} that has not been applied "
                f"yet ({len(pend.get('requests', {}))} document(s)).\n\n"
                f"• YES  = check its status now (and apply the results if "
                f"they are ready)\n"
                f"• NO   = CANCEL the batch (anything already processed is "
                f"still billed and can still be applied - cancelling refunds "
                f"nothing)\n"
                f"• CANCEL = decide later (use 'Check batch status')",
                icon="question")
            if choice is True:
                self._batch_check_status()
            elif choice is False:
                self._batch_cancel()

    # ---------------- logging / UI updates (thread-safe) ----------------
    def log(self, msg):
        # mirror every activity-log line into the developer console too
        CONSOLE.add(msg, "log")
        self.after(0, self._log_main, msg)

    def _log_main(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, msg):
        def upd():
            self.status_lbl.configure(text=msg)
            self.dashboard.status_changed(msg)
        self.after(0, upd)

    def set_progress(self, done, total):
        def upd():
            if self.dashboard.progress.phase == "audit" or self.dashboard.structured_progress:
                self.dashboard.refresh_progress()
                return
            self.progress.configure(maximum=max(total, 1), value=done)
            self.dashboard.worker_progress(done, total)
        self.after(0, upd)

    def set_preview(self, b64img, name):
        self.after(0, self._set_preview_main, b64img, name)

    def _set_preview_main(self, b64img, name):
        self.preview_name.configure(text=name or "—")
        if b64img and HAS_PIL:
            try:
                from io import BytesIO
                im = Image.open(BytesIO(base64.b64decode(b64img)))
                im.thumbnail((265, 400))
                self._preview_ref = ImageTk.PhotoImage(im)
                self.preview_canvas.configure(image=self._preview_ref, text="")
                return
            except Exception:
                pass
        self.preview_canvas.configure(image="", text="(preview unavailable)")
        self._preview_ref = None

    def on_cost(self, gbp, tokens):
        def upd():
            self._last_cost_gbp = float(gbp)
            self.cost_var.set(f"£{gbp:.2f}")
            self.token_var.set(f"{tokens:,} tokens")
        self.after(0, upd)

    def _update_stats(self, stats):
        for k, v in self.info_vars.items():
            v.set(str(stats.get(k, 0)))
        self.dashboard.update_stats(stats)

    # ---------------- unknown dialog bridge ----------------
    def ask_unknown(self, filename, guess, features, b64img):
        """Called from the worker thread; blocks until the user answers."""
        self._unknown_event.clear()
        self._unknown_result = None
        self.after(0, self._show_unknown, filename, guess, features, b64img)
        self._unknown_event.wait()
        return self._unknown_result

    def _show_unknown(self, filename, guess, features, b64img):
        dlg = UnknownDialog(self, filename, guess, features, b64img)
        self.wait_window(dlg)
        self._unknown_result = dlg.result
        self._unknown_event.set()

    # ---------------- batch-mode bridges & actions ----------------
    def review_unknowns(self, n_unknowns: int) -> bool:
        """Called ONCE from the batch-apply thread when the first unknown
        documents appear; blocks until the user decides whether to review
        this run's unknowns now or leave them all as 'Other - Unknown'."""
        self._unknown_event.clear()
        self._unknown_result = None

        def ask():
            self._unknown_result = messagebox.askyesno(
                "Review unknown documents?",
                f"The batch contained documents the AI could not match "
                f"({n_unknowns} so far). They have been filed as "
                f"'Other - Unknown'.\n\n"
                f"Review them now (one dialog per document, like live mode)?\n"
                f"Choosing No leaves them as 'Other - Unknown' - they will "
                f"land in the Bulk folder.")
            self._unknown_event.set()
        self.after(0, ask)
        self._unknown_event.wait()
        return bool(self._unknown_result)

    def _make_engine(self, api_key: str, model_id: str, reprocess=False, run_config=None) -> Engine:
        """Build an Engine wired to this window (shared by live + batch runs)."""
        cfg = run_config if run_config is not None else self.cfg
        api = ClaudeAPI(api_key, model_id)
        # Second opinion: a stronger-model client used ONLY for documents the
        # primary model can't settle (no match / confidence < threshold).
        # Skipped when the primary model is already at least that strong.
        escalation_api = None
        if bool(cfg.get("second_opinion", True)):
            prim = MODELS_BY_ID.get(model_id, {})
            strong = MODELS_BY_ID.get(SECOND_OPINION_MODEL_ID, {})
            if (model_id != SECOND_OPINION_MODEL_ID
                    and prim.get("in", 99.0) < strong.get("in", 0.0)):
                escalation_api = ClaudeAPI(api_key, SECOND_OPINION_MODEL_ID)
        self.dashboard.reset()
        self._latest_audit_completed = False
        pending = has_pending_batch(self.care_home_dir) if self.care_home_dir else {}
        stamp = pending.get("submitted_ts") or datetime.datetime.now().isoformat(timespec="seconds")
        self._notification_run_id = hashlib.sha256((str(self.care_home_dir) + stamp).encode()).hexdigest()[:16]
        self._notification_workers_total = len(worker_dirs_in(self.care_home_dir))
        self._last_notification_worker = 0
        self._last_notification_phase = ""
        if pending:
            self._notify("phase_started", phase="batch")
        else:
            self._notify("run_started", workers=len(worker_dirs_in(self.care_home_dir)))
        engine = Engine(
            self.care_home_dir, self.kb, api, self.care_home_dir.name,
            log=self.log, set_status=self.set_status,
            set_progress=self.set_progress, set_preview=self.set_preview,
            ask_unknown=self.ask_unknown, on_cost=self.on_cost,
            on_done=self._on_done,
            resolution=cfg.get("resolution", 1.5),
            skip_when_clear=cfg.get("skip_when_clear", False),
            adaptive_pages=cfg.get("adaptive_pages", True),
            auto_other=cfg.get("auto_other", False),
            max_workers=int(cfg.get("max_workers", DEFAULT_MAX_WORKERS)),
            max_files=int(cfg.get("max_files", DEFAULT_MAX_FILES)),
            max_budget_gbp=float(cfg.get("max_budget_gbp",
                                              DEFAULT_MAX_BUDGET_GBP)),
            max_file_mb=float(cfg.get("max_file_mb", DEFAULT_MAX_FILE_MB)),
            redact_logs=bool(cfg.get("redact_logs", False)),
            reprocess=reprocess,
            convert_pdf=bool(cfg.get("convert_pdf", True)),
            move_mode=bool(cfg.get("move_mode", False)),
            move_dest=self.move_dest,
            review_unknowns=self.review_unknowns,
            escalation_api=escalation_api,
            orientation_mode=str(
                cfg.get("orientation_mode", "audit")),
            orientation_confidence=float(
                cfg.get("orientation_confidence", 0.95)),
            orientation_margin=float(
                cfg.get("orientation_margin", 0.20)),
            bundle_split=bool(cfg.get("bundle_split", True)),
            cleanup_leftovers=bool(cfg.get("cleanup_leftovers", True)),
            post_run_audit=bool(cfg.get("post_run_audit", False)),
            on_activity=self.on_activity)
        # Existing batches opt in only through the marker saved with their
        # original submission. Never attach today's settings to a legacy batch.
        if pending:
            self._review_run_id = str(pending.get("auto_review_run_id") or "")
        if self._review_run_id:
            engine._review_run_id = self._review_run_id
            engine._review_controller = self._get_review_controller()
            engine.stats["auto_review_run_id"] = self._review_run_id
            snapshot = engine._review_controller.get_status(self._review_run_id).get("snapshot", {})
            if snapshot.get("review", {}).get("enabled"):
                engine.post_run_audit = True
            if not pending and getattr(self, "_resuming_live", False):
                engine._review_scope_worker_names = {name.casefold() for name in snapshot.get("scope_worker_names", [])}
                saved = read_live_checkpoint(self.care_home_dir) or {}
                engine._resume_processing_complete = bool(saved.get("processing_complete"))
                root = Path(snapshot["document_root"]).resolve()
                restored = set()
                for item in saved.get("audit_worker_dirs", []):
                    folder = Path(item).resolve()
                    key = str(folder).casefold()
                    if (key not in restored and folder.parent == root
                            and folder.name.casefold() in engine._review_scope_worker_names
                            and folder.is_dir()):
                        engine._audit_worker_dirs.append(folder)
                        restored.add(key)
        return engine

    def _batch_busy_guard(self) -> bool:
        """True (and warns) if a run/scan is already in progress."""
        if (self.worker_thread and self.worker_thread.is_alive()) \
                or getattr(self, "_scanning", False) \
                or getattr(self, "_recovery_busy", False) \
                or getattr(self, "_review_busy", False):
            messagebox.showwarning(
                "Busy", "Another run, scan, recovery check or AI Document Review is already in progress.")
            return True
        return False

    def _batch_check_status(self):
        """Poll the pending batch; if it has ended, download and apply the
        results (Phase B) on a background thread."""
        if not self.care_home_dir or self._batch_busy_guard():
            return
        if not has_pending_batch(self.care_home_dir):
            messagebox.showinfo("Batch status",
                                "No pending batch for this folder.")
            self._refresh_folder_state()
            self._refresh_run_controls(pending={})
            return
        api_key = get_api_key()
        if not api_key:
            messagebox.showwarning("API key needed",
                                   "Open Settings and add your Anthropic API "
                                   "key first.")
            return
        model_id = MODELS[self.cfg["model"]]["id"]
        # the batch was submitted with a specific model; honour it
        pend = has_pending_batch(self.care_home_dir)
        model_id = pend.get("model_id") or model_id
        if self._primary_recovery_needed(pend):
            self.engine = self._make_engine(api_key, model_id)
            self._check_primary_recovery()
            return
        self.start_btn.configure(state="disabled")
        self.pick_btn.configure(state="disabled")
        self.flatten_btn.configure(state="disabled")
        self.batch_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._reset_stats()
        self.engine = self._make_engine(api_key, model_id)
        self.log(f"\nChecking batch status for: {self.care_home_dir}")
        self.worker_thread = threading.Thread(
            target=self.engine.run_batch_apply, daemon=True)
        self.worker_thread.start()
        self._poll_stats()

    def _check_primary_recovery(self):
        """Read-only reconciliation first; submission needs the shown plan."""
        if self._batch_busy_guard():
            return
        self._recovery_busy = True
        self._refresh_run_controls(busy=True)
        self.stop_btn.configure(state="disabled")
        self.set_status("Checking saved primary requests against Anthropic batches…")
        engine = self.engine

        def check():
            try:
                result = engine.recover_primary_submission(allow_resubmit=False)
            except Exception as exc:
                result = {"status": "blocked", "message": str(exc)}
            self.after(0, self._primary_recovery_checked, engine, result)

        self.worker_thread = threading.Thread(target=check, daemon=True)
        self.worker_thread.start()

    def _primary_recovery_checked(self, engine, result):
        # A callback can reach Tk before the thread that queued it returns.
        if self.worker_thread and self.worker_thread.is_alive():
            self.after(25, self._primary_recovery_checked, engine, result)
            return
        self._recovery_busy = False
        pending, _ = self._refresh_folder_state()
        self._refresh_run_controls(pending=pending)
        message = str(result.get("message") or "Recovery could not be verified.")
        if result.get("status") not in ("ready", "needs_authorization", "matched"):
            self.set_status("Primary recovery needs attention.")
            messagebox.showwarning(
                "Primary batch recovery blocked",
                message + "\n\nThe saved state is retained. No remaining "
                "requests were submitted. Resolve the issue above, then use "
                "Check batch status again.")
            return
        remaining = int(result.get("remaining", 0) or 0)
        estimate = result.get("estimated_remaining_gbp")
        cost_note = (f"\nEstimated remaining primary submission cost: "
                     f"£{float(estimate):.2f}." if estimate is not None else "")
        prompt = (f"\n\nResume the {remaining} requests confirmed as not submitted?"
                  if remaining else
                  "\n\nSave the recovered batch IDs? No new requests are needed.")
        if not messagebox.askyesno(
                "Resume primary batch submission", message + cost_note + prompt):
            self.set_status("Recovery plan checked. Use Check batch status when ready.")
            return
        if self._batch_busy_guard():
            return
        self._recovery_busy = True
        self._refresh_run_controls(busy=True)
        self.set_status("Recovering primary batch submission…")

        def resume():
            try:
                # Re-verifies the plan immediately before saving/submitting.
                engine.recover_primary_submission(allow_resubmit=True)
            except Exception as exc:
                self._on_done(engine.stats, "batch_recovery_blocked:" + str(exc))

        self.worker_thread = threading.Thread(target=resume, daemon=True)
        self.worker_thread.start()
        self._poll_stats()

    def _batch_cancel(self):
        """Cancel the pending batch after an explicit warning."""
        if not self.care_home_dir or self._batch_busy_guard():
            return
        pend = has_pending_batch(self.care_home_dir)
        if not pend:
            messagebox.showinfo("Batch", "No pending batch for this folder.")
            return
        if not messagebox.askyesno(
                "Cancel batch?",
                "Cancelling stops requests that have not been processed yet.\n\n"
                "IMPORTANT: anything ALREADY processed is still billed and its "
                "results remain downloadable - cancelling refunds nothing.\n\n"
                "The state file is kept so any partial results can still be "
                "applied with 'Check batch status'. Cancel the batch?",
                icon="warning"):
            return
        api_key = get_api_key()
        if not api_key:
            messagebox.showwarning("API key needed",
                                   "Open Settings and add your Anthropic API "
                                   "key first.")
            return
        model_id = pend.get("model_id") or MODELS[self.cfg["model"]]["id"]
        eng = self._make_engine(api_key, model_id)

        def _work():
            try:
                out = eng.cancel_pending_batches()
                msg = "\n".join(f"{bid}: {st}" for bid, st in out) or "(none)"
                self.after(0, lambda: messagebox.showinfo(
                    "Batch cancel requested",
                    f"Cancellation requested:\n{msg}\n\nUse 'Check batch "
                    f"status' later to apply anything that had already "
                    f"been processed."))
            except Exception as e:
                traceback.print_exc()
                self.after(0, lambda: messagebox.showerror(
                    "Batch cancel failed", str(e)))
        threading.Thread(target=_work, daemon=True).start()

    # ---------------- run control ----------------
    def _start(self):
        if not self.care_home_dir or self._batch_busy_guard():
            return
        if has_pending_batch(self.care_home_dir):
            self._refresh_folder_state()
            self._refresh_run_controls()
            messagebox.showwarning(
                "Pending batch",
                "This folder has saved batch work. Use Check batch status "
                "to recover or continue it before starting a new run.")
            return
        if self.cfg.get("move_mode") and not self.move_dest:
            messagebox.showwarning(
                "Destination needed",
                "File movement is ON. Click \"Choose care-home folder\" and pick "
                "both a source and a destination folder first.")
            return
        api_key = get_api_key()
        if not api_key:
            messagebox.showwarning("API key needed",
                                   "Open Settings and add your Anthropic API key first.")
            return
        if not HAS_FITZ:
            if not messagebox.askyesno(
                    "PyMuPDF missing",
                    "PyMuPDF is not installed, so PDF documents cannot be rendered "
                    "for the AI (only images will work). Continue anyway?"):
                return

        # One immutable draft drives scan, confirmation and the resulting run.
        cfg = copy.deepcopy(self.cfg)
        checkpoint = read_live_checkpoint(self.care_home_dir) or {}
        self._resuming_live = bool(checkpoint and not checkpoint.get("finished"))
        self._resume_live_checkpoint = copy.deepcopy(checkpoint) if self._resuming_live else {}
        self._resume_review_run_id = str(checkpoint.get("auto_review_run_id") or "") if self._resuming_live else ""
        self._run_config = cfg
        self._review_snapshot_draft = None
        model_id = MODELS[cfg["model"]]["id"]
        max_file_mb = float(cfg.get("max_file_mb", DEFAULT_MAX_FILE_MB))

        # ---- PRE-FLIGHT SCAN (no API calls) on a BACKGROUND thread ---------
        # The scan walks the whole care-home tree, which can be slow on very
        # large or cloud-synced (OneDrive) cohorts. Running it off the UI thread
        # keeps the window responsive; results are handed back to
        # _start_after_scan on the Tk thread. The scan is CANCELLABLE via the
        # Stop button (which sets self._scan_cancel).
        # Discard any previous terminal dashboard state before the first
        # non-structured preflight status is emitted.
        self.dashboard.reset()
        self._scan_cancel = threading.Event()
        self._scanning = True
        self.pick_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.flatten_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")   # <-- Stop works during scan now
        self.set_status("Scanning folder (no API calls yet)… click Stop to cancel.")
        # determinate bar driven by per-worker progress, so it's clearly alive
        self.progress.configure(mode="determinate", maximum=100, value=0)

        care_dir = self.care_home_dir
        cancel = self._scan_cancel

        def _progress(done, total, files_so_far):
            pct = int((done / total) * 100) if total else 0
            self.after(0, self._scan_progress, done, total, files_so_far, pct)

        def _bg_scan():
            try:
                scan_ext = (CONVERT_SCAN_EXT
                            if cfg.get("convert_pdf", True) else None)
                scan = preflight_scan(care_dir, max_file_mb,
                                      cancel_event=cancel, progress_cb=_progress,
                                      scan_ext=scan_ext)
                already = 0
                if not scan.get("cancelled"):
                    already = has_existing_batches(care_dir, cancel_event=cancel)
                    review_settings = ai_workflows.auto_review_defaults(cfg)
                    if self._resuming_live:
                        if self._resume_review_run_id:
                            saved = self._get_review_controller().get_status(self._resume_review_run_id, processing_root=care_dir)
                            snapshot = saved["snapshot"]
                            expected_root = self.move_dest if cfg.get("move_mode") else care_dir
                            if Path(snapshot["document_root"]).resolve() != Path(expected_root).resolve():
                                raise RuntimeError("This interrupted run has a different saved document destination. Select its original destination before resuming.")
                            # Free identity/model preflight again, but keep the original
                            # snapshot, authority and worker scope, not today's defaults.
                            checked = self._get_review_controller().capture_snapshot(
                                run_id=snapshot["run_id"], processing_root=care_dir,
                                document_root=snapshot["document_root"], care_home=snapshot["care_home"],
                                worker_names=snapshot["scope_worker_names"], settings=snapshot["review"],
                                accuracy_audit_enabled=snapshot["accuracy_audit_enabled"])
                            for key in ("provider", "model", "effort", "expected_email"):
                                if checked["review"].get(key) != snapshot["review"].get(key):
                                    raise RuntimeError("The saved review account/model no longer matches. No processing was started.")
                            cfg.setdefault("ai_workflows", {})["auto_review"] = copy.deepcopy(snapshot["review"])
                            cfg["post_run_audit"] = snapshot["accuracy_audit_enabled"]
                        else:
                            # Legacy live checkpoints did not authorize auto review.
                            cfg.setdefault("ai_workflows", {})["auto_review"] = {"enabled": False}
                    elif review_settings.get("enabled", True) and scan.get("files"):
                        review_settings["workspace_root"] = review_settings.get("workspace_root") or str(ai_workflows.default_workspace_root())
                        review_settings["ledger_path"] = review_settings.get("ledger_path") or str(ai_workflows.default_ledger_path())
                        review_settings["misnaming_path"] = review_settings.get("misnaming_path") or str(APP_DIR / "Misnaming Record.xlsx")
                        review_settings["assets_root"] = str(bundled_resource("docs", "ai-review"))
                        review_settings["codex_homes"] = copy.deepcopy(cfg.get("ai_workflows", {}).get("codex_homes", []))
                        scope = worker_dirs_in(care_dir)[:int(cfg.get("max_workers", DEFAULT_MAX_WORKERS))]
                        self._review_snapshot_draft = self._get_review_controller().capture_snapshot(
                            run_id="stage2-" + uuid.uuid4().hex, processing_root=care_dir,
                            document_root=self.move_dest if cfg.get("move_mode") else care_dir,
                            care_home=care_dir.name, worker_names=[p.name for p in scope],
                            settings=review_settings,
                            accuracy_audit_enabled=bool(cfg.get("post_run_audit", False)))
                    if cancel.is_set():
                        scan["cancelled"] = True
                self.after(0, self._start_after_scan, api_key, model_id,
                           max_file_mb, scan, already, None)
            except Exception as e:
                traceback.print_exc()
                self.after(0, self._start_after_scan, api_key, model_id,
                           max_file_mb, None, 0, str(e))

        self.worker_thread = threading.Thread(target=_bg_scan, daemon=True)
        self.worker_thread.start()

    def _scan_progress(self, done, total, files_so_far, pct):
        """UI-thread callback: update the bar and status during the scan."""
        if not getattr(self, "_scanning", False):
            return
        self.progress.configure(value=pct)
        self.set_status(f"Scanning folder… {done}/{total} workers, "
                        f"{files_so_far} documents so far (Stop to cancel).")

    def _scan_ui_reset(self):
        """Restore the controls/progress bar after a scan that doesn't proceed."""
        self._scanning = False
        self._run_config = None
        self._review_snapshot_draft = None
        try:
            self.progress.stop()
        except Exception:
            pass
        self.progress.configure(mode="determinate", value=0)
        self._refresh_run_controls()
        self.set_status("Idle.")

    def _start_after_scan(self, api_key, model_id, max_file_mb, scan, already, error):
        # back on the UI thread now; the scan thread has finished
        cfg = self._run_config or copy.deepcopy(self.cfg)
        self._scanning = False
        try:
            self.progress.stop()
        except Exception:
            pass
        self.progress.configure(mode="determinate", value=0)

        if error is not None:
            messagebox.showerror("Scan failed", f"Could not scan the folder:\n{error}")
            self._scan_ui_reset()
            return

        # user pressed Stop during the scan
        if scan.get("cancelled"):
            self.log("Scan cancelled before any documents were sent.")
            self._scan_ui_reset()
            self.set_status("Scan cancelled.")
            return

        resume_audit_only = bool(
            getattr(self, "_resuming_live", False)
            and getattr(self, "_resume_review_run_id", "")
            and (getattr(self, "_resume_live_checkpoint", {}) or {}).get("processing_complete")
            and (getattr(self, "_resume_live_checkpoint", {}) or {}).get("audit_worker_dirs"))
        if scan["files"] == 0 and not resume_audit_only:
            messagebox.showinfo(
                "Nothing to do",
                "No eligible documents were found to send.\n\n"
                f"Worker folders: {scan['workers']}\n"
                f"Skipped (too big): {len(scan['oversized'])}")
            self._scan_ui_reset()
            return

        # ---- Part-2 estimator: page/resolution-aware Live vs Batch figures ---
        zoom = float(cfg.get("resolution", 1.5))
        adaptive = bool(cfg.get("adaptive_pages", True))
        vocab_block = self.kb.vocabulary_block()
        followup_model_id = model_id
        if bool(cfg.get("second_opinion", True)):
            primary_price = MODELS_BY_ID.get(model_id, {}).get("in", 99.0)
            stronger_price = MODELS_BY_ID.get(
                SECOND_OPINION_MODEL_ID, {}).get("in", 0.0)
            if primary_price < stronger_price:
                followup_model_id = SECOND_OPINION_MODEL_ID
        est_live = estimate_pipeline_costs_gbp(
            scan["files"], model_id, followup_model_id, zoom, vocab_block,
            batch=False,
            include_audit=bool(cfg.get("post_run_audit", False)))
        est_batch = estimate_pipeline_costs_gbp(
            scan["files"], model_id, followup_model_id, zoom, vocab_block,
            batch=True,
            include_audit=bool(cfg.get("post_run_audit", False)))
        est_gbp = est_live["gbp"]
        self._est_at_start = {"live": est_live, "batch": est_batch,
                              "files": scan["files"]}
        size_mb = scan["bytes"] / (1024 * 1024)
        # Time estimate is based on the number of files that will ACTUALLY be
        # sent this run - capped by the per-run file limit, since the run stops
        # there. So the figure matches what will really happen.
        sec_per_file = float(cfg.get("sec_per_file", DEFAULT_SEC_PER_FILE))
        files_this_run = min(scan["files"],
                             int(cfg.get("max_files", DEFAULT_MAX_FILES)))
        est_secs = estimate_runtime_seconds(files_this_run, sec_per_file)

        # detect a folder that was processed before (via the saved manifest)
        reprocess = False
        rerun_note = ""
        if already:
            choice = messagebox.askyesnocancel(
                "Folder may already be processed",
                f"This care home has a saved processing record ({already} file(s) "
                f"remembered from a previous run), so it looks like it was "
                f"processed before.\n\n"
                f"• YES  = process again but SKIP files already done with this "
                f"model (no repeat charges for unchanged files)\n"
                f"• NO   = force a full re-process of everything (will incur API "
                f"charges again)\n"
                f"• CANCEL = don't run\n\n"
                f"Recommended: YES.", icon="warning")
            if choice is None:
                self._scan_ui_reset()
                return
            reprocess = (choice is False)  # NO -> force reprocess
            rerun_note = ("force full re-process" if reprocess
                          else "skip already-processed files")

        # ---- CONFIRMATION SCREEN -----------------------------------------
        lines = [
            f"About to review:  {self.care_home_dir.name}",
            f"Path:  {self.care_home_dir}",
            "",
            f"Worker folders      : {scan['workers']}",
            f"Documents to send   : {scan['files']}",
            f"Total size          : {size_mb:.1f} MB",
        ]
        if cfg.get("move_mode") and self.move_dest:
            lines.append(f"Move processed to   : {self.move_dest}")
        if scan["oversized"]:
            lines.append(f"Skipped (too big)   : {len(scan['oversized'])} "
                         f"(> {max_file_mb:.0f} MB)")
        if scan.get("cloud_only"):
            lines.append(f"Skipped (cloud-only): {len(scan['cloud_only'])} "
                         f"(not downloaded)")
        lines += [
            "",
            f"Model               : {cfg['model']}",
            "",
            "--- Estimated cost (rough; billed on real usage) ---",
            f"Live classification : ~£{est_live['primary_gbp']:.2f}"
            f"  (prompt caching applied)",
            f"Primary batch       : ~£{est_batch['primary_gbp']:.2f}"
            f"  (50% batch discount applied)",
            f"Live finishing      : ~£{est_batch['finishing_gbp']:.2f}"
            f"  (required dating/ranking checks)",
            f"Follow-up reserve   : ~£{est_batch['followup_reserve_gbp']:.2f}"
            f"  ({est_batch['followup_reserve_files']} unresolved docs, optional)",
            f"Accuracy audit      : ~£{est_batch['audit_gbp']:.2f}"
            f"  ({'enabled' if cfg.get('post_run_audit', False) else 'disabled'})",
            f"Cumulative live     : ~£{est_live['gbp']:.2f}",
            f"Cumulative batch    : ~£{est_batch['gbp']:.2f}",
            f"Assumptions         : {cfg['model']}, {zoom:.1f}x resolution, "
            f"{'adaptive pages (live)' if adaptive else 'all pages'}; "
            f"~{EST_OUTPUT_TOKENS_PER_DOC} output tokens/doc",
            "",
            f"Estimated time      : ~{human_duration(est_secs)}"
            f"  (live mode, {files_this_run} files @ {sec_per_file:g}s; batch "
            f"results take ~1h-24h)",
            f"Hard spend limit    : £{float(cfg.get('max_budget_gbp', DEFAULT_MAX_BUDGET_GBP)):.2f}",
            f"Hard file limit     : {int(cfg.get('max_files', DEFAULT_MAX_FILES))} files",
            f"Hard worker limit   : {int(cfg.get('max_workers', DEFAULT_MAX_WORKERS))} workers",
            "", self._format_review_summary(ai_workflows.auto_review_defaults(cfg)),
            "AI Document Review uses the selected CLI subscription, separate from the API estimate above.",
        ]
        if rerun_note:
            lines += ["", f"Re-run mode         : {rerun_note}"]
        overview = "\n".join(lines)

        # Custom confirmation dialog: shows the overview plus scrollable, copyable
        # lists of the files that will be SKIPPED (oversized and cloud-only),
        # each with its own Copy button (and a Copy ALL when there's more than one).
        sections = []
        if scan.get("oversized"):
            sections.append((
                f"Files over the {max_file_mb:.0f} MB limit — SKIPPED "
                f"({len(scan['oversized'])}):", AMBER, scan["oversized"]))
        if scan.get("cloud_only"):
            sections.append((
                f"Cloud-only files (not downloaded) — SKIPPED "
                f"({len(scan['cloud_only'])}). Set these to "
                f"\u201cAlways keep on this device\u201d to include them:",
                ACCENT, scan["cloud_only"]))
        warn = est_gbp >= CONFIRM_COST_THRESHOLD_GBP
        # Overnight Batch cannot be selected while one is already pending.
        pend = has_pending_batch(self.care_home_dir)
        batch_blocked = ""
        if pend:
            batch_blocked = ("A batch submitted on "
                             f"{pend.get('submitted_ts', '?')} is still pending "
                             f"for this folder. Live and new batch processing "
                             f"are blocked until it is retrieved, completed, "
                             f"canceled and safely resolved, or its ambiguous "
                             f"submission is resolved.")
            messagebox.showwarning(
                "Existing batch blocks processing",
                batch_blocked + "\n\nUse 'Check batch status'. No live API "
                "request has been made.")
            self._scan_ui_reset()
            return
        dlg = ConfirmReviewDialog(self, overview, sections, warn,
                                  mode_choice=cfg.get("run_mode", "live"),
                                  batch_blocked=batch_blocked)
        self.wait_window(dlg)
        if not dlg.result:
            self._scan_ui_reset()
            return
        run_mode = dlg.mode
        try:
            self._review_run_id = getattr(self, "_resume_review_run_id", "")
            if self._review_snapshot_draft:
                self._get_review_controller().commit_run(self._review_snapshot_draft)
                self._review_run_id = self._review_snapshot_draft["run_id"]
        except Exception as exc:
            messagebox.showerror("Review setup could not be saved", str(exc) + "\nNo processing was started.", parent=self)
            self._scan_ui_reset()
            return
        # persist the last-used mode
        if run_mode != self.cfg.get("run_mode"):
            self.cfg["run_mode"] = run_mode
            save_config(self.cfg)

        # ---- GO ----------------------------------------------------------
        self.start_btn.configure(state="disabled")
        self.pick_btn.configure(state="disabled")
        self.flatten_btn.configure(state="disabled")
        self.batch_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._reset_stats()

        self.engine = self._make_engine(api_key, model_id, reprocess=reprocess, run_config=cfg)
        self.log(pipeline.build_identity("Stage 2"))
        self.log(f"Starting review of: {self.care_home_dir}")
        self.log(f"Mode: {'Overnight Batch (50% price)' if run_mode == 'batch' else 'Live'}   "
                 f"Model: {cfg['model']}   FX: {cfg.get('fx', 0.79)}   "
                 f"Resolution: {cfg.get('resolution',1.5)}x")
        self.log(f"Limits — workers: {cfg.get('max_workers', DEFAULT_MAX_WORKERS)}, "
                 f"files: {cfg.get('max_files', DEFAULT_MAX_FILES)}, "
                 f"spend: £{float(cfg.get('max_budget_gbp', DEFAULT_MAX_BUDGET_GBP)):.2f}, "
                 f"max file: {max_file_mb:.0f} MB")
        self.log(f"Adaptive pages: {'on' if cfg.get('adaptive_pages',True) else 'off'}   "
                 f"Skip API when clear: {'on' if cfg.get('skip_when_clear',False) else 'off'}   "
                 f"Auto-Other: {'on' if cfg.get('auto_other',False) else 'off'}   "
                 f"Redact logs: {'on' if cfg.get('redact_logs',False) else 'off'}")
        if reprocess:
            self.log("Re-run mode: FORCING full re-process (cache ignored).")
        target = (self.engine.run_batch_submit if run_mode == "batch"
                  else self.engine.run)
        self.worker_thread = threading.Thread(target=target, daemon=True)
        self.worker_thread.start()
        self._poll_stats()

    def _reset_stats(self):
        for v in self.info_vars.values():
            v.set("0")

    def _poll_stats(self):
        if self.engine:
            self._update_stats(self.engine.stats)
            workers = int(self.engine.stats.get("workers", 0))
            if workers > self._last_notification_worker:
                self._last_notification_worker = workers
                total = max(workers, getattr(self, "_notification_workers_total", 0),
                            TRACKER.snapshot().get("workers_total", workers))
                self._notify("worker_complete", completed=workers, total=total)
        if self.worker_thread and self.worker_thread.is_alive():
            self.after(500, self._poll_stats)

    def _stop(self):
        if getattr(self, "_review_busy", False):
            messagebox.showinfo("AI Document Review", "This review runs in a separate CLI process. "
                "View AI session shows its current work. Stage 2's Stop button "
                "controls processing and the accuracy audit, not the external AI.", parent=self)
            return
        # Phase 1: cancel an in-progress pre-flight scan
        if getattr(self, "_scanning", False):
            if getattr(self, "_scan_cancel", None) is not None:
                self._scan_cancel.set()
            self.set_status("Cancelling scan…")
            self.stop_btn.configure(state="disabled")
            return
        # Phase 2: stop a running review
        if self.engine:
            if self.dashboard.progress.phase == "audit" and not messagebox.askyesno(
                    "Stop accuracy audit?", "This leaves an incomplete audit. "
                    "Restarting checks documents again. Stop after the current request?", parent=self):
                return
            self.engine.stop()
            self.set_status("Stopping after the current file…")
            # release a blocked unknown-dialog wait, if any
            self._unknown_result = None
            self._unknown_event.set()

    def _on_done(self, stats, status):
        self.after(0, self._done_main, dict(stats), status)

    def _done_main(self, stats, status):
        if self.worker_thread and self.worker_thread.is_alive():
            self.after(25, self._done_main, stats, status)
            return
        self._recovery_busy = False
        self._update_stats(stats)
        pending, _ = self._refresh_folder_state()
        self._refresh_run_controls(pending=pending)
        if getattr(self, "dashboard", None):
            self.dashboard.finish(stats, status)
            self._notify_done(stats, status)
            if self._closing:
                return
        if self._begin_automatic_review(stats, status):
            # Do not put a modal completion dialog in front of an unattended
            # handoff. Its result remains visible in the dashboard and reports.
            return
        kind = status.partition(":")[0] if isinstance(status, str) else status
        if (not status or kind in ("batch_applied", "batch_audit_complete")) and stats.get("audit_status") not in ("failed", "pending", "skipped"):
            self.set_progress(1, 1)

        # ---- Overnight Batch statuses (Phase A / Phase B outcomes) ----
        if isinstance(status, str) and status.startswith("batch_"):
            self._done_batch(stats, status)
            return

        limit_reason = ""
        credit_worker = ""
        credit_detail = ""
        if status == "stopped":
            self.set_status("Stopped.")
            head = "Run stopped"
        elif isinstance(status, str) and status.startswith("credit:"):
            payload = status.split("credit:", 1)[1]
            credit_worker, _, credit_detail = payload.partition("|")
            self.set_status("STOPPED — API credit exhausted.")
            head = "Stopped — API credit ran out"
        elif isinstance(status, str) and status.startswith("limit:"):
            limit_reason = status.split("limit:", 1)[1]
            self.set_status(f"Stopped — {limit_reason}.")
            head = "Run stopped at a safety limit"
        elif status:
            self.set_status("Finished with errors.")
            head = "Finished with an error"
        else:
            self.set_status("Processing complete; accuracy audit needs attention." if stats.get("audit_status") in ("failed", "pending", "skipped") else "All workers complete.")
            head = "Review complete"

        # API-credit stop gets its own clear warning dialog (not the info box).
        if credit_worker:
            done_so_far = stats.get('workers', 0)
            messagebox.showwarning(
                "API credit ran out",
                "The run STOPPED because the Anthropic API account is out of "
                "credit.\n\n"
                f"It was processing worker:\n    {credit_worker}\n"
                "when it stopped (this worker may be only partly done).\n\n"
                f"Workers fully completed before the stop: {done_so_far}\n\n"
                f"This is recorded in the 'Run Status' sheet of:\n"
                f"{RECORD_XLSX.name}\n(in {APP_DIR}).\n\n"
                "Top up the account's credit, then run again on the same folder "
                "— already-processed files are skipped, so you won't be charged "
                "twice. The worker above is where to resume."
                + (f"\n\nAPI said: {credit_detail}" if credit_detail else ""))
            return

        summary = (f"{head}\n\n"
                   + (f"Reason            : {limit_reason}\n\n" if limit_reason else "")
                   + f"Workers processed : {stats.get('workers',0)}\n"
                   f"Documents renamed : {stats.get('renamed',0)}\n"
                   f"Unknowns defined  : {stats.get('unknown',0)}\n"
                   f"Duplicates removed: {stats.get('duplicates',0)}\n"
                   f"Cached (skipped)  : {stats.get('skipped_cached',0)}\n"
                   f"Oversized skipped : {stats.get('skipped_oversized',0)}\n"
                   f"Cloud-only skipped: {stats.get('skipped_cloud',0)}\n"
                   f"Ranked (2+ copies): {stats.get('ranked',0)}\n"
                   f"Overwrite filed   : {stats.get('overwrite',0)}\n"
                   f"Bulk filed        : {stats.get('bulk',0)}\n"
                   f"CoS dated tagged  : {stats.get('cos',0)}\n"
                   f"Contracts signed  : {stats.get('contracts',0)}\n"
                   f"DBS ranked        : {stats.get('dbs',0)}\n"
                   f"ECS latest        : {stats.get('ecs',0)}\n"
                   f"BRP latest        : {stats.get('brp',0)}\n"
                   f"eVisa latest      : {stats.get('evisa',0)}\n"
                   f"NI Number best    : {stats.get('ni',0)}\n"
                   f"Share Code dated  : {stats.get('sharecode',0)}\n"
                   f"Workers moved     : {stats.get('moved',0)}\n"
                   f"Skipped (done)    : {stats.get('skipped_done',0)}\n"
                   f"Errors            : {stats.get('errors',0)}\n\n"
                   f"Records saved in:\n{APP_DIR}")
        # Part 2: estimated vs actual, side by side
        try:
            actual = self.cost_var.get()
            if self._est_at_start:
                est = self._est_at_start["live"]["gbp"]
                summary += (f"\n\nCost — estimated: ~£{est:.2f}   "
                            f"actual: {actual}")
        except Exception:
            pass
        if stats.get("audit_skipped_budget"):
            summary += ("\n\nProcessing completed, but the accuracy audit was "
                        "skipped because the remaining cumulative budget could "
                        "not cover its separate expected cost "
                        f"(~£{stats.get('audit_expected_gbp', 0):.2f}).")
        if stats.get("audit_status") in ("failed", "pending"):
            summary += "\n\nThe accuracy audit is INCOMPLETE. Its progress is not a completed review; check Details & full log before restarting it."
        n_failed = (stats.get('errors', 0) + stats.get('skipped_oversized', 0)
                    + stats.get('skipped_cloud', 0))
        if n_failed:
            summary += (f"\n\n{n_failed} file(s) were skipped or errored. "
                        f"Full paths + reasons are in:\n{FAILED_CSV.name}\n"
                        f"(in the folder above). The developer console "
                        f"(Ctrl+Shift+I) shows the exact API reason for each.")
        messagebox.showinfo("Doc Review (AI) Station", summary)

    def _done_batch(self, stats, status):
        """End-of-run handling for the Overnight Batch statuses."""
        kind, _, payload = status.partition(":")
        if kind == "batch_busy":
            self.set_status("Another Stage 2 operation is using this folder.")
            messagebox.showwarning(
                "Batch folder is already in use",
                (payload or "Another Stage 2 process is working on this folder.")
                + "\n\nWait for that operation to finish, then use Check batch "
                "status in this window. This attempt did not submit or apply "
                "anything. The saved request list and batch IDs are retained. "
                "Leave the state and lock files in place; the operation lock "
                "is released automatically when the other operation ends.")
        elif kind == "batch_submitted":
            n_req, n_batches, primary_est, total_est = (
                payload.split("|") + ["?", "?", "?", "?"])[:4]
            self.set_status("Batch submitted - you can close the app.")
            messagebox.showinfo(
                "Overnight batch submitted",
                f"Submitted {n_req} document(s) in {n_batches} batch(es).\n\n"
                f"Primary batch estimate: ~£{primary_est}\n"
                f"Cumulative enabled estimate: ~£{total_est}\n"
                f"(This includes live finishing, the optional discounted "
                f"follow-up reserve and the enabled audit.)\n\n"
                f"You can CLOSE this app now. Results are usually ready "
                f"within an hour (up to 24 hours). Re-open this folder later "
                f"and press 'Check batch status' to fetch and apply them.")
        elif kind == "batch_pending":
            try:
                c = json.loads(payload)
            except Exception:
                c = {}
            phase = c.get("phase", "primary")
            self.set_status(f"{phase.title()} batch still processing.")
            messagebox.showinfo(
                f"{phase.title()} batch still processing",
                f"The {phase} batch has not finished yet.\n\n"
                f"Succeeded so far : {c.get('succeeded', '?')}\n"
                f"Still processing : {c.get('processing', '?')}\n"
                f"Errored          : {c.get('errored', '?')}\n"
                f"Canceled         : {c.get('canceled', '?')}\n"
                f"Expired          : {c.get('expired', '?')}\n\n"
                f"Try again later with 'Check batch status'. Batches usually "
                f"finish within an hour (up to 24 hours).")
        elif kind == "batch_followup_submitted":
            n_req, est, model, n_batches = (
                payload.split("|") + ["?", "?", "?", "?"])[:4]
            self.set_status("Discounted follow-up batch submitted.")
            messagebox.showinfo(
                "Follow-up batch submitted",
                f"Submitted {n_req} genuinely unresolved document(s) once to "
                f"the discounted {model} Message Batch service in "
                f"{n_batches} guarded chunk(s).\n\n"
                f"Expected follow-up cost: ~£{est}.\n\n"
                "No worker folders have been finalised or moved. Use 'Check "
                "batch status' again after all follow-up chunks finish.")
        elif kind == "batch_followup_warning":
            n_req, est, model = (payload.split("|") + ["?", "?", "?"])[:3]
            self.set_status("Large follow-up needs confirmation.")
            approved = messagebox.askyesno(
                "Unexpectedly large follow-up batch",
                f"The primary results left {n_req} documents unresolved. "
                f"Submitting them once to the discounted {model} batch is "
                f"expected to cost ~£{est}.\n\n"
                "No follow-up has been submitted yet. Continue?")
            if approved and self.engine:
                self.engine.approve_followup_warning()
                self.after(300, self._batch_check_status)
        elif kind == "batch_followup_over_budget":
            expected, budget = (payload.split("|") + ["?", "?"])[:2]
            self.set_status("Follow-up not submitted (over budget).")
            messagebox.showwarning(
                "Follow-up not submitted - cumulative budget",
                f"The cumulative expected cost (£{expected}) exceeds the "
                f"configured £{budget} ceiling. No follow-up request was sent. "
                "The state is saved; adjust the limit and use 'Check batch "
                "status' to continue.")
        elif kind in ("batch_primary_ambiguous", "batch_primary_incomplete"):
            self.set_status("Primary submission needs recovery — use Check batch status.")
            messagebox.showwarning(
                "Primary batch submission needs recovery",
                "The primary submission stopped before all batch IDs were "
                "confirmed. This is separate from the follow-up phase.\n\n"
                "Use Check batch status to compare the saved requests with "
                "Anthropic's batch records. Stage 2 will show a recovery plan "
                "and ask before submitting any requests verified as missing. "
                "Accepted requests are retained.\n\n"
                "Keep the same care-home folder and its saved state. Start "
                "processing and Flatten remain unavailable while this batch "
                "needs recovery.")
        elif kind == "batch_recovery_blocked":
            self.set_status("Primary recovery stopped — saved state retained.")
            messagebox.showwarning(
                "Primary batch recovery needs attention",
                f"{payload or 'Recovery could not be verified.'}\n\n"
                "The saved state and any accepted batch IDs are retained. "
                "Use Check batch status to verify them before continuing. "
                "Do not start a new run or remove the state file.")
        elif kind == "batch_followup_ambiguous":
            self.set_status("Follow-up resubmission blocked.")
            messagebox.showwarning(
                "Follow-up submission needs independent review",
                "A previous follow-up submission may have reached Anthropic, "
                "but its batch id was not durably saved. Automatic resubmission "
                "is blocked to prevent duplicate billing. The state file has "
                "been retained for independent review.")
        elif kind == "batch_followup_too_large":
            self.set_status("Follow-up not submitted (size guard).")
            payload_mb = (payload.split("|", 1)[0] if payload else "?")
            messagebox.showwarning(
                "One follow-up request is too large",
                f"A rendered follow-up request was {payload_mb} MB, above the "
                "100 MB per-batch hard guard. That chunk was not submitted; "
                "the state file and any earlier accepted chunk IDs are "
                "retained. Lower the processing resolution or prepare that "
                "single source file separately, then use Check batch status.")
        elif kind == "batch_applied":
            actual, _, est = payload.partition("|")
            self.set_status("Batch results applied.")
            sp_cost = self.cost_var.get()
            messagebox.showinfo(
                "Batch results applied",
                f"Batch results have been applied.\n\n"
                f"Succeeded         : {stats.get('batch_succeeded', 0)}\n"
                f"Errored           : {stats.get('batch_errored', 0)}\n"
                f"Expired           : {stats.get('batch_expired', 0)}\n"
                f"Canceled          : {stats.get('batch_canceled', 0)}\n"
                f"Unmatched (moved) : {stats.get('batch_missing', 0)}\n"
                f"Unknowns filed    : {stats.get('unknown', 0)}\n"
                f"Renamed           : {stats.get('renamed', 0)}\n"
                f"Duplicates removed: {stats.get('duplicates', 0)}\n"
                f"Ranked (2+ copies): {stats.get('ranked', 0)}\n"
                f"Overwrite filed   : {stats.get('overwrite', 0)}\n"
                f"Bulk filed        : {stats.get('bulk', 0)}\n"
                f"Errors            : {stats.get('errors', 0)}\n\n"
                f"Batch tokens: {stats.get('batch_in_tokens', 0):,} in / "
                f"{stats.get('batch_out_tokens', 0):,} out\n"
                f"Batch cost (primary + follow-up actual @50%): ~£{actual}  "
                f"(estimated at submit: £{est})\n"
                f"Cumulative cost meter: {sp_cost}\n"
                + ("Audit skipped: remaining budget was insufficient.\n"
                   if stats.get("audit_skipped_budget") else "") + "\n"
                f"Records saved in:\n{APP_DIR}")
        elif kind == "batch_over_budget":
            est, _, budget = payload.partition("|")
            self.set_status("Batch NOT submitted (over budget).")
            messagebox.showwarning(
                "Batch not submitted - over budget",
                f"The estimated batch cost (~£{est}) exceeds your configured "
                f"budget (£{budget}).\n\nNothing was submitted or billed. "
                f"Raise 'Max spend / run' in Settings, lower the resolution, "
                f"or process fewer files, then try again.")
        elif kind == "batch_already_pending":
            self.set_status("A batch is already pending.")
            messagebox.showwarning(
                "Batch already pending",
                "This folder already has a pending batch. Apply or cancel it "
                "first ('Check batch status').")
        elif kind == "batch_none_pending":
            self.set_status("No pending batch.")
            messagebox.showinfo("Batch status",
                                "No pending batch was found for this folder.")
        elif kind == "batch_nothing_to_submit":
            self.set_status("Nothing to submit.")
            messagebox.showinfo(
                "Nothing to submit",
                "Every eligible document is already processed (cached) or was "
                "skipped. Nothing was sent.")
        elif kind == "batch_no_renderable":
            count = payload or "eligible"
            self.set_status("Batch needs file attention; nothing was sent.")
            messagebox.showwarning(
                "No batch requests submitted",
                f"No provider request was accepted. {count} eligible document(s) "
                "were missing or could not be rendered.\n\n"
                "No provider batch/classification result was submitted or "
                "applied. Preparation changes, if any, remain. There is no "
                "submitted provider batch to check or apply. Restore or repair "
                "the documents, then start the batch again.")
        else:  # batch_submit_failed / batch_apply_failed
            self.set_status("Batch operation failed.")
            messagebox.showerror(
                "Batch operation failed",
                f"{payload or kind}\n\nNothing has been lost: if a batch was "
                f"submitted its state file is kept, and already-applied files "
                f"are remembered. See the developer console (Ctrl+Shift+I) "
                f"for details.")

    # ---------------- flatten-only ----------------
    def _flatten_only(self):
        if self._batch_busy_guard():
            return
        if self.care_home_dir and has_pending_batch(self.care_home_dir):
            messagebox.showwarning(
                "Pending batch", "Finish or recover the selected batch with "
                "Check batch status before flattening folders.")
            return
        d = filedialog.askdirectory(title="Choose the care-home folder to FLATTEN",
                                    initialdir=str(desktop_path()))
        if not d:
            return
        if has_pending_batch(Path(d)):
            messagebox.showwarning(
                "Pending batch", "That folder has saved batch work. Select it "
                "with Choose care-home folder and use Check batch status "
                "before flattening it.")
            return
        FlattenTool(self, Path(d)).run()


# ====================================================================
# FLATTEN-ONLY TOOL  (the standalone "tidy folders" command)
# ====================================================================
class FlattenTool:
    """For a chosen care-home folder: for every worker sub-folder, move all
    files (from any depth) up into the worker folder, then delete the empty
    sub-folders. Result: each worker folder contains only loose files."""

    def __init__(self, app: App, care_home_dir: Path):
        self.app = app
        self.dir = care_home_dir

    def run(self):
        workers = [d for d in self.dir.iterdir() if d.is_dir()]
        if not workers:
            messagebox.showinfo("Flatten", "No worker sub-folders found in that folder.")
            return
        if not messagebox.askyesno(
                "Flatten folders",
                f"This will move every file in each of the {len(workers)} worker "
                f"sub-folders of\n\n{self.dir.name}\n\nup into the worker folder "
                "and delete the empty sub-folders (Overwrite Documents, Bulk, "
                "Batch XX, and any legacy Compliance Documents/Other folders)."
                "\n\nNothing is deleted except empty folders. Continue?"):
            return
        threading.Thread(target=self._work, args=(workers,), daemon=True).start()

    def _work(self, workers):
        total_moved = 0
        self.app.log(f"\n=== FLATTEN: {self.dir} ===")
        for w in sorted(workers, key=lambda d: natural_key(d.name)):
            try:
                moved = flatten_worker(w, self.app.log)
                total_moved += moved
                self.app.log(f"  {w.name}: flattened {moved} file(s)")
            except Exception as e:
                self.app.log(f"  ! {w.name}: {e}")
        self.app.set_status(f"Flatten complete — {total_moved} file(s) moved.")
        self.app.after(0, lambda: messagebox.showinfo(
            "Flatten complete",
            f"Done.\n\n{len(workers)} worker folder(s) flattened.\n"
            f"{total_moved} file(s) moved up.\nEmpty sub-folders removed."))


# ====================================================================
def main():
    # crisp text on 125%/150% displays - must run before the Tk root exists
    if _api_usage is not None:
        try:
            _api_usage.enable_high_dpi()
        except Exception:
            pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
