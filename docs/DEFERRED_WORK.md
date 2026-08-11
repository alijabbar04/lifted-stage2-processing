# Deferred work

Things this repo has deliberately **not** done, with the reason and where they
belong instead. Kept so the next person does not re-derive the decision.

## A controlled `Proof of MOT` type — investigated, rejected

**Status: blocked on the platform, not on this repo. Do not add it.**

Three documents in the 2026-08-11 regression set (audit rows 40, 53, 86) are
gov.uk `check-mot.service.gov.uk` "Check MOT history" printouts that the
classifier answers `Proof of Vehicle Tax`. The clean fix looks like a dedicated
controlled type. It was checked and it is the wrong move:

**The Lifted Talent platform has no MOT document type.** Stage 3
(`lifted-stage3-uploading`) holds `PLATFORM_DOC_TYPES` — all 96 Document type
options the "Add new document" dialog offers, swept against the live dialog on
2026-08-07 by `tools/sweep_document_types.py` and pinned by a unit test
(`tests/test_doc_type_mapping.py` asserts 96 types in 12 categories). Its
**Vehicle** category is:

> Certificate of Driving Competency · Driving Licence Summary · Driving Permit ·
> Driving Practical Test Certificate · Driving Theory Test Certificate ·
> Proof of Car Insurance · Proof of Car Ownership · Proof of Vehicle Tax ·
> Vehicle Other

No MOT entry, and the word "MOT" appears nowhere in the Stage 3 source or docs.

Naming Stage 2 files `Proof of MOT` would make things **worse**, in both upload
paths:

- **Overwrite path** — `doc_type_for_filename()` matches `PLATFORM_DOC_TYPES`
  *exactly* and deliberately has no fuzzy fallback, so the file would be skipped
  as "no matching document type".
- **Bulk path** — Stage 3's `_force_other_types()` pins any file whose name
  starts `Other - ` to the portal's "Other" category. A file called
  `Proof of MOT.pdf` no longer starts that way, so it would lose that pin and
  the platform would auto-classify it wherever it liked.

The current `Other - MOT history check` naming is therefore already the correct
outcome: bulk upload, pinned to the platform's "Other" type. This is the same
failure mode that retired `Non-UK Driving Licence` (see `RETIRED_NAMES`) — a
name the dropdown cannot offer breaks the upload silently.

**If Lifted Talent ever adds an MOT type**, the work is: confirm the exact
spelling in the live dropdown, add it to Stage 3's `PLATFORM_DOC_TYPES` (and
re-run the sweep), then add the matching controlled type here with mutual
exclusions against `Proof of Vehicle Tax` — the two gov.uk services are
distinct, and audit rows 20 (a `vehicleenquiry.service.gov.uk` "Vehicle Tax and
MOT status" page, which **is** `Proof of Vehicle Tax`) and 23 (a
`check-mot.service.gov.uk` history page, which is not) are the reference pair.

## Known residuals from the 2026-08-11 accuracy pass

Left deliberately. Each was measured, and each was traded against something that
protects a control or another row — see `CHANGELOG.md` for the numbers.

| Rows | Symptom | Why it stays |
|---|---|---|
| 40, 53, 86 | MOT-history printout → `Proof of Vehicle Tax` | No platform type to fix it with (above). Tightening `Proof of Vehicle Tax` further pushed row 98, a genuine tax page, *out* of the controlled type. |
| 87 | International Driving Permit → `Non UK Driving Licence` | The rule works generally — the unflagged probe of the same document type flipped from wrong to right. This one scan is a small card under a heavy "CERTIFIED TRUE COPY" stamp. **Placement-affecting** (`Driving Permit` is bulk, `Non UK Driving Licence` is an overwrite type), so it is worth a human look at upload. |
| 106 | 2-document bundle (criminal-record declaration + emergency-contact form) → `Safeguarding Questionnaire` | Needs the file split, not a rule. See the splitter section below. |
| 108 | Employment-verification letter → `Reference` | `Reference` already requires the letter to assess the person; a stricter version cost control row 104, a genuine experience/character letter that really is a reference. Losing a control is worse. |

**No further rule tightening on 87, 106 or 108.** Each has been through the
harness; the next change in either direction costs more than it buys.

## Stage-1 multi-document bundle splitter

**Owner: `lifted-stage1-download-merger`, not this repo.**

The 2026-08-11 Watra Care verification found **18 of 113** flagged files were a
single PDF holding two or more distinct documents — e.g.

| File | What was actually inside |
|---|---|
| row 34 | Consulate appointment booking + Pakistani passport + visa vignette + UK driving licence |
| row 43 | UK driving licence (front/back) + DWP NI-number letter + staff ID badge |
| rows 60 / 64 | a council-tax or energy bill + bank statements |
| rows 106 / 114 | a Criminal Record Check Declaration + an emergency-contact form |
| rows 109 / 113 | a passport bio page + a BRP |

Stage 2 classifies a **file**, so a bundle can only ever get *one* name and one
Stage 3 upload slot — the other documents inside it are silently lost to the
compliance record. No amount of vocabulary work fixes that.

What Stage 2 **has** done (2026-08-11) is stop the bundle producing the *wrong*
name: rule 18(b) now says the first complete document wins, and every image is
labelled with its real page number so the model can tell page 1 from page 5
even on a sampled long document. That fixed the false alarms (rows 104/109/113)
and the two `BOTH_WRONG` bundles, but each bundle is still one file.

The real fix is to split the PDF **before** Stage 2 sees it, in the Stage 1
download/merge tool, so each document arrives as its own file and gets its own
type and its own upload. This repo already has most of the machinery to inform
that work and should be read first:

- `detect_bundle_starts()` — an authoritative page-by-page boundary scan that
  renders **every** page at low zoom in overlapping windows (`BUNDLE_SCAN_*`).
  It is deliberately conservative: when unsure it answers "continuation".
- `Engine._maybe_split_bundle()` — how a split is actually applied, including
  the `.splitbak` safety copy.
- `BUNDLE_SCAN_SYSTEM` — the prompt, including the rules that stop it cutting a
  genuine multi-page contract in half.

Splitting at Stage 1 also removes the duplicate cost: today a bundle-prone file
pays for a bundle scan in Stage 2 on every run.
