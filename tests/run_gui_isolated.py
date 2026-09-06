"""Release verification: one fresh Python/Tcl process per GUI test.

Tk 8.6 on this Windows Python runtime intermittently fails while loading a
different existing Tcl library script after repeated interpreter teardown in a
shared pytest process. Never count those environment skips as runtime proof.
This runner treats skips, timeouts and nonzero child exits as failures.

Run from the repository root: python tests/run_gui_isolated.py
"""
import json
from pathlib import Path
import re
import subprocess
import sys
import time


MODULES = ["tests/test_stage2_compact_ui.py", "tests/test_stage2_app_ui_smoke.py", "tests/test_stage2_workflow_ui.py"]


def main():
    root = Path(__file__).resolve().parents[1]
    collection = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", *MODULES],
                                cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if collection.returncode:
        print(collection.stdout + collection.stderr, flush=True)
        return 1
    nodes = [line.strip() for line in collection.stdout.splitlines()
             if line.startswith("tests/") and "::" in line and " " not in line]
    if not nodes:
        print("ERROR: no UI tests were collected", flush=True)
        return 1
    outcomes = []
    started = time.monotonic()
    for number, node in enumerate(nodes, 1):
        try:
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rs", node], cwd=root,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            output = result.stdout + result.stderr
            skipped = bool(re.search(r"\b\d+ skipped\b|^SKIPPED", output, re.M))
            passed = result.returncode == 0 and not skipped and bool(re.search(r"\b1 passed\b", output))
            outcome = {"test": node, "passed": passed, "returncode": result.returncode, "skipped": skipped}
            if not passed:
                outcome["output"] = output
        except subprocess.TimeoutExpired:
            outcome = {"test": node, "passed": False, "timeout": True}
        outcomes.append(outcome)
        print(f"{number}/{len(nodes)} {'PASS' if outcome['passed'] else 'FAIL'} {node}", flush=True)
        if not outcome["passed"]:
            print(outcome.get("output", "Test timed out."), flush=True)
    report = {"mode": "one-fresh-process-per-test", "tests": len(outcomes),
              "passed": sum(item["passed"] for item in outcomes),
              "failed": sum(not item["passed"] for item in outcomes),
              "elapsed_seconds": round(time.monotonic() - started, 1), "outcomes": outcomes}
    print(json.dumps(report, indent=2), flush=True)
    return int(report["failed"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
