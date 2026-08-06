# vocabulary/

**Everything in this folder except this file and `export_vocabulary.py` is a
generated mirror. Editing it changes nothing.**

The controlled vocabulary and the disambiguation rules are not data files — they
are constants in the Stage 2 source:

| What | Where |
|---|---|
| Controlled vocabulary, Crucial tier (13) | `SEED_CRUCIAL` — `src/Stage2_Processing.pyw` line ~347 |
| Controlled vocabulary, Important tier (61) | `SEED_IMPORTANT` — line ~522 |
| Controlled vocabulary, Other tier (13) | `SEED_OTHER` — line ~913 |
| Disambiguation rules (~17,000 chars, sent as the system prompt) | `DISAMBIGUATION_RULES` — line ~2255 |
| Retired / renamed names, pruned from old workbooks | `RETIRED_NAMES` — line ~1029 |
| Types Stage 3 uploads one-at-a-time | `OVERWRITE_TYPES` — line ~1046 |

Read **[../docs/VOCABULARY_GUIDE.md](../docs/VOCABULARY_GUIDE.md)** before
changing any of them. It explains the golden rule (never hardcode filenames),
the exact edit procedure, and a worked example.

## The generated files

Run this after any vocabulary change so the pull request diff shows what
actually changed, instead of hiding it inside a 9,500-line file:

```powershell
python vocabulary\export_vocabulary.py
```

| File | Contents |
|---|---|
| `controlled_vocabulary.csv` | `tier, overwrite_type, name, description` — one row per document type |
| `disambiguation_rules.txt` | the full rules block, as the model receives it |
| `retired_names.txt` | names pruned from older workbooks |

## Why the vocabulary is not stored here as the source of truth

The seeds are only what a **fresh install** starts with. At runtime the app
writes them into a workbook and that workbook then takes over:

```
SEED_* constants  --(first run only)-->  %APPDATA%\DocReviewAIStation\Filename Identification Record.xlsx
                                          ^ from then on, THIS is what the app reads
```

Every run loads the workbook, prunes `RETIRED_NAMES` from it, and appends any
new type you accept from the "unknown document" prompt. So the workbook
accumulates and drifts ahead of the seeds by design.

On the developer machine as of 2026-08-06 the workbook holds all 87 seeded
types plus 4 accumulated rows (`MOT history check`, a duplicate
`MOT test certificate`, the bare `Other` row, and a lower-cased
`other - action plan`). That drift is normal — but it is also why a fix must go
into the seeds **and** be reflected in the workbook to take effect on machines
that already have one. `VOCABULARY_GUIDE.md` covers both halves.

The workbook itself is deliberately **not** committed: its "Run Status" tab
records the care home and worker name a run stopped on, which is personal data.
