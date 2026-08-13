"""Offline checks of the v1.2.0 segmentation + ghost machinery.

Makes NO API calls and costs nothing, so it can be run on every change:

    python src\\test_segmentation.py

It does need the ground-truth documents (real worker files, never in the
repo) for the page-selection checks - point STAGE2_GT_ROOT at your copy, the
same variable eval_classifier.py uses. Exits non-zero on any failure.
"""
import importlib.util, os, sys, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "Stage2_Processing.pyw"
spec = importlib.util.spec_from_file_location("stage2", SRC)
s2 = importlib.util.module_from_spec(spec)
sys.modules["stage2"] = s2
spec.loader.exec_module(s2)
print("module loaded; APP_VERSION =", s2.APP_VERSION)
print("MAX_SEG_PAGES =", s2.MAX_SEG_PAGES, " GHOST_INK_FRAC =", s2.GHOST_INK_FRAC,
      " SEG_MIN_CONF =", s2.SEG_MIN_CONF)

_root = (os.environ.get("STAGE2_GT_ROOT") or "").strip()
GT = (Path(_root) if _root else
      Path(os.environ.get("LOCALAPPDATA", "")) / "Lifted" / "EvalGT"
      / "watra-postaudit") / "Misnamed Files"
if not GT.is_dir():
    print(f"Ground truth not found at {GT}\n"
          f"Set STAGE2_GT_ROOT to the folder holding 'Misnamed Files'.")
    sys.exit(2)
kb = s2.KnowledgeBase()
ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label} {extra}")
    else:
        fail += 1
        print(f"  FAIL  {label} {extra}")


print("\n--- ghost detection / page selection on real files ---")
cases = [
    ("row_0106*", 3, 1, 2),    # 3 pages, 1 ghost (p3), 2 shown
    ("row_0024*", 4, 2, 2),    # two share-code checks, 2 blank versos
    ("row_0064*", 8, 3, 5),
    ("row_0062*", 4, 0, 4),    # the faint-but-real file: nothing may be ghosted
    ("row_0018*", 42, None, None),  # 42 pages -> too long, no full view
]
for pat, pages, want_ghosts, want_shown in cases:
    f = next(iter(GT.glob(pat)), None)
    if not f:
        print("  (missing)", pat); continue
    total = s2.DocRender.page_count(f)
    idxs, ghosts, full = s2.segmentation_pages(f, total)
    label = f"{f.name[:34]:<34} pages={total} ghosts={len(ghosts)} shown={len(idxs)} full={full}"
    if want_ghosts is None:
        check("long file not segmentable", (not full) and len(idxs) == 3, label)
    else:
        check("page selection", total == pages and len(ghosts) == want_ghosts
              and len(idxs) == want_shown and full, label)

print("\n--- row 62: the faint real CoS must NOT be ghosted ---")
f = next(iter(GT.glob("row_0062*")))
inks = s2.page_ink_fractions(f)
print("   ink per page:", ", ".join(f"{v*100:.3f}%" for v in inks))
check("p3 (real CoS, 0.19%) kept", 2 not in s2.ghost_pages(f, inks))

print("\n--- plan_segments gates ---")
f106 = next(iter(GT.glob("row_0106*")))
total = s2.DocRender.page_count(f106)
idxs, ghosts, full = s2.segmentation_pages(f106, total)

good = {"documents": [
    {"pages": [1], "type": "Criminal Record Check Declaration", "confidence": 92},
    {"pages": [2, 3], "type": "Emergency Contact Details", "confidence": 90},
]}
plan = s2.plan_segments(kb, good, idxs, ghosts, total)
check("row 106 splits into 2", plan is not None and len(plan or []) == 2,
      f"-> {[ (p['type'], [x+1 for x in p['pages']]) for p in (plan or []) ]}")

same_type = {"documents": [
    {"pages": [1], "type": "Emergency Contact Details", "confidence": 95},
    {"pages": [2, 3], "type": "Emergency Contact Details", "confidence": 95},
]}
check("same-type bundle NOT split",
      s2.plan_segments(kb, same_type, idxs, ghosts, total) is None)

low_conf = {"documents": [
    {"pages": [1], "type": "Criminal Record Check Declaration", "confidence": 55},
    {"pages": [2, 3], "type": "Emergency Contact Details", "confidence": 95},
]}
check("low-confidence segment blocks the split",
      s2.plan_segments(kb, low_conf, idxs, ghosts, total) is None)

unknown = {"documents": [
    {"pages": [1], "type": "Unknown", "confidence": 95},
    {"pages": [2, 3], "type": "Emergency Contact Details", "confidence": 95},
]}
check("unknown segment blocks the split",
      s2.plan_segments(kb, unknown, idxs, ghosts, total) is None)

ghost_start = {"documents": [
    {"pages": [1, 2], "type": "Criminal Record Check Declaration", "confidence": 95},
    {"pages": [3], "type": "Emergency Contact Details", "confidence": 95},
]}
check("segment may not START on a ghost page (p3 is blank)",
      s2.plan_segments(kb, ghost_start, idxs, ghosts, total) is None)

