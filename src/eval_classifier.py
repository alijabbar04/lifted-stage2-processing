r"""
eval_classifier.py - Stage 2 classification regression harness
================================================================
Re-runs the EXACT production classification path (imported from
Stage2_Processing.pyw - classify_document_core + validate_result, the same
functions the live Engine uses) over the ground-truth set of previously
misclassified documents, and scores the predictions against the normalised
ideal labels.

Ground truth (NOT in this repository - it is real care-worker documents and
must never be committed). Point the harness at your local copy with:

    set STAGE2_GT_ROOT=D:\some\folder      (contains both items below)

or drop it in one of the default locations that are probed in order:
  - <Desktop>\Lifted\Week 4\  then  <Desktop>\
  - documents : <root>\Misnamed Files\
  - labels    : <root>\misnamed files record.xlsx
                (columns: File Path | AI name | Ideal name [| kind])

The optional 4th column `kind` splits the set for scoring:
  misnamed  a document the classifier got WRONG and must now get right
  control   a document it already gets RIGHT and must not start getting wrong
            (over-firing a new exclusion is the classic regression)
  probe     an extra diagnostic row, reported but excluded from the gate
Sets without the column are scored as one block, exactly as before.

IMPORTANT: the filenames in the ground-truth folder are the PREVIOUS RUN'S
WRONG ANSWERS (Stage 2 renamed them). The filename is therefore NEVER given
to the model here (filename_for_triage="").

Costs real API money (~1-2 calls per document). Prints an estimate and asks
for a y/N confirmation before sending anything.

Usage:
    python eval_classifier.py [--tag baseline|fixed] [--yes] [--bundles]
      --tag      label written into the output filename (default: timestamp)
      --yes      skip the interactive cost confirmation (for re-runs)
      --bundles  score SEGMENTATION against the bundle ground truth instead of
                 naming against the misnamed set (see below)

Output: eval_results[_<tag>].csv on the Desktop, one row per document plus a
summary line, with a baseline-reproduction column (does today's on-disk code
still produce the same wrong answer the spreadsheet recorded?). Every row also
carries its OWN measured token counts and cost, so per-document cost can be
compared between runs rather than estimated.

BUNDLE MODE (--bundles) scores the split decision, not the name. Its ground
truth is <GT root>/bundles/bundle_ground_truth.json, one entry per file:
  {"split": false, "type": "..."}            must NOT be split
  {"split": true, "exact": [{"type","pages"}]} must split exactly so
  {"split": true, "sampled": true, "types": [...], "min_segments": n}
                                             must split, types must appear
The outcomes are deliberately asymmetric: a MISSED split (the file was left
whole) is acceptable and merely loses a document to the old behaviour, while a
MIS-SPLIT (a boundary that disagrees with the ground truth, or any split of a
must-not-split file) is a hard failure - it destroys a compliance record.
"""

import csv
import json
import os
import re
import sys
import time
import importlib.util
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent
STAGE2_PATH = HERE / "Stage2_Processing.pyw"

# The ground-truth set is NOT part of the repository (real worker documents).
# STAGE2_GT_ROOT wins; otherwise probe the historic locations in order so the
# harness survives tidying. Home-relative, so it works for any user account.
def _desktop() -> Path:
    home = Path.home()
    for cand in (home / "OneDrive" / "Desktop", home / "Desktop"):
        if cand.is_dir():
            return cand
    return home / "Desktop"


_ENV_ROOT = (os.environ.get("STAGE2_GT_ROOT") or "").strip()
_GT_ROOTS = ([Path(_ENV_ROOT)] if _ENV_ROOT else []) + [
    _desktop() / "Lifted" / "Week 4",
    _desktop(),
]
GT_FOLDER = next((r / "Misnamed Files" for r in _GT_ROOTS
                  if (r / "Misnamed Files").is_dir()),
                 _GT_ROOTS[0] / "Misnamed Files")
GT_XLSX = next((r / "misnamed files record.xlsx" for r in _GT_ROOTS
                if (r / "misnamed files record.xlsx").is_file()),
               _GT_ROOTS[0] / "misnamed files record.xlsx")
# bundle ground truth sits beside the naming set, one level up
BUNDLE_DIR = next((r.parent / "bundles" for r in _GT_ROOTS
                   if (r.parent / "bundles").is_dir()),
                  _GT_ROOTS[0].parent / "bundles")


