# Changelog

## v1.2.0 — UNRELEASED — in-call bundle segmentation + orientation rework

> **Not released, not tagged, not merged.** Two of the four gates could not be
> run: the Anthropic API credit balance was exhausted 59 documents into the
> baseline. The bundle gate DID complete and it **fails** (78% correct against
> an 80% bar, and one mis-split against a zero-mis-split bar). What is written
> below is what was actually measured, including the failures. See
> "What is still needed" at the end.

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

### Gate 2 — bundles: **FAIL** (the one gate that completed)

Ground truth: the 18 `multi_doc_bundle` rows of the Watra verification, expected
maps derived from the notes written after a human read the evidence pages.

| Outcome | n | |
|---|---:|---|
| CORRECT | 14 | 78% — the bar is 80% |
| MISSED (left whole; acceptable) | 3 | rows 34, 64, 106 |
| **MIS-SPLIT (a wrong boundary)** | **1** | **row 43 — the bar is zero** |

Broken down by what each file should do:

| Expectation | n | Result |
|---|---:|---|
| must NOT be split (8 same-type bundles, 3 too long to see in full) | 11 | **11/11 correct** — no file that should be left alone was split |
| must be split | 7 | 3 correct (rows 60, 62, 114), 1 mis-split (43), 3 missed (34, 64, 106) |

**Row 43 is a real defect, not a ground-truth artefact.** The model returned
`[[1,2] UK Driving Licence, [3–12] Bank Statement]` for a 12-page ID bundle.
Pages 5–8 are a DWP National Insurance letter ("Page 1 of 4") and page 11 is a
Watra Care staff ID badge — both confirmed by rendering the pages. There is no
bank statement in the file. It merged three documents and invented a type for
them. Row 43 is the longest file segmentation attempted (8 non-ghost pages);
every correct split had ≤4.

**One ground-truth entry was corrected** after the run, and it is called out
rather than quietly changed: row 60 was originally scored MIS-SPLIT for
returning `[council tax][statement+statement]` instead of three segments. That
contradicted this design's own rule — two copies of one type are one upload
slot, which is exactly why the eight same-type bundles are scored as correctly
*not* split. The corrected entry requires the council-tax boundary and lets the
two statements share a segment.

### Gates 1, 3, 4 — NOT RUN (API credits exhausted)

| Gate | State |
|---|---|
| 1. Regression (identical names, zero spurious splits) | **not run** — baseline stopped at 59/130 |
| 3. Rotation (≥38/40 floor, cheaper per rotated doc) | **not run** |
| 4. Cost (median single-doc ≤ today's) | **partial** |

What was measured before credits ran out:

| | Baseline v1.1.0 (59 docs) | v1.2.0 bundle set (18 docs) |
|---|---:|---:|
| median £/doc | £0.02829 | £0.02647 |
| mean £/doc | £0.02523 | £0.02401 |
| median images/doc | 2 | 2 |

These two sets are not the same documents, so this is **not** a valid
before/after comparison — it is only evidence that nothing has blown up. The
real cost gate needs both runs over the same 130 files. Spend so far: **£1.50
baseline + £0.43 bundles = £1.93**.

Zero spurious splits were flagged on the 59 baseline documents that completed,
but the baseline runs v1.1.0 code, which cannot split at all — so that number
proves nothing until the v1.2.0 regression run happens.

Note for whoever re-runs this: the built-in pre-flight estimator is still low
on rotated scans (it estimated £0.07 for the 18 bundle files; they cost £0.43,
6×). That was known in v1.1.0 and is not fixed here.

### What is still needed before this can ship

1. **Top up the Anthropic API credits.** Everything below is blocked on it.
2. Re-run the baseline to completion and run the v1.2.0 regression:
   `python src\eval_classifier.py --tag v120-baseline` (against the v1.1.0
   source) and `--tag v120`. Gate 1 needs identical names and zero splits on
   the naming set; gate 4 compares the per-document £ columns.
3. **Diagnose the three MISSED rows (34, 64, 106).** They are the difference
   between 78% and passing. Row 106 is the known residual and its sibling row
   114 — the same form pack for a different worker — split correctly, so 106 is
   borderline rather than structurally impossible.
   `scratchpad\diagnose.py` dumps the raw `documents` map for exactly these
   files and was written for this; it never got to run.
4. **Decide row 43.** The candidate fix is lowering `MAX_SEG_PAGES` from 12 to
   7 non-ghost pages, which excludes row 43 from segmentation entirely (it
   becomes a flagged MISSED, which is acceptable) while keeping every file that
   split correctly. It is deliberately **not** applied here: it is a one-data-
   point change and could not be re-measured.
5. Only then: merge, tag, rebuild, release.

**Version strings are already bumped on this branch** — `APP_VERSION = "1.2.0"`,
`install.ps1 $Tag = "v1.2.0"`, installer `MyAppVersion 1.2`. The v1.2.0 GitHub
release does not exist yet, so `install.ps1` on this branch would fail to
download. That is safe while the branch is unmerged and is the reason it must
not be merged before the release is cut.

### The Ansa example — not tested

The 8-page Ansa Shahid file (Share Code + Passport + Visa Vignette + BRP, three
ghost versos, every page 90° out) is **not on this machine**. The bundle GT
folder and its expected map are ready for it at
`%LOCALAPPDATA%\Lifted\EvalGT\bundles\` — drop the file in and
`scratchpad\make_bundle_gt.py` will pick it up by filename.

### Residual row 106

Still open, but no longer for the old reason. The v1.1.0 note said it "needs the
file split, not a rule", and the machinery to split it now exists and is proven:
an end-to-end test splits that exact file into
`Other - Criminal Record Check Declaration.pdf` + `Emergency Contact Details.pdf`
with the blank page 3 travelling with the second child and the original archived.
What is not yet reliable is the model returning the two-document map for it on
demand — it did for row 114, not for 106. See item 3 above.

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
