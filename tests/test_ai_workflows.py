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
    runtime = {"executable": str(Path(sys.executable).resolve()), "version": "3.test", "implementation": "CPython",
               "modules": {"openpyxl": "3.test", "fitz": "1.test", "PIL": "11.test"}}
    monkeypatch.setattr(wf, "resolve_python_runtime", lambda environment=None, candidates=(): dict(runtime))
    monkeypatch.setattr(wf, "verify_python_runtime", lambda executable, environment=None: dict(runtime))
    def fake_validate(account, model_key, expected_email=None, effort=None, role=None):
        # Echo the exact requested model/effort like the real preflight does.
        choice = wf.model_choice(model_key, effort, role)
        return {"provider": account.provider, "email": "test@example.com", "model": choice["id"], "model_key": model_key,
                "effort": choice["effort"], "status": "verified", "executable": "codex.exe"}
    monkeypatch.setattr(wf, "validate_selection", fake_validate)
    return SimpleNamespace(account=account, audit=audit, documents=documents, processing=processing, source=source, ledger=ledger, runtime=runtime,
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


def test_python_runtime_probe_is_exact_external_and_checks_inspection_imports(tmp_path, monkeypatch):
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"stub")
    seen = {}
    payload = {"executable": str(executable.resolve()), "version": "3.13.7", "implementation": "CPython",
               "modules": {"openpyxl": "3.1.5", "fitz": "1.27.2.3", "PIL": "11.3.0"}}
    def fake_run(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload) + "\n", stderr="")
    monkeypatch.setattr(wf.subprocess, "run", fake_run)
    environment = {"PATH": "isolated", "CODEX_HOME": "selected"}
    assert wf.verify_python_runtime(executable, environment) == payload
    assert seen["args"][:3] == [str(executable.resolve()), "-I", "-c"]
    assert all(name in seen["args"][3] for name in wf.PYTHON_RUNTIME_MODULES)
    assert seen["kwargs"]["env"] == environment
    assert os.environ.get("CODEX_HOME") != "selected"


def test_python_runtime_probe_missing_library_fails_before_request(tmp_path, monkeypatch):
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"stub")
    monkeypatch.setattr(wf.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr="ModuleNotFoundError: No module named 'fitz'\n"))
    with pytest.raises(wf.WorkflowError, match="missing required document-inspection libraries.*PyMuPDF"):
        wf.verify_python_runtime(executable, {})


def test_python_runtime_resolution_rejects_frozen_app_and_fails_closed(tmp_path, monkeypatch):
    frozen_app = tmp_path / "Stage2_Processing.exe"
    frozen_app.write_bytes(b"not python")
    monkeypatch.setattr(wf.sys, "frozen", True, raising=False)
    monkeypatch.setattr(wf.sys, "executable", str(frozen_app))
    monkeypatch.setattr(wf.shutil, "which", lambda *a, **k: None)
    with pytest.raises(wf.WorkflowError, match="No suitable external Python runtime"):
        wf.resolve_python_runtime({}, ())
    with pytest.raises(wf.WorkflowError, match="frozen Stage 2 application"):
        wf.verify_python_runtime(frozen_app, {})


def test_python_runtime_resolution_uses_registered_process_local_override(tmp_path, monkeypatch):
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"stub")
    expected = {"executable": str(executable.resolve()), "version": "3.13.7", "implementation": "CPython", "modules": {}}
    seen = []
    def fake_verify(candidate, environment=None):
        seen.append((Path(candidate), dict(environment or {})))
        return expected
    monkeypatch.setattr(wf, "verify_python_runtime", fake_verify)
    result = wf.resolve_python_runtime({"STAGE2_PYTHON": str(executable), "PATH": "child-only"})
    assert result == expected
    assert seen[0] == (executable, {"STAGE2_PYTHON": str(executable), "PATH": "child-only"})


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
        assert manifest["python_runtime"] == setup.runtime
    assert str(setup.documents) in audit.provider_args
    assert str(setup.source) in learning.provider_args
    with pytest.raises(wf.WorkflowError, match="cannot be authorized"):
        wf.prepare_workflow("audit-review", setup.account, "luna", allow_code_changes=True, **setup.kwargs)
    with pytest.raises(wf.WorkflowError, match="cannot be authorized"):
        wf.prepare_workflow("code-learning", setup.account, "sol", allow_document_changes=True, **setup.kwargs)


