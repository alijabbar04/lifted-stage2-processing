"""Offline contract tests: no AI calls, account switches or worker mutations."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import stage2_ai_workflows as wf


@pytest.fixture
def setup(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in wf.RULE_FILES:
        (assets / name).write_text("# " + name, encoding="utf-8")
    documents = tmp_path / "Care home [Processed]"
    documents.mkdir()
    processing = tmp_path / "Care home [Files]"
    processing.mkdir()
    audit = tmp_path / "Filename_Audit_Report.csv"
    audit.write_text("file,verdict\na.pdf,LikelyMisnamed\n", encoding="utf-8")
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / ".git").mkdir()
    (source / "src" / "Stage2_Processing.pyw").write_text("# source", encoding="utf-8")
    (source / "src" / "ai_review.py").write_text("# transaction helper", encoding="utf-8")
    ledger = tmp_path / "Master_Filename_Review_Ledger.xlsx"
    ledger.write_bytes(b"existing ledger must not be touched")
    (ledger.parent / "review_records.jsonl").write_text("", encoding="utf-8")
    account = wf.Account("test", "codex", "Personal", tmp_path / "codex-home", "test@example.com")
    account.config_dir.mkdir()
    verified = {"provider": "codex", "email": "test@example.com", "model": "gpt-5.6-luna", "effort": "high", "status": "verified", "executable": "codex.exe"}
    monkeypatch.setattr(wf, "validate_selection", lambda *a, **k: verified.copy())
    return SimpleNamespace(account=account, audit=audit, documents=documents, processing=processing, source=source, ledger=ledger,
                           kwargs=dict(audit_report=audit, document_root=documents, care_home="Care home", source_root=source,
                                       assets_root=assets, workspace_root=tmp_path / "context", ledger_path=ledger,
                                       misnaming_path=tmp_path / "Misnaming Record.xlsx", processing_root=processing, completed_audit=True))


def test_account_environment_process_local_and_no_default_claude_redirect(tmp_path):
    account = wf.Account("1", "claude", "Default", Path.home() / ".claude")
    original = {"PATH": "x", "CLAUDE_CONFIG_DIR": "wrong", "CODEX_HOME": "wrong", "ANTHROPIC_API_KEY": "secret",
                "OPENAI_API_KEY": "secret2", "NODE_OPTIONS": "injected", "ELECTRON_RUN_AS_NODE": "1", "KEEP": "yes"}
    env = wf.account_environment(account, original)
    assert env == {"PATH": "x", "KEEP": "yes"}
    assert original["CLAUDE_CONFIG_DIR"] == "wrong"
    isolated = wf.Account("2", "claude", "Personal", tmp_path)
    assert wf.account_environment(isolated, {}) == {"CLAUDE_CONFIG_DIR": str(tmp_path)}


def test_discovery_reads_only_registered_metadata(tmp_path, monkeypatch):
    manager = tmp_path / "manager"
    manager.mkdir()
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "test@example.com"}}))
    (profile / ".credentials.json").write_text("DO NOT READ THIS")
    (manager / "profiles.json").write_text(json.dumps({"profiles": [{"id": "1", "name": "Personal", "configDir": str(profile)},
                                                                               {"id": "duplicate", "configDir": str(profile)}]}))
    codex = tmp_path / "codex"
    codex.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex))
    accounts = wf.discover_accounts(manager, [str(codex)])
    assert len(accounts) == 2
    assert accounts[0].email == "test@example.com"
    assert accounts[1].provider == "codex"
    assert accounts[1].email == ""


def test_shared_context_different_provider_same_role(setup):
    first = wf.prepare_workflow("audit-review", setup.account, "luna", **setup.kwargs)
    memory = first.workspace / "MEMORY.md"
    memory.write_text("verified prior lesson")
    account = wf.Account("claude-test", "claude", "Personal", setup.account.config_dir, "test@example.com")
    second = wf.prepare_workflow("audit-review", account, "opus", preflight=False, **setup.kwargs)
    assert first.workspace == second.workspace
    assert first.request_dir != second.request_dir
    assert memory.read_text() == "verified prior lesson"
    assert (first.workspace / "AGENTS.md").read_text() == (first.workspace / "CLAUDE.md").read_text()
    assert first.prompt_file.exists() and second.prompt_file.exists()
    assert second.command.startswith('/stage2-review-audit "')
    assert second.provider_args[-2:] == ["--", second.command]
    assert not first.command.startswith("/")
    for name in wf.RULE_FILES:
        assert (first.request_dir / "context" / name).read_bytes() == (second.request_dir / "context" / name).read_bytes()
    assert setup.ledger.read_bytes() == b"existing ledger must not be touched"


def test_roles_separate_memory_and_authority(setup):
    audit = wf.prepare_workflow("audit-review", setup.account, "luna", allow_document_changes=True, **setup.kwargs)
    learning = wf.prepare_workflow("code-learning", setup.account, "sol", allow_code_changes=True, **setup.kwargs)
    assert audit.workspace != learning.workspace
    for prepared in (audit, learning):
        manifest = json.loads((prepared.request_dir / "manifest.json").read_text())
        assert not manifest["allow_publish_or_install"]
        assert manifest["expected_account_email"] == "test@example.com"
        assert manifest["review_confidence_operator"] == ">"
        assert manifest["review_confidence_threshold"] == 80
        assert not manifest["expanded_review_authorized"]
    assert str(setup.documents) in audit.provider_args
    assert str(setup.source) in learning.provider_args
    with pytest.raises(wf.WorkflowError, match="cannot be authorized"):
        wf.prepare_workflow("audit-review", setup.account, "luna", allow_code_changes=True, **setup.kwargs)
    with pytest.raises(wf.WorkflowError, match="cannot be authorized"):
        wf.prepare_workflow("code-learning", setup.account, "sol", allow_document_changes=True, **setup.kwargs)


def test_queue_scope_and_processing_lock_paths(setup):
    prepared = wf.prepare_workflow("audit-review", setup.account, "luna", review_all_flags=True, allow_document_changes=True, **setup.kwargs)
    manifest = json.loads((prepared.request_dir / "manifest.json").read_text())
    assert manifest["review_all_flags"] and manifest["expanded_review_authorized"]
    assert manifest["processing_root"] == str(setup.processing)
    assert str(setup.processing) in prepared.provider_args


def test_incomplete_audit_and_missing_source_rejected(setup):
    with pytest.raises(wf.WorkflowError, match="Finish the post-run"):
        wf.prepare_workflow("audit-review", setup.account, "luna", **(setup.kwargs | {"completed_audit": False}))
    with pytest.raises(wf.WorkflowError, match="Git source checkout"):
        wf.prepare_workflow("code-learning", setup.account, "sol", **(setup.kwargs | {"source_root": setup.documents}))
    with pytest.raises(wf.WorkflowError, match="supported model"):
        wf.prepare_workflow("audit-review", setup.account, "sol", **setup.kwargs)
    with pytest.raises(wf.WorkflowError, match="Audit review requires"):
        wf.prepare_workflow("audit-review", setup.account, "luna", allow_document_changes=True,
                            **(setup.kwargs | {"source_root": setup.documents}))
    with pytest.raises(wf.WorkflowError, match="Audit review requires"):
        wf.prepare_workflow("audit-review", setup.account, "luna", **(setup.kwargs | {"source_root": None}))


def test_unverified_plans_cannot_launch(setup):
    prepared = wf.prepare_workflow("audit-review", setup.account, "luna", preflight=False, **setup.kwargs)
    with pytest.raises(wf.WorkflowError, match="prepared offline"):
        wf.launch_workflow(prepared)
    with pytest.raises(wf.WorkflowError, match="explicit authorization"):
        wf.launch_headless(prepared)
    with pytest.raises(wf.WorkflowError, match="prepared offline"):
        wf.launch_headless(prepared, authorized_unattended=True)


def test_learning_requires_helper_and_canonical_journal(setup):
    (setup.ledger.parent / "review_records.jsonl").unlink()
    Path(setup.kwargs["misnaming_path"]).write_bytes(b"legacy index")
    with pytest.raises(wf.WorkflowError, match="canonical AI review master ledger"):
        wf.prepare_workflow("code-learning", setup.account, "sol", **setup.kwargs)
    (setup.ledger.parent / "review_records.jsonl").touch()
    (setup.source / "src" / "ai_review.py").unlink()
    with pytest.raises(wf.WorkflowError, match="Git source checkout"):
        wf.prepare_workflow("code-learning", setup.account, "sol", **setup.kwargs)


def test_launch_rejects_audit_change_without_start(setup, monkeypatch):
    prepared = wf.prepare_workflow("audit-review", setup.account, "luna", **setup.kwargs)
    setup.audit.write_text("changed")
    monkeypatch.setattr(wf.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(wf.WorkflowError, match="changed after"):
        wf.launch_workflow(prepared)


def test_powershell_quotes_metacharacters_without_cmd_shell(setup):
    prepared = wf.prepare_workflow("audit-review", setup.account, "luna", **setup.kwargs)
    prepared.provider_args = ["C:/O'Neil/claude.exe", "--model", "fable", 'do $x; $(not-code) & "hello"']
    args = wf.powershell_launch_arguments(prepared)
    decoded = base64.b64decode(args[-1]).decode("utf-16le")
    assert "Start-Process -FilePath 'C:/O''Neil/claude.exe'" in decoded
    assert wf._ps_quote(subprocess.list2cmdline(prepared.provider_args[1:])) in decoded
    assert "-EncodedCommand" in args and "cmd.exe" not in args


def test_headless_policy_and_no_fallback(setup):
    prepared = wf.prepare_workflow("audit-review", setup.account, "luna", **setup.kwargs)
    args = wf.headless_arguments(prepared)
    assert args[:4] == ["codex.exe", "--ask-for-approval", "never", "exec"]
    assert args.count("--ask-for-approval") == 1
    assert "on-request" not in args
    assert "workspace-write" in args
    assert "gpt-5.6-luna" in args
    assert not any("bypass" in arg or "fallback" in arg for arg in args)


def test_report_types_and_sidecars(tmp_path):
    for name in ("Filename_Audit_Report.csv", "Filename_Audit_Report_2.xlsx", "Filename_Audit_Report_summary.csv", "Filename_Audit_Report_orientation.csv", "Filename_Audit_Report - Tables.csv", "not-an-audit.xlsx"):
        (tmp_path / name).touch()
    choices = wf.report_choices([tmp_path, tmp_path], tmp_path / "Master.xlsx", tmp_path / "Misnaming.xlsx")
    assert {p.name for p in choices["audits"]} == {"Filename_Audit_Report.csv", "Filename_Audit_Report_2.xlsx"}
    assert choices["review_ledger"].name == "Master.xlsx"


def test_model_and_identity_validation_fail_closed(tmp_path, monkeypatch):
    account = wf.Account("1", "codex", "Personal", tmp_path, "expected@example.com")
    monkeypatch.setattr(wf, "find_cli", lambda p: "codex.exe")
    monkeypatch.setattr(wf, "query_codex", lambda *a: {"account": {"type": "chatgpt", "email": "expected@example.com"}, "models": []})
    with pytest.raises(wf.WorkflowError, match="No fallback"):
        wf.validate_selection(account, "luna")
    monkeypatch.setattr(wf, "query_codex", lambda *a: {"account": {"type": "chatgpt", "email": "wrong@example.com"},
                                                     "models": [{"model": "gpt-5.6-luna", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]})
    with pytest.raises(wf.WorkflowError, match="identity changed"):
        wf.validate_selection(account, "luna")


def test_headless_success_is_not_review_completion(setup):
    prepared = wf.prepare_workflow("audit-review", setup.account, "luna", **setup.kwargs)
    run = wf.HeadlessRun(prepared, SimpleNamespace(poll=lambda: 0))
    assert run.poll() == 0
    status = json.loads((prepared.request_dir / "runner-status.json").read_text())
    assert status["state"] == "outputs-awaiting-verification"
    assert json.loads((prepared.request_dir / "manifest.json").read_text())["state"] == "prepared"


@pytest.mark.parametrize("shell_name", ["powershell.exe", "pwsh.exe"])
def test_real_powershell_roundtrips_prompt_without_execution(setup, shell_name):
    if os.name != "nt" or not shutil.which(shell_name):
        pytest.skip("Windows PowerShell quoting smoke test")
    prepared = wf.prepare_workflow("audit-review", setup.account, "luna", **setup.kwargs)
    payload = '/stage2-review-audit "C:\\O\'Neil & Sons\\$name [Processed]\\REQUEST.md"'
    prepared.provider_args = [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", payload]
    args = [arg for arg in wf.powershell_launch_arguments(prepared) if arg != "-NoExit"]
    args[0] = shutil.which(shell_name)
    result = subprocess.run(args, capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [payload]
