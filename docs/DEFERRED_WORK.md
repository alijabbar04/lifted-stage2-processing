# Deferred work

Things this repo has deliberately **not** done, with the reason and where they
belong instead. Kept so the next person does not re-derive the decision.

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
