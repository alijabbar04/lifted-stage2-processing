"""Build the bundle ground-truth folder (NON-REPO - real worker documents).

Reads the verification corrections (real worker data, outside the repo) and
writes the manifest eval_classifier.py --bundles scores against. Run it once;
re-run it after dropping the worked example in.

Sources:
  - the 18 rows tagged `multi_doc_bundle` in the Watra verification corrections
    (which includes residual row 106)
  - the 8-page worked example from the v1.2.0 brief, if it has been dropped in

Expected segment maps are derived from the verification notes (which were
written after a human read the evidence PNGs). Where a note explicitly walks
the pages, the map is exact. Where it only SAMPLED the file, the map records
the types that must appear and a minimum segment count instead of pretending
to a precision the evidence does not have - that is flagged `sampled: true`.
"""
import json, shutil
from pathlib import Path
import fitz

SRC = Path(r"C:\Users\mrali\AppData\Local\Lifted\EvalGT\watra-postaudit\Misnamed Files")
DEST = Path(r"C:\Users\mrali\AppData\Local\Lifted\EvalGT\bundles")
DEST.mkdir(parents=True, exist_ok=True)

# expected: list of {"type": ..., "pages": [1-based]} when exact,
# or "types": [...] + "min_segments": n when the evidence was sampled.
GT = {
    # ---- SAME-TYPE bundles: two copies of one kind of document. -----------
    # Correct behaviour is NOT to split (a bundle of two right-to-work checks
    # is still one type and one upload slot), so the expected map is a single
    # segment. These are the regression half of the bundle gate.
    24:  {"split": False, "type": "Share Code Check Result",
          "why": "two Home Office right-to-work RESULT pages, same person"},
    25:  {"split": False, "type": "Other - MOT history check",
          "why": "two check-mot.service.gov.uk printouts, same vehicle"},
    39:  {"split": False, "type": "Proof of Car Insurance",
          "why": "two motor-insurance certificates, same person"},
    44:  {"split": False, "type": "Share Code Check Result",
          "why": "two right-to-work RESULT pages, same person"},
    45:  {"split": False, "type": "Proof of Car Insurance",
          "why": "two motor-insurance certificates"},
    46:  {"split": False, "type": "Proof of Car Insurance",
          "why": "two esure certificates, consecutive years"},
    50:  {"split": False, "type": "Share Code Check Result",
          "why": "two right-to-work RESULT pages, same person"},
    98:  {"split": False, "type": "Proof of Vehicle Tax",
          "why": "two gov.uk vehicle tax/MOT status pulls, same vehicle"},

    # ---- MIXED-type bundles inside the segmentation page cap --------------
    34:  {"split": True, "exact": [
            {"type": "Other - Consulate appointment booking confirmation",
             "pages": [1, 2]},
            {"type": "Passport", "pages": [3]},
            {"type": "Visa Vignette", "pages": [4]},
            {"type": "UK Driving Licence", "pages": [5, 6]}],
          "why": "notes walk p1 booking, p2 blank, p3 passport, p4 vignette, "
                 "p5 provisional licence"},
    # CORRECTED after the first measured run. The original entry demanded
    # three segments (council tax + statement + statement). That contradicted
    # this design's own rule elsewhere in this very file: rows 24/39/44/45/
    # 46/50/98 are two copies of ONE type and are scored as correctly NOT
    # split, because one type is one upload slot. Two consecutive Lloyds
    # statements are the same case, so keeping them in one child is right and
    # the only boundary that MUST be found is the council-tax bill.
    60:  {"split": True, "sampled": True, "min_segments": 2,
          "types": ["Proof of Address", "Bank Statement"],
          "why": "p1 council-tax bill, p3 and p5 Lloyds statements; the "
                 "statements may share a segment (same type)"},
    62:  {"split": True, "exact": [
            {"type": "Share Code Check Result", "pages": [1, 2]},
            {"type": "Certificate of Sponsorship", "pages": [3, 4]}],
          "why": "p1-2 right-to-work result, p3 a genuine UKVI CoS"},
    106: {"split": True, "exact": [
            {"type": "Criminal Record Check Declaration", "pages": [1]},
            {"type": "Emergency Contact Details", "pages": [2, 3]}],
          "why": "p1 declaration, p2 emergency details, p3 blank. THE RESIDUAL."},
    114: {"split": True, "exact": [
            {"type": "Criminal Record Check Declaration", "pages": [1]},
            {"type": "Emergency Contact Details", "pages": [2, 3]}],
          "why": "same pack as row 106, different worker"},

    # ---- MIXED, but the verification only SAMPLED the pages ---------------
    43:  {"split": True, "sampled": True, "min_segments": 3,
          "types": ["UK Driving Licence", "National Insurance Number",
                    "ID Badge"],
          "why": "12-page ID bundle; notes read p1,p2,p5,p11 only"},
    64:  {"split": True, "sampled": True, "min_segments": 2,
          "types": ["Proof of Address", "ID Badge"],
          "why": "8-page bundle; notes read p1 energy bill and p5 ID badge"},

    # ---- TOO LONG to see in full: must NOT split, must be flagged ---------
    18:  {"split": False, "too_long": True, "type": "Reference",
          "why": "42-page mixed pack; beyond MAX_SEG_PAGES"},
    88:  {"split": False, "too_long": True, "type": "Reference",
          "why": "26-page pack; beyond MAX_SEG_PAGES"},
    102: {"split": False, "too_long": True, "type": "Reference",
          "why": "32-page pack; beyond MAX_SEG_PAGES"},
}

