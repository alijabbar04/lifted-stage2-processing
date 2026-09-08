# Changelog

## v1.5.2 — 2026-09-08 — Ranking attention and bounded recovery

- Preserve exact-path classification evidence when rebuilding records after
  duplicate removal, so surviving documents retain the correct evidence.
- Clarify policy/handbook versus individual worker-record boundaries, including
  mixed bundles and genuinely partially completed forms, without worker-specific rules.
- Keep failed or unavailable date, signature and quality finishing evidence as
  durable **ranking needs attention** state; do not turn an incomplete run
  into a completed result or guess missing evidence.
- Allow a separately confirmed retry only for eligible failed finishing
  operations against unchanged, hash-bound inputs. Reconcile uncertain
  provider outcomes first; never repeat primary classification blindly.
- Preserve captured scope, original audit identity and prior review history
  during supported explicit audit-row recovery. Normal review still includes
  flagged/error rows; explicit rows do not imply every **Correct** row was
  reviewed.
- Document that a signature sample covering the last three pages is not proof
  about every page of a long contract. No live accuracy improvement is claimed
  from offline evidence.
- Update public release metadata to build `2026.09.08-ranking1`.

## v1.5.1 — 2026-09-08 — Slack notifications and reliable review handoff

- Add optional Slack incoming-webhook delivery alongside Discord and Telegram,
  with masked local credential entry, channel-specific tests, truthful delivery
  status and setup guidance. A display label never overrides Slack's bound channel.
- Keep notifications aggregate, background-delivered and separate from processing
  outcomes. Handle Slack plain-text responses and bounded rate-limit retries.
- Bind batch recovery and completion to the full captured worker scope before
  automatic review can begin; retain incomplete work instead of approving it.
- Resolve and verify the exact Python helper runtime for AI handoffs, with explicit
  commands and child-only PATH configuration. Managed-session access still needs
  its own permission check; a host preflight does not grant sandbox access.

- Keep the existing `.docreview_batch_writer.lock` path while a batch writer is
  active, then unlock/close it and make a best-effort Windows cleanup attempt.
- Treat a Windows deletion refusal from another standard Python holder as a
  harmless remnant retired on a later successful use; POSIX and request/ledger
  locks remain permanent.
- Update the public/private installer metadata, bootstrap tag and frozen-app
  smoke-test identity to build `2026.09.08-slack1`.

## v1.4.1 — 2026-09-07 — Obsidian Compact progress and evidence handling

- Make preparation, scanning, batching, follow-up and recovery progress report
  the current operation and distinguish prepared, accepted, attention and
  incomplete states. A completed pass does not claim every conversion or
  submission succeeded.
- Preserve honest recovery state and original inputs when a submission is
  interrupted or a local conversion fails; do not imply 100% completion or
  retry an ambiguous paid submission automatically.
- Keep unreadable or empty evidence explicit and reviewable instead of inventing
  pages, scores or classifications; validate page references, peer semantics
  and review decisions against the available evidence.
- Isolate supported Office conversion output from same-stem files and bound
  read-timeout retries, while retaining the existing no-secret public installer
  and user-configured API credentials.
- Require the matching standalone guide before the optional bootstrap changes
  installed files; verify its checksum and test missing/mismatched guide cases.

## v1.4.0 — 2026-09-06 — Obsidian Compact and evidence-led AI review

- Implement the compact near-black/silver layout, black native Windows caption
  and real app icon; keep all navigation, API analytics, guide and detailed log.
- Report per-document audit progress, adjudication/wait state, cautious ETA and
  truthful incomplete outcomes. Report-writing must finish before completion.
- Add report choices, verified account/model handoffs for Luna/Opus audit review
  and Sol/Fable learning, with role-shared rules and durable file-based memory.
- Require evidence and complete peer ranking for controlled-category corrections;
  apply recoverable two-phase transactions and reconcile every changed peer in
  the master correction ledger. Learning changes require regression evidence.
- Add configurable Discord/Telegram lifecycle notifications, rate limiting,
  delivery tests and local credential references without public-build secrets.
- Replace the guide with a 12-page workflow guide and bundle it and the review
  rules in the portable executable; refresh installers and release checksums.
- Preserve the existing paid classification models, resolution and vocabulary.

## v1.3.3 — 2026-09-06 — interrupted primary submission recovery

- Save the entire primary document inventory before the first upload; do not
  apply a partially submitted inventory as though it were complete.
- Reconcile interrupted uploads against provider batch records and exact request
  identities before offering a state-backed, explicitly authorised resume.
- Recover older partial inventories without repeating conversion or flattening;
  preserve accepted work and send only the remaining requests in bounded chunks.
- Back up state before reconciliation and stop on changed sources, inconclusive
  provider evidence, or concurrent state changes.
- Make Check batch status offer recovery, refresh pending headers, block
  conflicting Start/Flatten actions, and avoid 100% progress on failure.
- Use one-shot Windows-native WinHTTP for primary and follow-up batch creation;
  preserve exact request bytes and certificate validation, with no automatic
  POST retry or transport fallback after an uncertain response.
