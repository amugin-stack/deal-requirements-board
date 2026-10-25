# Eval method + code quality review: findings

Branch `review-eval-quality`. 27 confirmed, 4 rejected (eval_method 13/2, code_quality 14/2 confirmed/rejected). Every confirmed finding was reproduced by a separate adversarial verifier with its own script; the script text is embedded in `findings.json` (`reproduction`). Nothing outside `review/eval-quality/` was changed; no API calls; no extractor ran on `holdout_v3.jsonl`.

**Loop status: not converged.** Four rounds (the cap) confirmed 13, 9, 2 and 3 new findings. In round 4 the eval-method reviewer found nothing new; the 3 confirmations were code-quality test gaps found by mutation testing. `check_success.py` check (c) therefore fails by design; see `LOG.md`.

## Confirmed findings (by severity)

| ID | Sev | Dimension | Title | Location |
|---|---|---|---|---|
| CQ-2 | medium | code_quality | git_info 'dirty' counts eval's own results/ output, so --repeats 2..N (and any run after an uncommitted result) are dirty and get dropped | `scripts/eval.py:394` |
| CQ-3 | medium | code_quality | Run identity (PROMPT_VERSION) ignores model and request parameters; check_success pools and matches runs on it alone | `extractors/claude.py:76` |
| CQ-31 | medium | code_quality | The loop guard only diffs exact protected paths, so new files can change scoring or skip all tests and the gate still passes | `loop/check_success.py:26` |
| CQ-4 | medium | code_quality | test_check_success leaves the gate's key contracts unpinned: 5 semantic mutants survive the whole suite | `tests/test_check_success.py:45` |
| CQ-41 | medium | code_quality | No test pins scoring's deal-value tolerance or city normalisation rules: six contract-breaking mutants survive | `scoring.py:88` |
| EM-1 | medium | eval_method | Cache replays across eval invocations, so the documented '3 separate dev runs' protocol yields one sample repeated 3 times | `scripts/eval.py:126` |
| EM-21 | medium | eval_method | Dev gate scores only the newest 3 runs of a prompt, so rerunning until 3 good runs come up passes a prompt whose full record fails | `loop/check_success.py:65` |
| EM-5 | medium | eval_method | loop_runs counts --rescore outputs as fresh runs (dev gate fillable from one sample; a final rescore permanently fails 'one look only') | `loop/check_success.py:133` |
| EM-7 | medium | eval_method | Dev and holdout_v3 use opposite city labels for 'near/outside X'; the v3 agreement doc doesn't flag the conflict | `data/posts.jsonl:8` |
| CQ-21 | low | code_quality | A truncated tool call (stop_reason max_tokens) is accepted and cached, and its missing keys are scored as correct_missing | `extractors/claude.py:121` |
| CQ-22 | low | code_quality | test_data validates labels through normalising validators, so a blank or non-string label passes and is scored as 'missed' | `tests/test_data.py:33` |
| CQ-23 | low | code_quality | Back-compat test pins the frozen regex results to current labels, so any label fix breaks it | `tests/test_scoring_stats.py:342` |
| CQ-24 | low | code_quality | load_run promises exit 2 with no traceback for any non-run file, but non-JSON input raises a raw JSONDecodeError | `scripts/eval.py:466` |
| CQ-42 | low | code_quality | Dev-vs-full --compare test asserts only header text, so it passes on an empty comparison and misses full-run-first order | `tests/test_eval_compare.py:158` |
| CQ-43 | low | code_quality | summarize_repeats, the --repeats 'noise floor' summary, is effectively untested | `scripts/eval.py:351` |
| CQ-5 | low | code_quality | test_eval re-implements rescore inline instead of calling it, so rescore_records' refusal path is untested | `tests/test_eval.py:58` |
| CQ-7 | low | code_quality | Dev gate trusts stored scores; an edit-label/run/revert sequence passes the guard while --rescore/--compare would fail it | `loop/check_success.py:65` |
| CQ-8 | low | code_quality | score() hard-codes the good-outcome tuple for its failures list instead of using GOOD_OUTCOMES | `scoring.py:203` |
| EM-2 | low | eval_method | README overstates the loop's keep rule: iteration 2 was kept with state and city each down 3.5 pts | `README.md:72` |
| EM-22 | low | eval_method | README timeline error analysis says the model fails to subtract elapsed time, but the 45-day failures come from over-shortening | `README.md:90` |
| EM-23 | low | eval_method | baseline-v1_comparison.json uses the old all-posts metric and disagrees with every reported baseline number, with nothing saying so | `results/baseline-v1_comparison.json:5` |
| EM-24 | low | eval_method | Hallucination rates count errored posts as clean: an all-errored run reports 0% hallucination | `scoring.py:224` |
| EM-25 | low | eval_method | README test count, 'next steps' and holdout status are out of date versus the code and data | `README.md:113` |
| EM-3 | low | eval_method | Iteration table reports 'kept' prompt changes without noise or significance, and omits the drop on the 11 untuned posts | `README.md:80` |
| EM-31 | low | eval_method | Text overlap contradicts the README's claim that the holdout was written blind to the other posts, and no brief was recorded | `README.md:49` |
| EM-6 | low | eval_method | Rescore/compare join old raw outputs to current labels by post id only, with no text or label hash | `scripts/eval.py:274` |
| EM-8 | low | eval_method | s14 state label NJ can only be reached by inferring from city names, which the schema forbids | `data/posts.jsonl:14` |

## What I'd fix first

