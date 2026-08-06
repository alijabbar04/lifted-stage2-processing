"""
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
                (columns: File Path | AI name | Ideal name)

IMPORTANT: the filenames in the ground-truth folder are the PREVIOUS RUN'S
WRONG ANSWERS (Stage 2 renamed them). The filename is therefore NEVER given
to the model here (filename_for_triage="").

Costs real API money (~1-2 calls per document). Prints an estimate and asks
for a y/N confirmation before sending anything.

Usage:
    python eval_classifier.py [--tag baseline|fixed] [--yes]
      --tag   label written into the output filename (default: run timestamp)
      --yes   skip the interactive cost confirmation (for re-runs)

Output: eval_results[_<tag>].csv on the Desktop, one row per document plus a
summary line, with a baseline-reproduction column (does today's on-disk code
still produce the same wrong answer the spreadsheet recorded?).
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
        # an Other answer whose label simply names the canonical type
        if norm(canonical) in norm(label):
            return True
    return False


def main():
    tag = ""
    auto_yes = False
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--tag" and args:
            tag = args.pop(0)
        elif a == "--yes":
            auto_yes = True
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

    # ---- read the ground truth ----
    wb = openpyxl.load_workbook(GT_XLSX)
    ws = wb[wb.sheetnames[0]]
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or not r[0]:
            continue
        fpath, ai_name, ideal = (str(r[0]).strip(), str(r[1] or "").strip(),
                                 str(r[2] or "").strip())
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
                     "canonical": canonical})

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
    print(f"Model: {model_id}   resolution: {resolution}x   "
          f"page policy: {'adaptive triage (live)' if adaptive else 'all pages, no triage (batch payload)'}")
    print(f"Estimated cost for {len(todo)} classification(s): "
          f"~GBP {est['gbp']:.2f} (billed on real usage)")
    if not auto_yes:
        ans = input("Send these documents to the Anthropic API? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted - nothing sent.")
            return 0

    api = stage2.ClaudeAPI(api_key, model_id)
    # low-confidence second opinion, mirroring Engine._make_engine exactly
    esc_api = None
    if bool(cfg.get("second_opinion", True)):
        prim = stage2.MODELS_BY_ID.get(model_id, {})
        strong = stage2.MODELS_BY_ID.get(stage2.SECOND_OPINION_MODEL_ID, {})
        if (model_id != stage2.SECOND_OPINION_MODEL_ID
                and prim.get("in", 99.0) < strong.get("in", 0.0)):
            esc_api = stage2.ClaudeAPI(api_key,
                                       stage2.SECOND_OPINION_MODEL_ID)
    print(f"Second opinion: "
          f"{esc_api.model_id if esc_api else 'off'}")

    out_rows = []
    n_match = 0
    n_repro = 0
    for i, r in enumerate(todo, 1):
        p = r["path"]
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
            out_rows.append([str(p), r["ai_name"], f"ERROR: {e}", "",
                             r["canonical"], "no", ""])
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
        repro = norm(stage2.base_controlled_name(name)) == norm(r["ai_name"])
        n_repro += 1 if repro else 0
        tags = ""
        if core.get("rotation_retried"):
            tags += " [rotation retry]"
        if core.get("escalated"):
            tags += " [2nd opinion]"
        print(f"-> {name!r} (conf {conf}) "
              f"{'MATCH' if ok else 'MISS'}"
              f"{' [reproduces old error]' if repro else ''}{tags}")
        out_rows.append([str(p), r["ai_name"], name, conf, r["canonical"],
                         "yes" if ok else "no",
                         "yes" if repro else "no"])

    # ---- write CSV ----
    desk = stage2.desktop_path()
    out = desk / (f"eval_results_{tag}.csv" if tag else "eval_results.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["file", "previous_AI_name", "new_prediction",
                    "confidence", "normalised_ideal", "match",
                    "reproduces_previous_error"])
        w.writerows(out_rows)
        w.writerow([])
        w.writerow([f"SUMMARY: {n_match} correct / {len(out_rows)} total "
                    f"({100.0*n_match/max(1,len(out_rows)):.0f}%); "
                    f"{n_repro} reproduce the previous wrong answer"])
    cost = stage2.tokens_cost_gbp(model_id, api.in_tokens, api.out_tokens)
    if esc_api is not None:
        cost += stage2.tokens_cost_gbp(esc_api.model_id, esc_api.in_tokens,
                                       esc_api.out_tokens)
    print(f"\nSUMMARY: {n_match}/{len(out_rows)} correct "
          f"({100.0*n_match/max(1,len(out_rows)):.0f}%), "
          f"{n_repro} reproduce the old wrong answer.")
    print(f"Actual API usage: {api.in_tokens:,} in / {api.out_tokens:,} out "
          f"tokens"
          + (f" (+{esc_api.in_tokens:,}/{esc_api.out_tokens:,} second-opinion)"
             if esc_api is not None else "")
          + f"  (~GBP {cost:.2f})")
    print(f"Results written to: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
