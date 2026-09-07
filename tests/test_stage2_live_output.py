"""Read-only live output viewer: rendering, quoting and no-submission guarantees."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import stage2_live_output as live
import stage2_ai_workflows as wf


CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "python ai_review.py prepare --care-home \"Ünïcode Hôme\"", "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "python ai_review.py prepare", "exit_code": 0, "status": "completed", "aggregated_output": "queue: 3 rows\nok ✓"}},
    {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "Reviewed 3 candidates — 2 Keep, 1 Rename."}},
    {"type": "item.started", "item": {"type": "mcp_tool_call", "server": "docs", "tool": "read_page", "arguments": {"path": "C:\\x\\Care Plan.pdf"}}},
    {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "docs", "tool": "read_page", "error": {"message": "file locked"}}},
    {"type": "item.completed", "item": {"type": "file_change", "status": "completed", "changes": [{"kind": "update", "path": "decisions.json"}]}},
    {"type": "item.completed", "item": {"type": "todo_list", "items": [{"text": "verify", "completed": True}, {"text": "sync", "completed": False}]}},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "summary only"}},
    {"type": "error", "message": "rate limited; retrying"},
    {"type": "turn.failed", "error": {"message": "usage limit reached"}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
]
CLAUDE_EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-fable-5-1"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading the ledger…"},
                                                  {"type": "tool_use", "name": "Bash", "input": {"command": "dir C:\\Lifted"}},
                                                  {"type": "thinking", "thinking": "PRIVATE-REASONING-MUST-NOT-SHOW"}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "Volume in drive C"}], "is_error": False}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "Access denied", "is_error": True}]}},
    {"type": "stream_event", "event": {"delta": "partial"}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 4, "duration_ms": 1200, "result": "Done: ledger synced."},
]
SPARSE = [{}, {"type": "mystery", "payload": [1, 2]}, {"type": "item.completed", "item": {"type": "weird"}}, {"type": "item.started"},
          {"type": "assistant"}, {"type": "user", "message": {}}, {"type": "turn.failed"}, {"type": "result"}]


def write_run(root, name="O'Neil & Sons [Processed]", exited=True):
    request = root / name / "20260907T000000Z-abc"
    request.mkdir(parents=True)
    (request / "manifest.json").write_text(json.dumps({"role": "audit-review", "model": "gpt-5.6-sol", "model_label": "Sol · Codex (OpenAI)",
                                                       "effort": "high", "expected_account_email": "mralijabbar04@gmail.com", "state": "launched"}), encoding="utf-8")
    lines = [json.dumps(e, ensure_ascii=False) for e in CODEX_EVENTS + SPARSE + CLAUDE_EVENTS] + ["not json at all {", ""]
    (request / live.EVENTS_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (request / live.STDERR_NAME).write_text("warning: something ✓\n", encoding="utf-8")
    status = {"state": "running", "pid": 1}
    if exited:
        status.update(state="outputs-awaiting-verification", process_exit_code=0, process_exited_utc="2026-09-07T00:00:00Z")
    (request / live.STATUS_NAME).write_text(json.dumps(status), encoding="utf-8")
    return request


EXPECTED_FRAGMENTS = [
    "[session] Codex thread t1", "[turn] started",
    "[command] python ai_review.py prepare --care-home \"Ünïcode Hôme\"",
    "[result] exit 0 (completed): python ai_review.py prepare", "    ok ✓",
    "[assistant] Reviewed 3 candidates — 2 Keep, 1 Rename.",
    "[tool] docs.read_page path=C:\\x\\Care Plan.pdf", "[tool error] docs.read_page: file locked",
    "[files] completed", "    update: decisions.json", "[plan]", "    - [x] verify", "    - [ ] sync",
    "[thinking summary] summary only", "[error] rate limited; retrying", "[error] turn failed: usage limit reached",
    "[turn] completed (input_tokens=10, output_tokens=5)",
    "[event] {}", "[weird] completed:", "[mystery]",
    "[session] Claude session s1 model claude-fable-5-1", "[assistant] Reading the ledger…",
    "[tool] Bash command=dir C:\\Lifted", "[result] Volume in drive C", "[result error] Access denied",
    "[done] success turns=4 duration_ms=1200", "    Done: ledger synced.",
    "[raw] not json at all {", "[stderr] warning: something ✓",
]


def test_python_renderer_handles_codex_claude_sparse_unicode_and_errors(tmp_path):
    request = write_run(tmp_path)
    transcript = live.render_transcript(request)
    for fragment in EXPECTED_FRAGMENTS:
        assert fragment in transcript, fragment
    assert "PRIVATE-REASONING-MUST-NOT-SHOW" not in transcript
    assert "partial" not in transcript
    assert "state=outputs-awaiting-verification exit=0" in transcript
    assert "not review completion" in transcript
    assert transcript.startswith("Stage 2 AI Document Review · Sol · Codex (OpenAI) / High · mralijabbar04@gmail.com — live output (read-only)")
    assert live.VIEWER_NOTICE in transcript
    assert live.render_line("") == [] and live.render_line("   \n") == []
    assert live.render_event(["not", "a", "dict"]) == ['[event] ["not", "a", "dict"]']
    long = live.render_event({"type": "item.completed", "item": {"type": "command_execution", "command": "x", "exit_code": 1, "aggregated_output": "y" * 5000}})
    assert any("chars not shown" in row for row in long)


def test_viewer_command_quotes_safely_and_never_invokes_a_provider(tmp_path):
    request = write_run(tmp_path, name="Care & Home [Files] 'quoted'")
    args = live.viewer_command(request, title='Tïtle "dq" & \'sq\'', script_dir=tmp_path / "viewer")
    assert args[1:6] == ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    script = Path(args[6])
    assert script.is_file() and script.parent == tmp_path / "viewer"
    assert args[7:] == ["-RequestDir", str(request), "-Title", 'Tïtle "dq" & \'sq\'']
    assert "cmd.exe" not in " ".join(args).casefold()
    body = script.read_text(encoding="utf-8")
    lowered = body.casefold()
    for forbidden in ("codex.exe", "claude.exe", "& codex", "& claude", "codex exec", "--resume", "--print", "--continue",
                      "stop-process", "start-process", "taskkill", "invoke-expression", "set-content", "out-file", "remove-item"):
        assert forbidden not in lowered, forbidden
    assert "does not submit prompts" in body
    # Same content hash → same script path; re-generation is idempotent.
    assert live.viewer_command(request, script_dir=tmp_path / "viewer")[6] == str(script)
    assert live.viewer_shell().casefold().endswith(("pwsh.exe", "powershell.exe"))


def test_open_live_output_is_read_only_new_console_and_reopenable(tmp_path):
    request = write_run(tmp_path, exited=False)
    before = {name: (request / name).read_bytes() for name in ("manifest.json", live.EVENTS_NAME, live.STATUS_NAME)}
    calls = []
    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return object()
    first = live.open_live_output(request, script_dir=tmp_path / "viewer", popen=popen)
    second = live.open_live_output(request, title="again", script_dir=tmp_path / "viewer", popen=popen)
    assert first is not None and second is not None
    assert len(calls) == 2
    for args, kwargs in calls:
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        assert kwargs["cwd"] == str(request)
        assert "-Once" not in args
        assert not any(arg.casefold().endswith(("codex.exe", "claude.exe")) for arg in args)
    assert calls[1][0][-1] == "again"
    assert {name: (request / name).read_bytes() for name in before} == before
    assert json.loads((request / "manifest.json").read_text())["state"] == "launched"
    with pytest.raises(wf.WorkflowError, match="does not exist"):
        live.open_live_output(tmp_path / "missing", popen=popen)
    def failing(*_a, **_k):
        raise OSError("no shell")
    with pytest.raises(wf.WorkflowError, match="unaffected"):
        live.open_live_output(request, script_dir=tmp_path / "viewer", popen=failing)
    assert len(calls) == 2


def test_session_title_without_status_or_effort(tmp_path):
    request = tmp_path / "r"
    request.mkdir()
    (request / "manifest.json").write_text(json.dumps({"role": "code-learning", "model": "claude-fable-5-1"}), encoding="utf-8")
    title = live.session_title(request)
    assert title.startswith("Stage 2 Improve Stage 2 · claude-fable-5-1")
    assert "account verified at launch" in title
    assert "live output (read-only)" in title


@pytest.mark.parametrize("shell_name", ["powershell.exe", "pwsh.exe"])
def test_real_powershell_viewer_renders_once_without_waiting(tmp_path, shell_name):
    if os.name != "nt" or not shutil.which(shell_name):
        pytest.skip("Windows PowerShell viewer smoke test")
    request = write_run(tmp_path)
    args = live.viewer_command(request, title='Tïtle "dq" & \'sq\'', once=True, script_dir=tmp_path / "viewer", shell=shutil.which(shell_name))
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    out = result.stdout
    for fragment in EXPECTED_FRAGMENTS:
        assert fragment in out, fragment
    assert 'Tïtle "dq" & \'sq\'' in out.splitlines()[0]
    assert str(request) in out
    assert "PRIVATE-REASONING-MUST-NOT-SHOW" not in out
    assert "[runner] role=audit-review model=gpt-5.6-sol effort=high account=mralijabbar04@gmail.com" in out
    assert "[runner] process exited (code 0)" in out
    assert "not review completion" in out
    assert "Press Enter" not in out
    # Nothing in the request folder was modified by viewing it.
    assert json.loads((request / "manifest.json").read_text())["state"] == "launched"


def test_real_powershell_viewer_waits_for_missing_events_without_error(tmp_path):
    shell = shutil.which("powershell.exe")
    if os.name != "nt" or not shell:
        pytest.skip("Windows PowerShell viewer smoke test")
    request = tmp_path / "pending"
    request.mkdir()
    (request / "manifest.json").write_text(json.dumps({"role": "audit-review", "state": "prepared"}), encoding="utf-8")
    args = live.viewer_command(request, once=True, script_dir=tmp_path / "viewer", shell=shell)
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    assert "waiting for provider-events.jsonl" in result.stdout
    assert not (request / live.EVENTS_NAME).exists()