# REGRESSION: asked to account for every page, the model lists the trailing
# blank as its own little "document". Absorb it into the segment before it -
# rejecting the whole plan for it left a real 4-document bundle unsplit.
trailing_blank = {"documents": [
    {"pages": [1], "type": "Criminal Record Check Declaration", "confidence": 92},
    {"pages": [2], "type": "Emergency Contact Details", "confidence": 90},
    {"pages": [3], "type": "Other - blank or continuation page", "confidence": 30},
]}
p = s2.plan_segments(kb, trailing_blank, idxs, ghosts, total)
check("an all-blank trailing 'document' is absorbed, not fatal",
      p is not None and len(p) == 2
      and [x + 1 for x in p[1]["pages"]] == [2, 3],
      f"-> {p and [[x+1 for x in q['pages']] for q in p]}")

# ...but once the blank segment is dropped there must still be a real split
only_one_real = {"documents": [
    {"pages": [1, 2], "type": "Emergency Contact Details", "confidence": 90},
    {"pages": [3], "type": "Other - blank page", "confidence": 30},
]}
check("one real document plus a blank tail is NOT a split",
      s2.plan_segments(kb, only_one_real, idxs, ghosts, total) is None)

gap_on_ghost = {"documents": [
    {"pages": [1], "type": "Criminal Record Check Declaration", "confidence": 95},
    {"pages": [3], "type": "Emergency Contact Details", "confidence": 95},
]}
check("a segment starting on a ghost page is refused (p3 is blank)",
      s2.plan_segments(kb, gap_on_ghost, idxs, ghosts, total) is None)

# genuine gap-closing, on a file whose boundary page is real content:
# row 64 = energy bill + ID badge, ghosts at pages 4, 6, 8
f64 = next(iter(GT.glob("row_0064*")))
t64 = s2.DocRender.page_count(f64)
i64, g64, _f64 = s2.segmentation_pages(f64, t64)
gap = {"documents": [
    {"pages": [1], "type": "Proof of Address", "confidence": 95},
    {"pages": [2], "type": "ID Badge", "confidence": 92},
]}
p = s2.plan_segments(kb, gap, i64, g64, t64)
check("unlisted trailing pages attach to the segment before them",
      p is not None and [x + 1 for x in p[0]["pages"]] == [1]
      and [x + 1 for x in p[1]["pages"]] == [2, 3, 4, 5, 6, 7, 8],
      f"-> {p and [[x+1 for x in q['pages']] for q in p]}")

single = {"documents": [{"pages": [1, 2, 3], "type": "Safeguarding Questionnaire",
                         "confidence": 95}]}
check("one document -> no split",
      s2.plan_segments(kb, single, idxs, ghosts, total) is None)

check("no documents key -> no split", s2.plan_segments(kb, {}, idxs, ghosts, total) is None)

overlap = {"documents": [
    {"pages": [1, 2], "type": "Criminal Record Check Declaration", "confidence": 95},
    {"pages": [2, 3], "type": "Emergency Contact Details", "confidence": 95},
]}
p = s2.plan_segments(kb, overlap, idxs, ghosts, total)
check("overlapping ranges are re-tiled, never duplicated",
      p is None or sorted(x for q in p for x in q["pages"]) == list(range(total)),
      f"-> {p and [[x+1 for x in q['pages']] for q in p]}")

print("\n--- classify payload builds with segment=True ---")
api = s2.ClaudeAPI("sk-test-not-used", "claude-haiku-4-5-20251001")
vocab = kb.vocabulary_block()
sysA, blocksA, mtA = api.classify_payload(vocab, ["Zm9v"], "", page_idxs=[0],
                                          total_pages=1, segment=False)
sysB, blocksB, mtB = api.classify_payload(vocab, ["Zm9v", "YmFy"], "",
                                          page_idxs=[0, 2], total_pages=4,
                                          segment=True)
check("non-segment prompt is unchanged (no documents map)",
      "PER-PAGE DOCUMENT MAP" not in sysA)
check("segment prompt adds the documents map", "PER-PAGE DOCUMENT MAP" in sysB)
check("segment prompt is a strict superset of today's",
      sysB.startswith(sysA))
check("segment max_tokens grows with pages", mtB > mtA, f"{mtA} -> {mtB}")

print("\n--- rotation helpers ---")
check("_rot_of reads a rotation", s2._rot_of({"rotation": 270}) == 270)
check("_rot_of rejects nonsense", s2._rot_of({"rotation": 45}) == 0)
check("four-orientation fallback still exists",
      hasattr(s2, "_rotation_retry_four"))
check("ROTATION_CONFIRM_NOTE defined", bool(s2.ROTATION_CONFIRM_NOTE))

print(f"\n==== {ok} passed, {fail} failed ====")
sys.exit(1 if fail else 0)
