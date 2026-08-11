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
         Vignette). Left loose, keeping any rank/date suffix on the filename
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
import platform
import datetime
import threading
import traceback
import collections
import urllib.request
import urllib.error
import urllib.parse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

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
BG        = "#0d0f12"
PANEL     = "#161a20"
PANEL2    = "#1d232b"
BORDER    = "#2a323d"
FG        = "#e8eaed"
FG_DIM    = "#9aa4b0"
ACCENT    = "#4da3ff"
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
# taken from Anthropic's public pricing. LAST VERIFIED: 2026-07-06.
# If Anthropic changes prices, edit ONLY the numbers here - every cost estimate
# and the live cost meter read from this block via MODELS_BY_ID.
#
# Opus is intentionally separated out. For a high-volume *classification*
# workflow it is far more expensive (15x the input price of Haiku) and rarely
# more accurate at this task, so it is hidden unless the user explicitly turns
# on the "advanced models" setting AND confirms an extra warning.
SAFE_MODELS = {
    "Haiku  (cheapest)":  {"id": "claude-haiku-4-5",  "in": 1.00, "out": 5.00},
    "Sonnet (balanced)":  {"id": "claude-sonnet-4-6", "in": 3.00, "out": 15.00},
}
ADVANCED_MODELS = {
    "Opus   (most able, EXPENSIVE)": {"id": "claude-opus-4-8", "in": 15.00, "out": 75.00},
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
# hard-coded elsewhere.  Ref: Anthropic Message Batches pricing, 2026-07-06.
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

# --------------------------------------------------------------------
# SAFETY / COST-CONTROL DEFAULTS
# These provide hard ceilings so a single click can never trigger an unbounded
# run. They are all user-editable in Settings but default to sensible values.
# --------------------------------------------------------------------
DEFAULT_MAX_WORKERS   = 100      # stop after this many worker folders in one run
DEFAULT_MAX_FILES     = 2000     # stop after this many files sent in one run
DEFAULT_MAX_BUDGET_GBP = 25.0    # stop when estimated spend reaches this (GBP)
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
           # physically rewrite sideways scans upright once identified
           "auto_rotate": True,
           # detect files that wrongly contain SEVERAL documents (sometimes
           # other workers') and split them before classification
           "bundle_split": True,
           # delete processing residue per worker when done (.splitbak
           # backups; .zip archives whose documents were extracted)
           "cleanup_leftovers": True,
           # OPTIONAL second accuracy check after the run: re-checks every
           # renamed document (full pages, adjudicated) and writes
           # Filename_Audit_Report.xlsx into the care-home folder
           "post_run_audit": False,
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
    cfg["auto_rotate"] = bool(cfg.get("auto_rotate", True))
    cfg["bundle_split"] = bool(cfg.get("bundle_split", True))
    cfg["cleanup_leftovers"] = bool(cfg.get("cleanup_leftovers", True))
    cfg["post_run_audit"] = bool(cfg.get("post_run_audit", False))
    if cfg.get("run_mode") not in ("live", "batch"):
        cfg["run_mode"] = "live"
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
    return cfg


def save_config(cfg: dict) -> bool:
    try:
        ensure_app_dir()
        # Defensive: never let an api_key field leak into the plaintext file.
        clean = {k: v for k, v in cfg.items() if k != "api_key"}
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
        self.path = OVERRIDE_XLSX

    def record(self, worker, original, ai_name, final_name):
        if not HAS_XLSX:
            return
        try:
            ensure_app_dir()
            if self.path.exists():
                wb = load_workbook(self.path)
                ws = wb.active
            else:
                wb = Workbook()
                ws = wb.active
                ws.title = "Overrides"
                ws.append(["timestamp", "worker", "original_filename",
                           "AI_suggested_name", "you_changed_to"])
                from openpyxl.styles import Font
                for cell in ws[1]:
                    cell.font = Font(name="Arial", bold=True)
            ws.append([datetime.datetime.now().isoformat(timespec="seconds"),
                       worker, original, ai_name, final_name])
            wb.save(self.path)
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
                produced = PdfConverter._office_to_pdf(src, src.parent, log)
                if produced and produced.exists():
                    # LibreOffice names it <stem>.pdf; move to our unique dest
                    if produced != dest:
                        try:
                            if dest.exists():
                                dest.unlink()
                            produced.rename(dest)
                        except Exception:
                            dest = produced   # use whatever LO produced
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
    def render(path: Path, zoom: float = None, pages="all", rotate: int = 0):
        """Return (list_of_b64_png, extracted_text).
        pages: "all" (a representative sample of up to MAX_PAGES - first two
        pages plus the LAST page for longer documents, so a signature/result
        page at the end is seen), "first" (page 1 only), or a list of 0-based
        page indices. rotate: degrees CLOCKWISE to rotate every rendered page
        (used by the rotation retry for sideways scans). Empty image list if
        unrenderable (text may still come back for text-based PDFs/docx/txt)."""
        if zoom is None:
            zoom = DocRender.DEFAULT_ZOOM
        ext = path.suffix.lower()
        try:
            if ext in IMG_EXT:
                # images are single-page; "first"/list still returns the image
                return DocRender._image(path, zoom, rotate)
            if ext in PDF_EXT and HAS_FITZ:
                return DocRender._pdf(path, zoom, pages, rotate)
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
    def _pdf(path: Path, zoom: float, pages, rotate: int = 0):
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
                idxs = [i for i in pages if 0 <= i < total][:DocRender.MAX_PAGES]
            mat = fitz.Matrix(zoom, zoom)
            if rotate in (90, 180, 270):
                # prerotate() is counter-clockwise; rotate is clockwise
                mat = mat.prerotate((360 - rotate) % 360)
            cap = DocRender._max_px(zoom)
            for i in idxs:
                page = doc[i]
                text.append(page.get_text())
                pix = page.get_pixmap(matrix=mat, alpha=False)
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
            # always grab text from the first dozen pages for date/signature clues
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
                         note: str = ""):
        """Build the (system, content_blocks, max_tokens) for a classification
        request. Shared by live classify() and Overnight Batch submission so BOTH
        modes send a byte-for-byte identical request (same system prompt, same
        image blocks, same max_tokens). This is the single definition of the
        classification prompt.
        (max_tokens must comfortably exceed the JSON reply: 320 used to truncate
        replies mid-'features', which parsed as {} and mis-filed the document.)"""
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
        blocks = []
        # rotation retries send MORE than MAX_PAGES images (one page rendered
        # in several orientations), so the cap is applied by the caller
        for b in imgs[:max(DocRender.MAX_PAGES, len(imgs))]:
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
                 note: str = "") -> dict:
        """LIVE classify: build the shared request and POST it synchronously.
        The fixed system prompt is prompt-cached (cache_system=True). `note` is
        an optional per-document instruction appended to the (uncached) user
        message - used by the rotation retry."""
        system, blocks, mt = self.classify_payload(vocab_block, imgs,
                                                   extracted_text, note=note)
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
        policy as _post. Returns the parsed JSON dict. Raises APIError /
        CreditExhausted like _post so callers handle failures uniformly."""
        self._check_host(url)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        attempt = 0
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
                if status in (429, 500, 502, 503, 529) and attempt < self.MAX_RETRIES:
                    import time as _t
                    _t.sleep(self.RETRY_BASE_DELAY * (2 ** attempt))
                    attempt += 1
                    continue
                raise APIError(status, label, detail)
            except urllib.error.URLError as e:
                if attempt < self.MAX_RETRIES:
                    import time as _t
                    _t.sleep(self.RETRY_BASE_DELAY * (2 ** attempt))
                    attempt += 1
                    continue
                raise APIError(0, f"network error ({getattr(e, 'reason', e)})")

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
    to plain 'Other' when there's no usable label.
    e.g. 'Other - reference request email', 'Other - bank address screenshot'."""
    if not label:
        return "Other"
    lab = re.sub(ILLEGAL, " ", str(label)).strip()
    lab = re.sub(r"\s+", " ", lab)
    # drop a leading 'other -' if the model echoed it, and any stray dashes
    lab = re.sub(r"^\s*other\s*[-:]\s*", "", lab, flags=re.I).strip(" -")
    if not lab:
        return "Other"
    # keep it a concise descriptive phrase: at most the first 6 words / 60 chars
    words = lab.split()
    lab = " ".join(words[:6])[:60].strip()
    return f"Other - {lab}" if lab else "Other"


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


def list_worker_docs(worker_dir: Path):
    """All loose document files directly representing this worker's docs,
    found recursively (so it copes whether or not sub-folders still exist)."""
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
    return n.startswith("_") or "overwrite_order" in n


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
             if p.is_file() and p.suffix.lower() in DOC_EXT]
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