def test_prepared_requests_use_and_export_only_verified_python(setup):
    original_path = os.environ.get("PATH")
    audit = wf.prepare_workflow("audit-review", setup.account, "luna", **setup.kwargs)
    learning = wf.prepare_workflow("code-learning", setup.account, "sol", allow_code_changes=True, **setup.kwargs)
    for prepared in (audit, learning):
        request = prepared.prompt_file.read_text(encoding="utf-8")
        manifest = json.loads((prepared.request_dir / "manifest.json").read_text())
        executable = setup.runtime["executable"]
        assert prepared.python_runtime == setup.runtime and manifest["python_runtime"] == setup.runtime
        assert f"Verified helper Python: {executable}." in request
        assert "do not substitute python, python3, py" in request
        assert "& " + wf._ps_quote(executable) in request
        assert "& 'python' " not in request and "& 'python3' " not in request and "& 'py' " not in request
        assert prepared.environment["STAGE2_PYTHON"] == executable
        assert prepared.environment["PATH"].split(os.pathsep)[0] == str(Path(executable).parent)
    assert os.environ.get("PATH") == original_path


def test_launch_rechecks_exact_python_without_fallback(setup, monkeypatch):
    prepared = wf.prepare_workflow("audit-review", setup.account, "luna", **setup.kwargs)
    changed = dict(setup.runtime)
    changed["version"] = "3.changed"
    monkeypatch.setattr(wf, "verify_python_runtime", lambda executable, environment=None: changed)
    monkeypatch.setattr(wf.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(wf.WorkflowError, match="runtime changed after preparation"):
        wf.launch_workflow(prepared)


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
        wf.prepare_workflow("audit-review", setup.account, "not-a-model", **setup.kwargs)
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


# --------------------------------------------------------------------------- #
# Shared model catalog, explicit effort and pre-run automatic review defaults
# --------------------------------------------------------------------------- #
def test_catalog_uses_explicit_ids_with_inferred_provider_and_per_role_ranking():
    assert set(wf.MODEL_CATALOG) == {"sol", "terra", "luna", "astra", "opus", "fable"}
    assert wf.MODEL_CATALOG["sol"]["id"] == "gpt-5.6-sol" and wf.MODEL_CATALOG["astra"]["id"] == "gpt-6-astra"
    assert wf.MODEL_CATALOG["opus"]["id"] == "claude-opus-5" and wf.MODEL_CATALOG["fable"]["id"] == "claude-fable-5-1"
    assert wf.model_keys_for_role("audit-review")[0] == "sol"
    assert wf.model_keys_for_role("code-learning")[0] == "fable"
    assert set(wf.model_keys_for_role("audit-review")) == set(wf.MODEL_CATALOG)  # no single-role lock
    assert wf.DEFAULT_MODEL == {"audit-review": "sol", "code-learning": "fable"}
    assert wf.DEFAULT_EXPECTED_EMAIL == ""
    # Compatibility view still exposes label/provider/id/effort/role.
    for key, choice in wf.MODEL_CHOICES.items():
        assert {"label", "provider", "id", "effort", "role", "roles"} <= set(choice)
        assert choice["provider"] == ("claude" if key in ("opus", "fable") else "codex")
    with pytest.raises(wf.WorkflowError):
        wf.model_keys_for_role("other")


def test_model_choice_resolves_immutable_metadata_and_effort_ranking():
    choice = wf.model_choice("sol", role="audit-review")
    assert choice["id"] == "gpt-5.6-sol" and choice["provider_label"] == "Codex (OpenAI)"
    assert choice["effort"] == "high" and choice["recommended_effort"] == "high" and not choice["effort_explicit"]
    assert choice["efforts"][:4] == ("high", "xhigh", "medium", "max")
    assert set(choice["advanced_efforts"]) == {"minimal", "low", "ultra"}
    with pytest.raises(TypeError):
        choice["effort"] = "low"
    explicit = wf.model_choice("sol", "XHigh", "audit-review")
    assert explicit["effort"] == "xhigh" and explicit["effort_explicit"] and explicit["effort_label"] == "Extra High"
    assert wf.model_choice("astra", role="code-learning")["recommended_effort"] == "medium"
    assert wf.model_choice("astra", role="audit-review")["recommended_effort"] == "high"
    assert wf.model_choice("fable", role="code-learning")["rank"] == 1
    assert wf.model_choice("fable")["role"] == "code-learning"  # primary role when unspecified
    assert wf.effort_choices("sol", "audit-review", advertised=["low", "medium", "high"]) == ("high", "medium", "low")
    assert wf.effort_choices("fable", "code-learning")[-1] == "low"
    with pytest.raises(wf.WorkflowError, match="does not accept"):
        wf.model_choice("fable", "ultra", "code-learning")
    with pytest.raises(wf.WorkflowError, match="supported model"):
        wf.model_choice("gpt-4")
    with pytest.raises(wf.WorkflowError, match="Unknown AI workflow role"):
        wf.model_choice("sol", role="publishing")


def test_explicit_effort_carried_through_manifest_prompt_command_and_status(setup):
    prepared = wf.prepare_workflow("audit-review", setup.account, "sol", effort="xhigh", **setup.kwargs)
    assert prepared.effort == "xhigh"
    manifest = json.loads((prepared.request_dir / "manifest.json").read_text())
    assert manifest["model"] == "gpt-5.6-sol" and manifest["model_key"] == "sol"
    assert manifest["effort"] == "xhigh" and manifest["effort_source"] == "explicit"
    assert manifest["preflight"]["effort"] == "xhigh"
    assert 'model_reasoning_effort="xhigh"' in prepared.provider_args
    assert "gpt-5.6-sol (Sol · Codex (OpenAI)) / xhigh effort" in prepared.prompt_file.read_text(encoding="utf-8")
    recommended = wf.prepare_workflow("code-learning", setup.account, "astra", **setup.kwargs)
    manifest = json.loads((recommended.request_dir / "manifest.json").read_text())
    assert manifest["effort"] == "medium" and manifest["effort_source"] == "recommended"
    # The old single-role default must never overwrite an explicit selection.
    assert wf.MODEL_CHOICES["astra"]["effort"] == "medium"
    assert json.loads((wf.prepare_workflow("code-learning", setup.account, "astra", effort="high", **setup.kwargs).request_dir / "manifest.json").read_text())["effort"] == "high"
    claude = wf.Account("claude-test", "claude", "Personal", setup.account.config_dir, "test@example.com")
    prepared = wf.prepare_workflow("audit-review", claude, "opus", effort="max", preflight=False, **setup.kwargs)
    assert prepared.provider_args[1:5] == ["--model", "claude-opus-5", "--effort", "max"]
    with pytest.raises(wf.WorkflowError, match="does not accept"):
        wf.prepare_workflow("audit-review", claude, "opus", effort="ultra", preflight=False, **setup.kwargs)
    run = wf.HeadlessRun(prepared, SimpleNamespace(poll=lambda: None))
    assert run.poll() is None


def test_launch_revalidates_the_exact_effort(setup, monkeypatch):
    prepared = wf.prepare_workflow("audit-review", setup.account, "sol", effort="xhigh", **setup.kwargs)
    seen = {}
    def revalidate(account, model_key, expected_email=None, effort=None, role=None):
        assert role == "audit-review"
        seen.update(model_key=model_key, effort=effort, expected=expected_email)
        return {"provider": "codex", "email": expected_email, "model": "gpt-5.6-sol", "effort": effort, "executable": "codex.exe"}
    monkeypatch.setattr(wf, "validate_selection", revalidate)
    monkeypatch.setattr(wf.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=1))
    wf.launch_workflow(prepared)
    assert seen == {"model_key": "sol", "effort": "xhigh", "expected": "test@example.com"}
    # Tampered manifest effort blocks launch rather than silently re-choosing.
    fresh = wf.prepare_workflow("audit-review", setup.account, "sol", effort="xhigh", **setup.kwargs)
    manifest_path = fresh.request_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["effort"] = "low"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(wf.WorkflowError, match="effort no longer matches"):
        wf.launch_workflow(fresh)
    # Revalidation that reports a different model/effort also blocks.
    other = wf.prepare_workflow("audit-review", setup.account, "sol", effort="xhigh", **setup.kwargs)
    monkeypatch.setattr(wf, "validate_selection", lambda *a, **k: {"provider": "codex", "email": "test@example.com", "model": "gpt-5.6-luna", "effort": "xhigh"})
    with pytest.raises(wf.WorkflowError, match="different model/effort"):
        wf.launch_workflow(other)


