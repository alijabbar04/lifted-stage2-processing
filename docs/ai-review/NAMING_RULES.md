# Source-backed naming, ranking, and folder rules

The configured source checkout is authoritative. Read `src/Stage2_Processing.pyw` and `docs/VOCABULARY_GUIDE.md`, especially `SEED_CRUCIAL`, `SEED_IMPORTANT`, `SEED_OTHER`, `RETIRED_NAMES`, `DISAMBIGUATION_RULES`, `base_controlled_name`, `Engine._second_pass`, `OVERWRITE_TYPES`, and `organize_worker`. `src/ai_review.py` reads constants through AST without importing or starting the app and refuses an unfamiliar ranking expression or a changed source snapshot.

## Classification

Classify what the document **is**, not a subject it mentions, a cover email, or the existing filename. Apply the current vocabulary's positive definition and exclusions. Prefer a supported controlled type; if none fits, use `Other - <short factual description>`. Do not invent a new controlled type to accommodate one worker. The helper accepts current seeded controlled names (normalizing case/spacing/hyphens) and descriptive Other names. It does not silently adopt arbitrary custom vocabulary workbook entries: a correction involving a genuinely custom controlled category needs a separately reviewed policy extension or must be deferred.

The controlled base strips a final numeric rank, then a trailing ` - (DD-MM-YYYY)` date. A legacy `Proof of Right to Work` filename is treated as Share Code Check Result by the helper's inventory. Existing unrecognized categories remain evidence; they are not renamed wholesale.

## Quality ranking: worst first, best last

All non-Other controlled-category peers for the same worker are assessed together. A correction out of one controlled category and into another reranks the remaining peers of **both** categories. Rank is not assigned solely from the filename or the incoming document. Inspect actual contents and tie each peer review to its exact hash.

The current Stage 2 comparison is ascending and lexicographic:

```text
(date_rank, signed_bonus, quality_score, legible, complete)
```

- `date_rank` is the supported document date's ordinal only for **Certificate of Sponsorship** and **Share Code Check Result**; it is zero for other categories. CoS uses the **issue/assigned date** (or certificate-status date when explicitly ASSIGNED), not expiry/use-by, employment start, or birth date. Share Code Check Result uses the **employer's check date**, including a supported printed or handwritten check date, not visa expiry. An unavailable date needs an explicit reason, not a guess. These definitions come from `ClaudeAPI.cos_issue_date` and `ClaudeAPI.share_code_check`.
- `signed_bonus` is 1 for an inspected **employee-signed Employment Contract**, 0 otherwise. The source accepts handwriting, a typed signature, an e-signature mark, or a clearly completed signature block with name and date; an empty line is not signed. It precedes quality for contracts; it is not a bonus for unrelated document types or an employer-only signature.
- `quality_score` is 0–100, considering clarity, current validity/recency, relevance, right person, completeness, and category-specific usefulness. Audit confidence is not quality. Missing assessments must fail, never masquerade as score zero.
- Legibility and completeness are boolean tie-breakers. Equal complete keys retain the frozen natural-path inventory order; do not manufacture a distinction.

The lowest-ranked file has no numeric suffix; subsequent files use `(01)`, `(02)`, and so on. The **highest suffix is best**. Supported dates precede that suffix, for example `Certificate of Sponsorship - (01-08-2026) (02).pdf`. A category with one remaining file becomes its unnumbered base (retaining a supported required date).

`Other - …` names are not quality-ranked. If that name is occupied in the destination folder, use the next free `(01)`, `(02)`, etc. This number is merely collision avoidance. Never overwrite or discard the existing file.

## Routing and archives

`OVERWRITE_TYPES`, not the Crucial/Important display tier, determines folder routing. Current entries are BRP, Share Code Document, National Insurance Number, Certificate of Sponsorship, Share Code Check Result, ECS Notice, Proof of Car Insurance, UK Driving Licence, Non UK Driving Licence, DBS Document, eVisa Screenshot, UKVI Draft Application, Proof of Vehicle Tax, Proof of Car Ownership, Visa Vignette, and Employment Contract. Read the actual set again if source changes.

Those types go directly under `<worker>/Overwrite Documents/`. Other uploadable types go under `<worker>/Bulk/Batch NN/`, with **at most 30 documents per batch**. A correction can therefore move from Bulk to Overwrite or back. The helper preserves the existing Bulk batch when space permits, otherwise picks the first batch with capacity. It does not reshuffle unrelated files or merge workers.

Backups, hidden/system artifacts, ZIP bundles, and archived originals are not upload peers. Stage 2's split-original archive lives outside the worker output tree. Do not move, rename, delete, or reprocess these archives to make a review pass. Do not call Engine processing/flattening routines or the app's cleanup stage from the review helper. The review creates its own exact-byte backup under the request directory and retains it after completion or failure.