- Target 10 MiB recovery chunks without lowering document quality, and hold
  an OS-backed per-care-home lock for each complete write operation.
- Keep existing classification models, resolution and the dark layout unchanged.

## v1.3.2 — 2026-09-05 — restart-safe follow-up chunking

- Follow-up classification now uses the same bounded multi-batch approach as
  the primary run. Large unresolved sets are planned below 90 MB per chunk and
  retain the existing 100 MB hard transport guard.
- Every follow-up chunk records its request IDs and a durable started/accepted
  marker. A clean restart continues with unsubmitted chunks; an ambiguous POST
  remains blocked so duplicate billing is not risked.
- The follow-up completion dialog now reports the number of submitted chunks.
  A genuinely oversized single-document request receives an actionable message
  instead of the previous vague whole-set warning.

## v1.3.1 orientation and restart-safety completion (2026-09-02)

- Fixed the frozen Windows application failing at startup when the host exposed
  `C:\Windows\Temp` as its runtime directory. One-file extraction is now pinned
  to `%LOCALAPPDATA%\Lifted\Stage2Runtime`, the PyInstaller pin is upgraded to
  6.22.1, and every application build must open the real Stage 2 v1.3.1 window
  under the formerly failing temp environment before it can pass.
- Added a bundled, CPU-only four-way document orientation preflight using the
  official 6,788,069-byte `PP-LCNet_x1_0_doc_ori` ONNX model. It checks every
  processed PDF page locally in bounded batches, never downloads at runtime and
  never calls an external API. Audit/shadow mode is the v1.3.1 default;
  automatic correction remains opt-in pending a representative reviewed
  benchmark.
- Automatic orientation uses atomic PDF replacement, high-confidence and
  margin gates, blank/sparse/photo/conflict vetoes, immediate hash persistence
  and per-page restart state. Corrected parent pages are marked consumed before
  splitting, and child state follows renames, preventing double rotation.
- Batch state now separates classification, per-worker finishing, movement and
  optional audit completion. Live finishing spend and chargeable operations are
  checkpointed individually; completed workers are not repeated after restart.
- Every primary batch POST now has a persisted chunk plan, request identities,
  attempt ID and pre-POST marker. Unknown submission outcomes remain ambiguous
  and block both resubmission and live processing.
- Generic format-only Other labels (`email`, `letter`, `form`, `scan`,
  `screenshot`, `PDF`) now receive the single discounted follow-up. Specific
  labels such as `P60` and `customer experience email` remain meaningful.
- Corrected the displayed Claude Opus 4.8 API price to the official $5/M input
  and $25/M output price (verified 2026-09-02); model IDs are unchanged.
- Follow-up application now distinguishes a completed generic answer from a
  failed, expired, canceled or missing answer. A meaningful completed follow-up
  wins, a completed generic answer stays `Other - Unknown`, and a failed
  follow-up falls back to the usable primary result without resubmission.
- Interrupted atomic orientation temps are excluded everywhere and removed only
  when their exact app-generated name and source-PDF sibling prove they are
  residue. Orientation work now observes stop requests before a PDF, between
  pages/batches and immediately before a physical rewrite.
- Removed the obsolete split-time rotation map. Split children now copy the
  locally preflighted parent bytes directly, preserving exactly-once rotation.
- Public release builds now generate a deterministic `SHA256SUMS.txt` last for
  the app, credential-free setup and ONNX asset; `install.ps1` verifies the app
  before installation. Application artifact publication remains blocked until
  the owner selects proprietary terms or an open-source licence.

## v1.3.1 — 2026-09-02 — bounded long PDFs and discounted batch follow-up

- Restored the v1.2 long-PDF cost guard. Long PDFs use the normal first-two-plus-
  last-page sample, remain intact, and are flagged only when that sample
  explicitly suggests multiple documents. Production processing no longer calls
  the all-page overlapping boundary scan or child-confirmation loop.
- Preserved the one-result short-PDF bundle path, including passport/visa/BRP
  splits and archival of the original.
- Preserved the complete controlled vocabulary and canonical snap-to-vocabulary
  behaviour. Unmatched documents now consistently retain descriptive
  `Other - <description>` names; specific primary-batch `other_label`/`guess`
  values are no longer replaced with `Unknown` or added to the workbook.
- Replaced automatic live per-document batch retries with one persisted,
  discounted Message Batches follow-up containing only malformed, generic or
  low-confidence results. When enabled, the stronger configured model is used
  directly. Resume is idempotent and fails closed if submission status is
  ambiguous, preventing automatic duplicate billing.
- Worker finalisation, movement and audit wait for any required follow-up batch.
  The existing empty-worker-folder movement correction is included.
- Cost estimates now separate primary batch classification, required live
  finishing, follow-up reserve and audit, with one cumulative budget. The audit
  reports its expected cost before starting and is skipped with a clear
  processing-complete message when the remaining budget is insufficient.
- The known local £2,500,000 ceiling is backed up and migrated to £35; new
  installations also default to a £35 cumulative ceiling.