def load_stage2():
    spec = importlib.util.spec_from_file_location("stage2", STAGE2_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stage2"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Step 2 - Ideal-name normalisation.
# Keys are the EXACT (lowercased, stripped) "Ideal name" strings in the xlsx,
# values are the canonical controlled-vocabulary names after the vocabulary
# changes. Per-file overrides (below) win over this table where the document
# content contradicted the rough label.
# ---------------------------------------------------------------------------
IDEAL_TO_CANONICAL = {
    "other - hospital discharge letter":         "Hospital Discharge Letter",
    "use of own car form declaration":           "Use of Own Car Declaration",
    "use of own car delcaration":                "Use of Own Car Declaration",
    "use of own car declaration form":           "Use of Own Car Declaration",
    "driving license summary":                   "Driving Licence Summary",
    "training certificate":                      "Training Certificate",
    "police check":                              "Proof of Police Check",
    "proof of police check":                     "Proof of Police Check",
    "english proficiency":                       "Proof of English Proficiency",
    "other - unknown":                           "Other - Unknown",
    "brp":                                       "BRP",
    "visa vignette":                             "Visa Vignette",
    "proof of tuberculosis test":                "Proof of Tuberculosis Test",
    # DBS Update Service 'Certificate check results' printout: under the
    # four-way DBS split this is the check RECORD, i.e. 'DBS Check' (decided
    # from the document content; Tom's rough label said 'dbs check notes').
    "dbs check notes":                           "DBS Check",
    "health declaration":                        "Health Declaration",
    "other - cos summary":                       "CoS Summary",
    "other - cos query email":                   "CoS Query Email",
    "other - document validation report":        "Document Validation Report",
    "other - corrective action report":          "Corrective Action Report",
    "other - media use agreement":               "Media Use Agreement",
    "other - nest access intranet declaration":  "Intranet Access Declaration",
    "other - mobile phone use agreement":        "Mobile Phone Use Agreement",
    "other - recepit of uniform declaration":    "Receipt of Uniform and Deposit Declaration",
    "id":                                        "ID Badge",
    "uk driving license":                        "UK Driving Licence",
    # 2026-08-11 Watra post-audit set: the verification report's true_type
    # strings are already canonical, so they are listed here only where a
    # variant spelling turns up in the labels.
    "share code check result":                   "Share Code Check Result",
    "employment application form":               "Employment Application Form",
    "proof of car insurance":                    "Proof of Car Insurance",
    "proof of vehicle tax":                      "Proof of Vehicle Tax",
    "employee handbook receipt":                 "Employee Handbook Receipt",
    "driving permit":                            "Driving Permit",
    "international driving permit":               "Driving Permit",
}

# Content-decided overrides (document inspected; rough label contradicted it).
# ProofofCarOwnership.pdf is byte-identical to 'Proof of Car Ownership.pdf'
# and both are a scan of the BACK of a UK driving licence photocard.
FILE_OVERRIDES = {
    "proofofcarownership.pdf": "UK Driving Licence",
}

# For truths that live in the Other group the filed name is
# 'Other - <model's short label>', so exact string equality is wrong.
# Accept an Other-group prediction whose label matches these patterns.
# Patterns are generalisable descriptions of the type, not file-specific.
OTHER_LABEL_PATTERNS = {
    "Hospital Discharge Letter":  r"(hospital|discharge|a&e|emergency dep|clinical)",
    "CoS Summary":                r"(cos|sponsor).*(summary|screenshot|record|note|page|amend)|sponsorship management",
    "CoS Query Email":            r"(cos|sponsor).*(email|query|correspondence)|email.*(cos|sponsor)",
    "Document Validation Report": r"(document|id|identity).*(validation|verification).*report|trustid|validation report",
    "Corrective Action Report":   r"(corrective|remedial|audit).*(action|report|finding)",
    "Media Use Agreement":        r"(media|photo|photography|image).*(agreement|permission|consent|policy)",
    "Mobile Phone Use Agreement": r"(mobile|phone).*(agreement|policy|declaration|acknowledg)",
    # Watra post-audit recurring Other types (canonical strings fixed in the
    # 2026-08-11 vocabulary pass) - the model words them slightly differently
    # every time, so match the type rather than the exact phrase.
    "Other - MOT history check":
        r"\bmot\b",
    "Other - Criminal Record Check Declaration":
        r"criminal record|rehabilitation of offenders|unspent conviction|"
        r"convictions? (self.)?declaration",
    "Other - Training Repayment Agreement":
        r"training.*(repay|cost).*(agreement|recovery)|repayment agreement",
    "Other - Employment offer acceptance letter":
        r"(offer|employment).*(accept)|accept.*(offer|employment)",
    "Other - Consulate appointment booking confirmation":
        r"(consulate|embassy|high commission).*(appoint|booking)|"
        r"appointment.*(booking|confirmation)",
    "Other - NHS GP Registration Confirmation Letter":
        r"(gp|doctor|surgery|nhs|practice).*(registrat|confirm)",
    "Other - employment verification confirmation letter":
        r"employment.*(verif|confirm)|(verif|confirm).*employment",
    "Other - bank letter about online banking":
        r"bank.*(letter|online|internet)|online banking",
    "Other - Unknown":            r".*",   # any Other-group abstention is a safe landing
}


def norm(s: str) -> str:
    return re.sub(r"[\s\-_]+", " ", (s or "").strip().lower())


# lookup must survive spacing/hyphen variations in the rough Ideal strings
IDEAL_TO_CANONICAL = {norm(k): v for k, v in IDEAL_TO_CANONICAL.items()}


def prediction_matches(pred_name: str, canonical: str, stage2) -> bool:
    """True if the production-filed name `pred_name` is a correct answer for
    the canonical truth, tolerating rank/date suffixes and, for Other-group
    truths, an equivalent 'Other - <description>'."""
    base = stage2.base_controlled_name(pred_name)
    if norm(base) == norm(canonical):
        return True
    # 'Other - Unknown' truth: any Other-group answer counts (see patterns)
    m = re.match(r"^\s*other\s*-\s*(.+)$", base, flags=re.I)
    if m:
        label = m.group(1)
        pat = OTHER_LABEL_PATTERNS.get(canonical)
        if pat and re.search(pat, label, flags=re.I):
            return True
        # an Other answer whose label simply names the canonical type. The
        # truth may itself be written as 'Other - <type>' (that IS the filed
        # name for an Other-group type), so compare on the type alone.
        want = re.sub(r"^\s*other\s*-\s*", "", canonical, flags=re.I)
        if norm(want) in norm(label):
            return True
    return False


def make_apis(stage2, cfg, api_key, model_id):
    """The primary client plus the low-confidence second-opinion client,
    mirroring Engine._make_engine exactly so the harness pays for - and
    measures - the same calls production would make."""
    api = stage2.ClaudeAPI(api_key, model_id)
    esc_api = None
    if bool(cfg.get("second_opinion", True)):
        prim = stage2.MODELS_BY_ID.get(model_id, {})
        strong = stage2.MODELS_BY_ID.get(stage2.SECOND_OPINION_MODEL_ID, {})
        if (model_id != stage2.SECOND_OPINION_MODEL_ID
                and prim.get("in", 99.0) < strong.get("in", 0.0)):
            esc_api = stage2.ClaudeAPI(api_key, stage2.SECOND_OPINION_MODEL_ID)
    return api, esc_api


def _seg_types(plan):
    return [s["type"] for s in (plan or [])]


def _seg_pages(plan):
    """1-based inclusive page ranges of a plan, for reporting/comparison."""
    return [[p + 1 for p in s["pages"]] for s in (plan or [])]


def score_bundle(spec, plan, possible_bundle, stage2):
    """Compare one segmentation decision with its ground truth.
    Returns (outcome, detail) where outcome is one of:
      CORRECT     did exactly the right thing
      MISSED      should have split, left whole (acceptable - old behaviour)
      MIS-SPLIT   split where it must not, or split to the wrong boundaries
                  (a hard failure: this destroys a compliance record)"""
    want_split = bool(spec.get("split"))
    got = _seg_pages(plan)
    types = _seg_types(plan)
    if not want_split:
        if plan is None:
            note = "left whole"
            if spec.get("too_long"):
                note += (" and flagged" if possible_bundle else
                         " but NOT flagged")
            return ("CORRECT", note)
        return ("MIS-SPLIT", f"split a must-not-split file into {got}")
    if plan is None:
        return ("MISSED", "left whole"
                + (" (flagged)" if possible_bundle else ""))
    if spec.get("sampled"):
        want_types = [norm(t) for t in spec.get("types", [])]
        have = [norm(t) for t in types]
        missing = [t for t in want_types if t not in have]
        if len(plan) < int(spec.get("min_segments", 2)):
            return ("MIS-SPLIT",
                    f"only {len(plan)} segments, expected at least "
                    f"{spec.get('min_segments')}: {got} {types}")
        if missing:
            return ("MIS-SPLIT",
                    f"missing expected type(s) {missing}: {types}")
        return ("CORRECT", f"{len(plan)} segments {types}")
    exact = spec.get("exact") or []
    want_pages = [list(e["pages"]) for e in exact]
    if got != want_pages:
        return ("MIS-SPLIT", f"boundaries {got}, expected {want_pages}")
    bad = [(a, b) for a, b in zip(types, [e["type"] for e in exact])
           if norm(stage2.base_controlled_name(a)) != norm(b)
           and norm(b) not in norm(a)]
    if bad:
        return ("MIS-SPLIT", f"right boundaries, wrong types: {bad}")
    return ("CORRECT", f"{len(plan)} segments {types}")


def run_bundles(stage2, kb, vocab, api, esc_api, model_id, resolution,
                adaptive, tag, auto_yes):
    """Score the SPLIT decision over the bundle ground truth."""
    manifest = BUNDLE_DIR / "bundle_ground_truth.json"
    if not manifest.is_file():
        print(f"No bundle ground truth at {manifest}.")
        return 1
    specs = json.loads(manifest.read_text(encoding="utf-8"))
    todo = [s for s in specs if (BUNDLE_DIR / s["file"]).is_file()]
    print(f"Bundle ground truth: {len(specs)} rows, {len(todo)} files found "
          f"in {BUNDLE_DIR}")
    missing = [s["file"] for s in specs if not (BUNDLE_DIR / s["file"]).is_file()]
    for m in missing:
        print(f"  ! missing file: {m}")
    est = stage2.estimate_run_cost_gbp(
        len(todo), model_id, resolution, adaptive, vocab_block=vocab,
        batch=False, include_second_pass=False, cached_prefix=True)
    print(f"Estimated cost: ~GBP {est['gbp']:.2f} (billed on real usage)")
    if not auto_yes:
        if input("Send these documents to the Anthropic API? [y/N] ").strip().lower() != "y":
            print("Aborted - nothing sent.")
            return 0

    rows, counts = [], {"CORRECT": 0, "MISSED": 0, "MIS-SPLIT": 0}
    for i, spec in enumerate(todo, 1):
        p = BUNDLE_DIR / spec["file"]
        t0i, t0o = api.in_tokens, api.out_tokens
        e0i, e0o = ((esc_api.in_tokens, esc_api.out_tokens)
                    if esc_api else (0, 0))
        print(f"[{i}/{len(todo)}] row {spec['row']} {p.name[:44]} "
              f"({spec['pages']}p) ...", end=" ", flush=True)
        try:
            core = stage2.classify_document_core(
                api, vocab, p, resolution=resolution,
                adaptive_pages=adaptive, escalation_api=esc_api)
        except Exception as e:
            print(f"ERROR {e}")
            rows.append([spec["row"], p.name, spec["pages"], "ERROR", str(e),
                         "", "", 0, 0, 0.0])
            continue
        result = core["result"] or {}
        total = stage2.DocRender.page_count(p)
        inks = stage2.page_ink_fractions(p)
        idxs, ghosts, full = stage2.segmentation_pages(p, total, inks)
        plan = stage2.plan_segments(kb, result, idxs, ghosts, total) if full \
            else None
        outcome, detail = score_bundle(spec, plan,
                                       core.get("possible_bundle"), stage2)
        counts[outcome] = counts.get(outcome, 0) + 1
        din = api.in_tokens - t0i
        dout = api.out_tokens - t0o
        cost = stage2.tokens_cost_gbp(model_id, din, dout)
        if esc_api:
            ein = esc_api.in_tokens - e0i
            eout = esc_api.out_tokens - e0o
            cost += stage2.tokens_cost_gbp(esc_api.model_id, ein, eout)
            din += ein; dout += eout
        print(f"{outcome} - {detail}  [{len(idxs)} imgs, {len(ghosts)} ghost, "
              f"GBP {cost:.4f}]")
        rows.append([spec["row"], p.name, spec["pages"], outcome, detail,
                     json.dumps(_seg_pages(plan)),
                     json.dumps(_seg_types(plan)), din, dout, round(cost, 5)])

    out = stage2.desktop_path() / f"eval_bundles_{tag}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["row", "file", "pages", "outcome", "detail",
                    "segment_pages", "segment_types", "in_tokens",
                    "out_tokens", "gbp"])
        w.writerows(rows)
        w.writerow([])
        for k in ("CORRECT", "MISSED", "MIS-SPLIT"):
            w.writerow([f"{k}: {counts.get(k, 0)}"])
    n = max(1, len(rows))
    print(f"\nSUMMARY  correct {counts.get('CORRECT',0)}/{len(rows)} "
          f"({100.0*counts.get('CORRECT',0)/n:.0f}%), "
          f"missed {counts.get('MISSED',0)}, "
          f"MIS-SPLIT {counts.get('MIS-SPLIT',0)}")
    print("GATE: >=80% correct AND zero mis-splits -> "
          + ("PASS" if counts.get("CORRECT", 0) >= 0.8 * n
             and not counts.get("MIS-SPLIT", 0) else "FAIL"))
    spent = stage2.tokens_cost_gbp(model_id, api.in_tokens, api.out_tokens)
    if esc_api:
        spent += stage2.tokens_cost_gbp(esc_api.model_id, esc_api.in_tokens,
                                        esc_api.out_tokens)
    print(f"Actual API usage: {api.in_tokens:,} in / {api.out_tokens:,} out "
          f"(~GBP {spent:.2f})")
    print(f"Results written to: {out}")
    return 0


