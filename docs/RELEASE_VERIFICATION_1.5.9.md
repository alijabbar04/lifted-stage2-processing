# v1.5.9 verification - strict finishing evidence

## Baseline and scope

Built on v1.5.8 / origin/main 70615319f2ab5ef509ad3b056aeee3c77e564de0,
not the v1.5.6 reference used by the supplied improvement review. Release build:
2026.09.15-evidence1. Python 3.13.14, PyInstaller 6.22.1 and Inno Setup 6.7.3.

F1 validates the three live response schemas before coercion and rejects
malformed stored signature/share-code evidence at the ranking boundary. CoS
replay already required a string and retains that check. F2 corrects vocabulary
guide prose; it changes neither the vocabulary nor OVERWRITE_TYPES.

Only those three helpers, the ranking consumers and version metadata differ
semantically in the main application module. Request setup/prompts, generic
JSON parsing, models/prices, date parsing, ranking policy and routing remain as
in v1.5.8. Current source was checked against the baseline through AST comparison.

## Evidence and tests

- Before the parser patch: 25 malformed-schema subtests failed, with 8 valid
  control tests passing. Stored malformed signature/share-code cases reproduced
  erroneous ranking/renaming before the consumer guards.
- Independent final focused verification: **73 passed, 38 subtests passed**.
- Full pytest regression: **614 passed, 200 subtests passed, 2 skipped**.
  One skip was Windows symlink availability; the other was an intermittent
  shared-process Tcl/Tk library-load problem. Neither skip was counted as passed.
- Fresh-process GUI verification: **56 of 56 passed**, zero skips/failures,
  including the actual-app smoke case skipped in the combined suite.
- Final packaging/version assertions: **5 passed**. These now also check the
  README, public installer text and guide version against the application.
- Windows installer/setup function suite: **44 passed**, including same-release
  review source discovery. The legacy isolated checksum harness was updated to
  the installer's current Assert-Checksum interface and passed all six scenarios.
  This repair affects the test harness, not installer runtime behaviour.
- Guide regenerated and all 15 rendered pages inspected; contents/page count
  retained, current release/default-model wording and recovery explanation checked.
- Frozen EXE launched successfully, reporting v1.5.9 / 2026.09.15-evidence1.
  Read-only archive inspection verified the embedded helper/ranking bytecode and
  guide against the tested source and current PDF.
- Credential-free public setup built and installed. Installed EXE equals the
  build byte-for-byte; Desktop and Start Menu shortcuts target that EXE with no
  extra arguments. All three installed guide copies match the release PDF.
  Existing configuration hash is unchanged. Previous executable was backed up.

Tests used synthetic documents, temporary state and mocked AI providers. No
worker processing, paid document evaluation, ledger edits or historical case
corrections were performed. The installer tests may download public release
source; this does not submit worker documents or start an AI review.

## Final artifact hashes (SHA-256)

| Artifact | SHA-256 |
| --- | --- |
| Stage2_Processing.exe | 93250d8d92a5fc9c1eabef4bf8d249bdc0eb664b3db022677e2ef971fb1ff118 |
| Stage2_Processing_Setup.exe | 783e4af33ce661d929304d3805ee68366cf055aaf7d11936025271c24c45dc25 |
| Stage2_Guide_AI_Processing.pdf | 1d119d867fd346bd850fbbb6e91cfba8183cdb7f45efe98f3298d19b61358009 |
| inference.onnx | af9a0a4f317ff0709ce752067807f819cb15d883f8ecad89f28df1c6ee2d9c92 |

## Deliberate limits

This is response-structure validation, not a proof that the model interpreted
a signature/date correctly. Malformed answers may now produce more honest
deferrals. Genuine false answers and explicitly empty date strings remain valid.
Previously coerced historical false/empty values cannot be distinguished from
valid evidence and are not blanket-invalidated or automatically repurchased.
Recover final checks retains its separate costed confirmation, attempt history,
exact input/family binding and unknown-provider-outcome safeguards.

No historical review ledgers, EntryIDs or case documents were supplied with the
proposals. There is no measured before/after naming accuracy claim. Rename
transaction redesign, Bulk occupancy changes, stricter date syntax and broader
signature page sampling remain outside this bounded release.