- Added offline regression coverage for long-PDF call counts, short bundle
  archival, descriptive Other naming, follow-up batching/restart idempotence,
  absence of live retry chains and cumulative budgets.

## v1.3.0 — 2026-08-20 — autonomous bundles, orientation and upload policy

- Employment Contract is now one of the 16 overwrite document types, keeping
  Stage 2 and Stage 3 on the same individual-overwrite policy.
- Short multi-page PDFs no longer exit at page-one triage; the full document map
  is requested immediately when the file can be shown safely in one call.
- Long PDFs now get an all-page boundary scan. A proposed cut is applied only
  after independent high-confidence child classifications confirm every
  boundary; originals remain archived and ambiguous packs stay whole.
- Fixed the mixed-orientation control-flow bug where correcting one text-layer
  page suppressed rotation fixes for scanned pages elsewhere in the same PDF.
- Added optional one-click Ollama setup and a double-confirmed local vision pass
  for image-only pages. Runtime/model failure is always a non-blocking fallback.

## v1.2.0 — UNRELEASED — in-call bundle segmentation + orientation rework

> **Not released, not tagged, not merged.** All four gates were run to
> completion this time (~£18 of API spend across seven runs). Two pass, two do
> not, and the two that do not need a decision that is not the classifier's to
> make — see "The two open decisions" at the end.
>
> The headline: **bundle segmentation works and is worth having** — it finds
> real multi-document files, including five the 2026-08-11 verification never
> caught. **The orientation cost saving does not ship**: measured, the cheap
> rotation path costs four misnamed compliance documents, and no confidence
> threshold can separate its good answers from its bad ones.

### What changed

Stage 2 classifies a *file*, so a scan holding four documents got one name and
the other three left the compliance record. v1.2.0 asks the classification call
— which already has the pages in front of it — to also return a per-page
`documents` map, then splits locally with PyMuPDF.

**Same-call segmentation shipped, not the escalation ladder.** The choice was
made on measurement, before any API spend. Over the 130-file ground truth,
sending every non-ghost page of a short file and dropping near-blank pages
*reduces* images sent from 296 to 281 (−5.1%), with the median per file
unchanged at 2. An escalation ladder would have cost a second call on every
suspected bundle to buy a saving that ghost-page exclusion already pays for.

**Ghost pages.** 141 of the 544 GT pages (26%) are near-blank versos, most
carrying a mirror-image bleed-through of their own front. They are no longer
sent to the model. They are never dropped from disk, and never start a segment.

The ink threshold is 0.05% of pixels below grey 160, and it is deliberately
low. Measured on real pages:

| Page | Ink | What it is |
|---|---:|---|
| row 62 p2 | 0.08% | bleed-through ghost — *not* excluded at this threshold |
| row 62 p3 | 0.19% | a real, full Certificate of Sponsorship |
| typical content | 0.7–6% | ordinary pages |

Only ~2.4× separates the faintest real page from the densest ghost, so some
ghosts are let through on purpose: sending a blank page wastes a fraction of a
penny, dropping a real one loses a document.

**Split gates.** A split needs all of: the model saw the file in full; ≥2
segments; ≥2 *distinct* types; every segment ≥75 confidence and a real
vocabulary type; no segment starting on a blank page; segments tiling the file
exactly. Two copies of one type are one type and are never split. Anything else
leaves the file whole and flags it.

**Orientation.** The four-orientation retry (four images to settle one page,
fired on 40 of 130 documents in v1.1.0) is replaced by straightening the render
locally from the rotation the model just reported and re-asking **once**. The
old retry remains as the fallback when that confirmation is unconvinced.
Rotation is now baked into split children, so split documents open upright.

**A safety bug fixed on the way.** The `.splitbak` copy that was supposed to
guarantee "a split never deletes a document" was being destroyed:
`flatten_worker()` moves everything under a worker back into processing, and
`cleanup_leftover_files()` deletes `.splitbak` outright (on by default).
Originals now go to `APP_DIR/Original Bundles/<care home>/<worker>/`.

### Gate 2 — bundles: **the safety half passes, the 80% bar is marginal**

Ground truth: the 18 `multi_doc_bundle` rows of the Watra verification, expected
maps derived from the notes written after a human read the evidence pages.

Final configuration:

| Outcome | n | |
|---|---:|---|
| CORRECT | 14 | **78%** — the bar is 80% |
| MISSED (left whole; acceptable) | 4 | rows 43, 62, 64, 106 |
| **MIS-SPLIT (a wrong boundary)** | **0** | the bar is zero ✔ |

| Expectation | n | Result |
|---|---:|---|
| must NOT be split (8 same-type bundles, 3 too long to see in full) | 11 | **11/11** — no file that should be left alone was split |
| must be split | 7 | 3 correct (rows 34, 60, 114), 4 missed |

**Read the 78% as a range, not a number.** Three measured runs scored 78%, 83%
and 78%, with individual files (34, 62, 64) flipping between correct and missed
across runs at identical or near-identical settings. The bar sits inside the
measurement noise, so "does it clear 80%" is not currently answerable — what IS
stable across every run is the part that matters most:

