"""End-to-end split test - no API calls, so it costs nothing to run:

    python src\\test_split_e2e.py

Drives Engine._maybe_split_bundle over a real 3-page bundle with a STUBBED
classification reply (the stub raises if anything tries to call the API,
which is itself the assertion that a split child is never re-classified),
and then checks what actually landed on disk: both children written, the
parent locally corrected before splitting, children inheriting that correction
exactly once including after restart, and the archive surviving two cleanup passes
that used to destroy it.

Needs the bundle ground truth (real worker files, never in the repo) - see
STAGE2_GT_ROOT, the same variable eval_classifier.py uses.
"""
import importlib.util, sys, shutil, tempfile, os
from pathlib import Path
import fitz

HERE = Path(__file__).resolve().parent
SRC = HERE / "Stage2_Processing.pyw"
_root = (os.environ.get("STAGE2_GT_ROOT") or "").strip()
GT = (Path(_root).parent if _root else
      Path(os.environ.get("LOCALAPPDATA", "")) / "Lifted" / "EvalGT") / "bundles"
if not GT.is_dir():
    print(f"Bundle ground truth not found at {GT}")
    sys.exit(2)

tmp = Path(tempfile.mkdtemp(prefix="stage2_split_"))
# keep the app's own state out of the real profile
os.environ["LOCALAPPDATA"] = str(tmp / "local")
os.environ["APPDATA"] = str(tmp / "roaming")
(tmp / "local").mkdir(parents=True, exist_ok=True)
(tmp / "roaming").mkdir(parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location("stage2", SRC)
s2 = importlib.util.module_from_spec(spec)
sys.modules["stage2"] = spec.loader.exec_module(s2) or s2

print("APP_DIR =", s2.APP_DIR)
ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print(f"  PASS  {label} {extra}")
    else:
        fail += 1; print(f"  FAIL  {label} {extra}")


class StubAPI:
    model_id = "stub"
    in_tokens = out_tokens = 0
    def classify(self, *a, **k):
        raise AssertionError("a split child must NOT be re-classified")
    def _post(self, *a, **k):
        raise AssertionError("no API call expected during a split")


class StubOrientation:
    def predict(self, images):
        return [{"predicted_orientation": 90, "correction": 270,
                 "confidence": 0.99, "runner_up_confidence": 0.005,
                 "margin": 0.985} for _ in images]


care = tmp / "Care Home [Processed]"
worker = care / "Aqil"
worker.mkdir(parents=True)
src = next(iter(GT.glob("row_0106*")))
target = worker / "Safeguarding Questionnaire.pdf"
shutil.copy2(src, target)
print("bundle:", target.name, s2.DocRender.page_count(target), "pages")

kb = s2.KnowledgeBase()
eng = s2.Engine(
    care_home_dir=care, kb=kb, api=StubAPI(), care_home_name="Care Home",
    log=lambda m: print("   log:", m), set_status=lambda *a: None,
    set_progress=lambda *a: None, set_preview=lambda *a: None,
    ask_unknown=lambda *a, **k: None, on_cost=lambda *a: None,
    on_done=lambda *a: None, auto_other=True, bundle_split=True,
    orientation_mode="automatic",
    orientation_predictor=StubOrientation())

# what the classification call would have returned for this file: two
# documents, page 3 being the blank verso of page 2, and every page sideways
result = {
    "match": False, "name": "", "group": "Other", "confidence": 88,
    "other_label": "criminal record check declaration",
    "rotation": 270, "rotations": [270, 270],
    "documents": [
        {"pages": [1], "type": "Criminal Record Check Declaration",
         "document_date": None, "confidence": 91},
        {"pages": [2, 3], "type": "Emergency Contact Details",
         "document_date": None, "confidence": 89},
    ],
}

before = {p.name for p in worker.iterdir()}
records = []
vocab = kb.vocabulary_block()
# Deterministic local preflight: make all three pages eligible so this harness
# specifically verifies correction -> split -> restart idempotence. Blank/sparse
# vetoes are covered separately by the unit suite.
old_evidence = s2.thumbnail_evidence
old_text_orientation = s2.detect_pdf_page_text_rotations
s2.thumbnail_evidence = lambda _image: {
    "blank": False, "sparse": False, "photograph_only": False,
    "ink_fraction": 0.2}
s2.detect_pdf_page_text_rotations = lambda *a, **k: {}
orientation = eng._orientation_preflight(target)
s2.thumbnail_evidence = old_evidence
s2.detect_pdf_page_text_rotations = old_text_orientation
check("parent corrected locally before split", orientation.get("changed"))
eng._consume_rotation_instructions(result)
out = eng._maybe_split_bundle(worker, target, result, vocab, records,
                              interactive=False, default_source="AI",
                              unknown_queue=None, depth=0)

check("split was applied", out is not None)
after = sorted(p.name for p in worker.iterdir() if p.is_file())
print("   worker folder now:", after)
check("parent no longer in the worker folder", not target.exists())
check("two children written", len([a for a in after if a.endswith('.pdf')]) == 2,
      f"-> {after}")

arch = s2.APP_DIR / "Original Bundles"
archived = list(arch.rglob("*.pdf"))
check("original archived outside the worker tree", len(archived) == 1,
      f"-> {[str(p.relative_to(arch)) for p in archived]}")
check("archive is NOT under the worker folder",
      all(worker not in p.parents for p in archived))
check("archived original still has all 3 pages",
      archived and s2.DocRender.page_count(archived[0]) == 3)

# children: page counts and baked-in rotation
kids = sorted(p for p in worker.glob("*.pdf"))
counts = [s2.DocRender.page_count(p) for p in kids]
check("children cover every page (1 + 2 = 3)", sorted(counts) == [1, 2],
      f"-> {[(p.name, c) for p, c in zip(kids, counts)]}")
# the reported rotation is a turn ON TOP of whatever /Rotate the page already
# carried, so the expected value is (original + 270) % 360 - and every page of
# a child, including the blank verso, must end up the same way up
d = fitz.open(src); orig = [pg.rotation for pg in d]; d.close()
want = [(r + 270) % 360 for r in orig]
print("   source /Rotate:", orig, "-> expected:", want)
rots = []
for p in kids:
    d = fitz.open(p); rots.append([pg.rotation for pg in d]); d.close()
flat = [r for rr in rots for r in rr]
check("rotation composed onto the children", sorted(flat) == sorted(want),
      f"-> {rots}")
check("every page of a child ends up the same way up",
      all(len(set(rr)) == 1 for rr in rots), f"-> {rots}")

# Restart must use inherited child state and never turn a child again.
class BombOrientation:
    def predict(self, _images):
        raise AssertionError("split child orientation must not rerun")

restart = s2.Engine(
    care_home_dir=care, kb=kb, api=StubAPI(), care_home_name="Care Home",
    log=lambda *a: None, set_status=lambda *a: None,
    set_progress=lambda *a: None, set_preview=lambda *a: None,
    ask_unknown=lambda *a, **k: None, on_cost=lambda *a: None,
    on_done=lambda *a: None, orientation_mode="automatic",
    orientation_predictor=BombOrientation())
restart_ok = True
try:
    for p in kids:
        restart._orientation_preflight(p)
except AssertionError:
    restart_ok = False
check("restart skips inherited child rotations", restart_ok)
post_restart = []
for p in kids:
    d = fitz.open(p); post_restart.extend(pg.rotation for pg in d); d.close()
check("restart leaves every page rotated exactly once",
      sorted(post_restart) == sorted(want), f"-> {post_restart}")

# the ghost page must have travelled with the segment before it
check("blank page 3 kept on disk (never dropped)", sum(counts) == 3)

# flatten/cleanup must not be able to destroy the archive
s2.cleanup_leftover_files(worker, log=lambda m: None)
s2.flatten_worker(worker, log=lambda m: None)
check("archive survives cleanup_leftover_files + flatten_worker",
      all(p.exists() for p in archived))

print(f"\n==== {ok} passed, {fail} failed ====")
print("temp tree:", tmp)
sys.exit(1 if fail else 0)