def organize_worker(worker_dir: Path, log=lambda m: None):
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
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
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
                          current: str, errors: int, finished: bool):
    try:
        p = Path(care_dir) / LIVE_CHECKPOINT_NAME
        p.write_text(json.dumps({
            "version": 1,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "workers_done": done, "workers_total": total,
            "current_worker": current, "errors": errors,
            "finished": finished,
        }), encoding="utf-8")
    except Exception:
        pass


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
        return bool(self.data) and not self.data.get("applied", False) \
            and bool(self.data.get("batches"))

    def init(self, care_home: str, model_id: str, resolution: float,
             settings: dict):
        self.data = {
            "version": 1,
            "care_home": care_home,
            "model_id": model_id,
            "resolution": float(resolution),
            "settings": dict(settings or {}),
            "submitted_ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "batches": [],          # [{"id","n","status_at_submit"}]
            "requests": {},         # custom_id -> {path, worker, worker_dir, fhash, pages}
            "est_gbp": 0.0,
            "est_input_tokens": 0,
            "est_output_tokens": 0,
            "applied": False,
        }

    def add_request(self, custom_id: str, path: Path, worker_dir: Path,
                    fhash: str, pages: int):
        self.data.setdefault("requests", {})[custom_id] = {
            "path": str(path), "worker": worker_dir.name,
            "worker_dir": str(worker_dir), "fhash": fhash, "pages": int(pages),
        }

    def add_batch(self, batch_id: str, n: int, status: str):
        self.data.setdefault("batches", []).append(
            {"id": batch_id, "n": int(n), "status_at_submit": status})

    def batch_ids(self):
        return [b.get("id") for b in self.data.get("batches", []) if b.get("id")]

    def request_for(self, custom_id: str) -> dict:
        return self.data.get("requests", {}).get(custom_id)

    def save(self):
        try:
            _write_hidden_json(self.path, self.data)
        except Exception:
            traceback.print_exc()

    def mark_applied(self):
        self.data["applied"] = True
        self.data["applied_ts"] = datetime.datetime.now().isoformat(timespec="seconds")
        self.save()

    def delete(self):
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            traceback.print_exc()


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
                    if p.suffix.lower() not in exts or _is_junk(p):
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
            doc = fitz.open(path)
            try:
                for page in doc:
                    # PDF /Rotate is clockwise-on-display
                    page.set_rotation((page.rotation + deg_clockwise) % 360)
                tmp = path.with_suffix(path.suffix + ".rot_tmp")
                doc.save(tmp)
            finally:
                doc.close()
            tmp.replace(path)
            return True
        if ext in IMG_EXT and HAS_PIL:
            im = Image.open(path)
            fmt = im.format
            im = im.rotate(-deg_clockwise, expand=True)  # PIL is CCW-positive
            im.save(path, format=fmt, quality=95)
            return True
    except Exception:
        traceback.print_exc()
        # never leave a half-written temp file behind
        try:
            tmp = path.with_suffix(path.suffix + ".rot_tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return False


def detect_pdf_page_text_rotations(path: Path, max_pages: int = None) -> dict:
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
                if best and votes[best] >= 0.8 * total:
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
    rotations = {int(i): int(d) for i, d in (rotations or {}).items()
                 if int(d or 0) in (90, 180, 270)}
    if not rotations or path.suffix.lower() not in PDF_EXT or not HAS_FITZ:
        return 0
    try:
        doc = fitz.open(path)
        try:
            n = 0
            for idx, deg in rotations.items():
                if 0 <= idx < len(doc):
                    page = doc[idx]
                    page.set_rotation((page.rotation + deg) % 360)
                    n += 1
            if not n:
                return 0
            tmp = path.with_suffix(path.suffix + ".rot_tmp")
            doc.save(tmp)
        finally:
            doc.close()
        tmp.replace(path)
        return n
    except Exception:
        traceback.print_exc()
        try:
            tmp = path.with_suffix(path.suffix + ".rot_tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return 0


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


def _rotation_retry(api, vocab, path, resolution, out, emit_cost=None):
    """If the classification reply says the page image is rotated (sideways
    phone photos and scans are endemic in these files, and the model misreads
    or refuses them), retry ONCE with page 1 rendered in all four orientations
    in a single call. The retry is strictly better-informed, so its result
    replaces the first one. No-op when the reply reports rotation 0."""
    res = out.get("result") or {}
    rot = _rot_of(res)
    if not rot:
        return out
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
        imgs, text = DocRender.render(path, zoom=resolution, pages="all")
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
    out = {"result": result or {}, "used_imgs": [], "used_text": "",
           "rotation_retried": False, "escalated": False}
    out = _rotation_retry(api, vocab, path, resolution, out, emit_cost)
    return _second_opinion(escalation_api, vocab, path, resolution, out,
                           emit_cost)


BUNDLE_SCAN_SYSTEM = (
    "You are checking whether ONE scanned compliance file wrongly contains "
    "SEVERAL distinct documents concatenated together (occasionally including "
    "documents belonging to a DIFFERENT PERSON than the rest of the file).\n\n"
    "You are shown the file's pages IN ORDER, each labelled with its real page "
    "number. For EVERY page decide: does this page BEGIN a new, separate "
    "document, or does it continue the document the previous page belongs to?\n\n"
    "STRICT RULES - read carefully:\n"
    "- A new document = a different ARTEFACT (a passport page after a DBS "
    "certificate, a contract after an ID card) or the same kind of artefact "
    "for a DIFFERENT PERSON (a second passport with a different name).\n"
    "- NOT new: continuation pages of the same document - page 2+ of a "
    "contract/letter/certificate/policy, the BACK of the same card, a "
    "signature page at the end of the same form, an appendix of the same "
    "report. Repetitive multi-page forms are ONE document.\n"
    "- Judge by content: headings, letterheads, person names, document "
    "numbers, page footers ('page 2 of 3' means continuation).\n"
    "- WHEN UNCERTAIN, ANSWER false (continuation). Splitting a real "
    "multi-page document in half is worse than leaving a bundle intact.\n"
    "- Page 1 always begins the first document; never report it.\n\n"
    "Respond ONLY with JSON, no prose:\n"
    '{"multiple_documents": true|false, '
    '"starts": [<real page numbers (as labelled) that BEGIN a new separate '
    'document - never page 1; [] when the file is one document>], '
    '"note": "<one short sentence>"}'
)

BUNDLE_SCAN_ZOOM = 1.0     # low-cost render for the bundle scan
BUNDLE_SCAN_WINDOW = 10    # pages per scan call (1 overlap page carried over)

# Document types that are inherently ONE PAGE (or one card, front/back): when
# a 2-3 page PDF classifies as one of these, the extra pages are very often a
# DIFFERENT stacked document (passport + driving licence + BRP scanned into
# one file was the recurring real-world miss), so such files always get the
# cheap full bundle scan even without a classifier hint. The scan itself
# stays the sole authority on whether to split - a same-card back page or a
# multi-page bank statement simply comes back 'single document'.
_ID_PAGE_TYPES = {
    "passport", "brp", "uk driving licence", "non uk driving licence",
    "visa vignette", "national insurance number", "bank statement",
    "evisa screenshot", "share code document", "id", "id badge",
    "proof of address",
}


def _bundle_prone(result: dict) -> bool:
    """True when the classification itself suggests the file might be a
    stack of one-page documents: it matched an ID-family type, or the model
    described the content as a mixed bundle."""
    for key in ("name", "other_label", "guess"):
        v = (result.get(key) or "").strip().lower()
        if not v:
            continue
        if _norm_type(v) in _ID_PAGE_TYPES:
            return True
        if "bundle" in v or "mixed" in v:
            return True
    return False


def detect_bundle_starts(api, path: Path, emit_cost=None, log=None):
    """Scan EVERY page of a PDF for document boundaries and return the sorted
    1-indexed page numbers where a NEW separate document begins (never 1;
    empty list = single document).

    This is the authoritative bundle check: classify() only ever sees a
    <=MAX_PAGES sample, so a second worker's documents hiding at page 4+ are
    invisible to it - this scan renders ALL pages (at low zoom to keep the
    cost down) in windows of BUNDLE_SCAN_WINDOW with the previous window's
    last page repeated as context, so a document straddling a window boundary
    is still recognised as a continuation."""
    total = DocRender.page_count(path)
    if total < 2:
        return []
    starts: set = set()
    idx = 0
    prev_ctx = None            # (page_no, b64) carried into the next window
    while idx < total:
        end = min(idx + BUNDLE_SCAN_WINDOW, total)
        want = list(range(idx, end))
        # DocRender caps explicit page lists at MAX_PAGES (a classification
        # cost guard) - render in chunks so the scan really sees EVERY page
        imgs = []
        for k in range(0, len(want), DocRender.MAX_PAGES):
            chunk = want[k:k + DocRender.MAX_PAGES]
            ims, _txt = DocRender.render(path, zoom=BUNDLE_SCAN_ZOOM,
                                         pages=chunk)
            imgs.extend(ims)
        if len(imgs) != len(want):
            return []   # a page failed to render -> labels would misalign;
                        # never split on uncertain evidence
        blocks = []
        if prev_ctx is not None:
            blocks.append({"type": "text", "text":
                           f"CONTEXT ONLY - page {prev_ctx[0]} (already part "
                           f"of the current document; do NOT report it):"})
            blocks.append(api._img_block(prev_ctx[1]))
        for off, b64 in enumerate(imgs):
            blocks.append({"type": "text",
                           "text": f"Page {want[off] + 1} of {total}:"})
            blocks.append(api._img_block(b64))
        blocks.append({"type": "text", "text":
                       f"Decide for pages {want[0] + 1}-{want[-1] + 1}. "
                       "JSON only."})
        raw = api._post(BUNDLE_SCAN_SYSTEM, blocks, max_tokens=300,
                        cache_system=True)
        if emit_cost:
            emit_cost()
        data = api._json_from(raw)
        for v in (data.get("starts") or []):
            try:
                n = int(v)
            except Exception:
                continue
            if want[0] + 1 <= n <= want[-1] + 1 and n != 1:
                starts.add(n)
        if imgs:
            prev_ctx = (want[len(imgs) - 1] + 1, imgs[-1])
        idx = end
    out = sorted(starts)
    if log and out:
        log(f"      bundle scan: new documents begin at page(s) "
            f"{', '.join(map(str, out))} of {total}")
    return out


def classify_document_core(api, vocab, path, *, resolution, adaptive_pages,
                           p1_imgs=None, p1_text=None, total_pages=None,
                           emit_cost=None, escalation_api=None):
    """Classify ONE document file through the live-mode path.

    p1_imgs/p1_text/total_pages may be passed in when the caller has already
    rendered page 1 (the Engine does, for its preview); otherwise they are
    rendered here. The document's FILENAME is never sent to the model: Stage 2
    renames files to its own previous guesses, so a wrong filename becomes
    self-reinforcing evidence on any re-run.
    `emit_cost` is called after every API call (the Engine uses it to update
    the live £ meter and enforce the budget ceiling). `escalation_api` is an
    optional stronger-model client for the low-confidence second opinion.

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

    def _all_idxs():
        # mirror of DocRender's pages='all' sampling policy
        if total_pages <= DocRender.MAX_PAGES:
            return list(range(max(total_pages, 1)))
        return [0, 1, total_pages - 1]

    if total_pages is None:
        total_pages = DocRender.page_count(path)
    if p1_imgs is None and p1_text is None:
        p1_imgs, p1_text = DocRender.render(path, zoom=resolution, pages="first")
    ext = path.suffix.lower()
    out = {"result": {}, "used_imgs": p1_imgs, "used_text": p1_text,
           "total_pages": total_pages, "page1_only": False,
           "triaged": False, "triage_reason": "", "rotation_retried": False,
           "escalated": False, "page_idxs": [0]}
    if adaptive_pages and ext in PDF_EXT and total_pages > 1:
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
        used_imgs, used_text = DocRender.render(path, zoom=resolution,
                                                pages="all")
        out["used_imgs"], out["used_text"] = used_imgs, used_text
        out["page_idxs"] = _all_idxs()
        out["result"] = api.classify(vocab, used_imgs, used_text)
        if emit_cost:
            emit_cost()
        return _finish(out)
    # single page / image / adaptive off: classify page 1
    # (for single-page docs page 1 IS the whole doc)
    if ext in PDF_EXT and total_pages > 1 and not adaptive_pages:
        used_imgs, used_text = DocRender.render(path, zoom=resolution,
                                                pages="all")
        out["used_imgs"], out["used_text"] = used_imgs, used_text
        out["page_idxs"] = _all_idxs()
    out["result"] = api.classify(vocab, out["used_imgs"], out["used_text"])
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
        label = other_label or (result.get("guess") or "").strip()
        if label and label.strip().lower() not in ("", "unknown", "other"):
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
                       resolution, auto_rotate=True, log=lambda m: None,
                       emit_cost=None, check_stop=lambda: None):
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
                if i % 25 == 0:
                    log(f"  [audit] {i}/{len(docs)} checked "
                        f"({n_flag} flagged so far)")
                continue
            if not (same or (fn_other and pr_other)):
                # prospective mismatch -> adjudicate before flagging
                adj = audit_adjudicate(adjudicator_api, vocab, p, resolution,
                                       fname_base, pred_base)
                if emit_cost:
                    emit_cost()
                notes.append("adjudicated")
                # physical page-rotation fixes from the adjudicator
                if auto_rotate and adj["confidence"] >= 60:
                    fixes = {idx: d for idx, d in
                             zip(adj["page_idxs"], adj["rotations"])
                             if d in (90, 180, 270)}
                    if fixes and fix_pdf_page_rotations(p, fixes):
                        n_rot += 1
                        notes.append(f"straightened {len(fixes)} rotated "
                                     f"page(s)")
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
        except Exception as e:
            row["Review Status"] = "Unable To Determine"
            row["Notes"] = f"error: {e}"
        row["Notes"] = "; ".join(n for n in [row.get("Notes", "")] + notes
                                 if n)
        rows.append(row)
        if i % 25 == 0:
            log(f"  [audit] {i}/{len(docs)} checked "
                f"({n_flag} flagged so far)")
    # Processing reports live in the dedicated C: reports tree
    # (%LOCALAPPDATA%\Lifted\Reports\Processing Reports\<care home>) — NOT in
    # the care-home data folder (2026-07-22 suite modernisation).
    rep_dir = processing_reports_dir(Path(out_dir).name)
    xlsx = unique_path(rep_dir, AUDIT_REPORT_STEM, ".xlsx")
    _write_audit_workbook(rows, xlsx)
    # register it so the ribbon's Reports browser can find it later
    record_processing_report(xlsx, Path(out_dir).name)
    log(f"  [audit] done: {len(rows)} checked, {n_flag} flagged, "
        f"{n_rot} file(s) straightened -> {xlsx.name}")
    return rows, xlsx


def _write_audit_workbook(rows, out_path: Path):
    """Write the audit rows + a Summary sheet, flagged rows first."""
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
    wb.save(out_path)


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


class Engine:
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
                 auto_rotate: bool = True,
                 bundle_split: bool = True,
                 cleanup_leftovers: bool = True,
                 post_run_audit: bool = False):
        self.dir = care_home_dir
        self.kb = kb
        self.api = api
        # optional stronger-model client for the low-confidence second opinion
        # (None = escalation off, or primary model already this strong)
        self.escalation_api = escalation_api
        # physically rewrite sideways scans upright once identified
        self.auto_rotate = bool(auto_rotate)
        # detect+split files that wrongly contain several documents
        self.bundle_split = bool(bundle_split)
        # delete processing residue (.splitbak/.zip) per worker when done
        self.cleanup_leftovers = bool(cleanup_leftovers)
        # optional second accuracy check over everything once the run ends
        self.post_run_audit = bool(post_run_audit)
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
                      "bundles_split": 0}

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
        gbp = tokens_cost_gbp(self.api.model_id,
                              self.api.in_tokens, self.api.out_tokens)
        if self.escalation_api is not None:
            gbp += tokens_cost_gbp(self.escalation_api.model_id,
                                   self.escalation_api.in_tokens,
                                   self.escalation_api.out_tokens)
        return gbp

    def _check_budget(self):
        """Stop the whole run if the live estimated spend reaches the ceiling."""
        if self.max_budget_gbp > 0 and self._current_cost_gbp() >= self.max_budget_gbp:
            raise LimitReached(
                f"budget limit reached (£{self.max_budget_gbp:.2f})")

    def _check_file_budget(self):
        if self.max_files > 0 and self.files_sent >= self.max_files:
            raise LimitReached(
                f"file limit reached ({self.max_files} files sent)")

    # ---- physical rotation fix ----
    def _maybe_fix_rotation(self, f: Path, result: dict, page_idxs=None):
        """When auto-rotate is on, rewrite a sideways/upside-down scan so it
        opens upright. Three independent signals, cheapest first:
          - the FREE local text-direction check (PDFs with a text layer) -
            trusted on its own, it only speaks when unambiguous, and catches
            e.g. upside-down pages whose text the model read without
            reporting rotation;
          - the model's PER-PAGE 'rotations' list (one entry per page it
            saw; `page_idxs` maps entries to real pages), gated on
            confidence >= 60 - fixes mixed-orientation scans page by page;
          - the model's single page-1 'rotation', gated the same way -
            whole-file fallback (also the only option for image files).
        Returns the file's NEW content hash when it was rewritten - the
        manifest must use that, since rewriting changes the bytes - else
        None."""
        if not self.auto_rotate:
            return None
        fixed_n = 0
        # FREE per-page text check first: fixes files that mix upright and
        # rotated pages (whole-file rotation cannot), across ALL pages
        per_page = detect_pdf_page_text_rotations(f)
        if per_page:
            fixed_n = fix_pdf_page_rotations(f, per_page)
        if not fixed_n and _conf_int(result) >= 60:
            # model's per-page report for the pages it actually saw
            rots = result.get("rotations")
            if isinstance(rots, list) and page_idxs \
                    and len(rots) == len(page_idxs):
                fixes = {}
                for idx, d in zip(page_idxs, rots):
                    try:
                        d = int(float(d or 0))
                    except Exception:
                        continue
                    if d in (90, 180, 270):
                        fixes[idx] = d
                if fixes:
                    total = DocRender.page_count(f)
                    same = set(fixes.values())
                    if len(fixes) == total and len(same) == 1:
                        # every page, same turn: whole-file rotate (also
                        # covers image files, which have no per-page API)
                        if fix_file_rotation(f, same.pop()):
                            fixed_n = total
                    else:
                        fixed_n = fix_pdf_page_rotations(f, fixes)
            if not fixed_n:
                rot = _rot_of(result)
                if rot and fix_file_rotation(f, rot):
                    fixed_n = DocRender.page_count(f)
        if not fixed_n:
            return None
        self.stats["rotated_fixed"] = self.stats.get("rotated_fixed", 0) + 1
        self.log(f"    · {self._redact(f.name)}: {fixed_n} rotated page(s) "
                 f"saved upright")
        try:
            return file_hash(f)
        except Exception:
            return None

    # ---- cost ----
    def _emit_cost(self):
        gbp = self._current_cost_gbp()
        toks = self.api.in_tokens + self.api.out_tokens
        if self.escalation_api is not None:
            toks += (self.escalation_api.in_tokens
                     + self.escalation_api.out_tokens)
        self.on_cost(gbp, toks)
        # after every cost update, enforce the budget ceiling
        self._check_budget()

    # ---- optional post-run accuracy audit ----
    def _run_post_run_audit(self):
        """When the Settings toggle is on, re-check every processed document
        (run_accuracy_audit: full pages, adjudicated flags) and write
        Filename_Audit_Report.xlsx into the care-home folder (the move
        destination when move mode is on). Audit-only: nothing is renamed;
        confidently-rotated pages are straightened. A failure here never
        breaks the finished run."""
        if not self.post_run_audit or not self._audit_worker_dirs:
            return
        try:
            self.set_status("Post-run accuracy audit…")
            self.log(f"\n=== POST-RUN ACCURACY AUDIT "
                     f"({len(self._audit_worker_dirs)} worker folder(s)) ===")
            out_dir = (self.move_dest if (self.move_mode and self.move_dest)
                       else self.dir)
            adjudicator = self.escalation_api or self.api
            rows, xlsx = run_accuracy_audit(
                self.api, adjudicator, self.kb, self._audit_worker_dirs,
                out_dir, resolution=self.resolution,
                auto_rotate=self.auto_rotate, log=self.log,
                emit_cost=self._emit_cost, check_stop=self._check_stop)
            flagged = sum(1 for r in rows
                          if r["Review Status"] not in ("Correct",
                                                        "Custom Name"))
            self.stats["audited"] = len(rows)
            self.stats["audit_flagged"] = flagged
            self.log(f"audit report -> {xlsx}")
        except (StopRequested, LimitReached, CreditExhausted):
            raise
        except Exception as e:
            self.log(f"! post-run audit failed: {e} (run itself is complete)")
            traceback.print_exc()

    # ---- run ----
    def run(self):
        try:
            workers = worker_dirs_in(self.dir)
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
                self.set_progress(idx - 1, total)
                TRACKER.update(workers_done=idx - 1, current_worker=w.name,
                               status=f"Worker {idx}/{total}",
                               stats=dict(self.stats))
                # checkpoint BEFORE the worker so a crash mid-worker records
                # the exact position for the next launch's resume offer.
                write_live_checkpoint(self.dir, idx - 1, total, w.name,
                                      self.stats.get("errors", 0), False)
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
                            self.stats["moved"] += 1
                            self.log(f"  moved worker folder -> {dest}")
                            final_dir = Path(dest)
                        except Exception as e:
                            self.stats["errors"] += 1
                            self.log(f"  ! could not move {w.name} to destination: "
                                     f"{e} (left in source)")
                            traceback.print_exc()
                    self._audit_worker_dirs.append(final_dir)
                except (StopRequested, LimitReached, CreditExhausted):
                    raise
                except Exception as e:
                    self.stats["errors"] += 1
                    self.log(f"  ! error on {w.name}: {e} "
                             f"{'(left in source, not moved)' if self.move_mode else ''}")
                    traceback.print_exc()
                self._emit_cost()
            self.set_progress(total, total)
            # run completed cleanly — the checkpoint is no longer needed and
            # must not trigger a resume offer next launch.
            clear_live_checkpoint(self.dir)
            TRACKER.update(workers_done=total, current_worker="",
                           status="complete", finished=True,
                           stats=dict(self.stats))
            self.manifest.save()
            self._run_post_run_audit()
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
        # 0) CONVERT EVERYTHING TO PDF first (Stage 2). Images, Office docs,
        #    .msg/.eml and .txt become PDFs in place; existing PDFs are kept.
        if self.convert_pdf:
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
            if fhash and not self.reprocess:
                cached = self.manifest.seen(fhash, self.api.model_id, self.resolution)
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
                            escalation_api=self.escalation_api)
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

            # SHARED apply path (live + batch): resolve unknowns (asking the
            # user in live mode), name, rename, log and record for the 2nd pass.
            vocab = self._apply_classification(
                worker_dir, f, result or {}, fhash, vocab, renamed_records,
                interactive=True, page1_img=(p1_imgs[0] if p1_imgs else None),
                used_imgs=used_imgs, used_text=used_text,
                default_source=default_source)

        # 2-4) DEDUP -> SECOND PASS -> ORGANISE (shared tail, used by batch too).
        self._finish_worker(worker_dir, renamed_records)

    # ---- multi-document bundle detection + split (live + batch) ----
    def _maybe_split_bundle(self, worker_dir: Path, f: Path, result: dict,
                            vocab: str, records: list, *, interactive: bool,
                            default_source: str, unknown_queue, depth: int):
        """Detect a file that wrongly contains SEVERAL distinct documents
        (occasionally other workers' documents) and split it before filing.

        Trigger: classify() flagged 'bundle_starts' among the pages it saw,
        OR the PDF has >=4 pages (classify only ever samples 3 pages, so a
        second document hiding at page 4+ is invisible to it). The trigger is
        then VERIFIED by detect_bundle_starts(), which scans every page - a
        false trigger simply comes back 'single document' and costs one cheap
        low-zoom call.

        Returns the updated vocabulary block when the file WAS handled as a
        bundle (each part classified + applied through the normal shared
        path; the original set aside as '<name>.splitbak', never deleted), or
        None when the file is a normal single document."""
        if f.suffix.lower() not in PDF_EXT or not HAS_FITZ or not f.exists():
            return None
        total = DocRender.page_count(f)
        if total < 2:
            return None
        hinted = []
        for v in (result.get("bundle_starts") or []):
            try:
                n = int(v)
            except Exception:
                continue
            if 2 <= n <= total:
                hinted.append(n)
        # ' [doc N]' parts were already boundary-scanned when created - only
        # re-check one if the classifier itself raises a fresh suspicion
        if not hinted and " [doc " in f.stem:
            return None
        # Unhinted short files are normally skipped (classify saw every page
        # of a 2-3 pager, so no hint usually means one document) - EXCEPT
        # when the file classified as an inherently one-page ID type: a
        # 2-3 page 'Passport' is very likely a stacked passport+licence+BRP
        # scan the classifier failed to flag (the recurring real-world miss).
        # The full scan below remains the only authority on splitting, so a
        # false trigger costs one cheap low-zoom call and changes nothing.
        if not hinted and total < 4 and not _bundle_prone(result):
            return None
        label = self._redact(f.name)
        self.log(f"    · {label}: bundle check "
                 f"({total} pages{', flagged by classifier' if hinted else ''})…")
        starts = detect_bundle_starts(self.api, f, emit_cost=self._emit_cost,
                                      log=self.log)
        if not starts:
            return None
        # 0-based inclusive page segments between consecutive starts
        bounds = [1] + starts + [total + 1]
        segments = [(bounds[i] - 1, bounds[i + 1] - 2)
                    for i in range(len(bounds) - 1)]
        segments = [(a, b) for a, b in segments if b >= a]
        if len(segments) < 2:
            return None

        # ---- write the parts, then set the original aside (never delete) --
        parts = []
        try:
            src = fitz.open(str(f))
            try:
                for i, (a, b) in enumerate(segments, 1):
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
        bak = f.with_name(f.name + ".splitbak")
        try:
            if bak.exists():
                bak = unique_path(worker_dir, f.name, ".splitbak")
            f.rename(bak)
        except Exception as e:
            for p in parts:
                try:
                    p.unlink()
                except Exception:
                    pass
            self.log(f"    ! could not set aside original {label}: {e} - left intact.")
            return None
        self.stats["bundles_split"] = self.stats.get("bundles_split", 0) + 1
        self.log(f"    ✂ {label}: contained {len(segments)} documents "
                 f"(pages {', '.join(f'{a + 1}-{b + 1}' for a, b in segments)})"
                 f" - split; original kept as {bak.name}")
        try:
            self.rename_log.record(self.care_home, worker_dir.name, f.name,
                                   bak.name, "", "bundle-split")
        except Exception:
            pass

        # ---- classify + apply each part through the normal shared path ----
        for p in parts:
            self._check_stop()
            try:
                core = classify_document_core(
                    self.api, vocab, p,
                    resolution=self.resolution,
                    adaptive_pages=self.adaptive_pages,
                    emit_cost=self._emit_cost,
                    escalation_api=self.escalation_api)
                presult = core["result"] or {}
                phash = file_hash(p)
                nh = self._maybe_fix_rotation(p, presult,
                                              page_idxs=core.get("page_idxs"))
                vocab = self._apply_classification(
                    worker_dir, p, presult, nh or phash, vocab, records,
                    interactive=interactive,
                    page1_img=(core["used_imgs"][0] if core["used_imgs"] else None),
                    used_imgs=core["used_imgs"], used_text=core["used_text"],
                    default_source="bundle-split", unknown_queue=unknown_queue,
                    _bundle_depth=depth + 1)
            except (StopRequested, CreditExhausted):
                raise
            except APIError as e:
                self.log(f"    ! API error on bundle part "
                         f"{self._redact(p.name)}: {e.message} - left for a "
                         f"later pass.")
                self.failed_log.record(self.care_home, worker_dir.name, p,
                                       "bundle part API error",
                                       getattr(e, "detail", ""))
                self.stats["errors"] += 1
            except Exception as e:
                self.log(f"    ! failed on bundle part "
                         f"{self._redact(p.name)}: {e}")
                traceback.print_exc()
                self.stats["errors"] += 1
        return vocab

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
          interactive=False (batch) : an unmatched doc is auto-filed as
                                       'Other - Unknown' and, if unknown_queue is
                                       given, queued for an optional review pass.

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
                self.stats["unknown"] += 1
                name, group = "Other", "Other"
                source = "auto-other"
                self.log(f"    ? {f.name}: not matched (conf {conf}) "
                         f"-> auto-filed as Other "
                         f"({other_label or 'unlabelled'})")
            else:
                # ---- BATCH: auto-file as 'Other - Unknown' + queue for review --
                self.stats["unknown"] += 1
                name, group = "Other", "Other"
                other_label = "Unknown"
                source = "batch-auto-other"
                self.log(f"    ? {f.name}: not matched (conf {conf}) "
                         f"-> filed as 'Other - Unknown' (queued for review)")

        # OTHER group is named "Other - <descriptor>". A document matched to a
        # CONTROLLED Other-tab name (e.g. 'CoS Summary') uses that name as the
        # descriptor, so recurring Other types get one consistent filename;
        # otherwise the AI's concise label is used. Falls back to plain "Other".
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
        self._second_pass(worker_dir, renamed_records)

        # 4) ORGANISE into two sub-folders: 'Overwrite Documents' (only the
        #    OVERWRITE_TYPES, loose, for Stage 3's individual overwrite flow)
        #    and 'Bulk' (everything else, split into Batch NN folders of up to
        #    30 for bulk upload).
        org = organize_worker(worker_dir, log=self.log)
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
    # Phase B (run_batch_apply): poll the batch(es); when ended, download the
    # results and apply each one through the SAME code path as live mode
    # (_apply_classification), then run dedupe / second pass (LIVE, standard
    # price) / organise per worker. The manifest is only updated when a result
    # is APPLIED, so a crash between download and apply never marks a file done.
    # ================================================================

    # keep each submitted batch comfortably inside the API's 256 MB / 100k caps
    BATCH_SUBMIT_MAX_BYTES = 100 * 1024 * 1024   # per-chunk memory/network cap
    BATCH_SUBMIT_MAX_REQUESTS = 10_000

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
            for idx, w in enumerate(workers, 1):
                self._check_stop()
                self.set_progress(idx - 1, total)
                # move mode: a worker already in the destination is done
                if self.move_mode and (self.move_dest / w.name).exists():
                    self.stats["skipped_done"] += 1
                    self.log(f"\n=== Worker {idx}/{total}: {w.name} — already "
                             f"in destination, skipped ===")
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

            # ---- enumerate eligible documents (same skip rules as live) ----
            self.set_status("Scanning documents for the batch…")
            eligible = []   # (worker_dir, path, fhash, pages)
            for w in workers:
                self._check_stop()
                if self.move_mode and (self.move_dest / w.name).exists():
                    continue
                for f in list_worker_docs(w):
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
                    if fhash and not self.reprocess and \
                            self.manifest.seen(fhash, self.api.model_id, self.resolution):
                        self.stats["skipped_cached"] += 1
                        self.log(f"    = {self._redact(f.name)}: already processed "
                                 f"(cached - will be applied without the API)")
                        continue
                    eligible.append((w, f, fhash, DocRender.page_count(f)))
                    if self.max_files > 0 and len(eligible) >= self.max_files:
                        self.log(f"NOTE: file limit reached ({self.max_files}); "
                                 f"remaining documents are left for a later run.")
                        break
                if self.max_files > 0 and len(eligible) >= self.max_files:
                    break

            if not eligible:
                self.log("Nothing to submit - every document is cached, skipped "
                         "or missing.")
                self.on_done(self.stats, "batch_nothing_to_submit")
                return

            # ---- BUDGET GATE (submit time - a batch cannot be stopped later) --
            vocab = self.kb.vocabulary_block()
            est = estimate_run_cost_gbp(
                len(eligible), self.api.model_id, self.resolution,
                adaptive=False,   # batch sends full pages; no live triage
                vocab_block=vocab, batch=True, include_second_pass=False,
                cached_prefix=False)
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
                        "convert_pdf": self.convert_pdf})
            state.data["est_gbp"] = round(est["gbp"], 4)
            state.data["est_input_tokens"] = est["input_tokens"]
            state.data["est_output_tokens"] = est["output_tokens"]
            # the state file is the only durable record of what was submitted:
            # refuse to submit anything if it cannot be written
            state.save()
            if not state.path.exists():
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

            def _submit_chunk():
                if not chunk:
                    return
                self.set_status(f"Submitting a batch of {len(chunk)} request(s)…")
                created = self.api.submit_batch(chunk)
                bid = created.get("id", "")
                state.add_batch(bid, len(chunk),
                                created.get("processing_status", ""))
                state.save()
                self.log(f"  > submitted batch {bid} with {len(chunk)} request(s)")
                self.stats["batches"] += 1
                chunk.clear()

            for i, (w, f, fhash, pages) in enumerate(eligible, 1):
                self._check_stop()
                self.set_status(f"Rendering {i}/{len(eligible)}: "
                                f"{self._redact(f.name)}")
                if not f.exists():
                    continue
                imgs, text = DocRender.render(f, zoom=self.resolution, pages="all")
                self.set_preview(imgs[0] if imgs else None, f.name)
                if not imgs and not text:
                    self.log(f"    - {self._redact(f.name)}: cannot render - "
                             f"left unchanged")
                    self.failed_log.record(self.care_home, w.name, f,
                                           "skipped: unrenderable", "")
                    continue
                system, blocks, mt = self.api.classify_payload(vocab, imgs, text)
                # custom_id: stable, unique, derived from the content hash
                # (sha-256 hex truncated + a per-hash counter for exact copies).
                n = hash_counts.get(fhash, 0)
                hash_counts[fhash] = n + 1
                cid = f"{fhash[:56]}-{n:03d}"
                req = self.api.build_batch_request(cid, system, blocks, mt)
                state.add_request(cid, f, w, fhash, pages)
                # rough serialized size (b64 data dominates)
                req_bytes = sum(len(b.get("source", {}).get("data", ""))
                                for b in blocks if b.get("type") == "image")
                req_bytes += sum(len(b.get("text", "")) for b in blocks
                                 if b.get("type") == "text") + len(system) + 2048
                if chunk and (chunk_bytes + req_bytes > self.BATCH_SUBMIT_MAX_BYTES
                              or len(chunk) >= self.BATCH_SUBMIT_MAX_REQUESTS):
                    _submit_chunk()
                    chunk_bytes = 0
                chunk.append(req)
                chunk_bytes += req_bytes
                n_built += 1
            _submit_chunk()

            state.save()
            self.stats["batch_requests"] = n_built
            self.set_progress(total, total)
            n_batches = len(state.batch_ids())
            self.log(f"\n=== BATCH SUBMITTED: {n_built} document(s) in "
                     f"{n_batches} batch(es). Estimated cost ~£{est['gbp']:.2f} "
                     f"(50% batch discount applied). ===")
            self.log("You can close this app now. Results are usually ready "
                     "within an hour (up to 24h). Re-open the folder and press "
                     "'Check batch status' to fetch and apply them.")
            self.on_done(self.stats,
                         f"batch_submitted:{n_built}|{n_batches}|{est['gbp']:.2f}")
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
    def poll_batches(self, state: "BatchState"):
        """Fetch current status for every batch in the state file. Returns a
        list of the raw batch objects (id, processing_status, request_counts,
        results_url...)."""
        out = []
        for bid in state.batch_ids():
            out.append(self.api.get_batch(bid))
        return out

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

    def run_batch_apply(self):
        """Phase B: poll & apply. If any batch is still processing, reports the
        counts and returns. When all have ended, downloads the results JSONL,
        applies every result through _apply_classification (the SAME path live
        mode uses), auto-reviews this run's unknowns, runs dedupe + LIVE second
        pass + organise per worker, then double-checks any leftover
        'Other - Unknown' files, and deletes the state file."""
        try:
            state = BatchState(self.dir)
            if not state.exists():
                self.log("No pending batch found for this folder.")
                self.on_done(self.stats, "batch_none_pending")
                return

            # ---- poll ----
            self.set_status("Checking batch status…")
            batches = self.poll_batches(state)
            pending = [b for b in batches
                       if b.get("processing_status") != "ended"]
            counts = {"processing": 0, "succeeded": 0, "errored": 0,
                      "canceled": 0, "expired": 0}
            for b in batches:
                rc = b.get("request_counts", {}) or {}
                for k in counts:
                    counts[k] += rc.get(k, 0)
            self.log(f"Batch status: {len(batches) - len(pending)}/{len(batches)} "
                     f"ended - {counts['succeeded']} succeeded, "
                     f"{counts['errored']} errored, {counts['processing']} still "
                     f"processing, {counts['canceled']} canceled, "
                     f"{counts['expired']} expired.")
            if pending:
                self.on_done(self.stats,
                             "batch_pending:" + json.dumps(counts))
                return

            # ---- download all results, keyed by custom_id ----
            self.set_status("Downloading batch results…")
            results = {}
            for b in batches:
                url = b.get("results_url")
                if not url:
                    continue
                for line in self.api.batch_results(url):
                    cid = line.get("custom_id")
                    if cid:
                        results[cid] = line.get("result", {}) or {}
            self.log(f"Downloaded {len(results)} result(s).")

            # reverse index: content hash -> [custom_ids] (for moved files)
            by_hash = {}
            for cid, meta in state.data.get("requests", {}).items():
                by_hash.setdefault(meta.get("fhash", ""), []).append(cid)

            vocab = self.kb.vocabulary_block()
            matched_cids = set()
            self._review_unknowns_answer = None   # asked once, lazily
            unknown_queue = []

            workers = worker_dirs_in(self.dir)
            total = len(workers)
            for idx, w in enumerate(workers, 1):
                self._check_stop()
                self.set_progress(idx - 1, total)
                self.log(f"\n=== Applying results {idx}/{total}: {w.name} ===")
                self.set_status(f"Applying {idx}/{total}: {w.name}")
                self._current_worker = w.name
                try:
                    records = []
                    worker_unknowns = []
                    for f in list_worker_docs(w):
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
                            new_path = unique_path(w, safe_stem(cname), f.suffix)
                            try:
                                f.rename(new_path)
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
                        if rtype == "succeeded":
                            msg = res.get("message", {}) or {}
                            usage = msg.get("usage", {}) or {}
                            if _api_usage:
                                _api_usage.record_usage(
                                    self.api.model_id, usage, batch=True)
                            self.stats["batch_in_tokens"] += usage.get(
                                "input_tokens", 0)
                            self.stats["batch_out_tokens"] += usage.get(
                                "output_tokens", 0)
                            raw = "\n".join(
                                blk.get("text", "")
                                for blk in msg.get("content", [])
                                if blk.get("type") == "text").strip()
                            parsed = self.api._json_from(raw)
                            self.stats["batch_succeeded"] += 1
                            parsed = self._rescue_batch_result(f, parsed,
                                                               vocab)
                            # batch requests are built with pages='all', so
                            # the reply's per-page rotations map to the same
                            # sampled indices the audit uses
                            new_hash = self._maybe_fix_rotation(
                                f, parsed, page_idxs=_audit_pages_for(f))
                            if new_hash:
                                fhash = new_hash
                            vocab = self._apply_classification(
                                w, f, parsed, fhash, vocab, records,
                                interactive=False,
                                used_imgs=[], used_text="",
                                default_source="batch-AI",
                                unknown_queue=worker_unknowns)
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
                        self._emit_cost()

                    # ---- automatic individual review of this run's unknowns --
                    # (runs BEFORE _finish_worker so a resolved document is
                    # deduped/dated/organised under its real type; no dialog)
                    if worker_unknowns:
                        unknown_queue.extend(worker_unknowns)
                        self._auto_review_unknowns(
                            w, worker_unknowns, records, vocab)

                    # save what has been applied so far (crash-safe resume)
                    self.manifest.save()

                    # ---- shared tail: dedupe -> LIVE second pass -> organise --
                    if records:
                        self._finish_worker(w, records)
                    # ---- final double-check: any file STILL named
                    # 'Other - Unknown' under this worker (incl. Bulk batches
                    # from earlier runs) gets one individual live re-check,
                    # exactly like the standalone Re-check Unknowns tool
                    self._recheck_leftover_unknowns(
                        w, vocab,
                        exclude_hashes={i.get("fhash") for i in worker_unknowns
                                        if i.get("fhash")})
                    self.stats["workers"] += 1
                    final_dir = w
                    # move mode: relocate the fully-processed worker (as live)
                    if self.move_mode and records:
                        try:
                            dest = move_worker_folder(w, self.move_dest)
                            self.stats["moved"] += 1
                            self.log(f"  moved worker folder -> {dest}")
                            final_dir = Path(dest)
                        except Exception as e:
                            self.stats["errors"] += 1
                            self.log(f"  ! could not move {w.name} to "
                                     f"destination: {e} (left in source)")
                            traceback.print_exc()
                    self._audit_worker_dirs.append(final_dir)
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

            self.set_progress(total, total)
            self.manifest.save()
            self._run_post_run_audit()
            # everything applied: retire the state file
            state.mark_applied()
            state.delete()
            actual_batch_gbp = tokens_cost_gbp(
                self.api.model_id, self.stats["batch_in_tokens"],
                self.stats["batch_out_tokens"], batch=True)
            self.log(f"\n=== BATCH APPLIED: {self.stats['batch_succeeded']} "
                     f"succeeded, {self.stats['batch_errored']} errored, "
                     f"{self.stats['batch_expired']} expired, "
                     f"{self.stats['batch_canceled']} canceled, "
                     f"{self.stats['batch_missing']} unmatched ===")
            self.log(f"Batch cost (actual usage @ 50%): ~£{actual_batch_gbp:.2f}  "
                     f"(estimated at submit: £{state.data.get('est_gbp', 0):.2f}); "
                     f"second-pass (live): ~£{self._current_cost_gbp():.2f}")
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
            self.kb.add(new_name, desc,
                        "Other" if decision == "Other" else "Relevant")
            vocab = self.kb.vocabulary_block()
            if decision == "Other":
                name, group = "Other", "Other"
            else:
                name, group = new_name, "Important"
            if group == "Other":
                name = other_name(new_name if new_name != "Other" else "")
            new_path = unique_path(worker_dir, safe_stem(name), p.suffix)
            try:
                p.rename(new_path)
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
        for bid in state.batch_ids():
            try:
                r = self.api.cancel_batch(bid)
                out.append((bid, r.get("processing_status", "canceling")))
                self.log(f"  cancel requested for batch {bid}")
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
                    if p.is_file() and p.suffix.lower() in DOC_EXT]
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

        # ---- date helpers for the two dated types ----
        def cos_date_for(r, p):
            imgs, text = self._pages_for_review(r, p)
            try:
                return parse_date(self.api.cos_issue_date(imgs, text))
            except (StopRequested, LimitReached, CreditExhausted):
                raise
            except Exception as e:
                self.stats["errors"] += 1
                self.log(f"      ! {self._redact(p.name)}: CoS date unreadable ({e})")
                return None
            finally:
                self._emit_cost()

        def sc_date_for(r, p):
            imgs, text = self._pages_for_review(r, p)
            try:
                d = self.api.share_code_check(imgs, text)
                return parse_date(d.get("check_date", ""))
            except (StopRequested, LimitReached, CreditExhausted):
                raise
            except Exception as e:
                self.stats["errors"] += 1
                self.log(f"      ! {self._redact(p.name)}: Share Code date "
                         f"unreadable ({e})")
                return None
            finally:
                self._emit_cost()

        def quality_for(r, p, doc_type):
            imgs, text = self._pages_for_review(r, p)
            try:
                q = self.api.doc_quality(imgs, text, doc_type)
            except (StopRequested, LimitReached, CreditExhausted):
                raise
            except Exception as e:
                self.stats["errors"] += 1
                self.log(f"      ! {self._redact(p.name)}: quality unreadable ({e})")
                q = {"score": 0, "legible": False, "complete": False,
                     "date": "", "note": "score failed"}
            finally:
                self._emit_cost()
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
                        signed_bonus = 1 if self.api.contract_signed(imgs, text) else 0
                    except Exception:
                        signed_bonus = 0
                    finally:
                        self._emit_cost()
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
    canvas.bind_all(
        "<MouseWheel>",
        lambda e: (zoom(1.1 if e.delta > 0 else 1 / 1.1) if e.state & 0x0004
                   else canvas.yview_scroll(-1 * int(e.delta / 100), "units")))
    win.bind("<Prior>", lambda e: go(-1))
    win.bind("<Next>", lambda e: go(1))
    win.bind("<Destroy>", lambda e: (doc.close()
                                     if e.widget is win else None))
    win.after(60, fit_width)


def style_button(btn, base, hover):
    btn.configure(bg=base, fg="white", activebackground=hover,
                  activeforeground="white", relief="flat", bd=0,
                  font=UI_B, cursor="hand2", padx=14, pady=7)
    btn.bind("<Enter>", lambda e: btn.configure(bg=hover))
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
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        dwm = ctypes.windll.dwmapi
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
        self.resizable(False, True)
        self.transient(master)
        self.grab_set()
        self.after(0, lambda: style_titlebar_black(self))
        _ui = float(getattr(master, "_ui_scale", 1.0) or 1.0)
        self.geometry(f"{int(560*_ui)}x{int(760*_ui)}")

        # ---- scrollable body so all controls fit on small screens ----
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.body = tk.Frame(canvas, bg=BG)
        self.body.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.body, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        b = self.body
        pad = {"padx": 16, "pady": 6}
        tk.Label(b, text="Settings", bg=BG, fg=FG, font=UI_H).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(14, 8))

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
        self.res_label = tk.Label(rframe, text="", bg=BG, fg=FG, font=UI_B, width=10,
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

        # ---------------- CONVERSION (Stage 2) ----------------
        tk.Label(b, text="PDF conversion", bg=BG, fg=ACCENT, font=UI_B).grid(
            row=22, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 2))
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
            row=23, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 0))
        lo_state = ("LibreOffice: found"
                    if PdfConverter.has_libreoffice()
                    else "LibreOffice: NOT found (text-only fallback for Office)")
        tk.Label(b, text=lo_state, bg=BG,
                 fg=(GREEN_HI if PdfConverter.has_libreoffice() else AMBER),
                 font=("Segoe UI", 8)).grid(row=24, column=0, columnspan=2,
                                            sticky="w", padx=34, pady=(0, 0))

        # ---------------- FILE MOVEMENT ----------------
        tk.Label(b, text="File movement", bg=BG, fg=ACCENT, font=UI_B).grid(
            row=25, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 2))
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
            row=26, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 0))

        # ---------------- POST-RUN CHECKS ----------------
        tk.Label(b, text="Post-run checks", bg=BG, fg=ACCENT, font=UI_B).grid(
            row=27, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 2))
        self.audit_var = tk.BooleanVar(
            value=bool(cfg.get("post_run_audit", False)))
        tk.Checkbutton(b, text="Accuracy audit after processing: re-check "
                              "every renamed document with the AI (full "
                              "pages, second-model confirmed) and write "
                              "Filename_Audit_Report.xlsx into the care-home "
                              "folder. Audit only - nothing is renamed. "
                              "Costs roughly as much as classifying the "
                              "folder a second time.",
                       variable=self.audit_var, bg=BG, fg=FG,
                       selectcolor=PANEL2, activebackground=BG,
                       activeforeground=FG, font=UI,
                       wraplength=400, justify="left", anchor="w").grid(
            row=28, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 0))

        # buttons
        bframe = tk.Frame(b, bg=BG)
        bframe.grid(row=29, column=0, columnspan=2, sticky="e", padx=16, pady=(12, 16))
        cancel = tk.Button(bframe, text="Cancel", command=self.destroy)
        style_button(cancel, PANEL2, BORDER)
        cancel.pack(side="right", padx=(8, 0))
        save = tk.Button(bframe, text="Save", command=self._save)
        style_button(save, GREEN, GREEN_HI)
        save.pack(side="right")

        tk.Label(b, text=f"Settings saved to  {CONFIG_PATH}\n"
                         f"(the API key is NOT stored in this file)",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8), justify="left").grid(
            row=25, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 10))

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
        if None in (maxw, maxf, maxb, maxmb, secpf):
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
        self.cfg["redact_logs"] = bool(self.redact_var.get())
        self.cfg["move_mode"] = bool(self.move_var.get())
        self.cfg["convert_pdf"] = bool(self.convert_var.get())
        self.cfg["post_run_audit"] = bool(self.audit_var.get())
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
                    for p in sub.glob("*.xlsx"):
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
        FX_RATE[0] = self.cfg.get("fx", 0.79)
        self.kb = KnowledgeBase()

        self.title("Stage 2 — Processing  (Doc Review AI)")
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

        # thread-safe bridge for blocking unknown-dialog
        self._unknown_event = threading.Event()
        self._unknown_result = None

        self._build_ui()
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
        # top bar (black ribbon)
        top = tk.Frame(self, bg=RIBBON, height=64)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)

        tk.Label(top, text="Stage 2 — Processing", bg=RIBBON, fg=RIBBON_FG,
                 font=("Segoe UI", 16, "bold")).pack(side="left", padx=18)

        # small, discoverable hint for the developer console shortcut
        hint = tk.Label(top, text="Console: Ctrl+Shift+I", bg=RIBBON, fg=FG_DIM,
                        font=("Segoe UI", 8), cursor="hand2")
        hint.pack(side="left", padx=(0, 12))
        hint.bind("<Button-1>", self._toggle_console)

        cog = tk.Button(top, text="⚙  Settings", command=self._open_settings)
        style_button(cog, "#1d232b", BORDER)
        cog.pack(side="right", padx=12)

        guide_btn = tk.Button(top, text="\U0001F4D6  Guide",
                              command=lambda: open_stage_guide(self))
        style_button(guide_btn, "#1d232b", BORDER)
        guide_btn.pack(side="right", padx=(4, 0))

        tools_btn = tk.Button(top, text="\U0001F9F0  Tools",
                              command=self._open_tools)
        style_button(tools_btn, "#1d232b", BORDER)
        tools_btn.pack(side="right", padx=(4, 0))

        api_btn = tk.Button(top, text="\U0001F4CA  API Usage",
                            command=self._open_api_analytics)
        style_button(api_btn, "#1d232b", BORDER)
        api_btn.pack(side="right", padx=(4, 0))

        reports_btn = tk.Button(top, text="\U0001F4D1  Reports",
                                command=self._open_reports)
        style_button(reports_btn, "#1d232b", BORDER)
        reports_btn.pack(side="right", padx=(4, 0))

        jobs_btn = tk.Button(top, text="\U0001F4C8  Jobs",
                             command=self._open_jobs)
        style_button(jobs_btn, "#1d232b", BORDER)
        jobs_btn.pack(side="right", padx=(4, 0))

        self.pick_btn = tk.Button(top, text="\U0001F4C1  Choose care-home folder",
                                  command=self._pick_folder)
        style_button(self.pick_btn, ACCENT, "#6fb6ff")
        self.pick_btn.pack(side="right", padx=4)

        # env banner
        self.banner = tk.Label(self, text="", bg=BG, fg=AMBER, font=("Segoe UI", 9),
                               anchor="w", justify="left")
        self.banner.pack(side="top", fill="x", padx=18, pady=(6, 0))

        # body: left preview | right log + controls
        body = tk.Frame(self, bg=BG)
        body.pack(side="top", fill="both", expand=True, padx=12, pady=10)

        # left - preview
        left = tk.Frame(body, bg=PANEL, width=440)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="Current document", bg=PANEL, fg=FG_DIM,
                 font=UI_B).pack(anchor="w", padx=14, pady=(12, 4))
        self.preview_name = tk.Label(left, text="—", bg=PANEL, fg=FG, font=UI,
                                     wraplength=410, justify="left")
        self.preview_name.pack(anchor="w", padx=14)
        self.preview_canvas = tk.Label(left, bg=PANEL2, text="(no document)",
                                       fg=FG_DIM)
        self.preview_canvas.pack(fill="both", expand=True, padx=14, pady=14)
        self._preview_ref = None

        # right - controls + info + log
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        # selected folder
        self.folder_lbl = tk.Label(right, text="No folder selected.", bg=BG,
                                   fg=FG, font=UI, anchor="w", justify="left",
                                   wraplength=620)
        self.folder_lbl.pack(anchor="w", fill="x")

        # control buttons
        ctl = tk.Frame(right, bg=BG)
        ctl.pack(anchor="w", fill="x", pady=(10, 6))
        self.start_btn = tk.Button(ctl, text="\u25B6  Start processing",
                                   command=self._start, state="disabled")
        style_button(self.start_btn, GREEN, GREEN_HI)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = tk.Button(ctl, text="\u25A0  Stop", command=self._stop,
                                  state="disabled")
        style_button(self.stop_btn, RED, RED_HI)
        self.stop_btn.pack(side="left", padx=(0, 8))
        self.flatten_btn = tk.Button(ctl, text="\U0001F5C2  Flatten folders only",
                                     command=self._flatten_only)
        style_button(self.flatten_btn, PANEL2, BORDER)
        self.flatten_btn.pack(side="left")
        self.batch_btn = tk.Button(ctl, text="⏳  Check batch status",
                                   command=self._batch_check_status,
                                   state="disabled")
        style_button(self.batch_btn, AMBER, AMBER_HI)
        self.batch_btn.pack(side="left", padx=(8, 0))

        # progress
        self.progress = ttk.Progressbar(right, mode="determinate", length=200)
        self.progress.pack(anchor="w", fill="x", pady=(4, 2))
        self.status_lbl = tk.Label(right, text="Idle.", bg=BG, fg=FG_DIM,
                                   font=UI, anchor="w")
        self.status_lbl.pack(anchor="w", fill="x")

        # session info panel
        info = tk.Frame(right, bg=PANEL)
        info.pack(anchor="w", fill="x", pady=(8, 8))
        tk.Label(info, text="Session information", bg=PANEL, fg=FG_DIM,
                 font=UI_B).grid(row=0, column=0, columnspan=4, sticky="w",
                                 padx=12, pady=(8, 4))
        self.info_vars = {}
        fields = [("Workers done", "workers"), ("Converted to PDF", "converted"),
                  ("Renamed", "renamed"),
                  ("Unknowns defined", "unknown"),
                  ("Ranked (2+ copies)", "ranked"),
                  ("Overwrite filed", "overwrite"), ("Bulk filed", "bulk"),
                  ("CoS dated", "cos"), ("Contracts signed", "contracts"),
                  ("DBS best", "dbs"),
                  ("ECS latest", "ecs"), ("BRP latest", "brp"),
                  ("eVisa latest", "evisa"), ("NI Number best", "ni"),
                  ("Share Code dated", "sharecode"),
                  ("Duplicates removed", "duplicates"),
                  ("Convert failed", "convert_failed"),
                  ("Cached (skipped)", "skipped_cached"),
                  ("Oversized skipped", "skipped_oversized"),
                  ("Page-1 only", "page1_only"), ("API skipped", "skipped_api"),
                  ("Errors", "errors")]
        n_field_rows = (len(fields) + 3) // 4   # 4 columns per row
        for i, (label, key) in enumerate(fields):
            r, c = divmod(i, 4)
            cell = tk.Frame(info, bg=PANEL)
            cell.grid(row=1 + r, column=c, sticky="w", padx=12, pady=2)
            tk.Label(cell, text=label, bg=PANEL, fg=FG_DIM,
                     font=("Segoe UI", 8)).pack(anchor="w")
            v = tk.StringVar(value="0")
            self.info_vars[key] = v
            tk.Label(cell, textvariable=v, bg=PANEL, fg=FG,
                     font=("Segoe UI", 13, "bold")).pack(anchor="w")

        cost_cell = tk.Frame(info, bg=PANEL)
        cost_cell.grid(row=1 + n_field_rows, column=0, columnspan=4, sticky="w",
                       padx=12, pady=(4, 10))
        tk.Label(cost_cell, text="Estimated API cost (whole session)", bg=PANEL,
                 fg=FG_DIM, font=("Segoe UI", 8)).pack(anchor="w")
        self.cost_var = tk.StringVar(value="£0.0000")
        tk.Label(cost_cell, textvariable=self.cost_var, bg=PANEL, fg=GREEN_HI,
                 font=("Segoe UI", 18, "bold")).pack(side="left")
        self.token_var = tk.StringVar(value="0 tokens")
        tk.Label(cost_cell, textvariable=self.token_var, bg=PANEL, fg=FG_DIM,
                 font=UI).pack(side="left", padx=(12, 0))

        # log
        tk.Label(right, text="Activity log", bg=BG, fg=FG_DIM, font=UI_B).pack(
            anchor="w", pady=(2, 2))
        logframe = tk.Frame(right, bg=BG)
        logframe.pack(fill="both", expand=True)
        self.log_text = tk.Text(logframe, bg="#0a0c0f", fg=FG, font=MONO,
                                relief="flat", wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logframe, command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)

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
        _api_usage.open_analytics_window(self, "Stage 2 Processing",
                                         tool_apps=tools)

    def _open_jobs(self):
        """Open (or focus) the Processing-jobs dashboard."""
        if getattr(self, "_jobs_win", None) is not None \
                and self._jobs_win.winfo_exists():
            self._jobs_win.deiconify()
            self._jobs_win.lift()
            return
        self._jobs_win = JobsDialog(self)

    def _open_reports(self):
        """Open (or focus) the Processing-reports browser."""
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
        SettingsDialog(self, dict(self.cfg), self._on_settings_saved)

    def _on_settings_saved(self, cfg):
        self.cfg = cfg
        FX_RATE[0] = cfg.get("fx", 0.79)
        self._refresh_env_banner()

    # ---------------- folder ----------------
    def _pick_folder(self):
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

        workers = [x for x in self.care_home_dir.iterdir() if x.is_dir()]
        label = (f"Care home:  {self.care_home_dir.name}\n"
                 f"Path:  {self.care_home_dir}\n"
                 f"{len(workers)} worker sub-folder(s) found.")
        if self.cfg.get("move_mode") and self.move_dest:
            label += f"\nMove mode: processed workers → {self.move_dest}"

        # ---- Overnight Batch: detect a pending submission for this folder ----
        pend = has_pending_batch(self.care_home_dir)
        if pend:
            n_req = len(pend.get("requests", {}))
            n_b = len(pend.get("batches", []))
            label += (f"\n⏳ PENDING BATCH: {n_req} document(s) in {n_b} "
                      f"batch(es), submitted {pend.get('submitted_ts', '?')} "
                      f"(est. £{pend.get('est_gbp', 0):.2f}).")
            self.batch_btn.configure(state="normal")
        else:
            self.batch_btn.configure(state="disabled")

        # ---- Crash recovery: an unfinished LIVE run leaves a checkpoint ----
        ckpt = read_live_checkpoint(self.care_home_dir)
        if ckpt and not ckpt.get("finished"):
            label += (f"\n▶ UNFINISHED RUN: stopped at worker "
                      f"{ckpt.get('workers_done', 0) + 1}/"
                      f"{ckpt.get('workers_total', '?')} "
                      f"('{ckpt.get('current_worker', '?')}', "
                      f"{ckpt.get('ts', '?')}). Press Start to resume — "
                      f"completed documents are skipped automatically.")
        self.folder_lbl.configure(text=label)
        self.start_btn.configure(state="normal")

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

        if pend:
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
        self.after(0, lambda: self.status_lbl.configure(text=msg))

    def set_progress(self, done, total):
        def upd():
            self.progress.configure(maximum=max(total, 1), value=done)
        self.after(0, upd)

    def set_preview(self, b64img, name):
        self.after(0, self._set_preview_main, b64img, name)

    def _set_preview_main(self, b64img, name):
        self.preview_name.configure(text=name or "—")
        if b64img and HAS_PIL:
            try:
                from io import BytesIO
                im = Image.open(BytesIO(base64.b64decode(b64img)))
                im.thumbnail((400, 520))
                self._preview_ref = ImageTk.PhotoImage(im)
                self.preview_canvas.configure(image=self._preview_ref, text="")
                return
            except Exception:
                pass
        self.preview_canvas.configure(image="", text="(preview unavailable)")
        self._preview_ref = None

    def on_cost(self, gbp, tokens):
        def upd():
            self.cost_var.set(f"£{gbp:.4f}")
            self.token_var.set(f"{tokens:,} tokens")
        self.after(0, upd)

    def _update_stats(self, stats):
        for k, v in self.info_vars.items():
            v.set(str(stats.get(k, 0)))

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

    def _make_engine(self, api_key: str, model_id: str, reprocess=False) -> Engine:
        """Build an Engine wired to this window (shared by live + batch runs)."""
        api = ClaudeAPI(api_key, model_id)
        # Second opinion: a stronger-model client used ONLY for documents the
        # primary model can't settle (no match / confidence < threshold).
        # Skipped when the primary model is already at least that strong.
        escalation_api = None
        if bool(self.cfg.get("second_opinion", True)):
            prim = MODELS_BY_ID.get(model_id, {})
            strong = MODELS_BY_ID.get(SECOND_OPINION_MODEL_ID, {})
            if (model_id != SECOND_OPINION_MODEL_ID
                    and prim.get("in", 99.0) < strong.get("in", 0.0)):
                escalation_api = ClaudeAPI(api_key, SECOND_OPINION_MODEL_ID)
        return Engine(
            self.care_home_dir, self.kb, api, self.care_home_dir.name,
            log=self.log, set_status=self.set_status,
            set_progress=self.set_progress, set_preview=self.set_preview,
            ask_unknown=self.ask_unknown, on_cost=self.on_cost,
            on_done=self._on_done,
            resolution=self.cfg.get("resolution", 1.5),
            skip_when_clear=self.cfg.get("skip_when_clear", False),
            adaptive_pages=self.cfg.get("adaptive_pages", True),
            auto_other=self.cfg.get("auto_other", False),
            max_workers=int(self.cfg.get("max_workers", DEFAULT_MAX_WORKERS)),
            max_files=int(self.cfg.get("max_files", DEFAULT_MAX_FILES)),
            max_budget_gbp=float(self.cfg.get("max_budget_gbp",
                                              DEFAULT_MAX_BUDGET_GBP)),
            max_file_mb=float(self.cfg.get("max_file_mb", DEFAULT_MAX_FILE_MB)),
            redact_logs=bool(self.cfg.get("redact_logs", False)),
            reprocess=reprocess,
            convert_pdf=bool(self.cfg.get("convert_pdf", True)),
            move_mode=bool(self.cfg.get("move_mode", False)),
            move_dest=self.move_dest,
            review_unknowns=self.review_unknowns,
            escalation_api=escalation_api,
            auto_rotate=bool(self.cfg.get("auto_rotate", True)),
            bundle_split=bool(self.cfg.get("bundle_split", True)),
            cleanup_leftovers=bool(self.cfg.get("cleanup_leftovers", True)),
            post_run_audit=bool(self.cfg.get("post_run_audit", False)))

    def _batch_busy_guard(self) -> bool:
        """True (and warns) if a run/scan is already in progress."""
        if (self.worker_thread and self.worker_thread.is_alive()) \
                or getattr(self, "_scanning", False):
            messagebox.showwarning(
                "Busy", "Another run or scan is already in progress.")
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
            self.batch_btn.configure(state="disabled")
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
        if not self.care_home_dir:
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

        model_id = MODELS[self.cfg["model"]]["id"]
        max_file_mb = float(self.cfg.get("max_file_mb", DEFAULT_MAX_FILE_MB))

        # ---- PRE-FLIGHT SCAN (no API calls) on a BACKGROUND thread ---------
        # The scan walks the whole care-home tree, which can be slow on very
        # large or cloud-synced (OneDrive) cohorts. Running it off the UI thread
        # keeps the window responsive; results are handed back to
        # _start_after_scan on the Tk thread. The scan is CANCELLABLE via the
        # Stop button (which sets self._scan_cancel).
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
                            if self.cfg.get("convert_pdf", True) else None)
                scan = preflight_scan(care_dir, max_file_mb,
                                      cancel_event=cancel, progress_cb=_progress,
                                      scan_ext=scan_ext)
                already = 0
                if not scan.get("cancelled"):
                    already = has_existing_batches(care_dir, cancel_event=cancel)
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
        try:
            self.progress.stop()
        except Exception:
            pass
        self.progress.configure(mode="determinate", value=0)
        self.pick_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        self.flatten_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.set_status("Idle.")

    def _start_after_scan(self, api_key, model_id, max_file_mb, scan, already, error):
        # back on the UI thread now; the scan thread has finished
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

        if scan["files"] == 0:
            messagebox.showinfo(
                "Nothing to do",
                "No eligible documents were found to send.\n\n"
                f"Worker folders: {scan['workers']}\n"
                f"Skipped (too big): {len(scan['oversized'])}")
            self._scan_ui_reset()
            return

        # ---- Part-2 estimator: page/resolution-aware Live vs Batch figures ---
        zoom = float(self.cfg.get("resolution", 1.5))
        adaptive = bool(self.cfg.get("adaptive_pages", True))
        vocab_block = self.kb.vocabulary_block()
        est_live = estimate_run_cost_gbp(
            scan["files"], model_id, zoom, adaptive, vocab_block,
            batch=False, include_second_pass=True, cached_prefix=True)
        est_batch = estimate_run_cost_gbp(
            scan["files"], model_id, zoom, False, vocab_block,
            batch=True, include_second_pass=True, cached_prefix=False)
        est_gbp = est_live["gbp"]
        self._est_at_start = {"live": est_live, "batch": est_batch,
                              "files": scan["files"]}
        size_mb = scan["bytes"] / (1024 * 1024)
        # Time estimate is based on the number of files that will ACTUALLY be
        # sent this run - capped by the per-run file limit, since the run stops
        # there. So the figure matches what will really happen.
        sec_per_file = float(self.cfg.get("sec_per_file", DEFAULT_SEC_PER_FILE))
        files_this_run = min(scan["files"],
                             int(self.cfg.get("max_files", DEFAULT_MAX_FILES)))
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
        if self.cfg.get("move_mode") and self.move_dest:
            lines.append(f"Move processed to   : {self.move_dest}")
        if scan["oversized"]:
            lines.append(f"Skipped (too big)   : {len(scan['oversized'])} "
                         f"(> {max_file_mb:.0f} MB)")
        if scan.get("cloud_only"):
            lines.append(f"Skipped (cloud-only): {len(scan['cloud_only'])} "
                         f"(not downloaded)")
        lines += [
            "",
            f"Model               : {self.cfg['model']}",
            "",
            "--- Estimated cost (rough; billed on real usage) ---",
            f"Live mode           : ~£{est_live['primary_gbp']:.2f}"
            f"  (prompt caching applied)",
            f"Overnight Batch     : ~£{est_batch['primary_gbp']:.2f}"
            f"  (50% batch discount applied)",
            f"Second pass (both)  : ~£{est_live['second_pass_gbp']:.2f}"
            f"  (dating/ranking - always LIVE at standard price)",
            f"Assumptions         : {self.cfg['model']}, {zoom:.1f}x resolution, "
            f"{'adaptive pages (live)' if adaptive else 'all pages'}; "
            f"~{EST_OUTPUT_TOKENS_PER_DOC} output tokens/doc",
            "",
            f"Estimated time      : ~{human_duration(est_secs)}"
            f"  (live mode, {files_this_run} files @ {sec_per_file:g}s; batch "
            f"results take ~1h-24h)",
            f"Hard spend limit    : £{float(self.cfg.get('max_budget_gbp', DEFAULT_MAX_BUDGET_GBP)):.2f}",
            f"Hard file limit     : {int(self.cfg.get('max_files', DEFAULT_MAX_FILES))} files",
            f"Hard worker limit   : {int(self.cfg.get('max_workers', DEFAULT_MAX_WORKERS))} workers",
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
                             f"for this folder - apply or cancel it first "
                             f"('Check batch status').")
        dlg = ConfirmReviewDialog(self, overview, sections, warn,
                                  mode_choice=self.cfg.get("run_mode", "live"),
                                  batch_blocked=batch_blocked)
        self.wait_window(dlg)
        if not dlg.result:
            self._scan_ui_reset()
            return
        run_mode = dlg.mode if not batch_blocked else "live"
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

        self.engine = self._make_engine(api_key, model_id, reprocess=reprocess)
        self.log(f"Starting review of: {self.care_home_dir}")
        self.log(f"Mode: {'Overnight Batch (50% price)' if run_mode == 'batch' else 'Live'}   "
                 f"Model: {self.cfg['model']}   FX: {FX_RATE[0]}   "
                 f"Resolution: {self.cfg.get('resolution',1.5)}x")
        self.log(f"Limits — workers: {self.cfg.get('max_workers', DEFAULT_MAX_WORKERS)}, "
                 f"files: {self.cfg.get('max_files', DEFAULT_MAX_FILES)}, "
                 f"spend: £{float(self.cfg.get('max_budget_gbp', DEFAULT_MAX_BUDGET_GBP)):.2f}, "
                 f"max file: {max_file_mb:.0f} MB")
        self.log(f"Adaptive pages: {'on' if self.cfg.get('adaptive_pages',True) else 'off'}   "
                 f"Skip API when clear: {'on' if self.cfg.get('skip_when_clear',False) else 'off'}   "
                 f"Auto-Other: {'on' if self.cfg.get('auto_other',False) else 'off'}   "
                 f"Redact logs: {'on' if self.cfg.get('redact_logs',False) else 'off'}")
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
        if self.worker_thread and self.worker_thread.is_alive():
            self.after(500, self._poll_stats)

    def _stop(self):
        # Phase 1: cancel an in-progress pre-flight scan
        if getattr(self, "_scanning", False):
            if getattr(self, "_scan_cancel", None) is not None:
                self._scan_cancel.set()
            self.set_status("Cancelling scan…")
            self.stop_btn.configure(state="disabled")
            return
        # Phase 2: stop a running review
        if self.engine:
            self.engine.stop()
            self.set_status("Stopping after the current file…")
            # release a blocked unknown-dialog wait, if any
            self._unknown_result = None
            self._unknown_event.set()

    def _on_done(self, stats, status):
        self.after(0, self._done_main, dict(stats), status)

    def _done_main(self, stats, status):
        self._update_stats(stats)
        self.start_btn.configure(state="normal")
        self.pick_btn.configure(state="normal")
        self.flatten_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        # batch button reflects whether this folder still has a pending batch
        try:
            self.batch_btn.configure(
                state=("normal" if self.care_home_dir
                       and has_pending_batch(self.care_home_dir) else "disabled"))
        except Exception:
            pass
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
            self.set_status("All workers complete.")
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
        if kind == "batch_submitted":
            n_req, n_batches, est = (payload.split("|") + ["?", "?", "?"])[:3]
            self.set_status("Batch submitted - you can close the app.")
            messagebox.showinfo(
                "Overnight batch submitted",
                f"Submitted {n_req} document(s) in {n_batches} batch(es).\n\n"
                f"Estimated cost: ~£{est}  (50% batch discount applied; the "
                f"second pass runs live at standard price when results are "
                f"applied).\n\n"
                f"You can CLOSE this app now. Results are usually ready "
                f"within an hour (up to 24 hours). Re-open this folder later "
                f"and press 'Check batch status' to fetch and apply them.")
        elif kind == "batch_pending":
            try:
                c = json.loads(payload)
            except Exception:
                c = {}
            self.set_status("Batch still processing.")
            messagebox.showinfo(
                "Batch still processing",
                f"The batch has not finished yet.\n\n"
                f"Succeeded so far : {c.get('succeeded', '?')}\n"
                f"Still processing : {c.get('processing', '?')}\n"
                f"Errored          : {c.get('errored', '?')}\n"
                f"Canceled         : {c.get('canceled', '?')}\n"
                f"Expired          : {c.get('expired', '?')}\n\n"
                f"Try again later with 'Check batch status'. Batches usually "
                f"finish within an hour (up to 24 hours).")
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
                f"Batch cost (actual @50%): ~£{actual}  "
                f"(estimated at submit: £{est})\n"
                f"Second pass (live): {sp_cost}\n\n"
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
        d = filedialog.askdirectory(title="Choose the care-home folder to FLATTEN",
                                    initialdir=str(desktop_path()))
        if not d:
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