- **zero mis-splits, every time.** No file was ever cut on a wrong boundary.
- **11/11 must-not-split files left alone, every time**, including all eight
  bundles of two same-type documents.

Which is the intended asymmetry: the gates are built to fail safe, and they do.
A missed split leaves a file exactly as v1.1.0 left it; nothing is lost that was
not already lost.

It took two measured rounds to get here, and what the first round exposed was
mostly wrong *gates*, not a wrong model:

- **A trailing blank page cancelled a real split.** Asked to account for every
  page, the model dutifully listed the blank verso at the end as a fifth
  "document" at confidence 30. That entry starts on a ghost page and fails the
  confidence floor, so the gates threw away the entire plan — and a 6-page
  bundle the model had segmented *perfectly* (booking confirmation, passport,
  visa vignette, driving licence) was left whole. An all-blank segment is now
  absorbed into the one before it. A segment that starts on a blank page but
  holds real content is still refused.
- **The confidence floor was set too high on a guess.** 75 blocked a correctly
  identified `ID Badge` reported at 70. Confidence guards the *type*, not the
  boundary — the one wrong boundary in the set arrived at confidence 92 — so it
  is now 70, still above the vocabulary's own 60 for naming a whole file.
- **`MAX_SEG_PAGES` 12 → 7, and this one is tuned, not derived.** The only
  wrong boundary in the set was the only file with more than 5 non-ghost pages:
  a 12-page ID bundle (8 non-ghost) where the model merged a DWP National
  Insurance letter and a staff ID badge into one ten-page "Bank Statement". The
  pages were rendered and read to confirm this was a real defect and not a
  ground-truth artefact — page 5 is the NI letter ("Page 1 of 4"), page 11 the
  ID badge, and there is no bank statement anywhere in the file. Over the cap a
  file is now classified exactly as today and flagged, so this fails safe; but
  it rests on **one data point** and should be revisited when more bundle
  ground truth exists.

Two scoring corrections were made, and both are called out here rather than
quietly applied, because a gate you adjust after seeing the result is worth
nothing if it is adjusted silently:

- **Row 60** was scored MIS-SPLIT for returning `[council tax][statement +
  statement]` instead of three segments. That contradicted this design's own
  rule — two copies of one type are one upload slot, which is exactly why the
  eight same-type bundles count as correctly *not* split. The entry now
  requires the council-tax boundary and lets the two statements share a segment.
- **Row 34** was scored MIS-SPLIT with *every boundary exactly right*, purely
  because the model said "Booking Confirmation" where the ground truth says
  "Other - Consulate appointment booking confirmation". That is the wording
  variance `OTHER_LABEL_PATTERNS` has existed for since v1.1.0. The bundle
  scorer now uses the same `prediction_matches()` comparison as the naming half
  of the harness, against the name each segment would actually be *filed*
  under. A label difference also gets its own `TYPE-DIFF` outcome now: calling
  it the same failure as cutting a document in half would make the
  zero-mis-split gate meaningless.

**Stability caveat.** Rows 34 and 62 swapped outcomes between the two rounds
(62 split correctly in the first and was missed in the second; 34 the reverse)
with no code change between them that touches either. Segmentation on
borderline files is not deterministic, so 15/18 should be read as approximate,
not as a fixed score.

### Gate 1 — regression: **FAIL as written**, by 2 documents

Both runs are the full 130-file set, same harness, measured per document.

The naming ground truth **contains** the 18 `multi_doc_bundle` rows, so those
files are expected to behave differently — that is the feature. The gate is
therefore scored on the other 112, where a changed name is a real regression.

| | baseline v1.1.0 | v1.2.0 |
|---|---:|---:|
| accuracy, 112 non-bundle files | 107/112 | **105/112** |
| names changed | — | 6 (3 worse, 1 better, 2 same verdict) |

Two of the three "worse" cancel out: row 23 lost the MOT-history/vehicle-tax
call while row 53 *gained* it — the same documented-flaky pair from v1.1.0's
residuals, swapping places. The genuine remainder is **two documents**, rows 81
and 105, both of which are demonstrably unstable: an isolation test ran each of
them four ways (baseline images, ghost-excluded, with and without the
segmentation prompt) and **all four configurations were wrong** for both. The
baseline's correct answers on them were luck, not signal.

**Two documents is exactly the run-to-run noise of this set.** Two v1.2.0 runs
of identical code over the same 130 files differed on 2 files. (Measured on
v1.2.0, not on the baseline — so treat it as an order of magnitude, not a
proof.)

**The "5 spurious splits" are not spurious.** Every one was checked by
rendering and reading its pages:

| File | What is actually inside |
|---|---|
| row 83 | Share Code Check Result + a Pakistani passport bio page (MRZ `P<PAK…`) |
| row 109 | Passport + Visa Vignette |
| row 111 | Employment offer acceptance letter + a reference request email |
| row 113 | Passport + Visa Vignette + **BRP** (a "RESIDENCE PERMIT" card) |
| row 75 | UK Driving Licence photocard + a licence-summary printout (borderline; the gates refuse it on a re-ask) |

