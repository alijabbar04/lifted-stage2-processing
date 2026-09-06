"""Offline benchmark for the bundled Stage 2 orientation model.

The input folder is a required command-line argument. Source files are opened
read-only; generated 0/90/180/270 variants live in a temporary directory and
are deleted at exit. This utility contains no networking or paid-API code.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
import tempfile
import time
import tracemalloc
from collections import Counter, defaultdict
from pathlib import Path

import fitz
from PIL import Image


REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from local_orientation import (  # noqa: E402
    MODEL_SHA256, OnnxOrientationPredictor,
)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
                    ".webp"}
ANGLES = (0, 90, 180, 270)


def peak_process_memory_bytes():
    """Best-effort peak resident working set, including native ONNX memory."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(
                process, ctypes.byref(counters), counters.cb):
            return int(counters.PeakWorkingSetSize)
        return 0
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)
    except Exception:
        return 0


def sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def representative_images(path: Path):
    if path.suffix.lower() == ".pdf":
        doc = fitz.open(path)
        try:
            indexes = sorted({0, len(doc) // 2, len(doc) - 1}) if len(doc) else []
            for index in indexes:
                pix = doc[index].get_pixmap(
                    matrix=fitz.Matrix(1.0, 1.0), alpha=False,
                    colorspace=fitz.csRGB)
                yield index + 1, Image.frombytes(
                    "RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
    else:
        with Image.open(path) as image:
            yield 1, image.convert("RGB")


def write_results(path: Path, rows, summary):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        details = workbook.active
        details.title = "Predictions"
        columns = list(rows[0]) if rows else ["source"]
        details.append(columns)
        for row in rows:
            details.append([row.get(column, "") for column in columns])
        sheet = workbook.create_sheet("Summary")
        sheet.append(["Metric", "Value"])
        for key, value in summary.items():
            sheet.append([key, value])
        workbook.save(path)
        return
    columns = list(rows[0]) if rows else ["source"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    summary_path = path.with_name(path.stem + "_summary.csv")
    with summary_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Metric", "Value"])
        writer.writerows(summary.items())


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Benchmark bundled page orientation locally (no network).")
    parser.add_argument("folder", type=Path,
                        help="explicitly selected non-production test folder")
    parser.add_argument("--output", type=Path, required=True,
                        help="output .csv or .xlsx path (outside source is OK)")
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--margin", type=float, default=0.20)
    args = parser.parse_args(argv)
    folder = args.folder.resolve()
    if not folder.is_dir():
        parser.error(f"folder does not exist: {folder}")

    model = REPO / "assets" / "orientation" / "inference.onnx"
    actual_hash = sha256(model)
    if actual_hash.lower() != MODEL_SHA256.lower():
        raise SystemExit("orientation model checksum mismatch")
    predictor = OnnxOrientationPredictor(model, batch_size=4)
    inputs = sorted(path for path in folder.rglob("*") if path.is_file()
                    and (path.suffix.lower() == ".pdf"
                         or path.suffix.lower() in IMAGE_EXTENSIONS))
    if not inputs:
        raise SystemExit("no supported PDF/image fixtures in selected folder")

    rows = []
    confusion = Counter()
    by_type = defaultdict(lambda: [0, 0])
    uncertain = false_rotation = 0
    started = time.perf_counter()
    tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="stage2-orientation-benchmark-") as td:
        variants = Path(td)
        for source in inputs:
            document_type = (source.parent.name
                             if source.parent != folder else source.stem)
            for page_number, upright in representative_images(source):
                batch = []
                for angle in ANGLES:
                    variant = upright.rotate(-angle, expand=True,
                                             fillcolor="white")
                    variant_path = variants / (
                        f"{len(rows):06d}-p{page_number}-{angle}.png")
                    variant.save(variant_path)
                    batch.append(variant)
                predicted = predictor.predict(batch)
                for expected, answer in zip(ANGLES, predicted):
                    actual = int(answer["predicted_orientation"])
                    is_uncertain = (float(answer["confidence"]) < args.confidence
                                    or float(answer["margin"]) < args.margin)
                    correct = actual == expected
                    confusion[(expected, actual)] += 1
                    by_type[document_type][1] += 1
                    by_type[document_type][0] += int(correct)
                    uncertain += int(is_uncertain)
                    false_rotation += int(expected == 0 and actual != 0
                                          and not is_uncertain)
                    rows.append({
                        "source": str(source), "document_type": document_type,
                        "page": page_number, "known_orientation": expected,
                        "predicted_orientation": actual,
                        "correction": answer["correction"],
                        "confidence": round(float(answer["confidence"]), 6),
                        "runner_up_confidence": round(float(
                            answer["runner_up_confidence"]), 6),
                        "margin": round(float(answer["margin"]), 6),
                        "uncertain": is_uncertain, "correct": correct,
                        "false_rotation_candidate": (
                            expected == 0 and actual != 0 and not is_uncertain),
                    })
    _current, peak_python = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_process = peak_process_memory_bytes()
    elapsed = time.perf_counter() - started
    total = len(rows)
    correct_total = sum(int(row["correct"]) for row in rows)
    type_accuracy = "; ".join(
        f"{name}={correct}/{count} ({correct/count:.1%})"
        for name, (correct, count) in sorted(by_type.items()))
    confusion_text = "; ".join(
        f"{expected}->{actual}:{confusion[(expected, actual)]}"
        for expected in ANGLES for actual in ANGLES)
    summary = {
        "input_folder": str(folder), "model_sha256": actual_hash,
        "samples": total,
        "overall_accuracy": f"{correct_total / total:.6f}",
        "per_document_type_accuracy": type_accuracy,
        "confusion_matrix": confusion_text,
        "uncertain_rate": f"{uncertain / total:.6f}",
        "false_rotation_candidates": false_rotation,
        "cpu_wall_seconds": f"{elapsed:.6f}",
        "peak_process_memory_bytes": peak_process,
        "peak_python_memory_bytes": peak_python,
        "network_or_paid_api_calls": 0,
    }
    write_results(args.output.resolve(), rows, summary)
    for key, value in summary.items():
        print(f"{key}: {value}")
    print(f"output: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
