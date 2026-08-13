"""Dump the RAW documents map for the bundle files that did not come back
CORRECT, so we can tell whether the model or the split gates is the limiter."""
import importlib.util, sys, json, os
from pathlib import Path

SRC = Path(r"C:\Users\mrali\repos\lifted-stage2-processing\src\Stage2_Processing.pyw")
spec = importlib.util.spec_from_file_location("stage2", SRC)
s2 = importlib.util.module_from_spec(spec); sys.modules["stage2"] = s2
spec.loader.exec_module(s2)

_root = (os.environ.get("STAGE2_GT_ROOT") or "").strip()
_base = (Path(_root) if _root else
         Path(os.environ.get("LOCALAPPDATA", "")) / "Lifted" / "EvalGT"
         / "watra-postaudit")
# look in the bundle set first, then the naming set - a file being diagnosed
# for a SPURIOUS split lives in the naming set, not the bundle set
SEARCH = [_base.parent / "bundles", _base / "Misnamed Files"]
cfg = s2.load_config()
s2.FX_RATE[0] = cfg.get("fx", 0.79)
model_id = s2.MODELS[cfg["model"]]["id"]
resolution = float(cfg.get("resolution", 1.5))
adaptive = bool(cfg.get("adaptive_pages", True)) and cfg.get("run_mode") == "live"
kb = s2.KnowledgeBase(); vocab = kb.vocabulary_block()
api = s2.ClaudeAPI(s2.get_api_key(), model_id)

WANT = [int(a) for a in sys.argv[1:] if a.isdigit()] or [34, 43, 60, 64, 106]
for n in WANT:
    f = next((h for d in SEARCH for h in sorted(d.glob(f"row_{n:04d}*"))
              if d.is_dir()), None)
    if not f:
        print(f"\n===== row {n}: no file found in {[str(d) for d in SEARCH]}")
        continue
    total = s2.DocRender.page_count(f)
    inks = s2.page_ink_fractions(f)
    idxs, ghosts, full = s2.segmentation_pages(f, total, inks)
    core = s2.classify_document_core(api, vocab, f, resolution=resolution,
                                     adaptive_pages=adaptive)
    r = core["result"] or {}
    print(f"\n===== row {n}  ({total}p, shown {[i+1 for i in idxs]}, "
          f"ghosts {[i+1 for i in ghosts]}, full_view={full}) =====")
    print(f"  whole-file answer : match={r.get('match')} "
          f"name={r.get('name')!r} other={r.get('other_label')!r} "
          f"conf={r.get('confidence')}")
    docs = r.get("documents")
    if not isinstance(docs, list):
        print("  documents: ABSENT")
    else:
        for d in docs:
            t = str(d.get("type") or "")
            canon = kb.canonical_name(t)
            print(f"    pages={d.get('pages')} conf={d.get('confidence')} "
                  f"type={t!r} -> canonical={canon!r}")
    plan = s2.plan_segments(kb, r, idxs, ghosts, total) if full else None
    print(f"  plan_segments -> "
          f"{None if plan is None else [[p+1 for p in s['pages']] for s in plan]}")
    if plan is None and isinstance(docs, list) and len(docs) > 1:
        print("  (gates refused the model's map - see gate list in plan_segments)")

print(f"\ntokens {api.in_tokens:,} in / {api.out_tokens:,} out  ~GBP "
      f"{s2.tokens_cost_gbp(model_id, api.in_tokens, api.out_tokens):.3f}")