def test_codex_preflight_checks_advertised_effort_for_the_explicit_selection(tmp_path, monkeypatch):
    account = wf.Account("1", "codex", "Personal", tmp_path)
    monkeypatch.setattr(wf, "find_cli", lambda p: "codex.exe")
    catalog = {"account": {"type": "chatgpt", "email": "personal@example.com", "planType": "pro"},
               "models": [{"model": "gpt-5.6-sol", "supportedReasoningEfforts": [{"reasoningEffort": level} for level in ("low", "medium", "high", "xhigh", "max", "ultra")]},
                          {"model": "gpt-5.6-luna", "supportedReasoningEfforts": [{"reasoningEffort": level} for level in ("low", "medium", "high", "xhigh", "max")]}]}
    monkeypatch.setattr(wf, "query_codex", lambda *a: catalog)
    result = wf.validate_selection(account, "sol", "personal@example.com", effort="ultra")
    assert result["model"] == "gpt-5.6-sol" and result["effort"] == "ultra" and result["plan"] == "pro"
    assert result["supported_efforts"] == ("low", "medium", "high", "xhigh", "max", "ultra")
    assert result["status"] == "account-and-model-verified"
    assert wf.validate_selection(account, "sol")["effort"] == "high"
    with pytest.raises(wf.WorkflowError, match="does not advertise Ultra"):
        wf.validate_selection(account, "luna", effort="ultra")
    with pytest.raises(wf.WorkflowError, match="No fallback model"):
        wf.validate_selection(account, "astra")
    with pytest.raises(wf.WorkflowError, match="belonging to the selected model"):
        wf.validate_selection(account, "opus")
    # Empty profile metadata is not "no account": the live identity is used.
    assert account.email == "" and result["email"] == "personal@example.com"


