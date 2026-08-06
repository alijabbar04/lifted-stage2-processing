#!/usr/bin/env python3
"""
export_vocabulary.py - regenerate the human-readable vocabulary mirror.

WHY THIS EXISTS
---------------
The controlled vocabulary and the disambiguation rules are NOT separate data
files: they live in the Stage 2 source as the module-level constants
SEED_CRUCIAL / SEED_IMPORTANT / SEED_OTHER and DISAMBIGUATION_RULES (see
../src/Stage2_Processing.pyw, and ../docs/VOCABULARY_GUIDE.md for the reasons).

That makes the source the single source of truth, but it also makes vocabulary
changes hard to review in a diff, because they are buried in a 9,500-line file.
So this script extracts them into flat files next to it:

    controlled_vocabulary.csv   tier, overwrite_type, name, description
    disambiguation_rules.txt    the full rules block sent to the model
    retired_names.txt           names pruned from older workbooks

Those three files are a GENERATED MIRROR. Editing them changes nothing - edit
the constants in ../src/Stage2_Processing.pyw and re-run this script.

Run it after any vocabulary change so the diff in the pull request shows what
actually changed:

    python vocabulary\\export_vocabulary.py

It imports the app as a module. That is safe and has no side effects: Stage 2
has no module-level executable statements (its main() is guarded by
__name__ == "__main__"), which is the same trick eval_classifier.py and
Stage2_Recheck_Unknowns.pyw use to share the production classification core.
"""
import csv
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGE2 = HERE.parent / "src" / "Stage2_Processing.pyw"

BANNER = (
    "# GENERATED FILE - DO NOT EDIT.\n"
    "# Regenerate with:  python vocabulary/export_vocabulary.py\n"
    "# Source of truth:  src/Stage2_Processing.pyw\n"
)


def load_stage2():
    if not STAGE2.is_file():
        sys.exit(f"Could not find {STAGE2}")
    spec = importlib.util.spec_from_file_location("stage2", STAGE2)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stage2"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    s2 = load_stage2()
    overwrite = set(s2.OVERWRITE_TYPES)

    tiers = (
        ("Crucial", s2.SEED_CRUCIAL),
        ("Important", s2.SEED_IMPORTANT),
        ("Other", s2.SEED_OTHER),
    )

    csv_path = HERE / "controlled_vocabulary.csv"
    total = 0
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["tier", "overwrite_type", "name",
                    "description_identification_features"])
        for tier, seed in tiers:
            for name, desc in seed:
                w.writerow([tier,
                            "yes" if name in overwrite else "no",
                            name,
                            " ".join(desc.split())])
                total += 1
    print(f"{csv_path.name}: {total} document types "
          f"({', '.join(f'{t}={len(s)}' for t, s in tiers)})")

    rules_path = HERE / "disambiguation_rules.txt"
    rules_path.write_text(BANNER + "\n" + s2.DISAMBIGUATION_RULES,
                          encoding="utf-8")
    print(f"{rules_path.name}: {len(s2.DISAMBIGUATION_RULES):,} characters")

    retired_path = HERE / "retired_names.txt"
    retired_path.write_text(
        BANNER
        + "#\n# Names a previous version used (or that a model once invented)\n"
          "# and that are pruned from any workbook carrying them. Matching is\n"
          "# case-insensitive. See RETIRED_NAMES in src/Stage2_Processing.pyw.\n\n"
        + "\n".join(sorted(s2.RETIRED_NAMES)) + "\n",
        encoding="utf-8")
    print(f"{retired_path.name}: {len(s2.RETIRED_NAMES)} retired names")

    ow_only = sorted(overwrite)
    print(f"overwrite types: {len(ow_only)} "
          f"(Stage 3 mirrors this set - keep them in sync)")


if __name__ == "__main__":
    main()
