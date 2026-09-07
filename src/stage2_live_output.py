"""Read-only visible viewer for supervised (headless) Stage 2 AI runs.

The automatic controller runs the provider CLI unattended and records its
event stream in the request folder (provider-events.jsonl, provider-stderr.log,
runner-status.json). This module renders that stream as readable terminal
output in a separate visible console. It is a viewer only: it never submits a
prompt, resumes an agent or signals the provider process. Closing the viewer
leaves the supervised run untouched. It is *live output*, not an interactive
chat; native interactive sessions are a different, clearly labelled path.

The viewer itself is a self-contained PowerShell script generated from this
module, so the same distribution works from source and from the frozen main
executable without a second Python interpreter.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import stage2_ai_workflows as workflows


EVENTS_NAME = "provider-events.jsonl"
STDERR_NAME = "provider-stderr.log"
STATUS_NAME = "runner-status.json"
OUTPUT_CLIP = 1500
VIEWER_NOTICE = ("Read-only live output viewer. This window shows the supervised run's recorded events; "
                 "it does not submit prompts, resume the agent or cancel the run. Closing it leaves the AI process running.")


# --------------------------------------------------------------------------- #
# Python rendering (tests, transcripts and any in-app summary)
# --------------------------------------------------------------------------- #
def _clip(text, limit=OUTPUT_CLIP):
    text = str(text if text is not None else "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"… (+{len(text) - limit} chars not shown)"


def _compact(value, limit=400):
    try:
        return _clip(json.dumps(value, ensure_ascii=False, sort_keys=True), limit)
    except (TypeError, ValueError):
        return _clip(repr(value), limit)


def _tool_summary(name, arguments):
    """One-line, non-secret description of a tool call."""
    if not isinstance(arguments, dict):
        return _compact(arguments, 300)
    for key in ("command", "cmd", "file_path", "path", "pattern", "query", "url", "prompt", "description"):
        if arguments.get(key):
            return f"{key}={_clip(str(arguments[key]), 300)}"
    return _compact(arguments, 300)


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or _compact(block, 200)))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if isinstance(content, dict):
        for key in ("text", "message", "content"):
            if content.get(key):
                return str(content[key])
    if content is None:
        return ""
    return _compact(content)


def _lines(prefix, text):
    text = str(text if text is not None else "").rstrip("\n")
    if not text:
        return [f"[{prefix}]"]
    rows = text.split("\n")
    return [f"[{prefix}] {rows[0]}"] + ["    " + row for row in rows[1:]]


def render_event(event):
    """Render one decoded provider event into readable lines.

    Handles Codex exec --json (thread/turn/item events) and Claude
    --output-format stream-json (system/assistant/user/result) records, and
    degrades to a compact dump for anything sparse or unknown.
    """
    if not isinstance(event, dict):
        return [f"[event] {_compact(event)}"]
    kind = str(event.get("type") or "")
    if not kind:
        return [f"[event] {_compact(event)}"]
    # ---- Codex exec --json ------------------------------------------------
    if kind == "thread.started":
        return [f"[session] Codex thread {event.get('thread_id', '?')}"]
    if kind == "turn.started":
        return ["[turn] started"]
    if kind == "turn.completed":
        usage = event.get("usage") or {}
        detail = ", ".join(f"{key}={usage[key]}" for key in ("input_tokens", "cached_input_tokens", "output_tokens") if key in usage)
        return ["[turn] completed" + (f" ({detail})" if detail else "")]
    if kind == "turn.failed":
        error = event.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        return _lines("error", f"turn failed: {message or 'no detail'}")
    if kind == "error":
        return _lines("error", event.get("message") or _compact(event))
    if kind.startswith("item."):
        item = event.get("item")
        if not isinstance(item, dict):
            return [f"[{kind}] {_compact(event)}"]
        phase = kind.split(".", 1)[1]
        item_type = str(item.get("type") or "item")
        if item_type == "agent_message":
            return _lines("assistant", _clip(item.get("text"), 6000)) if phase == "completed" else []
        if item_type == "reasoning":
            # Provider-supplied summaries only; private reasoning is not available.
            return _lines("thinking summary", _clip(item.get("text"), 800)) if phase == "completed" and item.get("text") else []
        if item_type == "command_execution":
            if phase == "started":
                return _lines("command", item.get("command"))
            if phase == "completed":
                status = item.get("status") or "completed"
                rows = [f"[result] exit {item.get('exit_code', '?')} ({status}): {_clip(str(item.get('command') or ''), 200)}"]
                output = item.get("aggregated_output")
                if output:
                    rows += ["    " + row for row in _clip(output).split("\n")]
                return rows
            return []
        if item_type == "file_change":
            if phase != "completed":
                return []
            changes = item.get("changes") or []
            rows = [f"[files] {item.get('status', 'completed')}"]
            for change in changes:
                if isinstance(change, dict):
                    rows.append(f"    {change.get('kind', 'change')}: {change.get('path', '?')}")
            return rows
        if item_type == "mcp_tool_call":
            name = f"{item.get('server', '?')}.{item.get('tool', '?')}"
            if phase == "started":
                return [f"[tool] {name} {_tool_summary(name, item.get('arguments'))}"]
            if phase == "completed":
                if item.get("error"):
                    return _lines("tool error", f"{name}: {_result_text(item.get('error'))}")
                return _lines("tool result", f"{name}: {_clip(_result_text(item.get('result')))}")
            return []
        if item_type == "web_search":
            return [f"[search] {item.get('query', '')}"] if phase != "updated" else []
        if item_type == "todo_list":
            if phase == "started":
                return []
            rows = ["[plan]"]
            for entry in item.get("items") or []:
                if isinstance(entry, dict):
                    rows.append(f"    - {'[x]' if entry.get('completed') else '[ ]'} {entry.get('text', '')}")
            return rows
        if item_type == "error":
            return _lines("error", item.get("message") or _compact(item))
        return [f"[{item_type}] {phase}: {_compact(item)}"]
    # ---- Claude stream-json ------------------------------------------------
    if kind == "system":
        if event.get("subtype") == "init":
            return [f"[session] Claude session {event.get('session_id', '?')} model {event.get('model', '?')}"]
        return [f"[system] {event.get('subtype', '')} {_compact({k: v for k, v in event.items() if k not in ('type', 'subtype')})}".rstrip()]
    if kind == "assistant":
        rows = []
        message = event.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and block.get("text"):
                rows += _lines("assistant", _clip(block["text"], 6000))
            elif block_type == "tool_use":
                rows.append(f"[tool] {block.get('name', '?')} {_tool_summary(block.get('name'), block.get('input'))}")
            elif block_type == "thinking":
                continue
        return rows or [f"[assistant] {_compact(message)}"]
    if kind == "user":
        rows = []
        message = event.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return _lines("user", _clip(content))
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                prefix = "result error" if block.get("is_error") else "result"
                rows += _lines(prefix, _clip(_result_text(block.get("content"))))
            elif isinstance(block, dict) and block.get("type") == "text":
                rows += _lines("user", _clip(block.get("text")))
        return rows or [f"[user] {_compact(message)}"]
    if kind == "result":
        state = "error" if event.get("is_error") else "done"
        rows = [f"[{state}] {event.get('subtype', '')} turns={event.get('num_turns', '?')} duration_ms={event.get('duration_ms', '?')}".rstrip()]
        if event.get("result"):
            rows += ["    " + row for row in _clip(str(event["result"]), 6000).split("\n")]
        return rows
    if kind in ("stream_event", "rate_limit_event"):
        return []
    return [f"[{kind}] {_compact({k: v for k, v in event.items() if k != 'type'})}"]


def render_line(line):
    """Render one raw JSONL line; non-JSON lines are shown verbatim as raw."""
    text = line.rstrip("\r\n")
    if not text.strip():
        return []
    try:
        event = json.loads(text)
    except ValueError:
        return [f"[raw] {_clip(text)}"]
    return render_event(event)


def render_events(lines):
    rendered = []
    for line in lines:
        rendered.extend(render_line(line))
    return rendered


def runner_status(request_dir):
    return workflows._json(Path(request_dir) / STATUS_NAME, {}) or {}


def session_title(request_dir):
    """Role, model, effort and account for the viewer caption; nothing secret."""
    request_dir = Path(request_dir)
    manifest = workflows._json(request_dir / "manifest.json", {}) or {}
    status = runner_status(request_dir)
    role = manifest.get("role") or status.get("role") or "AI run"
    label = manifest.get("model_label") or status.get("model") or manifest.get("model") or "model"
    effort = manifest.get("effort") or status.get("effort") or ""
    email = status.get("account_email") or manifest.get("expected_account_email") or "account verified at launch"
    title = f"Stage 2 {workflows.ROLE_TITLES.get(role, role)} · {label}"
    if effort:
        title += f" / {workflows.EFFORT_LABELS.get(effort, effort)}"
    return f"{title} · {email} — live output (read-only)"


def render_transcript(request_dir):
    """Readable transcript of a recorded run: header, events, stderr, status."""
    request_dir = Path(request_dir)
    rows = [session_title(request_dir), VIEWER_NOTICE, f"Request folder: {request_dir}", ""]
    events = request_dir / EVENTS_NAME
    if events.is_file():
        rows += render_events(events.read_text(encoding="utf-8", errors="replace").splitlines())
    else:
        rows.append(f"[runner] {EVENTS_NAME} has not been created yet.")
    stderr = request_dir / STDERR_NAME
    if stderr.is_file():
        text = stderr.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            rows += [""] + [f"[stderr] {row}" for row in _clip(text, 4000).split("\n")]
    status = runner_status(request_dir)
    if status:
        rows += ["", f"[runner] state={status.get('state', '?')} exit={status.get('process_exit_code', 'not exited')}"]
        rows.append("[runner] A CLI exit alone is not review completion; outputs and record reconciliation are verified separately.")
    return "\n".join(rows) + "\n"


# --------------------------------------------------------------------------- #
# Self-contained PowerShell viewer (works from source and the frozen exe)
# --------------------------------------------------------------------------- #
VIEWER_SCRIPT = r'''# Stage 2 read-only live output viewer. Generated by stage2_live_output.py.
# Shows a supervised AI run's recorded event stream. It never submits prompts,
# resumes an agent or signals the provider process.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RequestDir,
    [string]$Title = 'Stage 2 AI live output (read-only)',
    [switch]$Once,
    [int]$PollMilliseconds = 500
)
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { $Host.UI.RawUI.WindowTitle = $Title } catch {}
$script:Clip = 1500

function Clip-Text([string]$Text, [int]$Limit = $script:Clip) {
    if ($null -eq $Text) { return '' }
    if ($Text.Length -le $Limit) { return $Text }
    return $Text.Substring(0, $Limit) + ('... (+{0} chars not shown)' -f ($Text.Length - $Limit))
}
function Compact-Json($Value, [int]$Limit = 400) {
    try { return (Clip-Text (ConvertTo-Json -InputObject $Value -Compress -Depth 6) $Limit) } catch { return (Clip-Text ("$Value") $Limit) }
}
function Prop($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) { if ($Object.Contains($Name)) { return $Object[$Name] } else { return $null } }
    $member = $Object.PSObject.Properties[$Name]
    if ($null -ne $member) { return $member.Value }
    return $null
}
function Tool-Summary($Arguments) {
    if ($null -eq $Arguments) { return '' }
    if ($Arguments -is [string]) { return (Clip-Text $Arguments 300) }
    foreach ($key in @('command', 'cmd', 'file_path', 'path', 'pattern', 'query', 'url', 'prompt', 'description')) {
        $value = Prop $Arguments $key
        if ($value) { return ('{0}={1}' -f $key, (Clip-Text ("$value") 300)) }
    }
    return (Compact-Json $Arguments 300)
}
function Result-Text($Content) {
    if ($null -eq $Content) { return '' }
    if ($Content -is [string]) { return $Content }
    if ($Content -is [PSCustomObject]) {
        # A one-element block list unrolls to a single object in PowerShell.
        foreach ($key in @('text', 'message', 'content')) { $value = Prop $Content $key; if ($value) { return "$value" } }
        return (Compact-Json $Content)
    }
    if ($Content -is [System.Collections.IEnumerable]) {
        $parts = @()
        foreach ($block in $Content) {
            $text = Prop $block 'text'
            if ($null -eq $text) { $text = Prop $block 'content' }
            if ($null -eq $text) { $text = Compact-Json $block 200 }
            $parts += "$text"
        }
        return ($parts -join "`n")
    }
    return (Compact-Json $Content)
}
function Lines([string]$Prefix, $Text) {
    $body = ("$Text").TrimEnd("`r", "`n")
    if (-not $body) { return @("[$Prefix]") }
    $rows = $body -split "`n"
    $out = @("[$Prefix] " + $rows[0])
    for ($i = 1; $i -lt $rows.Count; $i++) { $out += ('    ' + $rows[$i]) }
    return $out
}
function Render-Event($Event) {
    if ($null -eq $Event -or -not ($Event -is [PSCustomObject])) { return @('[event] ' + (Compact-Json $Event)) }
    $kind = "$(Prop $Event 'type')"
    if (-not $kind) { return @('[event] ' + (Compact-Json $Event)) }
    switch ($kind) {
        'thread.started' { return @('[session] Codex thread ' + "$(Prop $Event 'thread_id')") }
        'turn.started' { return @('[turn] started') }
        'turn.completed' {
            $usage = Prop $Event 'usage'
            $detail = @()
            foreach ($key in @('input_tokens', 'cached_input_tokens', 'output_tokens')) { $v = Prop $usage $key; if ($null -ne $v) { $detail += ('{0}={1}' -f $key, $v) } }
            if ($detail.Count) { return @('[turn] completed (' + ($detail -join ', ') + ')') }
            return @('[turn] completed')
        }
        'turn.failed' {
            $err = Prop $Event 'error'
            $message = if ($err -is [string]) { $err } else { Prop $err 'message' }
            if (-not $message) { $message = 'no detail' }
            return (Lines 'error' ('turn failed: ' + $message))
        }
        'error' { $m = Prop $Event 'message'; if (-not $m) { $m = Compact-Json $Event }; return (Lines 'error' $m) }
        'system' {
            if ("$(Prop $Event 'subtype')" -eq 'init') { return @('[session] Claude session ' + "$(Prop $Event 'session_id')" + ' model ' + "$(Prop $Event 'model')") }
            return @('[system] ' + "$(Prop $Event 'subtype')")
        }
        'assistant' {
            $rows = @()
            $content = Prop (Prop $Event 'message') 'content'
            if ($content -is [string]) { return (Lines 'assistant' (Clip-Text $content 6000)) }
            foreach ($block in @($content)) {
                $bt = "$(Prop $block 'type')"
                if ($bt -eq 'text' -and (Prop $block 'text')) { $rows += (Lines 'assistant' (Clip-Text "$(Prop $block 'text')" 6000)) }
                elseif ($bt -eq 'tool_use') { $rows += ('[tool] ' + "$(Prop $block 'name')" + ' ' + (Tool-Summary (Prop $block 'input'))) }
            }
            if ($rows.Count) { return $rows }
            return @('[assistant] ' + (Compact-Json (Prop $Event 'message')))
        }
        'user' {
            $rows = @()
            $content = Prop (Prop $Event 'message') 'content'
            if ($content -is [string]) { return (Lines 'user' (Clip-Text $content)) }
            foreach ($block in @($content)) {
                $bt = "$(Prop $block 'type')"
                if ($bt -eq 'tool_result') {
                    $prefix = if (Prop $block 'is_error') { 'result error' } else { 'result' }
                    $rows += (Lines $prefix (Clip-Text (Result-Text (Prop $block 'content'))))
                } elseif ($bt -eq 'text') { $rows += (Lines 'user' (Clip-Text "$(Prop $block 'text')")) }
            }
            if ($rows.Count) { return $rows }
            return @('[user] ' + (Compact-Json (Prop $Event 'message')))
        }
        'result' {
            $state = if (Prop $Event 'is_error') { 'error' } else { 'done' }
            $rows = @(('[{0}] {1} turns={2} duration_ms={3}' -f $state, "$(Prop $Event 'subtype')", "$(Prop $Event 'num_turns')", "$(Prop $Event 'duration_ms')").TrimEnd())
            $text = Prop $Event 'result'
            if ($text) { foreach ($row in ((Clip-Text "$text" 6000) -split "`n")) { $rows += ('    ' + $row) } }
            return $rows
        }
        'stream_event' { return @() }
        'rate_limit_event' { return @() }
    }
    if ($kind.StartsWith('item.')) {
        $item = Prop $Event 'item'
        if ($null -eq $item) { return @("[$kind] " + (Compact-Json $Event)) }
        $phase = $kind.Substring(5)
        $itemType = "$(Prop $item 'type')"
        if (-not $itemType) { $itemType = 'item' }
        switch ($itemType) {
            'agent_message' { if ($phase -eq 'completed') { return (Lines 'assistant' (Clip-Text "$(Prop $item 'text')" 6000)) }; return @() }
            'reasoning' { if ($phase -eq 'completed' -and (Prop $item 'text')) { return (Lines 'thinking summary' (Clip-Text "$(Prop $item 'text')" 800)) }; return @() }
            'command_execution' {
                if ($phase -eq 'started') { return (Lines 'command' (Prop $item 'command')) }
                if ($phase -eq 'completed') {
                    $status = Prop $item 'status'; if (-not $status) { $status = 'completed' }
                    $exit = Prop $item 'exit_code'; if ($null -eq $exit) { $exit = '?' }
                    $rows = @(('[result] exit {0} ({1}): {2}' -f $exit, $status, (Clip-Text "$(Prop $item 'command')" 200)))
                    $output = Prop $item 'aggregated_output'
                    if ($output) { foreach ($row in ((Clip-Text "$output") -split "`n")) { $rows += ('    ' + $row) } }
                    return $rows
                }
                return @()
            }
            'file_change' {
                if ($phase -ne 'completed') { return @() }
                $status = Prop $item 'status'; if (-not $status) { $status = 'completed' }
                $rows = @("[files] $status")
                foreach ($change in @(Prop $item 'changes')) { if ($null -ne $change) { $rows += ('    {0}: {1}' -f "$(Prop $change 'kind')", "$(Prop $change 'path')") } }
                return $rows
            }
            'mcp_tool_call' {
                $name = "$(Prop $item 'server')" + '.' + "$(Prop $item 'tool')"
                if ($phase -eq 'started') { return @('[tool] ' + $name + ' ' + (Tool-Summary (Prop $item 'arguments'))) }
                if ($phase -eq 'completed') {
                    if (Prop $item 'error') { return (Lines 'tool error' ($name + ': ' + (Result-Text (Prop $item 'error')))) }
                    return (Lines 'tool result' ($name + ': ' + (Clip-Text (Result-Text (Prop $item 'result')))))
                }
                return @()
            }
            'web_search' { if ($phase -ne 'updated') { return @('[search] ' + "$(Prop $item 'query')") }; return @() }
            'todo_list' {
                if ($phase -eq 'started') { return @() }
                $rows = @('[plan]')
                foreach ($entry in @(Prop $item 'items')) { if ($null -ne $entry) { $mark = if (Prop $entry 'completed') { '[x]' } else { '[ ]' }; $rows += ('    - ' + $mark + ' ' + "$(Prop $entry 'text')") } }
                return $rows
            }
            'error' { $m = Prop $item 'message'; if (-not $m) { $m = Compact-Json $item }; return (Lines 'error' $m) }
        }
        return @("[$itemType] $phase" + ': ' + (Compact-Json $item))
    }
    $rest = [ordered]@{}
    foreach ($member in $Event.PSObject.Properties) { if ($member.Name -ne 'type') { $rest[$member.Name] = $member.Value } }
    return @("[$kind] " + (Compact-Json $rest))
}
function Render-Line([string]$Line) {
    $text = $Line.TrimEnd("`r", "`n")
    if (-not $text.Trim()) { return @() }
    try { $obj = ConvertFrom-Json -InputObject $text } catch { return @('[raw] ' + (Clip-Text $text)) }
    return (Render-Event $obj)
}
function Write-Rows($Rows) {
    foreach ($row in @($Rows)) {
        if ($null -eq $row) { continue }
        $color = 'Gray'
        if ($row.StartsWith('[assistant]')) { $color = 'White' }
        elseif ($row.StartsWith('[tool') -or $row.StartsWith('[command]') -or $row.StartsWith('[files]') -or $row.StartsWith('[search]') -or $row.StartsWith('[plan]')) { $color = 'Cyan' }
        elseif ($row.StartsWith('[result')) { $color = 'DarkGray' }
        elseif ($row.StartsWith('[error]') -or $row.StartsWith('[result error]') -or $row.StartsWith('[tool error]') -or $row.StartsWith('[stderr]')) { $color = 'Red' }
        elseif ($row.StartsWith('[runner]') -or $row.StartsWith('[session]') -or $row.StartsWith('[turn]') -or $row.StartsWith('[done]')) { $color = 'Yellow' }
        Write-Host $row -ForegroundColor $color
    }
}
# Incremental tailing without locking the writer: keep byte offsets and only
# decode through the last complete line so multi-byte UTF-8 never splits.
function New-Tail([string]$Path) { return @{ Path = $Path; Offset = 0L; Pending = New-Object 'System.Collections.Generic.List[byte]' } }
function Read-NewLines($Tail) {
    if (-not (Test-Path -LiteralPath $Tail.Path)) { return @() }
    try {
        $stream = [System.IO.File]::Open($Tail.Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
    } catch { return @() }
    try {
        if ($stream.Length -lt $Tail.Offset) { $Tail.Offset = 0L; $Tail.Pending.Clear() }
        if ($stream.Length -eq $Tail.Offset) { return @() }
        $stream.Seek($Tail.Offset, [System.IO.SeekOrigin]::Begin) | Out-Null
        $count = [int]($stream.Length - $Tail.Offset)
        $buffer = New-Object byte[] $count
        $read = $stream.Read($buffer, 0, $count)
        $Tail.Offset += $read
        if ($read -lt $count) { $buffer = $buffer[0..($read - 1)] }
        $Tail.Pending.AddRange([byte[]]$buffer)
    } finally { $stream.Dispose() }
    $bytes = $Tail.Pending.ToArray()
    $last = [Array]::LastIndexOf($bytes, [byte]10)
    if ($last -lt 0) { return @() }
    $complete = $bytes[0..$last]
    $Tail.Pending.Clear()
    if ($last -lt ($bytes.Length - 1)) { $Tail.Pending.AddRange([byte[]]$bytes[($last + 1)..($bytes.Length - 1)]) }
    $text = [System.Text.Encoding]::UTF8.GetString([byte[]]$complete)
    return @($text -split "`n")
}
function Read-Status([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return (ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8))) } catch { return $null }
}

$eventsPath = Join-Path $RequestDir 'provider-events.jsonl'
$stderrPath = Join-Path $RequestDir 'provider-stderr.log'
$statusPath = Join-Path $RequestDir 'runner-status.json'
$manifestPath = Join-Path $RequestDir 'manifest.json'
Write-Host $Title -ForegroundColor Green
Write-Host 'Read-only live output viewer. This window shows the supervised run''s recorded events; it does not submit prompts, resume the agent or cancel the run. Closing it leaves the AI process running.' -ForegroundColor DarkGray
Write-Host ('Request folder: ' + $RequestDir) -ForegroundColor DarkGray
$manifest = Read-Status $manifestPath
if ($null -ne $manifest) {
    Write-Host ('[runner] role={0} model={1} effort={2} account={3} state={4}' -f "$(Prop $manifest 'role')", "$(Prop $manifest 'model')", "$(Prop $manifest 'effort')", "$(Prop $manifest 'expected_account_email')", "$(Prop $manifest 'state')") -ForegroundColor Yellow
} else {
    Write-Host '[runner] manifest.json is not readable; showing whatever events exist.' -ForegroundColor Yellow
}
Write-Host ''
$events = New-Tail $eventsPath
$errors = New-Tail $stderrPath
$lastState = ''
$announcedWait = $false
while ($true) {
    $status = Read-Status $statusPath
    if ($null -ne $status) {
        $state = "$(Prop $status 'state')"
        if ($state -and $state -ne $lastState) { Write-Rows @("[runner] state=$state"); $lastState = $state }
    }
    if (-not (Test-Path -LiteralPath $eventsPath)) {
        if (-not $announcedWait) { Write-Rows @('[runner] waiting for provider-events.jsonl (run not started yet)'); $announcedWait = $true }
    } else {
        foreach ($line in (Read-NewLines $events)) { Write-Rows (Render-Line $line) }
    }
    foreach ($line in (Read-NewLines $errors)) { if ($line.Trim()) { Write-Rows @('[stderr] ' + $line.TrimEnd("`r")) } }
    $exited = $null
    if ($null -ne $status) { $exited = Prop $status 'process_exited_utc' }
    if ($exited) {
        foreach ($line in (Read-NewLines $events)) { Write-Rows (Render-Line $line) }
        foreach ($line in (Read-NewLines $errors)) { if ($line.Trim()) { Write-Rows @('[stderr] ' + $line.TrimEnd("`r")) } }
        Write-Rows @(('[runner] process exited (code {0}) at {1}; state={2}' -f "$(Prop $status 'process_exit_code')", "$exited", "$(Prop $status 'state')"))
        Write-Rows @('[runner] A CLI exit alone is not review completion; outputs and record reconciliation are verified separately.')
        break
    }
    if ($Once) { break }
    Start-Sleep -Milliseconds $PollMilliseconds
}
if (-not $Once) {
    Write-Host ''
    Write-Host 'Live output finished. Press Enter to close this viewer window.' -ForegroundColor Green
    try { [void](Read-Host) } catch {}
}
'''


def viewer_script_path(script_dir=None):
    """Write the self-contained viewer script once per content hash; return its path."""
    directory = Path(script_dir) if script_dir else workflows.default_workspace_root() / "viewer"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(VIEWER_SCRIPT.encode("utf-8")).hexdigest()[:12]
    target = directory / f"live-output-viewer-{digest}.ps1"
    if not target.is_file() or target.read_text(encoding="utf-8-sig") != VIEWER_SCRIPT:
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(VIEWER_SCRIPT, encoding="utf-8")
        os.replace(temporary, target)
    return target


def viewer_shell():
    return shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"


def viewer_command(request_dir, *, title=None, once=False, script_dir=None, shell=None):
    """argv for the read-only viewer; safe for any path/title characters.

    Arguments are passed as a real argv list (quoted by the Windows codec in
    subprocess), never through cmd.exe or string interpolation.
    """
    request_dir = Path(request_dir)
    script = viewer_script_path(script_dir)
    args = [shell or viewer_shell(), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-RequestDir", str(request_dir), "-Title", str(title or session_title(request_dir))]
    if once:
        args.append("-Once")
    return args


def open_live_output(request_dir, *, title=None, script_dir=None, shell=None, popen=None):
    """Open a visible console showing the supervised run's live output.

    Returns the viewer process. The viewer is read-only: it never submits or
    resumes an agent, and terminating it does not affect the provider process.
    Nothing in the request folder is modified.
    """
    request_dir = Path(request_dir)
    if not request_dir.is_dir() or not (request_dir / "manifest.json").is_file():
        raise workflows.WorkflowError("This AI request folder does not exist or has no manifest. Nothing was opened.")
    args = viewer_command(request_dir, title=title, script_dir=script_dir, shell=shell)
    launcher = popen or subprocess.Popen
    try:
        # Explicitly a new visible console: the user asked to see the work.
        return launcher(args, cwd=str(request_dir), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    except OSError as exc:
        raise workflows.WorkflowError("The live output viewer could not be opened. The supervised run, if any, is unaffected.") from exc