rows = []
for n, spec in sorted(GT.items()):
    hits = sorted(SRC.glob(f"row_{n:04d}*.pdf"))
    if not hits:
        print(f"  ! row {n}: no GT file"); continue
    src = hits[0]
    dest = DEST / src.name
    if not dest.exists():
        shutil.copy2(src, dest)
    d = fitz.open(dest); pages = len(d); d.close()
    rec = dict(spec)
    rec.update({"row": n, "file": dest.name, "pages": pages})
    rows.append(rec)
    kind = ("no-split" if not spec.get("split") else
            ("sampled" if spec.get("sampled") else "exact"))
    print(f"  row {n:>3}  {pages:>2}p  {kind:<8} {src.name}")

# The 8-page worked example from the v1.2.0 brief: a right-to-work check, a
# passport bio spread, an entry-clearance vignette and a BRP, with a blank
# verso after each and every content page 90 degrees out. It is not in the
# verification set - drop the original into DEST and it is picked up as the
# only non-row_ PDF there.
extra = [p for p in sorted(DEST.glob("*.pdf"))
         if not p.name.startswith("row_")]
if len(extra) == 1:
    d = fitz.open(extra[0]); pages = len(d); d.close()
    rows.append({"row": "example", "file": extra[0].name, "pages": pages,
                 "split": True, "exact": [
                     {"type": "Share Code Check Result", "pages": [1, 2]},
                     {"type": "Passport", "pages": [3, 4]},
                     {"type": "Visa Vignette", "pages": [5, 6]},
                     {"type": "BRP", "pages": [7, 8]}],
                 "why": "the 4-document worked example from the v1.2.0 brief"})
    print(f"  example  {pages}p  exact    {extra[0].name}")
elif len(extra) > 1:
    print(f"  ! {len(extra)} non-row_ PDFs in {DEST} - cannot tell which is "
          f"the worked example; leave exactly one")
else:
    print("  ! the 8-page worked example is NOT PRESENT - drop the original "
          f"into {DEST} to score it")

out = DEST / "bundle_ground_truth.json"
out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
print(f"\n{len(rows)} rows -> {out}")
print(f"  splits expected : {sum(1 for r in rows if r.get('split'))}")
print(f"  no-split expected: {sum(1 for r in rows if not r.get('split'))}")
