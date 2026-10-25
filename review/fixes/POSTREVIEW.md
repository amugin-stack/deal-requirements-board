# Post-fix review

After all fix sets were joined and the adversarial review's three findings (ADV-1..3) were fixed, the
fixed code was reviewed again by two read-only reviewers (eval methodology, compliance/security) for
NEW or REMAINING medium/high issues. Every candidate went to its own independent verifier, who
reproduced it and set the final severity. Confirmed findings are fixed with regression tests (blind
test-writer, then join) and recorded in `dispositions.json` under review `postreview-<n>`.

## Round 1

new_medium_or_high: 6

Reviewed at `7f4f951`. Reviewers: eval-method (6 candidates) and compliance (1 candidate).

| id | reviewer | claimed | verified | finding | fix |
|---|---|---|---|---|---|
| EVAL-1 | eval | medium | medium | EM-6 excluded relabeled posts from rescores, so a run made against a doctored label reproduced its stored score and passed CQ-7's rescore check | `e7b7deb`, `5e08d30` |
| EVAL-2 | eval | medium | medium | the rescore check ignored coverage: a run with its misses deleted reproduced its own rates | `5e08d30` |
| EVAL-3 | eval | medium/high | medium | the final gate had neither the freshness nor the rescore check | `5e08d30` |
| EVAL-4 | eval | medium | medium | a run made at a commit later `reset` away still counted while the history check no longer saw the commit | `5e08d30` |
| EVAL-5 | eval | medium | medium | a dirty final look was never counted, inviting a second clean look | `5e08d30` |
| EVAL-6 | eval | medium | medium (rare) | the strict output check followed the prompt version, so a strict run's errors vanished after the next prompt change | `e7b7deb` |
| COMP-1 | compliance | medium (borderline) | low | a lone UTF-16 surrogate from the extractor validated, then broke response serialization outside the route's error handling | `52e0e55` |

Regression tests: `tests/test_fix_postreview1.py` (25, written blind from the contracts, 19 red on the
pre-fix commit, all green on the fix) and `tests/test_fix_adversarial.py::test_comp1_*`. After the
fixes: 1318 passed, 1 skipped; ruff, format, mypy clean; all 21 saved runs still rescore identically to
`main` apart from the intended EM-61 keys.

Rejected or low-only observations from round 1 (not fixed, recorded for the owner):
- Follow-up echoes the poster's own spelling of a known city (e.g. full-width letters); only known-city
  keys can be echoed, so no injected words.
- `.gitignore` doesn't cover `.envrc` / `*.env`; eval CLI doesn't pin the SDK loggers like `api.py`
  does (`ANTHROPIC_LOG=debug` would print post text in eval only); `</post>` in a post isn't escaped
  in the prompt wrapper (prompt text is frozen by PROMPT_VERSION).
- Loop guard: `git update-index --skip-worktree` hides edits from git-based checks; `tests_check`
  doesn't bound xfails; `split` isn't part of the post hashes; the shipped loop can no longer pass its
  own (stricter) guard, which is expected: legacy runs carry no `request_sha` or cache flags.
- README Gate row doesn't mention the 90-day snapshot rule or the New York date (docs builder).

## Round 2

new_medium_or_high: 2

Reviewed at `830605e` (fresh reviewers; a first attempt was cut off when the session process exited and
was relaunched). Compliance: no medium/high. Eval-method: 2 candidates, both confirmed medium by their
verifiers.

| id | reviewer | claimed | verified | finding | fix |
|---|---|---|---|---|---|
| R2-EVAL-1 | eval | medium | medium | `final_looks` counted only runs whose commit descends from loop-start, so a final look made on a branch from `loop-start^` (same tree) was invisible to 'one look only' and the reachability check | `2dcf556` |
| R2-EVAL-2 | eval | medium | medium | the guard resolved the `loop-start`/`baseline-v1` tag names at run time, so `git tag -f loop-start HEAD` hid committed protected edits and earlier final looks; the verifier added that `git replace` could fake a pinned commit | `2dcf556`, `161490e` |

