# Changelog

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
  Real fix: a controlled `Proof of MOT` type, which is an `OVERWRITE_TYPES` and
  Stage 3 decision, not a wording one.
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

### Cost

The harness sent 130 documents per run at ~£2.9 per full run (Haiku, resolution
1.5, all pages, no triage — the Overnight Batch page policy). Note the built-in
pre-flight estimate said ~£0.41: it does not account for the four-orientation
rotation retry, which fired on about a third of this (unusually rotated) set.
