"""Offline contracts for exact automatic-review continuity in live resumes."""
import ast
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load_app import load_app


SOURCE = Path(__file__).resolve().parents[1] / "src" / "Stage2_Processing.pyw"


def method_node(class_name, method_name):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    return next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                and node.name == method_name)


def calls_in_order(node, names):
    calls = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = (child.func.attr if isinstance(child.func, ast.Attribute)
                else child.func.id if isinstance(child.func, ast.Name) else "")
        if name in names:
            calls.append((child.lineno, name))
    return [name for _line, name in sorted(calls)]


def test_live_checkpoint_is_atomic_and_records_exact_resume_scope(tmp_path):
    app = load_app()
    care = tmp_path / "Files"
    worker = tmp_path / "Processed" / "Worker One"
    care.mkdir()
    worker.mkdir(parents=True)
    assert app.write_live_checkpoint(
        care, 1, 1, "", 0, False,
        "review-run-1", [worker], processing_complete=True) is True
    saved = app.read_live_checkpoint(care)
    assert saved["version"] == 3
    assert saved["auto_review_run_id"] == "review-run-1"
    assert saved["processing_complete"] is True
    assert saved["audit_worker_dirs"] == [str(worker.resolve())]
    function = next(node for node in ast.parse(SOURCE.read_text(encoding="utf-8-sig")).body
                    if isinstance(node, ast.FunctionDef) and node.name == "write_live_checkpoint")
    assert "_write_hidden_json" in calls_in_order(function, {"_write_hidden_json"})


def test_engine_retains_checkpoint_until_audit_terminal():
    run = method_node("Engine", "run")
    ordered = calls_in_order(run, {"write_live_checkpoint", "_record_review_processing",
                                   "_run_post_run_audit", "clear_live_checkpoint"})
    # The last clear is after the normal processing receipt and audit call.
    assert ordered.index("_record_review_processing") < ordered.index("_run_post_run_audit")
    assert ordered.index("_run_post_run_audit") < len(ordered) - 1 - ordered[::-1].index("clear_live_checkpoint")
    source = ast.get_source_segment(SOURCE.read_text(encoding="utf-8-sig"), run)
    assert 'processing_complete=True' in source
    assert "LiveCheckpointWriteError" in source


def test_processing_complete_resume_is_audit_only_and_reuses_receipt():
    run = method_node("Engine", "run")
    source = ast.get_source_segment(SOURCE.read_text(encoding="utf-8-sig"), run)
    resume = source.index('_resume_processing_complete')
    worker_scan = source.index('workers = worker_dirs_in')
    assert resume < worker_scan
    assert 'audit_receipt.get("status") == "complete"' in source
    assert "resuming only" in source


def test_zero_file_preflight_allows_only_exact_saved_audit_resume():
    start = method_node("App", "_start_after_scan")
    source = ast.get_source_segment(SOURCE.read_text(encoding="utf-8-sig"), start)
    assert "resume_audit_only" in source
    gate = source.index('if scan["files"] == 0 and not resume_audit_only')
    assert source.index('_resume_review_run_id') < gate
    assert source.index('processing_complete') < gate
    assert source.index('audit_worker_dirs') < gate


def test_restored_audit_scope_is_deduplicated_and_snapshot_bounded():
    make = method_node("App", "_make_engine")
    source = ast.get_source_segment(SOURCE.read_text(encoding="utf-8-sig"), make)
    assert 'engine._review_scope_worker_names' in source
    assert 'folder.parent == root' in source
    assert 'key not in restored' in source
    run = ast.get_source_segment(
        SOURCE.read_text(encoding="utf-8-sig"), method_node("Engine", "run"))
    assert 'worker.name.casefold() in saved_scope' in run
    assert 'for old in self._audit_worker_dirs' in run