Regression tests: `tests/test_fix_postreview2.py` (11, written blind, 9 red on `830605e`, all green on
the fix). After the fixes: 1329 passed, 1 skipped; ruff, format, mypy clean (the env-access test caught
a first version that read `os.environ`; fixed with git's `--no-replace-objects` flag).

Low observations from round 2 (not fixed, recorded for the owner):
- `build_brokers` stamps TREC rows with a hand-set `AS_OF` date, not the raw file's download date; bumping
  it without a new download would make stale statuses look fresh (operator error).
- FastAPI's built-in telemetry and httpx2's proxy variables read the environment outside `config.py`
  (library behaviour; nothing exported without an OpenTelemetry provider).
- A strict-recorded run is re-checked against today's `ExtractedFields` keys, so a future schema change
  would turn its records into errors on rescore (schema.py is protected inside the loop).
- A final look written outside `results/` (`--results-dir`) or deleted can't be counted by any
  file-based guard; a protected look ledger would be needed.

## Round 3

new_medium_or_high: 2

Reviewed at `6a5aafd`, the last round the brief allows. Compliance: no medium/high (nothing since round 2
touched the product path). Eval-method: 3 candidates, one verifier each.

| id | reviewer | claimed | verified | finding | fix |
|---|---|---|---|---|---|
| R3-EVAL-1 | eval | high/medium | **low** | the guard imports agent-editable `claude.py` into its own process, so deliberately malicious extractor code can rebind the guard's functions; same class no in-repo guard can stop (that code also runs inside eval), and already on `main` | wontfix (hardening noted) |
| R3-EVAL-2 | eval | medium | medium | `commits_since` used plain `diff-tree`, blind to an "evil merge" carrying its own protected edit and to merged-in root commits | `e34b609` |
| R3-EVAL-3 | eval | medium | medium (regression from round 2) | the loop-start pin lives in the protected file, which can't name the commit containing it, so a new loop could never be set up without failing the guard | `31233e0` (`--loop-start <sha>` from the orchestrator) |

Regression tests: `tests/test_fix_postreview3.py` (12, written blind, 5 red on `6a5aafd`, all green on
the fix; the brief's literal unrelated-root scenario was already caught, so the writer moved the
removal into a merge commit to expose the gap). After the fixes: 1341 passed, 1 skipped; ruff, format,
mypy clean.

Status after round 3: not converged (2 medium fixed). The owner asked for a fourth round over these fixes.

Low observations from round 3 (recorded for the owner):
- `.git/info/exclude` / `core.excludesFile` and `.gitignore`d paths (e.g. a `.venv` `.pth`) hide files from
  the allow-list (same class as skip-worktree).
- Rescore loads labels from each run's own dataset `path`; a fabricated run pointing outside `data/`
  reproduces itself (fabrication class).
- `loop/DRAFT.md` is allow-listed per commit, so FIXED sections can be edited and reverted between
  checks; only the working tree is compared.
- An allowed new `tests/test_*.py` runs arbitrary code during the guard's pytest step.
- Two existing tests pin `PROMPT_VERSION == "ac1a145b"` (for the final eval), so resuming the tuning loop
  would turn pytest red on any honest prompt change.

## Round 4

new_medium_or_high: 1

Run at the owner's request over the round-3 guard fixes, reviewed at `0861b3c`. Compliance: no
medium/high (nothing touched the product path; `--loop-start` can't inject options or shell). Eval-method:
2 candidates, one verifier each.

| id | reviewer | claimed | verified | finding | fix |
|---|---|---|---|---|---|
| R4-EVAL-1 | eval | medium | medium | `.git/info/grafts` rewrites parentage and `--no-replace-objects` doesn't disable it, so a reverted protected commit dropped out of loop-start..HEAD while its run still counted (the rescore check still catches doctored scoring or labels; doctored eval code writing forged raws slipped through) | `44f850b` |
| R4-EVAL-2 | eval | medium | low | the round-3 whole-tree view false-failed an honest merge from main into a loop branch (merging isn't in the protocol) | `58bcee4` |

Regression tests: `tests/test_fix_postreview4.py` (6, written blind, 5 red on `0861b3c`, all green on the
fix). After the fixes: 1347 passed, 1 skipped; ruff, format, mypy clean.

**Status after round 4: not converged (1 medium found and fixed); no round 5 has reviewed `44f850b`.**

Low observations from round 4 (recorded for the owner):
- `--loop-start` protects the reference point only when the orchestrator supplies it; an agent running the
  guard itself could pass any commit (documented in the docstring).
- HANDOFF.md and the agent spec still show `check_success.py --stage ...` without `--loop-start`; a new loop
  run that way uses the old pin (fails loudly, not a bypass). Docs builder.
- `commits_since` makes two git calls per commit (about 12 s for 400 commits).
- `main(--loop-start)` sets a module global, which persists across in-process calls.
