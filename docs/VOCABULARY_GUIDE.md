# Fixing misclassifications the right way

> ## The golden rule
>
> **Never fix a classification by hardcoding a filename.**
>
> No `if filename == "..."`. No list of known-bad names. No special case for one
> worker, one care home, or one document. Every classification fix goes into the
> **controlled vocabulary** or the **disambiguation rules**, so it applies to
> every document Stage 2 will ever see.
>
> A hardcoded filename fixes exactly one file and silently rots. A vocabulary
> fix teaches the classifier, and the regression harness can prove it worked.

## Why this matters more than it looks

Stage 2 does not pattern-match filenames. It renders each document, sends the
page images to the Claude API with the vocabulary and rules as the system
prompt, and files the result. The filename a document arrives with is
**deliberately never shown to the model** — incoming filenames are the previous
run's wrong answers, so feeding them back in just launders the mistake. (This
was a real bug: `filename_for_triage` was removed for exactly this reason.)

So there is nowhere sensible to put a filename special-case even if you wanted
one. The vocabulary and the rules *are* the classifier's logic.

---

## Where the vocabulary and rules actually live

They are **constants in the source**, not separate data files:

| What | Constant | Location |
|---|---|---|
| Crucial tier (13 types) | `SEED_CRUCIAL` | [`src/Stage2_Processing.pyw`](../src/Stage2_Processing.pyw) line ~347 |
| Important tier (61 types) | `SEED_IMPORTANT` | line ~522 |
| Other tier (13 types) | `SEED_OTHER` | line ~913 |
| Disambiguation rules (~17,000 chars) | `DISAMBIGUATION_RULES` | line ~2255 |
| Retired / renamed names | `RETIRED_NAMES` | line ~1029 |
| Types Stage 3 overwrite-uploads | `OVERWRITE_TYPES` | line ~1046 |

A flat, reviewable mirror of all of it is generated into
[`../vocabulary/`](../vocabulary/) — see that folder's README. The mirror is
**generated**; editing it does nothing.

### Seeds vs workbook: how an edit reaches a machine that already ran

```
SEED_* constants  --(every run)-->  %APPDATA%\DocReviewAIStation\Filename Identification Record.xlsx
                                     ^ loaded, RE-SEEDED, and rewritten on each load
```

`KnowledgeBase._read()` runs on every launch and, for **every seeded
(canonical) name**, overwrites the workbook's description with the one in the
source. So a description you change in `SEED_*` lands automatically on the next
run, on your machine and on colleagues' — no Excel editing, no row deleting.
Types a *user* added from the unknown-document prompt are not seeded, so they
keep their own text.

The same load also prunes anything in `RETIRED_NAMES` and drops a workbook row
that duplicates a seeded name in different casing.

**What still needs a second edit:**

- **Renaming or removing a type** — the old spelling must go into
  `RETIRED_NAMES`, or existing workbooks keep serving the dead name.
- **Changing `OVERWRITE_TYPES`** — Stage 3 mirrors that set; edit both sides.

**Prove it landed** rather than assuming. Load a `KnowledgeBase` and read the
workbook back off disk (this is what the 2026-08-11 pass did — see the
propagation check in that changelog entry):

```python
kb = KnowledgeBase()                      # exactly what a run does
assert "your new wording" in kb.vocabulary_block()
```

---

## How to add a new document type

1. Pick the tier. This is not cosmetic — it changes where Stage 3 uploads the file:
   - **`SEED_CRUCIAL`** — right-to-work / sponsorship critical documents.
   - **`SEED_IMPORTANT`** — normal compliance documents (most things).
   - **`SEED_OTHER`** — real documents that exist but need no compliance slot.
2. Add a `(name, description)` tuple. The **name becomes the filename**, so it
   must be valid on Windows (no `/ \ : * ? " < > |`) and under 80 characters
   (`MAX_NAME_LEN`). Match the Lifted portal's document-type spelling exactly —
   e.g. `Non UK Driving Licence` has no hyphen because the portal's type has
   none, and the hyphenated variant had to be retired when Stage 3 could not
   find it at upload.
