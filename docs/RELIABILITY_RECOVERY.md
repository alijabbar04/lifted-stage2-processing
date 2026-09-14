# Reliable batch recovery - v1.5.6

This release addresses reproducible interrupted-run faults without changing models,
rendering resolution, classification prompts, ranking policy or completion evidence.

## Behaviour and acceptance criteria

| Area | Required behaviour |
| --- | --- |
| Deduplication | Write a hash-bound transition before deleting exact copies; checkpoint survivors before paid finishing; preserve classification provenance; resume only the recorded transition. |
| GET result retrieval | Bounded retry on transient connection/read failures; validate complete identities before caching; atomic full-batch cache; no partial merge or duplicate result acceptance. |
| Paid submission | Exactly one Windows Send; retain uncertain outcome and reconcile; distinct send/status/response numeric diagnostics, no blind alternate transport. |
| Final-check recovery | Inspect local failures before retrieving results; exact failed-operation/input binding and explicit costed confirmation; no changed or ambiguous retry. |
| Unreadable sources | Same hash-bound exclusion in initial and resumed preparation; original inventory retained; never exclude an accepted/uncertain request or silently complete the worker. |
| Empty input | No documents supplied is distinct from processed. A previously nonempty or submitted inventory cannot become an approved empty case. |
| UI | Result batches verified is distinct from workers complete; failed/source outcomes identify actions instead of exposing only opaque internal status names. |

## Boundaries

- The earlier generic COM HRESULT did not establish a payload-size or network root
  cause. This release improves observability and recovery; it does not claim to
  cure every external network/TLS fault.
- Existing unexplained legacy checkpoint mismatches are not automatically blessed.
  Recovery must retain unique-content protection and original paid request lineage.
- A result cache is derived local data. It cannot override saved request identities
  or cause an incomplete provider response to be accepted as complete.
- Damaged originals still require complete originals. Empty folders need source
  documents or an explicit scope decision. Neither is an accuracy pass.
- Local processing requires an awake machine. No Windows power policy is changed.
- Tests use synthetic inputs/mocked providers. No live worker cohort, paid document
  request, post-run audit or AI review is part of release verification.

## Technical references

Windows reports WinHTTP error values through the low 16 bits of HRESULT; COM can
wrap the useful cause. See [Microsoft WinHTTP error messages](https://learn.microsoft.com/en-us/windows/win32/winhttp/error-messages)
and [COM exception information](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/automat/raising-exceptions-during-invoke).
Batch retrieval and request identity behaviour follows the
[Claude Message Batches documentation](https://platform.claude.com/docs/en/build-with-claude/batch-processing).

## Release verification

Build the updated guide, run the regression suite and Windows installer-function
tests, build with `build/build.ps1`, then build the public installer with
`build/build_public_installer.ps1`. Never publish the private key-embedding installer.
Verify the installed EXE and shortcut target against the release SHA256; check the
real window title, Guide and recovery controls. Publish matching source, tag,
portable app, public installer, PDF guide, install scripts and checksum manifest.

### Verified for v1.5.6 (14 September 2026)

- Final complete pytest run: **601 passed, 167 subtests passed, 1 skipped**
  (Windows symlink capability). A separate minimum-window recovery-label and
  retry-progress integration test also passed after addition to the test suite.
- Windows PowerShell 5.1 installer-function suite: **27 passed**.
- Independent reviews accepted the duplicate-removal journal, source exceptions,
  paid-retry gate and public packaging. Crash/partial-deletion, changed-source,
  ambiguous-submission, corrupt-cache and legacy-cache cases use synthetic data.
- All production source hashes remained unchanged between the final regression
  run and executable build. PyInstaller 6.22.1 collected both new recovery/cache
  modules; the real frozen window opened as **v1.5.6 / 2026.09.14-recovery1**, including
  the unusual temporary-directory startup smoke test.
- Public installer built successfully and installed with exit code 0. The desktop
  shortcut launched the updated installed executable. Its SHA256 matches the
  portable release below; the previous executable was backed up locally.
- The in-app Guide displayed v1.5.6. All 15 PDF pages were rendered and checked;
  all three installed guide copies matched. Existing configuration was unchanged.
- No live document processing, paid API checks, audit or AI review was launched.

| Public artifact | SHA256 |
| --- | --- |
| Stage2_Processing.exe | `dcbbdd91b47a32ac20cb5b54a32dea79e3773d902e9a9e851530918f673e9e41` |
| Stage2_Processing_Setup.exe | `08c365b4970d560a73eb17f9413a8bb7e2fdea9d5fad05147c0fba60d17a33d5` |
| Stage2_Guide_AI_Processing.pdf | `30c6043d897bb37ed3758a3b81399d7bf28df9ed1fd7adc74c3f612d9fc879e8` |

The full suite also exercises real Tk controls. Earlier runs encountered an
intermittent Tk interpreter/library-load skip; the final full run did not skip
those UI checks, and the separately launched frozen desktop app was verified.
