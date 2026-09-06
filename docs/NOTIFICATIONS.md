# Stage 2 notifications

Notifications are optional and separate from processing. In Settings, enable
Discord, Telegram, both, or neither. Select the destination and configure a bot
token using the OS credential store or an existing local `.env` file. Use **Test
notification** to confirm delivery. A locally available credential is not proof
that a destination is reachable or that the bot has permission to post.

The installed user's existing Claudius Messenger configuration can be referenced
locally. It is not copied into source code, release archives, installers, or this
guide. Public installations start with both channels disabled. Telegram needs an
explicit chat/channel destination; Stage 2 never guesses a recipient.

## What you receive

| Event | Message and timing |
| --- | --- |
| Run begins | Worker-folder count and preparation started. |
| Phase changes | Preparation, processing, organising, post-run accuracy audit, and AI review launch/finish when the app has evidence of that event. |
| Progress | Aggregate document progress at 25%, 50%, 75%, 100%; at most once per two minutes except final progress. This can be disabled. |
| Worker complete | Count of worker folders processed, without names. |
| Batch submitted | Request/batch counts; explicitly says submission is not run completion. |
| Follow-up batch | Request/batch counts and that the same run continues later. |
| Extended waiting | After ten minutes by default, then no more than every thirty minutes; says waiting alone does not mean failure. |
| Action needed / failed | Short controlled reason and a direction to the application for details. Duplicate reports of the same condition are suppressed. |
| Accuracy audit finished | Documents checked and flags for review; says AI results are not a guarantee and points to Reports. |
| AI review launched | Identifies audit review vs software-change review, without falsely claiming the external session is already finished. |
| Run finished | Worker count, review flags, and available estimated API cost. It points to Reports for actual audit status. |

The audit-progress percentage is **work completed**, not an accuracy score.
Document contents, worker names, filenames, full paths, raw error text, AI prompts,
API keys and account credentials are never included in notification templates.
Notifications do not attach reports or send them to an AI automatically.

## Reliability and delivery limits

Sending runs on a background thread, with a bounded queue and up to three attempts
for transient errors. Rate-limit retry delays are bounded at thirty seconds.
Credential/permission failures are not repeatedly retried. One channel failing
does not stop the other channel or document processing. Activity records a brief,
redacted delivery result; processing success and notification success are separate.

This is a **best-effort notifier**, not a durable queue or replacement for the
application's reports. Unsent messages can be lost when the application exits.
Discord retries use a stable nonce to reduce duplicate messages. Telegram has no
equivalent send-message idempotency key, so a network timeout can produce a duplicate.
Milestone deduplication survives repeated status checks in the same app session;
restarting the app starts a fresh notification session.

## Maintainer integration

`src/stage2_notifications.py` has no new runtime dependency; it uses the standard
library and optionally the already supported `keyring` package. Keep settings at
`config["notifications"] = normalize_settings(...)`. Never add raw bot tokens to
that dictionary. `set_bot_token(channel, token)` stores a token through `keyring`;
if that is unavailable it asks the user to reference an existing `.env` file.

```python
from stage2_notifications import NotificationService, normalize_settings

notifier = NotificationService(config.get("notifications"))
notifier.emit("run_started", run_id=job_id, workers=5)
notifier.emit("phase_started", run_id=job_id, phase="processing")
notifier.emit("progress", run_id=job_id, phase="audit", completed=384, total=600)
notifier.emit("audit_complete", run_id=job_id, total=600, needs_review=7)
notifier.emit("run_complete", run_id=job_id, workers=5, needs_review=7,
              audit_status="complete", cost_gbp=8.42)
# Drain on Tk's main thread, not from a network callback:
for delivery_message in notifier.drain_statuses():
    activity_log(delivery_message)
# Settings changes:
notifier.configure(config["notifications"])
# On application exit, non-blocking:
notifier.close()
```

Use a stable `run_id` for resumed batch status checks. `phase` accepts `preparing`,
`processing`, `organising`, `audit`, `audit_review`, `improvement_review`, `tests`,
`build`, or `batch`. `emit` returns whether the event was queued, not whether it was
delivered. Do not report a queued Test as delivered; show the asynchronously drained
delivery status. Pass elapsed seconds to `long_wait` / `batch_waiting` and call on a
timer; throttling is internal. Do not synthesize an AI-review completion event from
launch success: it needs the external session's recorded terminal outcome.
For `run_complete`, pass the actual `audit_status`: `complete`, `disabled`,
`skipped`, `pending`, or `failed`. The default is `unknown`, which explicitly
does not claim the audit finished or report a misleading zero review flags.

Settings schema (all channel keys are non-secret references):

```json
{
  "discord": {"enabled": false, "destination": "", "credential_file": "", "token_env": "DISCORD_BOT_TOKEN", "credential_ref": "discord"},
  "telegram": {"enabled": false, "destination": "", "credential_file": "", "token_env": "TELEGRAM_BOT_TOKEN", "credential_ref": "telegram"},
  "progress_enabled": true,
  "wait_minutes": 10
}
```

Provider references: [Discord Create Message](https://docs.discord.com/developers/resources/message#create-message)
and [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage).