3. Write the description as **identification features, plus explicit
   exclusions**. The exclusions do the real work. Follow the house style:

   ```python
   ("Hospital Discharge Letter", "An NHS hospital DISCHARGE letter/summary - "
                   "hospital or NHS-trust letterhead, diagnoses, a clinical "
                   "narrative, and 'Discharge Details' (date/time of "
                   "discharge, disposition). It is a clinical document about "
                   "an episode of care. It is NOT any DBS document (even if "
                   "kept near DBS paperwork), NOT a 'Health Declaration' "
                   "(the form the worker fills in about their own health), "
                   "and NOT 'Medical Conditions'."),
   ```

   Name the types it will be confused with, in quotes, with `NOT`. Capitalise
   the distinguishing feature.
4. If it needs an individual overwrite-upload rather than a bulk upload, add it
   to `OVERWRITE_TYPES` — **and add it to Stage 3's mirrored set too.** The two
   must stay in sync or the upload silently fails.
5. Regenerate the mirror and commit both:
   ```powershell
   python vocabulary\export_vocabulary.py
   ```

## How to add or tighten a disambiguation rule

`DISAMBIGUATION_RULES` is one big concatenated string, structured as:

- **Three overriding rules (I, II, III).** Rule I is the important one:
  *classify a document by what it IS, never by what it mentions, references or
  accompanies.* Most misclassifications are a violation of rule I.
- **Numbered rules 0–19**, each a specific confusion pair or cluster, e.g.
  `1. NATIONAL INSURANCE vs PASSPORT`, `6. DBS DOCUMENTS - FOUR DISTINCT
  THINGS`, `18. MIXED BUNDLES`.

Prefer tightening a type's description first. Reach for a rule when the fix is
about the *relationship between two types* rather than one type's definition.

Write rules as `X vs Y` with the deciding test, not as prose. Keep them short —
the whole block is sent on every single call, and it is prompt-cached, so
churn costs money as well as accuracy.

---

## Worked example: the pay-rise letter

This is a real fix from the 2026-07-15 Southern Sefton audit.

**Symptom.** Letters that only told a worker their new hourly rate were being
filed as `Employment Contract Amendment` — a Crucial-tier type. Wrong tier,
wrong upload slot, and it polluted a right-to-work-adjacent category.

**The wrong fix.** Special-casing the filename, or a keyword rule like
"if it says 'salary' it's not an amendment". Both are brittle: a genuine
variation of terms very often changes pay too, so a keyword test breaks the
true positives while chasing the false ones.

**The right fix.** The classifier was behaving correctly given a vague
definition — the definition did not say what an amendment *requires*. So the
`Employment Contract Amendment` description in `SEED_CRUCIAL` gained a
requirement and an explicit exclusion, with somewhere for the rejected
documents to go:

```python
("Employment Contract Amendment", "A FORMAL VARIATION/AMENDMENT to "
                    "existing contract terms - titled e.g. 'Amendment to "
                    "Contract', 'Variation of Terms', 'Change to Terms and "
                    "Conditions', 'Change in contract', typically short "
                    "and referring back to an existing contract, changing "
                    "the POSITION/role, hours, or contractual terms "
                    "(possibly together with pay). It is NOT the contract "
                    "/ statement-of-terms itself ('Employment Contract'), "
                    "and NOT a simple pay-rise letter: a letter that ONLY "
                    "notifies a new salary/hourly rate with no change of "
                    "role or terms is 'Other - salary increase letter', "
                    "not an amendment."),
```

Three things make this work, and they generalise to any fix:

1. **A positive requirement** — a change of role, hours or terms. Pay alone is
   not enough.
2. **An explicit exclusion** naming the confusable case in the words it
   actually appears in ("a simple pay-rise letter").