def main():
    tag = ""
    auto_yes = False
    bundles = False
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--tag" and args:
            tag = args.pop(0)
        elif a == "--yes":
            auto_yes = True
        elif a == "--bundles":
            bundles = True
    if not tag:
        tag = time.strftime("%Y%m%d_%H%M%S")

    stage2 = load_stage2()

    api_key = stage2.get_api_key()
    if not api_key:
        print("No API key found (keyring 'DocReviewAIStation' / "
              "ANTHROPIC_API_KEY). Aborting.")
        return 1

    cfg = stage2.load_config()
    stage2.FX_RATE[0] = cfg.get("fx", 0.79)
    model_id = stage2.MODELS[cfg["model"]]["id"]
    resolution = float(cfg.get("resolution", 1.5))
    # Page policy mirrors the configured production mode: the last-used mode is
    # Overnight Batch, whose requests are built with pages="all" and NO triage
    # (classify_payload only). Live mode would use the adaptive triage path.
    run_mode = cfg.get("run_mode", "live")
    adaptive = bool(cfg.get("adaptive_pages", True)) and run_mode == "live"

    kb = stage2.KnowledgeBase()
    vocab = kb.vocabulary_block()

    print(f"Model: {model_id}   resolution: {resolution}x   "
          f"page policy: "
          f"{'adaptive triage (live)' if adaptive else 'all pages, no triage (batch payload)'}")

    if bundles:
        api, esc_api = make_apis(stage2, cfg, api_key, model_id)
        print(f"Second opinion: {esc_api.model_id if esc_api else 'off'}")
        return run_bundles(stage2, kb, vocab, api, esc_api, model_id,
                           resolution, adaptive, tag, auto_yes)

    # ---- read the ground truth ----
    wb = openpyxl.load_workbook(GT_XLSX)
    ws = wb[wb.sheetnames[0]]
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or not r[0]:
            continue
        fpath, ai_name, ideal = (str(r[0]).strip(), str(r[1] or "").strip(),
                                 str(r[2] or "").strip())
        kind = (str(r[3]).strip().lower() if len(r) > 3 and r[3] else "misnamed")
        p = Path(fpath)
        if not p.exists():
            cand = GT_FOLDER / p.name
            p = cand if cand.exists() else p
        canonical = FILE_OVERRIDES.get(p.name.lower()) \
            or IDEAL_TO_CANONICAL.get(norm(ideal))
        if canonical is None:
            print(f"  ! no canonical mapping for ideal {ideal!r} - treated "
                  f"literally")
            canonical = ideal
        rows.append({"path": p, "ai_name": ai_name, "ideal": ideal,
                     "canonical": canonical, "kind": kind})

    missing = [r for r in rows if not r["path"].exists()]
    if missing:
        for r in missing:
            print(f"  ! missing file: {r['path']}")
    todo = [r for r in rows if r["path"].exists()]
    print(f"Ground truth: {len(rows)} rows, {len(todo)} files found.")

    # ---- cost estimate + confirmation ----
    est = stage2.estimate_run_cost_gbp(
        len(todo), model_id, resolution, adaptive, vocab_block=vocab,
        batch=False, include_second_pass=False, cached_prefix=True)
    print(f"Estimated cost for {len(todo)} classification(s): "
          f"~GBP {est['gbp']:.2f} (billed on real usage)")
    if not auto_yes:
        ans = input("Send these documents to the Anthropic API? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted - nothing sent.")
            return 0

    api, esc_api = make_apis(stage2, cfg, api_key, model_id)
    print(f"Second opinion: "
          f"{esc_api.model_id if esc_api else 'off'}")

    by_kind = {}
    for r in todo:
        by_kind.setdefault(r["kind"], []).append(r)
    print("Set: " + ", ".join(f"{k}={len(v)}"
                              for k, v in sorted(by_kind.items())))

    out_rows = []
    n_match = 0
    n_repro = 0
    kind_hits = {k: [0, 0] for k in by_kind}      # kind -> [correct, total]
    misses = []
    percost = []          # per-document GBP, for the median/mean comparison
    for i, r in enumerate(todo, 1):
        p = r["path"]
        t0i, t0o = api.in_tokens, api.out_tokens
        e0i, e0o = (esc_api.in_tokens, esc_api.out_tokens) if esc_api else (0, 0)

        def _spent():
            """Tokens and GBP this document actually cost, primary plus any
            second opinion. Measured, not estimated."""
            din, dout = api.in_tokens - t0i, api.out_tokens - t0o
            g = stage2.tokens_cost_gbp(model_id, din, dout)
            if esc_api:
                ein = esc_api.in_tokens - e0i
                eout = esc_api.out_tokens - e0o
                g += stage2.tokens_cost_gbp(esc_api.model_id, ein, eout)
                din += ein; dout += eout
            return din, dout, g

        print(f"[{i}/{len(todo)}] {p.name} ...", end=" ", flush=True)
        try:
            # the core never sends the filename to the model (essential here:
            # these filenames are the previous run's wrong answers)
            core = stage2.classify_document_core(
                api, vocab, p, resolution=resolution,
                adaptive_pages=adaptive, escalation_api=esc_api)
            result = core["result"]
        except Exception as e:
            print(f"ERROR {e}")
            kind_hits[r["kind"]][1] += 1
            misses.append((r["kind"], p.name, r["canonical"], f"ERROR: {e}"))
            din, dout, gbp = _spent()
            out_rows.append([str(p), r["ai_name"], f"ERROR: {e}", "",
                             r["canonical"], "no", "", r["kind"],
                             0, 0, "", "", din, dout, round(gbp, 5), 0])
            continue

        # interpret the result exactly as production does (auto-Other path)
        matched, name, group, conf, features, other_label = \
            stage2.validate_result(kb, result)
        if not matched or not name:
            if not other_label:
                other_label = (result.get("guess") or "").strip()
            matched, name, group = False, "Other", "Other"
        if group == "Other":
            if matched and name and name != "Other":
                name = stage2.other_name(name)
            else:
                name = stage2.other_name(other_label)

        ok = prediction_matches(name, r["canonical"], stage2)
        n_match += 1 if ok else 0
        kind_hits[r["kind"]][0] += 1 if ok else 0
        kind_hits[r["kind"]][1] += 1
        if not ok:
            misses.append((r["kind"], p.name, r["canonical"], name))
        repro = norm(stage2.base_controlled_name(name)) == norm(r["ai_name"])
        n_repro += 1 if repro else 0
        tags = ""
        if core.get("rotation_retried"):
            tags += f" [rotation: {core.get('rotation_path', 'retry')}]"
        if core.get("escalated"):
            tags += " [2nd opinion]"
        # a SPLIT on the naming set would be a regression: these files were
        # each filed under one name and the gate is that that does not change
        nseg = 0
        try:
            plan = stage2.plan_segments(
                kb, result, core.get("page_idxs") or [],
                set(core.get("ghost_pages") or []),
                core.get("total_pages") or 1) if core.get("segment_view") else None
            nseg = len(plan or [])
        except Exception:
            nseg = 0
        if nseg:
            tags += f" [WOULD SPLIT into {nseg}]"
        din, dout, gbp = _spent()
        percost.append(gbp)
        print(f"-> {name!r} (conf {conf}) "
              f"{'MATCH' if ok else 'MISS'}"
              f"{' [reproduces old error]' if repro else ''}{tags} "
              f"[{len(core.get('used_imgs') or [])} imgs, "
              f"{len(core.get('ghost_pages') or [])} ghost, GBP {gbp:.4f}]")
        out_rows.append([str(p), r["ai_name"], name, conf, r["canonical"],
                         "yes" if ok else "no",
                         "yes" if repro else "no", r["kind"],
                         len(core.get("used_imgs") or []),
                         len(core.get("ghost_pages") or []),
                         # `rotation_path` only exists from v1.2.0; record the
                         # boolean too or a BASELINE run (which sets only
                         # rotation_retried) looks like it never rotated
                         # anything and the rotation gate cannot be scored
                         "yes" if core.get("rotation_retried") else "",
                         core.get("rotation_path", ""),
                         din, dout, round(gbp, 5), nseg])

    # ---- write CSV ----
    desk = stage2.desktop_path()
    out = desk / (f"eval_results_{tag}.csv" if tag else "eval_results.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["file", "previous_AI_name", "new_prediction",
                    "confidence", "normalised_ideal", "match",
                    "reproduces_previous_error", "kind",
                    "images_sent", "ghost_pages", "rotation_retried",
                    "rotation_path",
                    "in_tokens", "out_tokens", "gbp", "would_split_into"])
        w.writerows(out_rows)
        w.writerow([])
        w.writerow([f"SUMMARY: {n_match} correct / {len(out_rows)} total "
                    f"({100.0*n_match/max(1,len(out_rows)):.0f}%); "
                    f"{n_repro} reproduce the previous wrong answer"])
        for k, (hit, tot) in sorted(kind_hits.items()):
            w.writerow([f"  {k}: {hit}/{tot} "
                        f"({100.0*hit/max(1,tot):.0f}%)"])
        if percost:
            srt = sorted(percost)
            w.writerow([f"  cost/doc: median GBP {srt[len(srt)//2]:.5f}, "
                        f"mean GBP {sum(srt)/len(srt):.5f}, "
                        f"total GBP {sum(srt):.4f}"])
    cost = stage2.tokens_cost_gbp(model_id, api.in_tokens, api.out_tokens)
    if esc_api is not None:
        cost += stage2.tokens_cost_gbp(esc_api.model_id, esc_api.in_tokens,
                                       esc_api.out_tokens)
    print(f"\nSUMMARY: {n_match}/{len(out_rows)} correct "
          f"({100.0*n_match/max(1,len(out_rows)):.0f}%), "
          f"{n_repro} reproduce the old wrong answer.")
    for k, (hit, tot) in sorted(kind_hits.items()):
        print(f"  {k:<9} {hit}/{tot} ({100.0*hit/max(1,tot):.0f}%)")
    if percost:
        srt = sorted(percost)
        print(f"  cost/doc:  median GBP {srt[len(srt)//2]:.5f}   "
              f"mean GBP {sum(srt)/len(srt):.5f}   "
              f"total GBP {sum(srt):.4f}")
    spurious = [row for row in out_rows if row[-1]]
    print(f"  spurious splits on the naming set: {len(spurious)}"
          + (" (REGRESSION - the gate is zero)" if spurious else ""))
    for row in spurious:
        print(f"    ! {Path(row[0]).name} would split into {row[-1]}")
    if misses:
        print("\nMISSES (kind | file | wanted | got):")
        for k, fname, want, got in misses:
            print(f"  {k:<9} {fname[:52]:<52} {want!r} -> {got!r}")
    print(f"Actual API usage: {api.in_tokens:,} in / {api.out_tokens:,} out "
          f"tokens"
          + (f" (+{esc_api.in_tokens:,}/{esc_api.out_tokens:,} second-opinion)"
             if esc_api is not None else "")
          + f"  (~GBP {cost:.2f})")
    print(f"Results written to: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