1. **Make the loop's "3 runs" mean 3 independent samples (EM-1, EM-5, EM-21, CQ-2).** Four bugs hit the same gate input: separate invocations replay the cache, `--rescore` outputs count as runs, extra reruns silently push older runs out of the window, and `--repeats` marks its own later runs dirty. One small change set covers the class: `loop_runs` keeps only results with fresh API calls (`usage.cache_hits == 0`, no `rescored_from`), the gate requires exactly 3 such runs per prompt version (or scores all of them), and `git_info()` ignores `results/` and is captured once per invocation. Do this before the loop ever runs again.
2. **Close the guard and pin the gate and the scorer with tests that can fail (CQ-31, CQ-4, CQ-41, CQ-3).** Turn the PROTECTED deny-list into an allow-list (a new `scoring/` package or `tests/conftest.py` currently bypasses it). The anti-peeking rule (`finals[-1]`), the ±1% deal-value rule and city normalisation all have surviving mutants; add the distinguishing tests. Record model + request fingerprint in results and match on them.
3. **Resolve the city/state label conventions before the one look at holdout_v3 (EM-7, EM-8).** v3 is the reserved final look; a "near X" convention that contradicts dev will cost points that are labeling, not model, errors. Decide the rule, write it into `schema.py`, fix the minority labels.
4. **README honesty fixes (EM-2, EM-3).** State the real keep tolerance, add run-to-run spread and paired p-values to the iteration table, and report the 11 untuned posts. Cheap, and it pre-empts the obvious reviewer question.
5. **Make the label tests able to fail on bad labels (CQ-22, CQ-23).** Round-trip every label in `test_data.py`, and stop the back-compat test pinning current labels, or the EM-7/EM-8 label fixes will look like regressions.
6. **Latent provenance, error handling and duplication (EM-6, EM-24, CQ-7, CQ-8, CQ-5, CQ-21, CQ-24).** Store text/label hashes in results, exclude errored posts from hallucination denominators, gate on rescored rather than stored scores, use one good-outcome predicate, reject any `stop_reason` other than `tool_use`, and make `load_run` exit 2 on non-JSON.
7. **Smaller test gaps (CQ-42, CQ-43).** Assert on `--compare` content in both argument orders; unit-test `summarize_repeats`.
8. **Docs (EM-22, EM-23, EM-25, EM-31).** Reword the holdout-independence claim to what is recorded (h08 repeats s04's sentence), fix the timeline diagnosis before acting on DRAFT backlog item 1, mark `baseline-v1_comparison.json` as old-metric, and refresh the stale README claims.


## Details

### CQ-2 (medium, code_quality): git_info 'dirty' counts eval's own results/ output, so --repeats 2..N (and any run after an uncommitted result) are dirty and get dropped

`scripts/eval.py:394`

```
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}
```

**Claim.** git_info() runs `git status --porcelain` over the whole repo; results/ is not gitignored, and run_once() calls git_info() per repeat after save() wrote the previous repeat's files. So `eval.py --repeats N` with the default results dir marks repeats 2..N dirty even with unchanged code, and loop_runs drops them; the same hit iteration 0 (T232525Z, T232557Z). The flag conflates eval output with code changes, and no test pins it (a `"dirty": False` mutant passes all 837 tests).

**Verifier.** The two iteration-0 dirty files came from separate invocations with an uncommitted earlier result, not from --repeats: same root cause, different trigger. DRAFT.md:60 documents 'commit between runs' as a workaround, which still leaves --repeats unusable for the gate.

**Reproduction output.**

```
default results dir, --repeats 3: r1 dirty=False, r2 dirty=True, r3 dirty=True
--results-dir outside repo: r1/r2/r3 all False
mutant dirty:False -> 837 passed, 5 xfailed
```

**Fix.** Compute dirty excluding results/ (`git status --porcelain -- . ':(exclude)results'`) and snapshot git_info() once in main() before the repeat loop.

**Regression test.** In a tmp git repo with ev.ROOT/RESULTS_DIR patched, run main(['--extractor','regex','--split','dev','--repeats','2']) and assert both results have git.dirty False; then modify a tracked .py and assert git_info()['dirty'] is True.

### CQ-3 (medium, code_quality): Run identity (PROMPT_VERSION) ignores model and request parameters; check_success pools and matches runs on it alone

`extractors/claude.py:76`

```
PROMPT_VERSION = hashlib.sha256(
```

**Claim.** PROMPT_VERSION hashes only SYSTEM_PROMPT and TOOL, not max_tokens, tool_choice, the <post> wrapper or the model (DEAL_MODEL/DEFAULT_MODEL). eval.py fingerprints the extractor source for the cache only; results files record model and prompt_version but no request fingerprint. check_success.dev_runs_for and final_checks match on prompt_version alone and never compare model, so the dev gate can pool runs from other request settings or models, and a final on a different model 'passed dev first'.

**Verifier.** git.commit is used only for ancestry/dirty filtering, never as run identity. DRAFT.md allows edits anywhere in claude.py; 'don't switch models' is prose only. History so far is haiku only, hence medium not high.

**Reproduction output.**

```
max_tokens=64 / no <post> wrapper / tool_choice auto / DEFAULT_MODEL=sonnet -> PROMPT_VERSION=ac1a145b (all identical); SYSTEM_PROMPT edited (control) -> faebae25
dev_runs_for pooled models: ['claude-sonnet-5-5','claude-opus-5-5','claude-haiku-5-5']; dev_checks all True
final_checks 'prompt passed dev first' with other model: True
```

**Fix.** One request identity in claude.py (hash of SYSTEM_PROMPT, TOOL, max_tokens, tool_choice, message template, model — or the source sha eval.py already computes), recorded in every results file; check_success matches on (model, identity).

**Regression test.** tests/test_check_success.py: runs with equal prompt_version but different model/request_sha are excluded by dev_runs_for and fail 'prompt passed dev first'; a claude.py test asserts the identity changes when max_tokens or the template changes.

### CQ-31 (medium, code_quality): The loop guard only diffs exact protected paths, so new files can change scoring or skip all tests and the gate still passes

`loop/check_success.py:26`

```
    "loop/check_success.py", "scoring.py", "schema.py", "scripts/eval.py", "data/",
```

**Claim.** guard_checks runs `git diff --quiet loop-start -- *PROTECTED`, which only sees the listed paths. A committed scoring/__init__.py shadows scoring.py (eval.py and check_success put ROOT first on sys.path, and a package beats a same-named module there) and can rewrite the scoring rules while 'protected files unchanged' passes: on the regex dev split, with no extractor change, state .895->.947, property_type .947->1.0, transaction_type .895->1.0. Separately, a new tests/conftest.py (the agent spec allows 'add new test files under tests/') can skip every test, so pytest exits 0 and 'tests pass' passes even with a failing test present. pytest.ini is unprotected too.

**Verifier.** The scoring/ shadow breaks the agent spec ('Edit only extractors/claude.py ...') but only the guard enforces the spec, and it misses it; the conftest route fits the spec's literal wording. Lowered high->medium: both need an agent that sets out to game the gate, and the extra files would show in a human diff of the recorded commit.

**Reproduction output.**

```
no shadow: state 0.895 property_type 0.947 transaction_type 0.895
with committed scoring/__init__.py: scoring resolved to .../clone/scoring/__init__.py; protected unchanged rc==0: True; state 0.947 property_type 1.0 transaction_type 1.0
conftest branch: all four guard checks passed=True, 'tests pass' detail '20 skipped in 0.65s' (control without conftest: '1 failed, 19 passed')
```

**Fix.** Turn the guard into an allow-list over `git diff --name-status loop-start` plus untracked files: only extractors/claude.py, loop/DRAFT.md (non-FIXED), loop/LESSONS.md, loop/state.json, new results/claude_*.{json,md} and new tests/test_*.py. Reject any added conftest.py, pytest.ini, setup.cfg, pyproject.toml, sitecustomize.py, top-level module/package or directory named like a protected module. Make 'tests pass' require zero skipped/deselected and at least the loop-start pass count.

**Regression test.** tests/test_check_success.py with a tmp git repo tagged loop-start: (1) committing scoring/__init__.py fails the protected check; (2) committing a skip-all tests/conftest.py fails 'tests pass'; (3) committing only extractors/claude.py plus a new tests/test_new.py passes every guard check.

### CQ-4 (medium, code_quality): test_check_success leaves the gate's key contracts unpinned: 5 semantic mutants survive the whole suite

`tests/test_check_success.py:45`

```
def test_final_allows_one_look_at_a_dev_passed_prompt():
```

**Claim.** Fixtures use one run or identical copies ([dev_run()]*3, a single final_run), so selection and order never matter; dev 'mean accuracy' is never failed alone, and the final 'schema errors' and 'holdout non-deal hallucinations' checks are never driven to FAIL. Five mutants of loop/check_success.py survive the full suite: finals[-1]->finals[0] (the 'first look counts' anti-peeking rule), mean >= DEV_MIN_MEAN->True, runs[:REPEATS]->runs[-REPEATS:], final n_errors(...)==0 ...->True, holdout fields_filled <= FINAL_MAX_NON_DEAL_FILLED->True.

**Verifier.** Control mutant proves the copy's tests import the copy. loop_runs is newest-first, so finals[-1] is the first look. These tests are a PROTECTED path, i.e. the frozen spec of the gate, yet would not detect a weakened gate.

**Reproduction output.**

```
BASELINE: 837 passed, 5 xfailed
M1 finals[-1]->finals[0]: 837 passed
M2 mean >= DEV_MIN_MEAN->True: 837 passed
M3 runs[:REPEATS]->runs[-REPEATS:]: 837 passed
M4 final schema-errors->True: 837 passed
M5 holdout non-deal fills->True: 837 passed
control (one-look check->True): 1 failed, 836 passed
```

**Fix.** Distinguishable fixtures and exact failed-set assertions: [newer_v2, older_v1] finals, [bad]+[good]*3 vs [good]*3+[bad] dev runs, an every-field-0.9 dev set failing only 'mean accuracy', final_run(errors=1) and final_run(non_deal_filled=2). Re-cut loop-start afterwards since the file is protected.

**Regression test.** test_final_uses_oldest_run_and_gates_errors_and_hallucinations and test_dev_uses_newest_three_and_gates_mean_alone, asserting exact failed sets (see findings.md).

### CQ-41 (medium, code_quality): No test pins scoring's deal-value tolerance or city normalisation rules: six contract-breaking mutants survive

`scoring.py:88`

```
        return abs(pred - gold) <= DEAL_VALUE_TOLERANCE * gold
```

**Claim.** The ±1% deal-value rule and city normalisation (ignore case, periods, commas, extra spaces; no spelling tolerance) are covered by one over-prediction and one comma-free city case (tests/test_eval.py:43-44), and no stored result has a deal_value or city that differs from its label, so rescore/golden tests can't catch changes either. All six mutants pass the full suite: dropping abs() (every under-prediction 'correct': 5_000_000 vs 1), tolerance 0.01->0.02, <= -> <, skipping comma removal, joining city tokens with '', and deleting the missing_fields error count for errored deal posts. Scoring is correct today; the headline metric's rules are simply unpinned.

**Verifier.** Confirmed every mutant survives and no stored record can distinguish them. Medium: a test gap on the headline metric, not a current bug.

**Reproduction output.**

```
M1_no_abs: 837 passed, 5 xfailed | M2_tol_002: 837 passed | M3_strict_lt: 837 passed | M4_no_comma: 837 passed | M5_join_empty: 837 passed | M6_no_missing_err: 837 passed
sanity original field_outcome(deal_value_usd, 5M, 1): wrong
scan of all 43 results files: 388 filled deal values all exactly equal to label, 342 cities all raw-equal (no near-misses)
```

**Fix.** Leave scoring.py; add parametrised unit tests for values_match/field_outcome/normalize and a score() case with an errored deal post.

**Regression test.** field_outcome('deal_value_usd', 100_000, p): 101_000 and 99_000 correct, 101_001 and 98_999 and 1 wrong; field_outcome('city','St. Louis','st  louis,') correct, 'Stlouis' and 'St Lewis' wrong; score() with one errored deal post -> missing_fields['error'] == 1.

### EM-1 (medium, eval_method): Cache replays across eval invocations, so the documented '3 separate dev runs' protocol yields one sample repeated 3 times

`scripts/eval.py:126`

```
        Only the first lookup of a key on this instance can hit. The CLI shares one instance
```

**Claim.** RawCache's at-most-once-hit rule is per instance (_looked_up), and main() builds a fresh RawCache per CLI invocation with the cache on by default. The loop protocol (HANDOFF.md:68, .claude/agents/extractor-improver.md:17, loop/DRAFT.md:40) runs `scripts/eval.py --extractor claude --split dev` as three separate invocations, so runs 2 and 3 are full cache replays of run 1 (and run 1 too after a revert restores a previously cached claude.py). loop/check_success.py averages them as 3 independent runs and never checks call.cached or usage.cache_hits, so the 3-run mean and the per-run gates rest on n=1. Even --repeats 3 replays a prior invocation in run 1. Committed results predate the cache and are unaffected; the next loop run would not be.

**Verifier.** Reproduced through main() itself. Replay across invocations is deliberate resume behaviour (class docstring, test_cached_run_records_are_marked), but the get() docstring promises independent repeats and every loop document still prescribes three separate invocations. Lowered from high to medium: loop/state.json is 'done' and all committed loop results predate the cache, so no published number is affected.

**Reproduction output.**

```
invocation 1: api_calls=24 cached_records=0/24
invocation 2: api_calls=0 cached_records=24/24
invocation 3: api_calls=0 cached_records=24/24
identical raws across 3 invocations: 24/24
is_deal_request per run: [0.708, 0.708, 0.708]
check_success inspects 'cached'/'cache_hits': False
--repeats 3 after warm cache, (api_calls, cache_hits) per run: [(0, 24), (24, 0), (24, 0)]
control --no-cache two invocations identical raws: 3 / 24
```

**Fix.** Make cross-invocation replay opt-in (--resume, or a per-invocation run nonce in the cache identity). In loop/check_success.py reject runs with any records[*].call.cached or usage.cache_hits > 0, or with raw outputs identical to another counted run. Update HANDOFF/agent docs to one `--repeats 3 --no-cache` invocation until then.

**Regression test.** tests/test_eval_cache.py: with a nondeterministic fake client, call main() twice with the same tmp cache_dir and no flags; assert the second invocation made N_DEV calls and has 0 cached records (or that only --resume hits). tests/test_check_success.py: dev_checks fails when any run has usage.cache_hits > 0.

### EM-21 (medium, eval_method): Dev gate scores only the newest 3 runs of a prompt, so rerunning until 3 good runs come up passes a prompt whose full record fails

`loop/check_success.py:65`

```
    scores = [r["datasets"]["posts_dev"]["scores"] for r in runs[:REPEATS]]
```

**Claim.** dev_runs_for(version) returns every clean dev run of the current prompt, but dev_checks() scores only runs[:REPEATS] and drops the rest without reporting them. The FIXED spec says 'last 3 runs' but gives no reason for letting newer runs supersede older ones, and nothing limits reruns of one PROMPT_VERSION, so the gate admits optional stopping. The reverted prompt b4f9c9ab (non-deal fills [0,1,1]) passes once 2 more clean runs are added, although 2 of its 5 runs hallucinated on non-deal posts. Unused so far (max 3 clean runs per prompt in results/).

**Verifier.** Distinct from CQ-4 (tests), EM-1 (cache) and EM-5 (rescores): the documented window itself is the hole, and dropped runs leave no trace in the gate output. Medium: open but never exercised.

**Reproduction output.**

```
b4f9c9ab fills [0, 1, 1]; max clean dev runs of any prompt: 3
b4f9c9ab: own3 fails ['non-deal hallucinations']; +2 lucky reruns -> fails [] (total runs 5)
b0950301: own3 fails ['mean accuracy']; +2 lucky reruns -> fails ['weakest field']
```

**Fix.** Fail when a prompt has more than REPEATS clean runs (a rerun needs a new version/commit), or gate on all runs of the prompt; either way list the run count and any ignored files in the Check detail.

**Regression test.** tests/test_check_success.py: 5 runs newest-first where the oldest has non_deal fields_filled=1; assert dev_checks does not all pass.

### EM-5 (medium, eval_method): loop_runs counts --rescore outputs as fresh runs (dev gate fillable from one sample; a final rescore permanently fails 'one look only')

`loop/check_success.py:133`

```
        if r.get("split", "all") == split and not r["git"]["dirty"] and after_start:
```

**Claim.** scripts/eval.py --rescore saves results/claude_<new stamp>.json with the source's split and prompt_version and a fresh clean git stamp. loop_runs never checks 'rescored_from', so one real dev run plus two rescores (committed between) fills REPEATS=3 and dev_checks averages three copies of one sample. A rescore of the split='all' run adds a second final; finals[-1] stays the original, but 'one look only' then fails forever. (Also reported independently as CQ-1 by the code-quality reviewer.)

**Verifier.** Default RESULTS_DIR is the repo's results/ and the stem is claude_<stamp>, so it matches the glob. No test covers loop_runs or rescore exclusion. The final-side effect is a false FAIL rather than a second look, since finals[-1] is the oldest.

**Reproduction output.**

```
before: dev runs for ac1a145b = 1 | final runs = 1
after: dev runs = [(claude_20261025T000002Z.json, rescored), (claude_20261025T000001Z.json, rescored), (claude_20261024T234059Z.json, original)]
  dev mean accuracy passed=True 0.955; all 5 dev checks pass
final 'one look only' passed=False '2 final runs since loop-start'
```

**Fix.** In loop_runs: `if "rescored_from" in r: continue`; better, save rescores under a distinct stem (rescore_<stamp>) and keep the source commit separate from the rescore commit.

**Regression test.** tests/test_check_success.py: monkeypatch cs.ROOT to tmp_path and cs.git to succeed; write one dev run plus two rescores; assert len(cs.dev_runs_for('v1')) == 1 and dev_checks fails 'repeats'; same for split='all' and loop_runs('all').

### EM-7 (medium, eval_method): Dev and holdout_v3 use opposite city labels for 'near/outside X'; the v3 agreement doc doesn't flag the conflict

`data/posts.jsonl:8`

```
{"id": "s08", "source": "synthetic", "split": "dev",
```

**Claim.** posts.jsonl labels the reference city for 'near/outside X' (s08 'outside Savannah' -> 'Savannah', s15 'near Boise, Idaho' -> 'Boise'); holdout_v3.jsonl labels the same pattern null (v3-09 'near Amarillo, Texas', v3-16 'outside Bend, Oregon', both lead-resolved). schema.py's city description has no rule, and docs/holdout_v3_agreement.md flags the ambiguity but not the conflict (its labelers were barred from posts.jsonl). The final model output null for s08 (v3-correct, scored a dev miss and listed as README error #5) and 'Boise' for s15 (dev-correct, v3-wrong): dev tuning rewards the convention v3 penalises.

**Verifier.** holdout.jsonl has no reference-city case; other 'near the X' hits are neighbourhoods and consistent. Medium kept: 4 labels in one field across two eval sets, and v3 is the reserved final look.

**Reproduction output.**

```
L8 s08 city='Savannah' | warehouse outside Savannah
L15 s15 city='Boise' | zoned ag near Boise, Idaho
v3: L9 v3-09 city=None adj=lead-resolved | land near Amarillo, Texas
v3: L16 v3-16 city=None adj=lead-resolved | raw land outside Bend, Oregon
result s08: city missed, expected Savannah, got null
```

**Fix.** Pick one convention, write it into schema.py's city description, and relabel s08/s15 or v3-09/v3-16 (or exclude city on all four). Update README error #5 and the agreement doc.

**Regression test.** Data test across posts/holdout/holdout_v3: deal posts matching r'\b(near|outside|just (north|south|east|west) of)\s+[A-Z][a-z]+' (excluding 'near the ...') must all label city the same way, or be in a shared city-excluded set.

### CQ-21 (low, code_quality): A truncated tool call (stop_reason max_tokens) is accepted and cached, and its missing keys are scored as correct_missing

`extractors/claude.py:121`

```
        if response.stop_reason == "refusal":
```

**Claim.** extract_raw rejects only stop_reason 'refusal' and responses without a tool call. A 'max_tokens' (or any non-'tool_use') response with a partial tool_use input is returned as-is; parse_requirement fills absent keys with None, run_post records error=None and caches it, and score() credits null-labelled fields as correct_missing. This contradicts the module's 'raise rather than repair, so the eval counts it'. Unlikely in practice: max_tokens 1024 vs ~100-200 output tokens, and strict mode emits keys in schema order, so truncation would almost always cut only the unscored notes. --rescore likewise validates old raws missing keys without checking.

**Verifier.** Unguarded path confirmed; lowered medium->low on realism. 73 pre-strict raws (de789943) lack keys and were scored error=None, consistent with the old schema, not evidence of truncation.

**Reproduction output.**

```
mid-object truncation -> error: None | prediction: {is_deal_request: True, all others None} | cached files: 1
notes-only truncation -> scored fields intact
outcomes vs all-null gold: state/city/timeline correct_missing
score state: correct_missing 1, error 0, accuracy 1.0
```

**Fix.** In extract_raw raise ExtractionError when stop_reason != 'tool_use'; optionally require every ExtractedFields key in raw on the Claude path and on rescore.

**Regression test.** Fake client returning stop_reason='max_tokens' with input {'is_deal_request': True}: extract_raw raises ExtractionError and run_posts records error not None, prediction None.

### CQ-22 (low, code_quality): test_data validates labels through normalising validators, so a blank or non-string label passes and is scored as 'missed'

`tests/test_data.py:33`

```
        ExtractedFields.model_validate(p["label"])  # raises on a bad state code, enum, etc.
```

**Claim.** ExtractedFields' mode='before' validators (schema.py:88-96) turn '', whitespace and non-string state/city into None instead of rejecting them. test_every_label_matches_schema only checks that validation doesn't raise; unlike tests/test_holdout_v3.py it never checks the label round-trips. Scoring reads the raw label, so city '' gives 'missed' instead of 'correct_missing', expected_missing() omits the field, and a non-string gold state crashes normalize. Latent: all current labels round-trip. Related: parse_requirement({'state': 123}) silently yields None despite its 'Raises ExtractionError instead of repairing bad values' docstring.

**Verifier.** scoring.normalize does not rescue '' because field_outcome checks `gold is None` first. Latent guard gap, low.

**Reproduction output.**

```
current non-roundtrip labels: 0
state '' -> None; state 5 -> None; city 7 -> None; state 'ZZ' -> ExtractionError
field_outcome(city,'',None) = missed; field_outcome(state,5,'TX') raises AttributeError
parse_requirement {'state':123} -> state None (no error)
mutated copy: tests/test_data.py 7 passed
```

**Fix.** Assert a round trip in test_every_label_matches_schema, as test_holdout_v3 does; optionally let non-strings through the before-validators so pydantic rejects them.

**Regression test.** Parametrised labels with city '', city '  ', state 5, state '' each fail a shared label_roundtrips() helper; parse_requirement({'is_deal_request': True, 'state': 123}) raises ExtractionError.

### CQ-23 (low, code_quality): Back-compat test pins the frozen regex results to current labels, so any label fix breaks it

`tests/test_scoring_stats.py:342`

```
def test_score_preexisting_keys_unchanged(stem: str) -> None:
```

**Claim.** Documented as a score() output-shape check ('keeps every old key and value; keys are only added'), it re-scores the frozen results/regex_20261024T224817Z.json against current data labels and requires every old count to match. Any legitimate label correction that moves a regex count fails it (the s14 relabel from EM-8 gives fields.state.wrong 1 != 2). results/ can't be edited, so a label fix forces editing this test, which contradicts the P6 design that rescoring uses current labels.

**Verifier.** No document says the test is meant to guard stored results against label drift; docs/agent-graphs/p6-eval-stats.md says the opposite. test_rescore_every_results_file checks only key shape and is fine.

**Reproduction output.**

```
before: 837 passed, 5 xfailed
after s14 relabel: FAILED tests/test_scoring_stats.py::test_score_preexisting_keys_unchanged[posts] -- .fields.state.wrong assert 1 == 2
1 failed, 836 passed, 5 xfailed
```

**Fix.** Check key-set inclusion against the saved scores structurally; compare values against a re-score of the same records, or against a label snapshot under tests/golden/.

**Regression test.** With s14 relabelled in a tmp copy, the back-compat test passes; with a key removed from score()'s output, it fails.

### CQ-24 (low, code_quality): load_run promises exit 2 with no traceback for any non-run file, but non-JSON input raises a raw JSONDecodeError

`scripts/eval.py:466`

```
    """Load an eval-run results file, exiting with status 2 (no traceback) for anything else,
```

**Claim.** Only JSON without 'datasets' takes the exit-2 path; read_text/json.loads run outside it, so passing a run's .md twin (next to every .json) or a missing path to --rescore/--compare gives a JSONDecodeError/FileNotFoundError traceback. test_summary_file_exits_2 covers only the JSON-summary case.

**Verifier.** Error-handling contract mismatch; only a worse error message, no wrong output. Low.

**Reproduction output.**

```
load_run -> RAISED JSONDecodeError Expecting value: line 1 column 1 (char 0)
main --rescore -> RAISED JSONDecodeError
main --compare -> RAISED JSONDecodeError
repo changed: False
```

**Fix.** Wrap read+parse in try/except (OSError, json.JSONDecodeError) -> one-line stderr message and SystemExit(2).

**Regression test.** Extend test_summary_file_exits_2 with a Markdown file, an empty file and a missing path for both --rescore and --compare; assert exit 2 and no 'Traceback'.

### CQ-42 (low, code_quality): Dev-vs-full --compare test asserts only header text, so it passes on an empty comparison and misses full-run-first order

`tests/test_eval_compare.py:158`

```
    assert "McNemar" in capsys.readouterr().out
```

**Claim.** test_cli_compare_dev_run_with_full_run asserts only that 'McNemar' is in stdout; format_comparison's fixed header always contains it, even when it prints 'No dataset path in common', so the test passes when compare_results returns {}. It runs dev-first only; full runs list holdout before posts, so mutating `continue` to `break` at scripts/eval.py:304 survives the whole suite, and then `--compare <full run> <dev run>` compares nothing while the reverse order still works.

**Verifier.** No other test exercises full-first compare. Test gap only; low.

**Reproduction output.**

```
compare_results -> {}: test_cli_compare_dev_run_with_full_run 1 passed
mutant continue->break (eval.py:304 only), full suite: 837 passed, 5 xfailed
CLI mutant full-first: No dataset path in common: nothing to compare.
CLI mutant dev-first: ## `data/posts.jsonl`: 24 common posts
CLI original full-first: ## `data/posts.jsonl`: 24 common posts
```

**Fix.** Assert on comparison content ('## `data/posts.jsonl`: 24 common posts', and 'No dataset path in common' absent) in both argument orders.

**Regression test.** Parametrise test_cli_compare_dev_run_with_full_run over dev-first and full-first; unit-test compare_results with A = {holdout: unmatched, posts: matched} and assert POSTS_PATH is compared.

### CQ-43 (low, code_quality): summarize_repeats, the --repeats 'noise floor' summary, is effectively untested

`scripts/eval.py:351`

```
"min": min(accs, default=None), "max": max(accs, default=None),
```

**Claim.** No test calls summarize_repeats or format_repeat_summary; the one --repeats CLI test checks only for the substrings 'mean' and 'min–max', and its fake client gives identical accuracy every run. So min->max, using only the first run, counting None accuracy as 0%, and counting runs as len(results) all survive the full suite. The docstring calls this 'the run-to-run noise floor that a prompt change has to beat', so a regression would silently mislead keep/revert decisions.

**Verifier.** The rescore_records call-provenance part of the candidate was dropped as a variant of CQ-5. Low.

**Reproduction output.**

```
BASELINE => 837 passed, 5 xfailed
M1 min->max => 837 passed
M2 first run only => 837 passed
M3 None as 0% => 837 passed
M4 runs=len(results) => 837 passed
```

**Fix.** Unit-test summarize_repeats and format_repeat_summary with hand-built results whose per-run accuracies differ.

**Regression test.** Three runs with accuracies 0.5, 0.9, None -> mean 0.7, min 0.5, max 0.9, runs 2; a dataset present in one run -> runs 1; rendering shows '50–90%' and '(2 runs)', and 'n/a' when all runs are None.

### CQ-5 (low, code_quality): test_eval re-implements rescore inline instead of calling it, so rescore_records' refusal path is untested

`tests/test_eval.py:58`

```
    rescored = {pid: ev.to_record(r["raw"], r["error"]) for pid, r in saved["datasets"]["x"]["records"].items()}
```

**Claim.** test_invalid_output_is_an_error_and_rescore_needs_no_client rebuilds records with its own copy of rescore_records' comprehension instead of calling it, and no test or saved results file passes a refusal record (raw=None, error set) through rescore_records. Dropping the error argument there (to_record(r["raw"])) passes the whole suite, but turns every refusal into error=None, after which score() and post_correctness() crash with TypeError on --rescore/--compare.

**Verifier.** Correction: post_correctness crashes rather than silently miscounting, so the failure is loud; lowered to low.

**Reproduction output.**

```
original suite: 837 passed, 5 xfailed | mutant suite: 837 passed, 5 xfailed
ORIGINAL rescored error='refused: no tool call'; is_deal error=1
MUTANT rescored error=None; score CRASH TypeError; post_correctness CRASH TypeError
results/*.json: 0 records with raw=None
```

**Fix.** Call ev.rescore_records in test_eval.py:58 and add a refusal record to the rescore tests.

**Regression test.** test_rescore_records_keeps_refusal_error: rescore_records({'t1': {'raw': None, 'prediction': None, 'error': 'refused'}}) keeps error 'refused' and score() counts is_deal_request error == 1.

### CQ-7 (low, code_quality): Dev gate trusts stored scores; an edit-label/run/revert sequence passes the guard while --rescore/--compare would fail it

`loop/check_success.py:65`

```
    scores = [r["datasets"]["posts_dev"]["scores"] for r in runs[:REPEATS]]
```

**Claim.** dev_checks gates on each results file's stored scores block, while compare_results/rescore re-score raw outputs against current labels. guard_checks only diffs the working tree against loop-start, so committing a data/ or scoring.py edit, running and committing 3 dev evals, then reverting the edit passes the gate on scores computed against doctored labels; rescoring the same outputs fails it. Hand-edits to a scores block are also undetected (results/ is unprotected). Both policies agree on every saved run today.

**Verifier.** Narrowed: a plain mid-loop label fix is caught by the protected-files guard, the dirty filter and the ancestry filter; the surviving path is commit-run-revert, which uses the agent's own revert step. Every path needs a hard-rule violation, so low.

**Reproduction output.**

```
mean accuracy  stored=True 0.945 (bar 0.932) | rescored=False 0.915
weakest field  stored=True timeline 0.860 | rescored=False state 0.702 (bar 0.85)
[repro.py] today: stored gate == rescored gate, all True
```

**Fix.** Recompute gate inputs with scripts/eval.py rescore(r) against current labels; fail if stored and rescored gated metrics differ; make the guard reject any commit since loop-start touching PROTECTED (git log loop-start..HEAD -- PROTECTED), not just the net diff.

**Regression test.** Three fake dev runs with wrong 'state' outputs whose stored scores were computed against labels patched to match; assert the dev gate fails against real labels; assert a hand-edited perfect scores block does not pass.

### CQ-8 (low, code_quality): score() hard-codes the good-outcome tuple for its failures list instead of using GOOD_OUTCOMES

`scoring.py:203`

```
                if o not in ("correct", "correct_missing"):
```

**Claim.** accuracy (_with_accuracy) and post_correctness (_post_correct) read GOOD_OUTCOMES at call time, while score() builds the failures list from its own copy of the literal. If the policy changes, accuracy and post_correctness follow but failures does not, so a post can be counted wrong yet be missing from the report's failures. No test checks failures against accuracy numerators.

**Verifier.** Latent; outputs agree today. No stronger policy duplication found (the 7-field mean exists only in check_success).

**Reproduction output.**

```
default GOOD_OUTCOMES=('correct','correct_missing') | state accuracy 1.0 | post_correct True | failures []
patched GOOD_OUTCOMES=('correct',) | state accuracy 0.0 | post_correct False | failures []
```

**Fix.** `if o not in GOOD_OUTCOMES:` at line 203; better, have score() call _post_correct so there is one predicate.

**Regression test.** Property test: per key field, n_deal minus the accuracy numerator equals the number of failures rows for that field (errors included), also under monkeypatch.setattr(scoring, 'GOOD_OUTCOMES', ('correct',)).

### EM-2 (low, eval_method): README overstates the loop's keep rule: iteration 2 was kept with state and city each down 3.5 pts

`README.md:72`

```
kept a change only if the mean rose and no field regressed
```

**Claim.** README says the loop 'kept a change only if the mean rose and no field regressed'. The rule actually used (HANDOFF.md:31 'no field drops > 4 pts'; .claude/agents/extractor-improver.md step 6) tolerates a one-post drop. Iteration 2 (b0950301) was kept although, versus the previous best iteration 1 (a86a1ad8), its 3-run state mean fell 0.930->0.895 and city 0.895->0.860.

**Verifier.** Confirmed iteration 2 was compared against iteration 1 (agent spec step 6 compares to best in state.json; LESSONS.md reports a86a1ad8 -> b0950301). Against iteration 0 nothing regressed, but that was not the reference. Lowered to low: wording issue, each drop is 2/3 of one post, numbers unaffected.

**Reproduction output.**

```
1 {'is_deal_request': 0.958, 'state': 0.93, 'city': 0.895, ...} mean 0.9264
2 {'is_deal_request': 1.0, 'state': 0.895, 'city': 0.86, ...} mean 0.9298
1->2 {'state': -3.5, 'city': -3.5, 'property_type': 5.3, 'is_deal_request': 4.2, others 0.0}
```

**Fix.** README line 72: 'kept a change only if the dev mean rose and no field dropped by more than about one post (~4 points)'; optionally note iteration 2's state/city dip.

**Regression test.** Doc-consistency test: recompute per-field 3-run means for each kept iteration in loop/state.json from results/, assert no field fell more than 0.045 versus the previous kept iteration, and assert README states the same tolerance as HANDOFF.md.

### EM-22 (low, eval_method): README timeline error analysis says the model fails to subtract elapsed time, but the 45-day failures come from over-shortening

`README.md:90`

```
1. **Timeline arithmetic: the main weakness**
```

**Claim.** README says the model 'doesn't subtract time already elapsed in a 45-day window'. For h08 and s04 ('45-day ID clock started last week') the label is 31_to_90_days (~38 days left) and the final prompt answered within_30_days; without subtraction it would have answered 31_to_90_days, the label. All 6 cited timeline failures bucket too short. loop/DRAFT.md backlog item 1 prescribes 'subtract elapsed time', which pushes further in the direction the model already errs.

**Verifier.** No cited post supports the README's mechanism. The README's own 'fix next' (compute the bucket in code) remains sound, which limits impact: low.

**Reproduction output.**

```
h08 label=31_to_90_days pred=within_30_days; s04 label=31_to_90_days pred=within_30_days (also in iteration-4 dev runs 234059/234146/234229)
h02,h06 over_90->within_30; s17,s25 over_90->31_to_90
s04 label_note: 'started ~7 days ago, leaving ~38 days.'
```

**Fix.** Say the model over-shortens (every miss lands in a shorter bucket); replace DRAFT backlog item 1 with a worked example or code-side bucketing.

**Regression test.** Test over the final results file asserting each timeline mismatch is predicted shorter than labelled, or a unit test for a code-side bucket(45, elapsed_days=7) == '31_to_90_days'.

### EM-23 (low, eval_method): baseline-v1_comparison.json uses the old all-posts metric and disagrees with every reported baseline number, with nothing saying so

`results/baseline-v1_comparison.json:5`

```
    "original (35, author-written)": {
```

**Claim.** The committed summary scores the 6 key fields on all posts (outcome counts sum to 35/15), so non-deal posts earn correct_missing credit; current scoring.py scores key fields on deal posts only (29/12). It gives the regex baseline 0.951 (35 posts) and 0.810 (holdout) vs 0.941 and 0.764 in README, DRAFT.md, HANDOFF.md and check_success.py. It has no metric note; eval.py and the P6 doc mention it only as 'a summary, not an eval run'.

**Verifier.** Committed in 45bd556, before the scoring rewrite. Not cited as a source for any current number, so reader confusion only: low. Fix must not edit results/.

**Reproduction output.**

```
SUMMARY original (35): n_posts=35 mean7=0.951 count_sums=[35]
SUMMARY holdout (15): n_posts=15 mean7=0.810 count_sums=[15]
RUN holdout: n_deal=12 key-field count_sums=[12] mean7=0.764
RUN posts: n_deal=29 key-field count_sums=[29] mean7=0.941
```

**Fix.** Without touching results/: note in eval.py's load_run docstring, the P6 doc and the README that this file predates deal-only scoring and is superseded by regex_20261024T224817Z.json; have eval.py stamp a metric version in new results.

**Regression test.** Test that any results JSON without 'datasets' whose key-field counts sum to n_posts is named as legacy in the docs.

### EM-24 (low, eval_method): Hallucination rates count errored posts as clean: an all-errored run reports 0% hallucination

`scoring.py:224`

```
        "fields": {f: _with_hallucination_rate(_with_accuracy(c, n_deal), n_deal)
```

**Claim.** hallucination_rate/ci95 divide by n_deal and posts_with_any_rate/ci95 by n_non_deal, both including errored posts, which can never hallucinate. Errors push both rates and their Wilson intervals toward 0: an all-errored holdout run renders 'halluc. rate 0% [0-24]' and '0 of 3 non-deal posts, 0% [0-56]' in eval.py's markdown. In the real first-prompt run, property_type shows 1/12 instead of 1/10.

**Verifier.** Literally consistent with 'errors stay in the denominator', but for a bad-outcome rate that flatters the run, and neither docstring nor P6 doc discusses errored posts. The error count is printed in the same row and the headline run has 0 errors: low.

**Reproduction output.**

```
state: hallucinated 0, error 12, hallucination_rate 0.0, ci95 [0.0, 0.2425]
non_deal: posts_with_any_rate 0.0, ci95 [0.0, 0.5615]
MD: | state | 0% [0–24] | 0% [0–24] | 0 | 0 | 0 | 0 | 0 | 12 |
real run holdout: errors 2; property_type 1/12=0.083 [0.015,0.354]
```

**Fix.** Use non-errored posts as the denominator (None when all errored), or return None when any post errored; document the denominator in SCORING_NOTE.

**Regression test.** All-errored records -> every hallucination_rate and posts_with_any_rate is None; 1 errored of 3 deal posts with 1 hallucination among the other 2 -> rate 1/2.

### EM-25 (low, eval_method): README test count, 'next steps' and holdout status are out of date versus the code and data

`README.md:113`

```
.venv/bin/pytest -q                                    # 500 tests, offline, no API key needed
```

**Claim.** README claims 500 tests; the suite collects 842 (837 pass + 5 xfail). Lines 142-143 list Wilson intervals, paired comparison, a resumable cache, token/cost tracking and --repeats as next steps; all exist. Line 103 says a different-model holdout is 'planned' without mentioning that data/holdout_v3.jsonl exists and is unscored; v3 was labelled by Sonnet and Opus, so it is still same-family. A reader can't tell which measurement safeguards exist.

**Verifier.** Stale statements understate rather than overstate the safeguards; documentation only: low.

**Reproduction output.**

```
837 passed, 5 xfailed | README:113 '# 500 tests' | README:142-143 next steps already implemented | scoring.py:35 wilson_interval, :58 exact_mcnemar_p | eval.py:506 --repeats | agreement doc: Labeler A (Sonnet), Labeler B (Opus) | holdout_v3 refs in README: 0
```

**Fix.** Drop or update the test count; move implemented features out of Limitations; describe holdout_v3 (exists, same-family, unscored) and keep a non-Claude holdout as the real plan.

**Regression test.** Doc test comparing any README test count with pytest collection, and asserting every data/*.jsonl is listed in the README data table.

### EM-3 (low, eval_method): Iteration table reports 'kept' prompt changes without noise or significance, and omits the drop on the 11 untuned posts

`README.md:80`

```
| 4 | Same rule, scoped to deal requests only | 0.945 | kept; dev gate passed |
```

**Claim.** Dev-mean gains for kept iterations 2 (+0.003) and 4 (+0.015) are no bigger than the same prompt's clean run-to-run spread (0.75-1.5 pts) and not significant under the repo's own paired exact McNemar (iter1->2 pooled p=0.79; iter2->4 per-run p=0.25/0.5/1.0, pooled p=0.07). The README says neither, and omits that on the 11 untuned posts.jsonl holdout-split posts the final prompt scores slightly below the first (0.957 vs 0.943, p=1.0). The README does credit strict mode for the main gain, and iteration 2's lender rule transfers to h12, so this is missing uncertainty/transfer reporting, not a false headline.

**Verifier.** Narrowed: the reviewer's '5pt spread' used two dirty runs that were excluded on purpose; clean spread is 0-1.5 pts. 'Do not transfer' is false for iteration 2 (h12). What survives: no noise/significance reporting for keep decisions and an unreported (non-significant) dip on the untuned author posts. Lowered to low.

**Reproduction output.**

```
iter0 0.8828/0.8813/0.8904; iter1 0.9264x3; iter2 0.9323/0.9248/0.9323; iter4 0.9549/0.9398/0.9398
iter1 vs iter2 per-run p [1.0,1.0,1.0], pooled (6,8) p=0.791
iter2 vs iter4 per-run p [0.25,0.5,1.0], pooled (1,7) p=0.07
posts-holdout(11) first 0.9571 final 0.9429, p=1.0
holdout.jsonl first 0.8048 final 0.9524; final-only cells all h01/h02/h12, p=0.00098
```

**Fix.** Add per-run min-max and a paired McNemar p vs the previous kept prompt to the iteration table; say iterations 2 and 4 are within run-to-run noise on dev; report first vs final on the 11 untuned posts; attribute the holdout gain to strict mode (h01/h02) and the lender rule (h12).

**Regression test.** Test that recomputes each kept iteration's paired_comparison from loop/state.json + results/ and asserts the README iteration table's p/spread column matches within rounding.

### EM-31 (low, eval_method): Text overlap contradicts the README's claim that the holdout was written blind to the other posts, and no brief was recorded

`README.md:49`

```
seeing only `schema.py`, never the extractors or the other posts.
```

**Claim.** README lines 49 and 103 say data/holdout.jsonl was written by a separate agent that saw only schema.py, never the other posts. No brief is recorded (commit 45bd556 message, HANDOFF.md, DRAFT.md), posts.jsonl was in the tree at that commit, and the text contradicts the claim: h08 contains dev s04's full sentence '45-day ID clock started last week.' verbatim; h13 opens 'Just closed a $2.1M' like s06 (posts-holdout split); h14 repeats dev s13's 'BOV came in at $X ... wants $Y'. None of these phrases are in schema.py. The copied posts did not drive the 0.805->0.952 gain (h08 fails 3 fields in both runs; h13/h14 never fail; the gain is h12 and h01), but README line 90 cites h08 and s04 as independent timeline failures.

**Verifier.** Not proven leakage ('Just closed a' and 'BOV came in at' are common shorthand), but a verbatim 6-word sentence is strong evidence and the blindness claim is unrecorded. Lowered medium->low because the overlapping posts contribute nothing to the measured gain. Corrected the reviewer's 's06 is dev' to posts-holdout split.

**Reproduction output.**

```
h05 ~ s05 [4-gram] 'anyone know a good' in_schema=False
h08 ~ s04 [6-gram] '45-day id clock started last week' in_schema=False
h13 ~ s06 [4-gram] 'just closed a $2.1m' in_schema=False
h14 ~ s13 [4-gram] 'bov came in at' in_schema=False
per-post failures first->final: h01 1/0 | h02 1/1 | h06 0/1 | h08 3/3 | h12 6/0 | total 11 -> 5
```

**Fix.** Reword README lines 49/103 to what is recorded and list the overlapping pairs, or replace h08/h13/h14; note in the error analysis that h08 and s04 share wording; point to holdout_v3 (recorded blind brief) as the stronger independent set.

**Regression test.** tests/test_data.py::test_holdout_has_no_shared_ngrams_with_posts: no holdout text shares a normalised word 6-gram with any posts.jsonl text, outside a documented allowlist.

### EM-6 (low, eval_method): Rescore/compare join old raw outputs to current labels by post id only, with no text or label hash

`scripts/eval.py:274`

```
        posts = [p for p in load_posts(ROOT / ds["path"]) if p["id"] in ds["records"]]
```

**Claim.** Results files store no per-post text_sha256 or dataset hash (text_sha256 exists only in the .cache identity). rescore() and compare_results() match by id alone, so if a post's text or label is edited under the same id they score the old output against the new label with no warning, and failure rows show the new text beside the old output. Removing a post silently shrinks n. Rescore does not overwrite history (it writes a new file with rescored_from); this is latent, as data and rescored accuracies are unchanged today.

**Verifier.** Narrowed: 'silently changes history' is overstated (new file, source byte-identical). git {commit, dirty} could recover labels for clean runs but nothing checks it, and dirty runs have no provenance.

**Reproduction output.**

```
baseline is_deal acc matches stored: True
stdout/stderr during rescore after edit: '' ''
is_deal stored: 15/15 1.0 | edited h01 text+label: 14/15 0.933 + 6 hallucinated_on_non_deal rows showing the NEW text
after removing h01: 14/14 1.0 | warnings: ''
compare n_common_posts holdout: 14 of 15
```

**Fix.** Store text_sha256 (and a label hash) per record and file_sha256 per dataset in every results file; in rescore/compare, flag or exclude drifted, missing or new ids and print a one-line warning for legacy files without hashes.

**Regression test.** Save a fake-extractor run over a 2-post tmp dataset, rewrite post 1 under the same id, point eval.ROOT at tmp, and assert rescore() reports drift for post 1 (capsys or a 'drift' list) instead of scoring it; delete post 2 and assert it is reported missing.

### EM-8 (low, eval_method): s14 state label NJ can only be reached by inferring from city names, which the schema forbids

`data/posts.jsonl:14`

```
{"id": "s14", "source": "synthetic", "split": "dev",
```

**Claim.** In s14 ('Anyone licensed in both NY and NJ? Client deciding between Jersey City and Hoboken condos') the states appear only in a licensing question; choosing NJ for the property requires knowing where Jersey City/Hoboken are. schema.py: 'Never infer it from a city'. Every other multi-state label ties the state to the property in text (s18, h08, h10), and s11/s12 are null for unambiguous cities. The final model returned null and was scored 'missed' (+ missing_fields 'wrong'); README error #5 counts it as a model error, contradicting #3.

**Verifier.** Adversarial reading ('NJ is named') fails: nothing in the text attaches NJ rather than NY to the property except city geography. One dev record, two field outcomes: low.

**Reproduction output.**

```
STATE DESC: ... Only if the post names the state. Never infer it from a city, neighborhood, or metro name.
s14 ['NJ','NY'] label state= NJ
s18 TX ('in Texas'); h08 AZ ('Phoenix, AZ'); h10 TX ('McKinney TX'); v3-08 None (Kansas City)
PRED s14: state missed expected NJ got null; missing_fields wrong
```

**Fix.** Relabel s14 state null (add 'state' to expected missing) and drop it from README error #5, or add an explicit schema rule for states named in licensing context and revisit s11/s12.

**Regression test.** Data test: every non-null label state appears in the text, and posts naming two states carry a label_note saying which one is the property's; plus assert s14 state is None.

## Rejected candidates

| ID | Dimension | Title | Why rejected |
|---|---|---|---|
| CQ-1 | code_quality | A --rescore output counts as a fresh Claude run in the loop gates | Rejected as a duplicate of EM-5, which was verified separately and is kept as the single confirmed entry. |
| CQ-6 | code_quality | select()/load_split() don't validate split; non-'dev' values are mislabelled posts_dev or score 0 posts | Rejected as unreachable: argparse limits --split to ['all','dev'] and is the only path to evaluate(); the one direct caller (tests/test_eval.py:64) passes 'dev'; api.py and check_success never call it. Hardening nit only. |
| EM-4 | eval_method | README's 35-post tie includes the 24 dev/tuning posts without saying so in that sentence | Rejected: numbers reproduce, but README discloses the 24/11 split (lines 72, 102), already treats the 35-post set as contaminated and not the headline, and contamination favours Claude so the omission doesn't oversell it. The 11-post gap is one field cell out of 77. The untuned-post dip is kept inside EM-3. |
| EM-9 | eval_method | missing_fields row is derived from key-field nullness and 'double-counts' errors | Rejected: the equivalence holds, but it is a per-post null-pattern check no other row gives (88 records differ from any single field row), it measures follow-up-question correctness, README line 37 says it is code-computed, and it is not in the 7-field mean, so nothing is double-counted. |

## README numbers audit

Recomputed from `results/*.json` (read-only) by the script at the end of this section; every cell was also re-scored from raw outputs against current labels, with identical results.

| Cell | README claims | Recomputed | Source | Match |
|---|---|---|---|---|
| Holdout is_deal_request (regex / first / final) | 93% / 80% / 100% | 0.9333 / 0.8000 / 1.0000 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Holdout state | 75% / 83% / 100% | 0.7500 / 0.8333 / 1.0000 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Holdout city | 75% / 83% / 100% | 0.7500 / 0.8333 / 1.0000 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Holdout property_type | 75% / 75% / 92% | 0.7500 / 0.7500 / 0.9167 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Holdout transaction_type | 75% / 83% / 100% | 0.7500 / 0.8333 / 1.0000 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Holdout deal_value_usd | 58% / 83% / 100% | 0.5833 / 0.8333 / 1.0000 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Holdout timeline | 83% / 75% / 75% | 0.8333 / 0.7500 / 0.7500 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Holdout missing_fields (exact set) | 33% / 75% / 92% | 0.3333 / 0.7500 / 0.9167 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Holdout mean of the 7 fields | 0.764 / 0.805 / 0.952 | 0.7643 / 0.8048 / 0.9524 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Fields guessed on deal posts | 3 / 1 / 1 | 3 / 1 / 1 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Fields filled on non-deal posts | 0 / 5 / 0 | 0 / 5 / 0 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Posts that errored | 0 / 2 / 0 | 0 / 2 / 0 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| Holdout composition | 15 posts, 12 deal, 3 non-deal | 15 / 12 / 3 | claude_20261024T234359Z | yes |
| One post moves a field by about | 8 points | 1/12 = 8.3 | derived | yes |
| 35 posts, final prompt mean | 0.941 | 0.9409 | claude_20261024T234359Z (posts) | yes |
| 35 posts, baseline mean | 0.941 | 0.9409 | regex_20261024T224817Z (posts) | yes |
| Baseline drop 35 posts -> holdout | 18 points | 17.7 | regex_20261024T224817Z | yes |
| Iteration 0 dev mean (3 runs) | 0.885 | 0.8848 (0.8828, 0.8813, 0.8904) | claude_...232452Z, 232649Z, 232722Z | yes |
| Iteration 1 dev mean | 0.926 | 0.9264 | claude_...232921Z, 233007Z, 233059Z | yes |
| Iteration 2 dev mean | 0.930 | 0.9298 | claude_...233307Z, 233351Z, 233433Z | yes |
| Iteration 3 dev mean | 0.952 | 0.9524 | claude_...233711Z, 233757Z, 233842Z | yes |
| Iteration 4 dev mean | 0.945 | 0.9449 | claude_...234059Z, 234146Z, 234229Z | yes |
| Iteration 3 reverted for a non-deal city fill | hiring post got a city | non-deal fills per run [1, 1, 0] | iteration 3 files | yes |
| First / final prompt versions | de789943 / ac1a145b | de789943 / ac1a145b | claude_...231514Z / 234359Z | yes |
| Three headline runs from clean commits | clean | git.dirty False x3 | regex_20261024T224817Z / claude_20261024T231514Z / claude_20261024T234359Z (holdout) | yes |
| 12/12 Wilson 95% interval | about 76-100% | 75.7-100% | scoring.wilson_interval | yes |
| Dev gate bar = baseline dev mean | 0.932 | 0.9323 | regex_20261024T224817Z (posts_dev subset) | yes |
| Keep rule (prose, not a number) | no field regressed | iteration 2 kept with state -3.5, city -3.5 pts | loop/state.json + iteration 1/2 files | **no** |
| Test count in Running it (prose, EM-25) | 500 tests | 842 collected: 837 passed, 5 xfailed | pytest -q | **no** |

**Verdict:** every numeric README cell matches `results/*.json` to the printed precision, and re-scoring the cited raw outputs against the current labels reproduces them exactly. Two prose claims do not match: the keep rule (EM-2) and the stale test count (EM-25: '500 tests'; the suite collects 842, 837 passed + 5 xfailed).

Audit script (run from the worktree: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I audit.py`; read-only):

```python
"""Recompute README numbers from results/*.json (stored scores AND rescored against current labels, in-process, no writes)."""
import json, sys, os
REPO = "/Users/annikamugin/Documents/deal-requirements-board-review-eval"
sys.path.insert(0, REPO); os.chdir(REPO)
from schema import KEY_FIELDS
import scoring
R = lambda f: json.load(open(f"results/{f}"))
def acc(sc, f):
    return sc[f]["accuracy"] if f in ("is_deal_request", "missing_fields") else sc["fields"][f]["accuracy"]
def mean7(sc): return sum(acc(sc, f) for f in ("is_deal_request",) + KEY_FIELDS) / 7
def mean8(sc): return sum(acc(sc, f) for f in ("is_deal_request",) + KEY_FIELDS + ("missing_fields",)) / 8
def mean_key_missing(sc): return sum(acc(sc, f) for f in KEY_FIELDS + ("missing_fields",)) / 7
def rescored(d, name):
    ds = d["datasets"][name]
    posts = [p for p in scoring.load_posts(ds["path"]) if p["id"] in ds["records"]]
    recs = {}
    from extractors import parse_requirement, ExtractionError
    for pid, r in ds["records"].items():
        if r["raw"] is None: recs[pid] = {"prediction": None, "error": r["error"]}; continue
        try: recs[pid] = {"prediction": parse_requirement(r["raw"]).model_dump(mode="json"), "error": None}
        except ExtractionError as e: recs[pid] = {"prediction": None, "error": str(e)}
    return scoring.score(posts, recs)

cols = {"regex": "regex_20261024T224817Z.json", "first": "claude_20261024T231514Z.json", "final": "claude_20261024T234359Z.json"}
print("== Holdout table ==")
for c, f in cols.items():
    d = R(f); sc = d["datasets"]["holdout"]["scores"]; rs = rescored(d, "holdout")
    print(c, f)
    for fld in scoring.SCORED_FIELDS:
        print(f"  {fld:18s} stored={acc(sc,fld):.4f} ({acc(sc,fld):.0%}) rescored={acc(rs,fld):.4f}")
    nd = sc["non_deal_hallucinations"]
    print(f"  mean7(isdeal+6)={mean7(sc):.4f} mean8={mean8(sc):.4f} mean(6key+missing)={mean_key_missing(sc):.4f} rescored mean7={mean7(rs):.4f}")
    print(f"  guessed on deal posts={sum(sc['fields'][k]['hallucinated'] for k in KEY_FIELDS)}  non-deal filled={nd['fields_filled']} errors={sc['is_deal_request']['error']}")
    print(f"  n_posts={sc['n_posts']} n_deal={sc['n_deal_posts']}")
    if "posts" in d["datasets"]:
        ps = d["datasets"]["posts"]["scores"]; print(f"  posts(35) mean7={mean7(ps):.4f} rescored={mean7(rescored(d,'posts')):.4f}")
print("== Dev iterations ==")
state = json.load(open("loop/state.json"))
for h in state["history"]:
    ms = [mean7(R(f)["datasets"]["posts_dev"]["scores"]) for f in h["results_files"]]
    fm = {k: sum(acc(R(f)["datasets"]["posts_dev"]["scores"], k) for f in h["results_files"]) / 3 for k in ("is_deal_request",) + KEY_FIELDS}
    print(h["iteration"], h["prompt_version"], "per-run", [round(m, 4) for m in ms], "mean", round(sum(ms) / 3, 4), "state.json", h["dev_mean"])
    print("   field means", {k: round(v, 3) for k, v in fm.items()})
    print("   non-deal fills", [R(f)["datasets"]["posts_dev"]["scores"]["non_deal_hallucinations"]["fields_filled"] for f in h["results_files"]],
          "errors", [R(f)["datasets"]["posts_dev"]["scores"]["is_deal_request"]["error"] for f in h["results_files"]])
for f in ["claude_20261024T232525Z.json", "claude_20261024T232557Z.json"]:
    print("dirty", f, round(mean7(R(f)["datasets"]["posts_dev"]["scores"]), 4))
# regex dev mean (baseline's dev mean 0.932 claim) and 0.941 claims
d = R(cols["regex"]); ps = d["datasets"]["posts"]
dev_posts = [p for p in scoring.load_posts("data/posts.jsonl") if p["split"] == "dev"]
recs = {p["id"]: ps["records"][p["id"]] for p in dev_posts}
print("regex dev mean7", round(mean7(scoring.score(dev_posts, recs)), 4), "regex posts35 mean7", round(mean7(ps["scores"]), 4))
d = R("regex_20261024T224548Z.json"); print("regex 224548 holdout mean7", round(mean7(d["datasets"]["holdout"]["scores"]), 4))
# Wilson 12/12
print("wilson 12/12", scoring.wilson_interval(12, 12))
print("== posts.jsonl by split (dev = Claude tuning set) ==")
allp = scoring.load_posts("data/posts.jsonl")
for c, f in cols.items():
    ds = R(f)["datasets"]["posts"]
    for sp in ("dev", "holdout"):
        ps = [p for p in allp if p["split"] == sp]
        print(c, sp, len(ps), "mean7", round(mean7(scoring.score(ps, {p['id']: ds['records'][p['id']] for p in ps})), 4))
print("== regex vs final, holdout.jsonl, paired ==")
sys.path.insert(0, REPO + "/scripts"); import eval as E
c = E.compare_results(R(cols["regex"]), R(cols["final"]))["data/holdout.jsonl"]
for fld, r in c["fields"].items(): print(f"  {fld:18s} regex={r['a_correct']} final={r['b_correct']} p={r['p_value']:.3g}")
```