def test_claude_preflight_accepts_full_model_ids_and_documented_efforts(tmp_path, monkeypatch):
    account = wf.Account("1", "claude", "Personal", tmp_path, "someone@example.com")
    monkeypatch.setattr(wf, "find_cli", lambda p: "claude.exe")
    monkeypatch.setattr(wf, "_run_json", lambda args, env: {"loggedIn": True, "email": "someone@example.com", "authMethod": "claude.ai"})
    help_text = ("Options:\n  --effort <level>                      Effort level for the current session\n"
                 "                                        (low, medium, high, xhigh, max)\n"
                 "  --model <model>                       Model for the current session. Provide\n"
                 "                                        an alias (e.g. 'fable') or a model's full name (e.g. 'claude-fable-5').\n"
                 "  -n, --name <name>                     Session name\n")
    monkeypatch.setattr(wf.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=help_text, returncode=0))
    result = wf.validate_selection(account, "fable", effort="xhigh")
    assert result["model"] == "claude-fable-5-1" and result["effort"] == "xhigh"
    assert result["supported_efforts"] == ("low", "medium", "high", "xhigh", "max")
    assert "entitlement-checked-by-Claude-at-launch" in result["status"]
    assert wf.validate_selection(account, "opus")["effort"] == "high"
    old_help = help_text.replace("(low, medium, high, xhigh, max)", "(low, medium, high)")
    monkeypatch.setattr(wf.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=old_help, returncode=0))
    with pytest.raises(wf.WorkflowError, match="does not document Extra High"):
        wf.validate_selection(account, "fable", effort="xhigh")
    monkeypatch.setattr(wf.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="Usage: claude [options]", returncode=0))
    with pytest.raises(wf.WorkflowError, match="--model/--effort"):
        wf.validate_selection(account, "fable")
    monkeypatch.setattr(wf.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=help_text, returncode=0))
    with pytest.raises(wf.WorkflowError, match="identity changed"):
        wf.validate_selection(account, "fable", "other@example.com")


