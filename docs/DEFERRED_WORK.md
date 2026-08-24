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

## Stage-1 multi-document bundle splitter — SUPERSEDED (v1.2.0)

**This entry is closed. Segmentation lives in Stage 2's classification call.**

The original decision sent this work to `lifted-stage1-download-merger`, so a
bundle would arrive at Stage 2 already split. That was wrong for two reasons,
both of which only became clear when the numbers were measured:

- **Stage 1 has no vision budget.** It is a download/merge tool; it never looks
  inside a PDF and would have to start paying for page images purely to find
  boundaries. Stage 2 is *already* paying for those page images.
- **Boundaries alone are not enough.** Knowing that a new document starts at
  page 3 does not say what is on page 3, so every part still had to be
  classified afterwards. Asking "where are the cuts" and "what is each piece"
  as two questions costs two answers; asking once costs one.

v1.2.0 therefore returns a per-page `documents` map from the same call that
classifies the file (`classify_payload(segment=True)`), and splits locally with
PyMuPDF, which is free. A file that is not a bundle returns one entry and is
named exactly as before. The cost of the extra pages is more than covered by
dropping near-blank pages from the request — measured over the 130-file Watra
set, images sent fell from 296 to 281.

The old machinery is gone, not deprecated: `detect_bundle_starts()`,
`BUNDLE_SCAN_SYSTEM` and `BUNDLE_SCAN_*` were removed so there is only one
bundle system. What replaced them:

- `segmentation_pages()` / `ghost_pages()` — which pages are sent, and which
  near-blank versos are skipped (never dropped from disk).
- `plan_segments()` — the split gates. Conservative by construction: at least
  two segments of DISTINCT types, every segment ≥ 75 confidence and a real
  vocabulary type, no segment starting on a blank page, and the segments must
  tile the file exactly. Anything else leaves the file whole.
- `Engine._maybe_split_bundle()` — applies the split, archives the original,
  and files each part through the ordinary naming/ranking/placement path.

**Note for whoever reads the old advice:** the `.splitbak` copy that this entry
described as the safety net was not one. `flatten_worker()` moves every file
under a worker back into processing and `cleanup_leftover_files()` deletes
`.splitbak` outright, so the original was being destroyed on the next run.
Originals now go to `APP_DIR/Original Bundles/<care home>/<worker>/`.