3. **A destination for the excluded document** — `Other - salary increase
   letter`. Without this the classifier has nowhere to put it and produces a
   low-confidence `Other - Unknown` instead, which just moves the problem.

The same audit fixed `Probation Review` (now requires explicit probation
wording, so appraisal/induction/sickness reviews no longer match) and rewrote
rule 18 (a covering email never outranks the document it transmits) by exactly
this pattern.

**Then prove it.** Re-run the regression harness against the ground-truth set:

```powershell
python src\eval_classifier.py --tag my-fix
```

It re-runs the real production path (`classify_document_core` +
`validate_result` — the same functions the live Engine calls) so a pass means
the app is fixed, not just the harness. The ground-truth documents are real
worker files and are **not** in this repo; point the harness at your local copy
with `set STAGE2_GT_ROOT=<folder>`. It costs real API money and asks for
confirmation before sending anything.

---

## Things that will bite you

- **Invented names are rejected, not accepted.** `validate_result` snaps a
  case/spacing variant to the canonical spelling and otherwise marks the result
  unmatched. If you add a type but spell it differently in a rule, the rule does
  nothing. Copy-paste the exact name.
- **Never name a type `Other`.** The bare `Other` row is excluded from the
  prompt vocabulary and guarded against in the accept paths; a type actually
  called `Other` produces `Other - Other` filenames.
- **Confidence thresholds decide whether your fix is even used.** A controlled
  name is accepted on auto-review at confidence ≥ 60 (`AUTO_REVIEW_MATCH_CONF`),
  a descriptive `Other - <label>` relabel at ≥ 40 (`AUTO_REVIEW_LABEL_CONF`),
  and anything under 40 escalates to a Sonnet second opinion
  (`SECOND_OPINION_MAX_CONF`). If a fix is "right but unconfident", sharpen the
  description rather than lowering a threshold.
- **Renaming a type is two edits.** Add the old spelling to `RETIRED_NAMES` as
  well, or existing workbooks keep serving the dead name. (Changing only a
  *description* needs no second edit — see the re-seeding note above.)
- **A regression set needs controls, and controls need checking.** The
  2026-08-11 pass first picked its "still correct" controls automatically from
  an audit's `Correct` rows; **6 of 12 turned out to be audit misses of the very
  patterns being fixed**, so the fixed classifier answering them correctly
  would have been scored as a regression. Look at the pages before you trust a
  control.
- **`OVERWRITE_TYPES` is mirrored in Stage 3.** Editing one side only breaks
  uploads — this exact desync caused the `DBS Check` upload failures in
  2026-07-13.
- **Log the misclassification.** There is a standing instruction to record every
  misnamed document found, user-reported or otherwise, in
  `%APPDATA%\DocReviewAIStation\Misnaming Record.xlsx` (append with
  [`src/misname_log.py`](../src/misname_log.py); open it from **🧰 Tools**).
  Mark it Resolved once the vocabulary fix ships.
- **Verify certificate-like scans at readable size before renaming.** A
  near-miss in 2026-07-17: two lookalike files, and the *genuine* DBS
  certificate was nearly renamed from a thumbnail. Render it properly first.

## Checklist for a vocabulary change

- [ ] Fix is in `SEED_*` / `DISAMBIGUATION_RULES` — **no filename hardcoded anywhere**
- [ ] Positive requirement + explicit exclusion + a destination for excluded docs
- [ ] Exact canonical spelling reused; name is Windows-legal and < 80 chars
- [ ] Old spelling added to `RETIRED_NAMES` if renaming
- [ ] `OVERWRITE_TYPES` updated on **both** sides (Stage 2 and Stage 3) if relevant
- [ ] Existing workbook handled (row deleted, cell edited, or name retired)
- [ ] `python vocabulary\export_vocabulary.py` re-run, mirror committed
- [ ] `eval_classifier.py` re-run, or the reason it was not stated in the PR
- [ ] Misnaming Record row marked Resolved