def test_auto_review_defaults_merge_without_inventing_paths(tmp_path):
    defaults = wf.auto_review_defaults({})
    assert defaults == {"enabled": True, "model_key": "sol", "effort": "high", "account_id": "", "expected_email": "",
                        "allow_document_changes": True, "review_all_flags": True, "source_root": "", "workspace_root": "", "ledger_path": "", "misnaming_path": ""}
    assert wf.auto_review_defaults(None) == defaults and wf.auto_review_defaults({"ai_workflows": "bad"}) == defaults
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cfg = {"ai_workflows": {"source_root": str(checkout), "ledger_path": "L.xlsx", "workspace_root": "W", "audit-review_account": "codex-abc",
                            "auto_review": {"enabled": False, "effort": "XHIGH", "model_key": "terra", "expected_email": None}}}
    merged = wf.auto_review_defaults(cfg)
    assert merged["enabled"] is False and merged["model_key"] == "terra" and merged["effort"] == "xhigh"
    assert merged["expected_email"] == ""  # None never overrides the default
    assert merged["source_root"] == str(checkout) and merged["ledger_path"] == "L.xlsx" and merged["workspace_root"] == "W"
    assert merged["account_id"] == "codex-abc"
    assert merged is not cfg["ai_workflows"]["auto_review"] and cfg["ai_workflows"]["auto_review"] == {"enabled": False, "effort": "XHIGH", "model_key": "terra", "expected_email": None}
    stale = {"ai_workflows": {"source_root": str(tmp_path / "gone")}}
    assert wf.auto_review_defaults(stale)["source_root"] == ""
    assert wf.AUTO_REVIEW_DEFAULTS["enabled"] is True


def test_auto_review_summary_is_honest_about_state():
    assert wf.auto_review_summary({}) == "After processing: Accuracy audit → Sol / High document review · account identity verified at launch · Apply corrections on."
    off = wf.auto_review_summary({"ai_workflows": {"auto_review": {"enabled": False}}})
    assert "Automatic AI document review is off" in off
    proposals = wf.auto_review_summary({"ai_workflows": {"auto_review": {"allow_document_changes": False, "model_key": "opus", "effort": "max", "expected_email": ""}}})
    assert "Opus 5 / Max" in proposals and "Propose corrections only" in proposals and "verified at launch" in proposals
    assert "needs configuration" in wf.auto_review_summary({"ai_workflows": {"auto_review": {"model_key": "nope"}}})


def test_role_aware_validation_and_live_output_reexport(tmp_path, monkeypatch):
    account = wf.Account("1", "codex", "Personal", tmp_path)
    monkeypatch.setattr(wf, "find_cli", lambda p: "codex.exe")
    monkeypatch.setattr(wf, "query_codex", lambda *a: {"account": {"type": "chatgpt", "email": "personal@example.com"},
                                                     "models": [{"model": "gpt-6-astra", "supportedReasoningEfforts": ["medium", "high"]}]})
    # role decides the recommended effort when none is explicit: Astra is Medium for code learning, High for document review.
    assert wf.validate_selection(account, "astra", role="code-learning")["effort"] == "medium"
    assert wf.validate_selection(account, "astra", role="audit-review")["effort"] == "high"
    import stage2_live_output as live
    seen = {}
    monkeypatch.setattr(live, "open_live_output", lambda request_dir, **kwargs: seen.update(request_dir=request_dir, **kwargs) or "viewer")
    assert wf.open_live_output(tmp_path, title="t") == "viewer"
    assert seen == {"request_dir": tmp_path, "title": "t"}