These are real multi-document files. The control set was wrong, not the
splitter: `multi_doc_bundle` only tagged files where the bundle produced a
*wrong name*. A bundle that rule 18(b) happened to name correctly was never
tagged, even though its other documents were still lost. `DEFERRED_WORK.md`
corroborates — its own table already listed rows 109/113 as "a passport bio
page + a BRP". **So the real bundle rate in this set is ~27 of 130, not 18.**

### Gate 3 — rotation: **the cost half does not ship**

The free text-layer tier and persisting rotation into split children both ship.
The cheap model tier does not, and this is the most important measurement in
the release.

Replacing the four-orientation retry with a corrected-render confirmation is
genuinely cheaper — **−7.9% per rotated document** — and genuinely less
accurate, over the same 67 rotated documents:

| rotation path | rescued | cost/doc (median) |
|---|---:|---:|
| four-orientation retry (v1.1.0) | **64/67** | £0.02854 |
| corrected-render confirmation | 60/67 | £0.02628 |

**No confidence threshold recovers the difference.** The seven misses came back
at 92, 92, 92, 92, 92, 92 and 95 — indistinguishable from a correct-answer
distribution that is almost entirely 92–95. At a 95 floor you would catch six
of the seven misses and drag nine correct answers to the dearer path with them.
Confidence carries no signal here, which is also why zero of 67 confirmations
ever fell back.

Four misnamed compliance documents is not worth 7.9% of the rotation subset, so
the proven path stays primary (`ROTATION_FAST_CONFIRM = False`). The cheap path
is kept, tested and one flag away for anyone who finds a signal that *does*
separate its good answers from its bad — disagreement with the pre-rotation
answer is the obvious candidate and is **not** measured.

With the proven path restored, rotation is at parity: 64/67 (95.5%) against the
baseline's 69/72 (95.8%), at +0.3% cost.

### Gate 4 — cost: **PASS on the stated metric**, but read the caveat

| | baseline | v1.2.0 |
|---|---:|---:|
| **median £/doc (the gate)** | £0.02809 | **£0.02758** |
| mean £/doc | £0.02191 | £0.02211 |
| total, 130 files | £2.8482 | £2.8747 |
| images sent | 296 | 303 |

The median improves; the mean and total rise slightly. The brief expected
ghost-page exclusion to *fund* segmentation, and in an earlier configuration it
did — **−13.5% images, −15% median cost**. That version was abandoned on
purpose, and the reason is worth recording:

Dropping blank pages everywhere reduced **47 of the 130 files to a single
image** — the two-sided sheet with a blank back, the commonest shape in a
care-home folder. A lone page cannot be split, so those requests were being
perturbed for no segmentation benefit at all, and on borderline documents any
perturbation reshuffles the answer. Every policy that prevents that collapse
costs *more* than the baseline:

| page policy | images vs v1.1.0 | files left byte-identical |
|---|---:|---:|
| drop blank pages everywhere | −13.5% | 54/130 |
| never collapse to one image | +2.4% | 101/130 |
| only touch files longer than the old sample | +3.4% | 104/130 |

The saving and the guardrail are in direct conflict, and the brief makes the
guardrait inviolable, so the rule is now: **only change what is sent when
segmentation can actually use it.** A file that cannot be segmented is sent
exactly as v1.1.0 sent it.

(The built-in pre-flight estimator is still low on rotated scans — it estimated
£0.07 for the 18 bundle files, which cost £0.43. Known in v1.1.0, not fixed
here.)

### The two open decisions

Neither is the classifier's to make, which is why this is not merged.

**1. Is finding ~9 more bundles worth 2 borderline naming changes?**

Segmentation splits real multi-document files that previously lost everything
after page 1 — 5 of the tagged bundle set plus 5 the verification never tagged.
Against that, two documents (rows 81 and 105) that the baseline happened to get
right now come out wrong. Both are unstable regardless of this change; both are
in the "Other" group, so both land in the platform's Other bucket either way.

Gate 1 as written says *identical* classifications, so as written it fails. The
honest reading is that it fails by an amount indistinguishable from the noise
floor of the measurement, in exchange for documents that were being silently
lost.

**2. Accept rotation at cost parity?**

The brief asked for the rotated-document path to get both more accurate and
cheaper. Measured, those two are in conflict: the cheap path costs four
documents. Shipping as-is means the orientation work delivers the free
text-layer tier and upright split children, but **no cost saving** — the
"≈¼ the retry cost" in the brief is not achievable at current accuracy.

If the answer to either is no, the fallback is clean: `bundle_split` is already
a Settings toggle, and `ROTATION_FAST_CONFIRM` is a one-line switch.

### What to do once those are decided

1. Merge, tag `v1.2.0`, rebuild, publish the release, then let `install.ps1`
   point at it (the tag it names does not exist yet — see below).
2. Worth doing regardless of the decision: **re-tag the bundle ground truth.**
   The five newly found bundles (rows 75, 83, 109, 111, 113) should be added to
   it with expected maps, so the next change to this code is measured against
   27 known bundles rather than 18.

