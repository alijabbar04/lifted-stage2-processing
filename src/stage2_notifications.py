"""Optional, non-blocking Stage 2 lifecycle notifications.

Only aggregate, template-driven messages leave the app. No documents, worker
names, filenames, paths, API keys, or exception text are accepted by templates.
Settings hold credential references, never tokens. Delivery failures are kept
separate from the processing result and never raise into a processing callback.
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Callable


KEYRING_SERVICE = "Lifted.Stage2.Notifications"
MAX_PENDING = 64
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = 15
PHASES = {
    "preparing": "Preparing documents", "processing": "Processing documents",
    "scanning": "Local document scanning and orientation checks",
    "followup_plan": "Stronger-model follow-up planning",
    "followup_upload": "Stronger-model follow-up preparation and submission",
    "organising": "Organising processed files", "audit": "Post-run accuracy audit",
    "audit_review": "AI audit review", "improvement_review": "AI change review",
    "tests": "Tests", "build": "Desktop build", "batch": "Overnight batch",
    "converting": "PDF conversion", "deduplicating": "Exact-duplicate review",
    "ranking": "Dating, signed checks and ranking", "orientation": "Local page orientation",
}
REASONS = {
    "batch_ambiguous": "A batch submission needs reconciliation before processing can continue. Do not resubmit it.",
    "budget": "The configured budget needs your review before continuing.",
    "login": "The selected AI account needs sign-in before continuing.",
    "account": "The selected AI account or model is not available.",
    "review": "A review decision is needed before continuing.",
    "connection": "The service could not be reached after bounded retries.",
    "general": "Open Stage 2 for the details and next action.",
}
IMPORTANT = {"blocked", "error", "run_complete", "stopped", "audit_complete", "review_complete"}
EVENTS = {
    "test", "run_started", "phase_started", "phase_complete", "progress",
    "worker_complete", "batch_submitted", "batch_waiting", "followup_submitted",
    "long_wait", "audit_started", "audit_complete", "audit_skipped",
    "review_started", "review_complete", "blocked", "error", "stopped", "run_complete",
}


def default_settings() -> dict:
    """Public-install defaults are opt-in and contain no personal destination."""
    return {
        "discord": {"enabled": False, "destination": "", "credential_file": "",
                    "token_env": "DISCORD_BOT_TOKEN", "credential_ref": "discord"},
        "telegram": {"enabled": False, "destination": "", "credential_file": "",
                     "token_env": "TELEGRAM_BOT_TOKEN", "credential_ref": "telegram"},
        "progress_enabled": True, "wait_minutes": 10,
    }


def _integer(value, default=0, minimum=0, maximum=1_000_000_000):
    try:
        return max(minimum, min(maximum, int(value)))
    except (ValueError, TypeError, OverflowError):
        return default


def normalize_settings(settings=None) -> dict:
    """Allow-list settings so a pasted token cannot be persisted by this module."""
    result = default_settings()
    if not isinstance(settings, dict):
        return result
    for channel in ("discord", "telegram"):
        source = settings.get(channel, {})
        if not isinstance(source, dict):
            continue
        result[channel]["enabled"] = source.get("enabled") is True
        for key in ("destination", "credential_file", "token_env", "credential_ref"):
            value = source.get(key, result[channel][key])
            result[channel][key] = str(value or "").strip()[:1024]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", result[channel]["token_env"]):
            result[channel]["token_env"] = default_settings()[channel]["token_env"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", result[channel]["credential_ref"]):
            result[channel]["credential_ref"] = channel
    result["progress_enabled"] = settings.get("progress_enabled", True) is True
    result["wait_minutes"] = _integer(settings.get("wait_minutes", 10), 10, 1, 120)
    return result


def set_bot_token(channel: str, token: str, credential_ref: str | None = None) -> None:
    """Store in the existing OS credential store; never fall back to plaintext."""
    if channel not in ("discord", "telegram"):
        raise ValueError("Choose Discord or Telegram.")
    ref = credential_ref or channel
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", ref):
        raise ValueError("Invalid credential reference.")
    token = str(token or "").strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("Enter a valid bot token without spaces.")
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, ref, token)
    except Exception:
        raise RuntimeError("The OS credential store is unavailable. Select an existing local .env file instead.") from None


def _resolve_token(channel: str, settings: dict) -> str:
    ref = settings.get("credential_ref", channel)
    try:
        import keyring
        value = keyring.get_password(KEYRING_SERVICE, ref)
        if value:
            return value.strip()
    except Exception:
        pass
    env_key = settings.get("token_env", default_settings()[channel]["token_env"])
    if os.environ.get(env_key, "").strip():
        return os.environ[env_key].strip()
    credential_file = settings.get("credential_file", "")
    if credential_file:
        try:
            path = Path(credential_file)
            if path.is_file() and path.stat().st_size <= 128 * 1024:
                for line in path.read_text(encoding="utf-8-sig").splitlines():
                    key, separator, value = line.partition("=")
                    if separator and key.strip() == env_key:
                        return value.strip().strip('"').strip("'")
        except (OSError, UnicodeError):
            pass
    return ""


def credentials_status(channel: str, settings: dict) -> str:
    """Read-only local check. Does not send a message or reveal the token."""
    config = normalize_settings(settings)[channel]
    if not config["destination"]:
        return "Destination not configured"
    if not _valid_destination(channel, config["destination"]):
        return "Destination format is not valid"
    return "Credential available (use Test to verify delivery)" if _resolve_token(channel, config) else "Bot token not found"


def _valid_destination(channel, destination):
    pattern = r"\d{5,25}" if channel == "discord" else r"(?:-?\d{1,25}|@[A-Za-z][A-Za-z0-9_]{4,63})"
    return bool(re.fullmatch(pattern, str(destination)))


def _phase(value):
    return PHASES.get(value, "Processing documents")


def format_event(event: str, **data) -> str:
    """The only notification message boundary; arbitrary text is ignored."""
    if event not in EVENTS:
        raise ValueError("Unknown notification event.")
    phase = _phase(data.get("phase", "processing"))
    completed, total = _integer(data.get("completed")), _integer(data.get("total"))
    completed = min(completed, total) if total else completed
    workers = _integer(data.get("workers"))
    review = _integer(data.get("needs_review"))
    errors = _integer(data.get("errors"), maximum=completed)
    audit_count = (f"{completed:,} document attempt(s) finished, including {errors:,} failed check(s)"
                   if errors else f"{completed:,} document(s) checked")
    documents = _integer(data.get("documents", total))
    batches = _integer(data.get("batches"))
    count = f"{completed:,} of {total:,}" if total else f"{completed:,}"
    reason = REASONS.get(data.get("reason"), REASONS["general"])
    templates = {
        "test": "Notifications are connected. Future updates will cover phase changes, progress, waiting, attention needed and completion. Document contents and worker names are not sent.",
        "run_started": f"Run started: {workers:,} worker folder(s). Preparing documents for processing.",
        "phase_started": f"{phase} started.",
        "phase_complete": f"{phase} finished.",
        "progress": f"{phase}: {count} document(s) finished" + (f" ({completed * 100 // total}%)." if total else "."),
        "worker_complete": f"Worker folders processed: {count}. Continuing with the remaining run steps.",
        "batch_submitted": f"Overnight batch submitted: {documents:,} document request(s) across {batches:,} batch(es). Waiting for the provider; this is not a completed run.",
        "followup_submitted": f"Follow-up batch submitted: {documents:,} request(s) across {batches:,} batch(es). The same run will continue when results are ready.",
        "batch_waiting": "The provider is still processing the overnight batch. No resubmission is needed. This update does not mean the batch is stuck.",
        "long_wait": f"{phase}: no new progress reported for {_integer(data.get('wait_seconds')) // 60:,} minute(s). The current operation may still be working. Check Stage 2 for details; elapsed time alone is not a failure.",
        "audit_started": f"Post-run accuracy audit started: {total:,} document(s) to check. This re-checks filenames; it does not rename files.",
        "audit_complete": f"Post-run accuracy audit finished: {audit_count}; {review:,} flagged for review. The audit is AI evidence, not a guarantee. Open Reports to review it.",
        "audit_skipped": "The post-run accuracy audit was not run. Open Stage 2 for the reason; processing completion does not imply that filenames were audited.",
        "review_started": f"{phase} launched. The selected AI session has the shared naming rules and workflow instructions. Launch is not confirmation that the review has finished.",
        "review_complete": f"{phase} finished. Open Reports for the recorded decisions; inspect validation evidence before accepting software changes.",
        "blocked": f"Action needed. {reason} Completed work and any retained batch state remain available.",
        "error": f"{phase} did not finish. {reason} Review the application log before retrying.",
        "stopped": f"{phase} stopped. This is not a completed run. Check Stage 2 before restarting; an interrupted audit may need to be checked again.",
        "run_complete": f"Run finished: {workers:,} worker folder(s) processed. Open Reports for the run outcome.",
    }
    message = "Stage 2 | " + templates[event]
    if event == "run_complete":
        audit_status = data.get("audit_status", "unknown")
        audit_summary = {
            "complete": f" Accuracy audit finished; {review:,} document(s) flagged for review.",
            "disabled": " Accuracy audit was disabled; filenames have not had the optional post-run check.",
            "skipped": " Accuracy audit was skipped; filenames have not had the optional post-run check.",
            "pending": " Accuracy audit is still pending; this is processing completion only.",
            "failed": " Accuracy audit did not finish; review Stage 2 before treating the audit as complete.",
            "unknown": " Check Reports for the accuracy audit status; this notification does not confirm it finished.",
        }
        message += audit_summary.get(audit_status, audit_summary["unknown"])
    if event == "run_complete" and data.get("cost_gbp") is not None:
        try:
            cost = float(data["cost_gbp"])
            if math.isfinite(cost) and cost >= 0:
                message += f" Estimated API cost: GBP {cost:,.2f} (not a billing statement)."
        except (ValueError, TypeError, OverflowError):
            pass
    return message[:1900]


class DeliveryError(Exception):
    def __init__(self, label, *, retry=False, delay=0):
        super().__init__(label)
        self.retry, self.delay = retry, delay


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_json(url, headers, payload):
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", **headers}, method="POST")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            value = json.loads(response.read(65536).decode("utf-8"))
            if isinstance(value, dict) and value.get("ok") is False:
                code = _integer(value.get("error_code"))
                delay = _integer(value.get("parameters", {}).get("retry_after"), 2, 1, 30)
                raise DeliveryError(f"Provider rejected the message (HTTP {code}).", retry=code == 429 or code >= 500, delay=delay)
            return value
    except urllib.error.HTTPError as error:
        code = error.code
        delay = 2
        if code == 429:
            try:
                body = json.loads(error.read(65536).decode("utf-8"))
                delay = max(1, min(30, float(body.get("retry_after", body.get("parameters", {}).get("retry_after", 2)))))
            except Exception:
                pass
        if code in (401, 403):
            label = "Bot credentials or destination permission were rejected."
        elif code == 404:
            label = "The configured notification destination was not found."
        else:
            label = f"Notification provider returned HTTP {code}."
        raise DeliveryError(label, retry=code == 429 or code >= 500, delay=delay) from None
    except DeliveryError:
        raise
    except Exception:
        # Never forward urllib exceptions: Telegram embeds its token in the URL.
        raise DeliveryError("Notification service could not be reached.", retry=True, delay=2) from None


def _send(channel, config, message, event_id):
    if not _valid_destination(channel, config["destination"]):
        raise DeliveryError("Notification destination is missing or invalid.")
    token = _resolve_token(channel, config)
    if not token:
        raise DeliveryError("Bot token was not found. Configure it in Settings.")
    if channel == "discord":
        _post_json(f"https://discord.com/api/v10/channels/{config['destination']}/messages",
                   {"Authorization": f"Bot {token}", "User-Agent": "LiftedStage2/1.0"},
                   {"content": message, "allowed_mentions": {"parse": []},
                    "nonce": event_id[:24], "enforce_nonce": True})
    else:
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise DeliveryError("Telegram bot token format is invalid.")
        _post_json(f"https://api.telegram.org/bot{token}/sendMessage", {},
                   {"chat_id": config["destination"], "text": message,
                    "link_preview_options": {"is_disabled": True}})


class NotificationService:
    """Thread-safe best-effort queue. emit() performs no filesystem or HTTP I/O.

    Construct once per application; pass the same run_id for resumed batch polls.
    Call drain_statuses() on the Tk thread to surface delivery results. Network
    retries and delays occur only on the daemon worker, never on the Tk thread.
    """

    def __init__(self, settings=None, *, sender: Callable | None = None, clock=None, start=True):
        self._settings = normalize_settings(settings)
        self._sender = sender or _send
        self._clock = clock or time.monotonic
        self._condition = threading.Condition()
        self._pending = collections.deque()
        self._statuses = collections.deque(maxlen=64)
        self._seen = collections.OrderedDict()
        self._progress = {}
        self._waits = {}
        self._stopping = threading.Event()
        self._busy = False
        self._thread = threading.Thread(target=self._work, name="stage2-notifications", daemon=True)
        if start:
            self._thread.start()

    def configure(self, settings):
        with self._condition:
            self._settings = normalize_settings(settings)

    def emit(self, event: str, *, run_id="run", phase="processing", **data) -> bool:
        """Queue an event; False means disabled, duplicate, throttled or closed.

        Progress is sent at 25/50/75/100%, at most once per two minutes except
        100%. Long waits begin at wait_minutes, then at most every 30 minutes.
        Unknown payload fields are ignored by the message formatter.
        """
        try:
            message = format_event(event, phase=phase, **data)
        except (ValueError, TypeError):
            return False
        now = self._clock()
        with self._condition:
            if self._stopping.is_set():
                return False
            channels = [name for name in ("discord", "telegram") if self._settings[name]["enabled"]]
            if not channels:
                return False
            identity = (str(run_id), phase)
            detail = ""
            if event == "progress":
                if not self._settings["progress_enabled"]:
                    return False
                total, done = _integer(data.get("total")), _integer(data.get("completed"))
                if not total or not done:
                    return False
                bucket = min(4, done * 4 // total)
                previous, previous_time = self._progress.get(identity, (0, -float("inf")))
                if not bucket or bucket <= previous or (bucket < 4 and now - previous_time < 120):
                    return False
                detail = str(bucket)
            elif event in ("long_wait", "batch_waiting"):
                if _integer(data.get("wait_seconds")) < self._settings["wait_minutes"] * 60:
                    return False
                previous_time = self._waits.get(identity, -float("inf"))
                if now - previous_time < 1800:
                    return False
                detail = str(int(now // 1800))
            elif event == "worker_complete":
                detail = str(_integer(data.get("completed")))
            elif event in ("batch_submitted", "followup_submitted"):
                detail = str(data.get("batch_id", ""))
            elif event == "test":
                detail = str(time.time_ns())
            elif event in ("blocked", "error"):
                detail = str(data.get("reason", "general"))
            key = hashlib.sha256(repr((identity, event, detail)).encode()).hexdigest()[:24]
            if key in self._seen:
                return False
            if len(self._pending) >= MAX_PENDING:
                if event in IMPORTANT:
                    # Reserve important outcomes by replacing a queued progress
                    # update first. Never grow the queue indefinitely.
                    victim = next((item for item in self._pending if item[0] not in IMPORTANT), None)
                    if victim is not None:
                        self._pending.remove(victim)
                    else:
                        self._statuses.append("Notifications: queue full; latest update was not queued. Processing is unaffected.")
                        return False
                else:
                    self._statuses.append("Notifications: queue full; an update was not queued. Processing is unaffected.")
                    return False
            self._seen[key] = now
            while len(self._seen) > 4096:
                self._seen.popitem(last=False)
            if event == "progress":
                self._progress[identity] = (bucket, now)
            if event in ("long_wait", "batch_waiting"):
                self._waits[identity] = now
            # State used by UI emitters stays bounded across many care homes.
            for mapping in (self._progress, self._waits):
                if len(mapping) > 256:
                    mapping.pop(next(iter(mapping)))
            item = (event, key, message, channels)
            # Preserve lifecycle chronology: completion must not overtake start.
            self._pending.append(item)
            self._condition.notify_all()
            return True

    def drain_statuses(self) -> list[str]:
        with self._condition:
            result = list(self._statuses)
            self._statuses.clear()
            return result

    def is_idle(self) -> bool:
        """Non-blocking, synchronized delivery status for settings/test controls."""
        with self._condition:
            return not self._pending and not self._busy

    def _work(self):
        while not self._stopping.is_set():
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._stopping.is_set())
                if self._stopping.is_set():
                    break
                event, key, message, channels = self._pending.popleft()
                self._busy = True
            for channel in channels:
                with self._condition:
                    config = dict(self._settings[channel])
                if not config["enabled"] or self._stopping.is_set():
                    continue
                last_error = "Notification delivery failed."
                delivered = False
                for attempt in range(MAX_ATTEMPTS):
                    try:
                        self._sender(channel, config, message, key)
                        delivered = True
                        self._status(f"{channel.title()} notification delivered: {event.replace('_', ' ')}.")
                        break
                    except DeliveryError as exc:
                        last_error = str(exc)
                        if not exc.retry or attempt + 1 == MAX_ATTEMPTS:
                            break
                        if self._stopping.wait(min(30, max(1, exc.delay, 2 ** attempt))):
                            break
                    except Exception:
                        # Callers/transport must not leak tokens in error text.
                        last_error = "Notification delivery failed unexpectedly."
                        break
                if not delivered:
                    self._status(f"{channel.title()} notification not delivered: {last_error} Processing is unaffected.")
            with self._condition:
                self._busy = False
                self._condition.notify_all()

    def _status(self, message):
        with self._condition:
            self._statuses.append(message)

    def flush(self, timeout=5.0) -> bool:
        """Bounded wait for tests/shutdown helpers; do not call from Tk callbacks."""
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while self._pending or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def close(self):
        """Non-blocking shutdown; unsent updates are best-effort, not a job log."""
        self._stopping.set()
        with self._condition:
            self._pending.clear()
            self._condition.notify_all()