**Version strings are already bumped on this branch** — `APP_VERSION = "1.2.0"`,
`install.ps1 $Tag = "v1.2.0"`, installer `MyAppVersion 1.2`. The v1.2.0 GitHub
release does not exist yet, so `install.ps1` on this branch would fail to
download. That is safe while the branch is unmerged and is the reason it must
not be merged before the release is cut.

### The worked example — not tested

The 8-page worked example from the brief (Share Code + Passport + Visa Vignette
+ BRP, three ghost versos, every page 90° out) is **not on this machine**, so
the release's headline example has never been run end to end. Its shape is
covered indirectly — row 113 is the same kind of file (Passport + Visa Vignette
+ BRP) and splits correctly into three. The bundle GT folder and its expected
map are ready for it at
`%LOCALAPPDATA%\Lifted\EvalGT\bundles\` — drop the file in as the only
non-`row_` PDF there and re-run `python src\build_bundle_gt.py`.

### Residual row 106 — still open, but the reason has changed

v1.1.0 recorded this as "needs the file split, not a rule". The splitting
machinery now exists and is proven on this exact file: `test_split_e2e.py`
drives a real split of it into `Other - Criminal Record Check Declaration.pdf`
+ `Emergency Contact Details.pdf`, with the blank page 3 travelling with the
second child and the original archived.

What does not happen reliably is the model *seeing* two documents in it. Asked
directly, it returns a single segment covering all three pages — `Emergency
Contact Details` at confidence 85 — so there is nothing for the gates to act
on. Its sibling row 114, the same form pack for a different worker, is
segmented correctly every time.

So the residual is now a **recall** problem in the page map rather than a
missing capability, and it is on the safe side of the trade: the file is left
whole, exactly as today. Note that the whole-file name it now produces
(`Emergency Contact Details`, page 2's content) is a *different* wrong answer
from v1.1.0's `Safeguarding Questionnaire`, and still not the rule-18(b) answer
of the first complete document. That is a naming question, not a segmentation
one, and it is untouched here.

## v1.1.0 — 2026-08-11 — Watra Care post-audit accuracy pass

A post-run accuracy audit of the Watra Care Limited batch flagged 113 documents
at confidence > 80. Every one was then adjudicated against the page images:
**105 confirmed, 5 wrong in a third way, 3 audit false alarms — 110 real
misclassifications**, all corrected on disk. This release fixes the classifier
so that class of mistake stops happening, and proves it with the repo's own
regression harness.

### Results

Ground truth: the 110 corrected documents, plus 17 controls (documents the
classifier already got right, of every type the new exclusions touch) and 3
probes (Watra documents the audit never flagged but which are visibly the same
errors). Real worker files — kept outside the repo, never committed.

| Set | Before | After |
|---|---|---|
| the 110 misclassified documents | 101/110 (92%) | **104/110 (95%)** |
| controls — must not regress | 16/17 | **17/17 (100%)** |
| probes — unflagged instances of the same errors | 0/3 | **3/3 (100%)** |

Per pattern, on the previously-wrong documents:

| Pattern | n | before | after |
|---|---:|---:|---:|
| `ID Badge` → `Share Code Check Result` | 18 | 18/18 | 18/18 |
| `Passport`/`BRP`/`Non UK` → `UK Driving Licence` | 19 | 17/19 | 19/19 |
| `Passport` → `BRP` | 4 | 3/4 | 4/4 |
| `Certificate of Sponsorship` → insurance / bank / vehicle tax | 11 | 11/11 | 11/11 |
| `Employment History` → `Employment Application Form` | 7 | 7/7 | 7/7 |
| request email / experience letter → `Reference` | 8 | 6/8 | 8/8 |
| `Employment Contract` → MOT history / `Term Time Evidence` | 7 | 7/7 | 5/7 |
| `Safeguarding Questionnaire` → declaration / RTW / handbook | 8 | 7/8 | 7/8 |
| `Training Certificate` → `Other - Training Repayment Agreement` | 3 | 2/3 | 3/3 |
| `Non UK Driving Licence` vs `Driving Permit` | 1 | 0/1 | 0/1 |
| `Other -` label → controlled type | 6 | 5/6 | 6/6 |
| everything else (singleton pairs) | 18 | 18/18 | 16/18 |
| **total** | **110** | **101/110 (92%)** | **104/110 (95%)** |

Residual failures are listed at the end of this entry.

### Read the "before" column carefully: most of the fix is the rebuild

The baseline was measured against the source as it stood, not against the exe
that actually processed Watra Care — and it already scored 101/110. The shipped
build was from 5 August and is behind this source. On today's code the
**rotation retry fired on 40 of the 130 documents and rescued 38 of them**, so
the great majority of the "rotated scan" bucket was already handled and simply
never reached the operator.

The practical consequence is that **rebuilding and redeploying the exe is the
single largest part of this fix**; the vocabulary and pipeline work below closes
the residual 9 and the 3 unflagged probes. It is also a standing lesson: an
accuracy report written from a production run describes the *deployed build*,
which may be months behind `main`. Check the build date before concluding the
classifier is broken.

### Historic regression set — could not be run

The 33-document ground-truth set from the 2026-07-09 overhaul no longer exists
on this machine. Its labels workbook survives at
`OneDrive\Documents\misnamed files record.xlsx`, and every row still points at
`OneDrive\Desktop\Misnamed Files\…`, but that documents folder is gone — a
full sweep of `OneDrive\`, `Documents\`, `Downloads\` and `AppData\Local\Lifted`
finds no copy, by folder name or by the individual filenames recorded in the
workbook. Labels without documents cannot be scored.

The watra-postaudit set's 17 controls cover the same ground for the types this
release touches; the wider historic coverage is simply not available. If a copy
of those 33 files turns up, run
`python src\eval_classifier.py --tag historic-regression` against it.

### Vocabulary (`SEED_*`)

Every change is a positive requirement plus an explicit exclusion plus a
destination type, per `docs/VOCABULARY_GUIDE.md`.

- **`ID Badge`** — a gov.uk / Home Office "View a right to work" page (worker
  photo + "permission to work in the UK until…" + a "Details of check" box) is
  `Share Code Check Result`, however the page is rotated and whatever the
  incoming filename says. *(18 documents)*
- **`Passport`** — a credit-card photocard with no two-line `P<` MRZ is never a
  passport: `DRIVING LICENCE` → `UK Driving Licence`, `RESIDENCE PERMIT` →
  `BRP`. Reading the first MRZ characters (`P<` / `V` / `IR`) is now spelled
  out, as is "no MRZ at all on a small card". *(18)*
- **`UK Driving Licence` / `Non UK Driving Licence`** — decide from field **4c**
  (the issuing authority), never from field 3 (the holder's place of birth).
  `4c. DVLA` or a UK/GB flag panel is the UK licence, including a UK
  *provisional* licence, whatever country the holder was born in. *(21)*
- **`Driving Permit`** — was a one-line stub; now defines the International
  Driving Permit properly, with the mutual exclusion against `Non UK Driving
  Licence`. **Placement-affecting**: `Driving Permit` is bulk, `Non UK Driving
  Licence` is an overwrite type. *(1)*
- **`Certificate of Sponsorship`** — the word "Certificate" in a title never
  makes a CoS; require the CoS number *and* sponsor *and* job/salary fields. A
  "Certificate of Motor Insurance" is `Proof of Car Insurance`, a bank statement
  is `Bank Statement`, a vehicle-tax page is `Proof of Vehicle Tax`. *(11)*
- **`Safeguarding Questionnaire`** — no longer a bin for any signed
  "PERSONAL INFORMATION"/"Declaration" sheet: excludes a Criminal Record Check
  Declaration, a Staff Handbook Declaration receipt and a right-to-work result,
  and requires actual safeguarding-knowledge questions. *(8)*
- **`Employment History`** — if any page carries "Position applied for",
  personal details or the closing declarations, the file is the whole
  `Employment Application Form`; the history grid is a section of it. *(7)*
- **`Reference`** — covers a former employer's experience/character letter, but
  only when it *assesses* the person; a letter that merely confirms job title
  and dates is an Other document. *(8)*
- **`Training Certificate`** — completion certificates only: not a Training
  Repayment Agreement, not an employer experience letter. *(5)*
- **`Proof of Address`** — names council-tax and utility bills explicitly, in
  preference to an `Other - council tax bill` label. **`Bank Statement`**,
  **`Term Time Evidence`**, **`Employment Contract`** and **`Proof of Vehicle
  Tax`** gained requirements/exclusions for the same reason. *(9)*
- **Five new `Other`-tier types** so recurring documents get a stable filed
  name instead of a different free-form label every run: `MOT history check`
  (×6), `Criminal Record Check Declaration` (×4), `Training Repayment
  Agreement` (×3), `Employment offer acceptance letter` (×2), and `MOT test
  certificate` (see the workbook fix below). All are Other tier — bulk upload,
  **no `OVERWRITE_TYPES` change, so no Stage 3 impact**.

### Disambiguation rules

- **Rule 18 (mixed bundles)** — "the first page's document wins" is now "the
  FIRST COMPLETE DOCUMENT wins", with the stacked-ID and mixed-pack cases named
  and an instruction not to classify from the most compliance-critical page.
  This was the root of all three audit false alarms.
- **Rule 20 (new)** — the four documents that are habitually given an `Other -`
  label when a controlled name already covers them. Deliberately a short list,
  not a general preference: an earlier draft that said "prefer controlled over
  Other" pushed MOT-history printouts into `Proof of Vehicle Tax`.
- **Rule 21 (new)** — UK vs non-UK driving licence, decided by the issuer.
- **Rule 14** — the "any certificate is a Training Certificate" clause now
  carves out repayment agreements and experience letters.
- **Rule 10** — the ID cluster gained `Driving Permit` and the 4c/place-of-birth
  test.

### Pipeline

- **Pre-classification deskew.** The free local text-direction check ran only
  *after* the answer came back, so the model was asked to read sideways pages
  and only the reactive rotation retry could rescue it. It now straightens the
  render before the first call. No API cost. `DocRender.render(rotate=...)`
  accepts a `{page: degrees}` dict so a scan that mixes orientations is fixed
  page by page.
- **Content crop / up-res for small documents.** A photocopied ID card sits
  alone on an A4 sheet, so a full-page render spends its pixels on white paper
  and the card's own text is unreadable — which is how driving licences and
  BRPs were filed as `Passport`. When the page's content covers less than a
  third of the sheet, that region is rendered instead, at a zoom that spends
  the same pixel budget on it. Local, no API cost, no new dependency.
- **Real page numbers in the prompt.** Long documents are sampled (first two
  pages plus the last), so "the third image" was not "page 3" — and rule 18(b)
  turns on exactly that. Each image is now labelled `Page N of M`, in both live
  and Overnight Batch requests, which stay byte-for-byte identical.
- **Cross-tab duplicate fix in the vocabulary workbook.** A type the user
  accepts from the unknown-document prompt lands in *Important*; if the same
  name is later seeded into *Other*, the workbook held it twice with two
  different descriptions and `group_of()` answered with whichever tab it
  checked first — so the type's tier, and its Stage 3 upload route, depended on
  tab order. Found in production: `MOT test certificate` was in both.

### Harness

- `eval_classifier.py` takes an optional 4th ground-truth column, `kind`
  (`misnamed` / `control` / `probe`), scores each separately and prints a miss
  table. Controls are the point: over-firing a new exclusion is the classic
  way a fix like this goes wrong, and it did — twice — during this pass.
- `IDEAL_TO_CANONICAL` and `OTHER_LABEL_PATTERNS` extended for the new
  canonical `Other - …` strings; an `Other - <type>` truth now compares on the
  type alone.

### Notes for whoever picks this up next

- **The propagation question is settled**: `KnowledgeBase._read()` re-seeds
  every canonical description on every load, so editing a `SEED_*` constant
  *does* reach a machine that has already run. `README.md` and
  `VOCABULARY_GUIDE.md` said the opposite and have been corrected. Verified by
  reading the workbook off disk before and after a `KnowledgeBase()` load.
- **Controls need checking by eye.** The first attempt picked them
  automatically from the audit's "Correct" rows; 6 of 12 turned out to be audit
  *misses* of the very patterns being fixed, which would have scored a correct
  new answer as a regression.
- **Exclusions belong at the end of the wrong type's description; the positive
  claim belongs in the right type's.** Putting a prominent "TWO CHECKS FIRST"
  test at the head of `Non UK Driving Licence` made that type more salient and
  pulled six UK licences into it.
- The Stage-1 bundle splitter is deliberately out of scope — see
  [docs/DEFERRED_WORK.md](docs/DEFERRED_WORK.md).

### Residual failures (6 of 110)

Each was traded against something that protects a control or another row, and
left rather than fixed by loosening a guard.

- **Rows 40, 53, 86 — a gov.uk "Check MOT history" printout answered
  `Proof of Vehicle Tax`.** Three of the six MOT rows are now right. Tightening
  `Proof of Vehicle Tax` far enough to catch the last three pushed row 98 — a
  genuine "Vehicle Tax and MOT status" page — out of the controlled type into an
  `Other -` label, and stating the split inside rule 20 sent all six MOT rows to
  `Proof of Vehicle Tax`. Both directions were measured; this is the better one.
  A controlled `Proof of MOT` type was investigated as the clean fix and
  **rejected: the platform has no MOT document type**, so the name would be
  untypable at upload and would also lose the "Other" pin that the current
  `Other - MOT history check` naming gets in the bulk path. Full evidence in
  [docs/DEFERRED_WORK.md](docs/DEFERRED_WORK.md); this is now a permanent
  documented residual, not a to-do.
- **Row 108 — an employment-verification letter answered `Reference`.**
  `Reference` now requires the letter to assess the person, but a stricter
  version cost control row 104 (a genuine experience/character letter that *is*
  a reference). Losing a control is worse.
- **Row 106 — a 2-document bundle (criminal-record declaration + emergency
  contact form) answered `Safeguarding Questionnaire`.** Rule 18(b) fixed the
  other bundles; this one needs the file split. See `docs/DEFERRED_WORK.md`.
- **Row 87 — an International Driving Permit answered `Non UK Driving
  Licence`.** The rule works in general: the unflagged probe of the same
  document type flipped from wrong to right. This particular scan is a small
  card under a heavy "CERTIFIED TRUE COPY" stamp. Placement-affecting, so it is
  worth a human look at upload time.

All four are settled, not open: each has been through the harness in both
directions and the next tightening costs more than it buys. They are tabulated
in [docs/DEFERRED_WORK.md](docs/DEFERRED_WORK.md) so nobody re-opens them by
accident.

### Cost

The harness sent 130 documents per run at ~£2.9 per full run (Haiku, resolution
1.5, all pages, no triage — the Overnight Batch page policy). Note the built-in
pre-flight estimate said ~£0.41: it does not account for the four-orientation
rotation retry, which fired on about a third of this (unusually rotated) set.
