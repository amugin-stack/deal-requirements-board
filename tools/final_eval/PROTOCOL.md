# Final eval protocol: `data/holdout_v3.jsonl`, one look

`data/holdout_v3.jsonl` holds 25 double-labeled posts (19 deal requests, 6 non-deal) that no
extractor has ever been run on. The project's final number is **one** run of the frozen regex
baseline and **one** run of the Claude extractor (prompt `ac1a145b`) on these posts. These tools
make that run mechanical (one command per step), auditable (every refusal and check is printed)
and blind to its outcome (nothing about the result can trigger a rerun).

## Rules

1. **One look.** Each extractor is run on holdout_v3 once. Before its first extractor call,
   `run.py` creates a `<extractor>_<stamp>.look` marker next to the results; it is never removed,
   so even a killed run leaves evidence. `run.py` refuses a second run when the repo's `results/`
   or `--results-dir` holds a marker or a results file recording that extractor on holdout_v3
   (matched by split, dataset name, dataset path or post ids; `.json` matched case-insensitively
   anywhere in the name; symlinks followed; an unparseable `<extractor>_*.json` counts), or when
   any commit in git history has such a file under `results/`. `--results-dir` must be inside
   `results/`, so a look can't be written where the next guard won't see it.
   `--allow-second-look` overrides the one-look check; it is off by default, is only ever passed
   with the owner's written approval, and the results file records that it was used.
2. **Clean tree, on `main`.** `run.py` refuses a dirty tree (any `git status --porcelain` output,
   untracked files included) or a HEAD that is not the branch `main` (detached HEAD counts as not
   on main). This is why the regex results are committed before the Claude run (step 3 below): the
   new, untracked results file would otherwise make the Claude run's tree dirty.
3. **No cache.** `run.py` never reads or writes the raw-output cache. It refuses to run if the cache
   holds any entry for a holdout_v3 post text, because a cached output means someone already ran an
   extractor on it.
4. **The key is never printed.** No output, error or results file contains the API key. On an error,
   only the exception type and a message with the key redacted are printed.
5. **A failed run is still a look.** If a run errors part-way (network, rate limit, Ctrl-C), `run.py`
   writes a partial results file for the posts it finished and exits 1. Commit that file as it is.
   Do **not** rerun without the owner's written approval; if approved, rerun with
   `--allow-second-look` and report both files.
6. **Validation does not trigger reruns.** `validate.py` reports whether a file is a clean final
   look. A FAIL on `errors` or `non_deal` is a *result* (the extractor erred or guessed), and is
   reported, not fixed by rerunning.

## Command sequence (orchestrator, from the main checkout)

Precondition: branch `final-eval-tools` has been merged into `main` with the owner's approval
(these tools are not on `main` before that).

```bash
cd ~/Documents/deal-requirements-board
git switch main
git status --porcelain                        # must print nothing
git log -1 --format='%H %s'                   # record the commit
.venv/bin/pip install -q -e '.[dev]'          # the installed deal_board must be this checkout
.venv/bin/python -c "import deal_board.paths as p; print(p.REPO_ROOT)"   # must print this checkout's path
.venv/bin/python -c "from deal_board.extractors.claude import PROMPT_VERSION; print(PROMPT_VERSION)"   # ac1a145b
echo "DEAL_MODEL=${DEAL_MODEL:-<unset>}"     # must be unset or claude-haiku-5-5 (run.py --expect-model enforces it)
```

1. Regex baseline (no key needed, no API calls):

```bash
.venv/bin/python tools/final_eval/run.py --extractor regex --expect-prompt ac1a145b
```

2. Commit its three new files **immediately** (`results/regex_<stamp>.json`, `.md` and `.look`;
   `git status --porcelain` lists exactly these). An uncommitted look can be deleted; a committed
   one stays in history, where the guard finds it:

```bash
git status --porcelain
git add results/regex_<stamp>.json results/regex_<stamp>.md results/regex_<stamp>.look
git commit -m "final eval: regex baseline on holdout_v3 (first and only look)"
```

3. Claude (the key comes from the shell or `.env`; never echo it):

```bash
.venv/bin/python tools/final_eval/run.py --extractor claude --expect-prompt ac1a145b --expect-model claude-haiku-5-5
```

4. Commit its new files immediately, whatever the outcome (exit 0; exit 1 with a partial file;
   exit 1 with only the `.look` marker if saving failed):

```bash
git status --porcelain
git add results/claude_<stamp>.json results/claude_<stamp>.md results/claude_<stamp>.look
git commit -m "final eval: Claude ac1a145b on holdout_v3 (first and only look)"
```

5. Validate each file (PASS/FAIL per line; exit 0 only if every check passes):

```bash
.venv/bin/python tools/final_eval/validate.py results/regex_<stamp>.json
.venv/bin/python tools/final_eval/validate.py results/claude_<stamp>.json --expect-prompt ac1a145b --expect-model claude-haiku-5-5
```

6. Build the README tables (holdout v3 headline, holdout v2 in its own section):

```bash
.venv/bin/python tools/final_eval/tables.py results/regex_<stamp>.json results/claude_<stamp>.json --holdout-v2 results/claude_20261024T234359Z.json results/regex_20261024T224817Z.json
```

7. Paste the output into README's Results section, keep the validate.py output with it (any FAIL
   line is reported in the README, not hidden), and commit the README.

## CLI contract

All three scripts are standalone (`python tools/final_eval/<name>.py`), import the installed
`deal_board`, and reuse `deal_board.cli.eval` and `deal_board.scoring` instead of reimplementing
scoring, saving, git provenance or usage accounting. Each exposes `main(argv, hooks) -> int` so tests
can inject fakes; the `if __name__ == "__main__"` block is `sys.exit(main())`. `ROOT` below is
`deal_board.paths.REPO_ROOT`.

A dataset path string is written the way `cli/eval.py` writes it: relative to `ROOT` when the file
is under `ROOT` (`"data/holdout_v3.jsonl"`), otherwise the absolute path. `eval.rescore` resolves
both via `ROOT / path`.

### `run.py`

```
run.py --extractor {regex,claude} [--expect-prompt VERSION] [--expect-model MODEL] [--results-dir DIR] [--cache-dir DIR] [--allow-second-look]
```

| flag | default | meaning |
|---|---|---|
| `--extractor` | required | `regex` (frozen baseline) or `claude` |
| `--expect-prompt` | `ac1a145b` | Claude's `PROMPT_VERSION` must equal this |
| `--expect-model` | none | if given, the built Claude extractor's model must equal this (PROTOCOL always passes it; the default is none only so the frozen smoke check's fake model runs) |
| `--results-dir` | `ROOT/results` | where the results `.json` + `.md` are written |
| `--cache-dir` | `deal_board.cli.eval.CACHE_DIR` | raw-output cache to inspect (never written) |
| `--allow-second-look` | off | skip the one-look refusal (owner approval only) |

Exit codes: **0** complete run written; **1** run aborted part-way, partial file written;
**2** refused before any extractor call (nothing written), or a usage error from argparse.
Every refusal prints one line to stderr starting `REFUSED: ` and naming the rule.

Refusal checks, in this order, all before the extractor is built or called:

1. `dirty`: `hooks.git_state().dirty` is true.
2. `branch`: `hooks.git_state().branch != "main"`.
3. `prompt` (claude only): `hooks.prompt_version() != --expect-prompt`.
4. `key` (claude only): `hooks.has_api_key()` is false.
5. `one-look` (skipped with `--allow-second-look`): under `--results-dir` or any of
   `hooks.guard_results_dirs` (recursive, symlinks followed) there is a `<extractor>_*.look`
   marker; or a file with `.json` anywhere in its name (any case) that is unparseable and named
   `<extractor>_*`, or parses to an object with `extractor` equal to the requested extractor and
   `split == "holdout_v3"`, a `"holdout_v3"` key in `datasets`, a dataset `path` ending in
   `holdout_v3.jsonl`, or record ids overlapping the dataset's post ids; or
   `hooks.history_looks(extractor, ids)` (default: the same rule applied to every `results/` file
   in any commit of `git rev-list --all` that mentions `holdout_v3` or `v3-`) is non-empty.
   `results-dir`: if `hooks.guard_results_dirs` is non-empty, `--results-dir` must resolve inside
   one of them.
6. `cache`: `--cache-dir` or any of `hooks.guard_cache_dirs` contains (recursively) a `*.json` file
   that either is named `<key>.json` where `key = deal_board.cli.eval.cache_key(model, prompt,
   text)` for a holdout_v3 post text (model = configured `DEAL_MODEL` and
   `deal_board.config.DEFAULT_MODEL`, prompt = `--expect-prompt`), or is a JSON object whose
   `text_sha256` equals `deal_board.cli.eval.text_sha256(text)` for a holdout_v3 post text.
   Checked for both extractors.

Then: build the extractor with `hooks.make_extractor(name)` (returns `(extractor, meta)`; meta is
`{}` for regex and `{"model", "prompt_version"}` for claude). For claude, if
`meta["prompt_version"] != --expect-prompt` it refuses (`prompt`, exit 2), and if `--expect-model`
is given and `meta["model"]` differs it refuses (`model`, exit 2). Then the `.look` marker is
created (exclusive; `-2`, `-3` if taken). Posts are read from
`hooks.data_path`, and each is run with `deal_board.cli.eval.run_post(extractor, text, None)` (cache
`None`: no read, no write), one at a time, so a crash keeps the finished records.

Results file (written with `deal_board.cli.eval.save`, so the stem is
`<extractor>_<UTC timestamp>` and a taken stem gets `-2`, `-3`, never overwriting):

```json
{
  "extractor": "claude",
  "model": "claude-haiku-5-5",            // null for regex
  "prompt_version": "ac1a145b",           // null for regex
  "split": "holdout_v3",
  "timestamp": "20261025T120000Z",
  "git": {"commit": "<sha>", "dirty": false},
  "scoring": "<deal_board.cli.eval.SCORING_NOTE>",
  "repeat": {"index": 1, "of": 1},
  "cache_enabled": false,
  "usage": { ... deal_board.cli.eval.usage_totals(datasets, model) ... },
  "final_eval": {
    "protocol": "tools/final_eval/PROTOCOL.md",
    "branch": "main",
    "data_sha256": "<sha256 of the dataset file bytes>",
    "second_look": false,                 // true iff --allow-second-look was passed
    "expected_model": "claude-haiku-5-5", // --expect-model, or null
    "marker": "claude_20261025T120000Z.look",
    "complete": true,                     // false for a partial file
    "aborted": null                       // or {"completed": 7, "total": 25, "error_type": "APIConnectionError"}
  },
  "datasets": {
    "holdout_v3": {"path": "data/holdout_v3.jsonl", "records": {"v3-01": {...}}, "scores": { ... deal_board.scoring.score(posts_done, records) ... }}
  }
}
```

Records are exactly what `run_post` returns (`raw`, `prediction`, `error`, `call`). The `.md`
written next to it is `deal_board.cli.eval.markdown(result)` with its first line replaced by
`# Final eval: <extractor> on holdout_v3 (first and only look)` (or `(SECOND look)`, or
`(INCOMPLETE: k of 25 posts)`), because `markdown()` titles any non-`all` split "holdout not scored".
On success stdout gets that markdown and a final line `saved <path>`.

Hooks (`run.Hooks`, a frozen dataclass; `run.default_hooks()` gives the real ones; tests use
`dataclasses.replace`):

| field | type | default |
|---|---|---|
| `make_extractor` | `Callable[[str], tuple[Extractor, JSONObject]]` | `deal_board.cli.eval.make_extractor` |
| `git_state` | `Callable[[], GitState]` | branch from `git rev-parse --abbrev-ref HEAD` in `ROOT`, commit/dirty from `deal_board.cli.eval.git_info()` |
| `has_api_key` | `Callable[[], bool]` | `deal_board.config.load_settings().has_api_key` |
| `prompt_version` | `Callable[[], str]` | `deal_board.extractors.claude.PROMPT_VERSION` |
| `secret` | `Callable[[], str \| None]` | `load_settings().api_key_value()`, used only to redact messages |
| `data_path` | `Path` | `ROOT / "data/holdout_v3.jsonl"` |
| `guard_results_dirs` | `tuple[Path, ...]` | `(deal_board.cli.eval.RESULTS_DIR,)` |
| `guard_cache_dirs` | `tuple[Path, ...]` | `(deal_board.cli.eval.CACHE_DIR,)` |
| `history_looks` | `Callable[[str, frozenset[str]], list[str]]` | git-history scan described under `one-look` |

`run.GitState` is a frozen dataclass `GitState(branch: str, commit: str, dirty: bool)`.
`main(argv: list[str] | None = None, hooks: Hooks | None = None) -> int`. If saving fails after the
extractor calls, it prints a redacted `ERROR:` line and returns 1; the marker remains. `.env` is loaded
(`deal_board.config.load_dotenv_file()`) only in the `__main__` block, never in `main()`, so tests
never see a real key.

### `validate.py`

```
validate.py RESULTS_JSON [--expect-prompt VERSION] [--expect-model MODEL]   # prompt default ac1a145b
```

Prints one line per check, `PASS <name>: <detail>` or `FAIL <name>: <detail>`, then `VALID` or
`INVALID (<n> failed)`. Exit **0** iff every check passes, **1** if any fails, **2** if the file
can't be read or isn't a JSON object (one `ERROR: ` line). Checks, by name, in this order:

| name | passes when |
|---|---|
| `extractor` | `extractor` is `"regex"` or `"claude"` |
| `split` | `split == "holdout_v3"` |
| `datasets` | `datasets` has exactly one key, `"holdout_v3"`, whose `path` is the dataset path string of `hooks.data_path` |
| `records` | exactly 25 records, and their ids are exactly the label file's ids |
| `errors` | no record has a truthy `error` (detail gives the count and ids) |
| `complete` | `final_eval.complete` is `true` and `final_eval.aborted` is null |
| `first_look` | `final_eval.second_look` is `false`, `final_eval.branch == "main"`, `final_eval.protocol == "tools/final_eval/PROTOCOL.md"` |
| `labels` | `final_eval.data_sha256` equals the sha256 of the label file now (labels unchanged since the run) |
| `provenance` | no `rescored_from`, `cache_enabled` is `false`, `usage.cache_hits == 0`, every record has `call` with `cached: false` |
| `git_clean` | `git.dirty` is exactly `false` (missing counts as a FAIL) |
| `git_ancestor` | `hooks.is_ancestor(git.commit)` is true |
| `prompt` | claude: `prompt_version == --expect-prompt`; regex: `prompt_version` is null |
| `model` | claude: `model` is a non-empty string, equal to `--expect-model` if given; regex: `model` is null |
| `raw_consistent` | every record's `prediction`/`error` is what `deal_board.cli.eval.rescore_records` re-derives from its `raw` |
| `scores` | stored `scores` equal `deal_board.scoring.score(posts, records)` recomputed from the file's records against the labels |
| `intervals` | each of the 8 `deal_board.scoring.SCORED_FIELDS` has a non-null `accuracy` and a `ci95` `[lo, hi]` with `0 <= lo <= accuracy <= hi <= 1` |
| `non_deal` | predictions on the label file's non-deal posts fill 0 of the 6 key fields (detail: `<n> fields filled on non-deal posts (<ids>)`) |

Hooks (`validate.Hooks`, frozen dataclass, `validate.default_hooks()`): `data_path: Path` (default
`ROOT / "data/holdout_v3.jsonl"`, the labels) and `is_ancestor: Callable[[str], bool]` (default:
true iff the commit is hex and `git merge-base --is-ancestor <commit> main` succeeds in `ROOT`; no
HEAD fallback). Also exported: `validate(result, posts, *, expected_path, is_ancestor,
expect_prompt, data_sha256, expect_model=None) -> list[Check]` with `Check(name: str, ok: bool, detail: str)` frozen.
`main(argv: list[str] | None = None, hooks: Hooks | None = None) -> int`.

### `tables.py`

```
tables.py REGEX_JSON CLAUDE_JSON [--holdout-v2 OLD_CLAUDE_JSON OLD_REGEX_JSON]
```

Prints README-ready markdown to stdout, exit **0**. Exit **2** with one `ERROR: ` line on stderr
when: a file can't be read; the first positional's `extractor` isn't `regex` or the second's isn't
`claude` (same for the `--holdout-v2` pair, which is Claude first); the positional pair doesn't
share a dataset (both must contain `holdout_v3`, or else both contain `holdout`); or a
`--holdout-v2` file contains `holdout_v3` or lacks `holdout`; any file has `rescored_from` (a
rescore is not a look); or a holdout_v3 file has no `final_eval` block. The positional pair uses
`holdout_v3` if both have it, else `holdout` (the v2 set), and the heading says which, so the
two sets are never in the same table.

Both runs are re-scored from their raw outputs against the current labels with
`deal_board.cli.eval.rescore`, so old files without `ci95` still get Wilson intervals. Paired
comparison uses `deal_board.cli.eval.compare_results(regex, claude)` (exact McNemar, A = regex,
B = Claude) for the table's dataset path.

Output, per table:

```
### <Final holdout v3 | Earlier holdout v2>: `<path>`, <n> posts (<d> deal, <nd> non-deal)

| Field | Regex baseline | Claude `<prompt>` | Δ (pts) | Hallucinated (regex / Claude) | p (exact McNemar) |
|---|---:|---:|---:|---:|---:|
| is_deal_request | 93% [70–99] | 100% [80–100] | +7 | – | 1 |
| state | ... one row per deal_board.scoring.SCORED_FIELDS, 8 rows, first cell exactly the field name ...
| **Mean of the 7 fields** | 0.764 | 0.952 | +19 | | |
| Fields guessed on deal posts | 3 | 1 | | | |
| Fields filled on non-deal posts | 0 | 0 | | | |
| Posts that errored | 0 | 0 | | | |
```

- Accuracy cells are `deal_board.cli.eval.with_ci(accuracy, ci95)`, e.g. `92% [64–99]`.
- Δ is `round((claude - regex) * 100)` with a sign (`+7`, `-8`, `+0`); `n/a` if either is null.
- Hallucinated is the field's `hallucinated` count on deal posts, `regex / Claude`; `–` for
  `is_deal_request` and `missing_fields`.
- p is `paired_comparison`'s `p_value` formatted `{:.3g}`.
- The mean is over `is_deal_request` and the 6 key fields (not `missing_fields`), 3 decimals; its
  Δ is in points like the rows.
- "Fields guessed on deal posts" sums `hallucinated` over the 6 key fields; "filled on non-deal"
  is `non_deal_hallucinations.fields_filled`; "Posts that errored" counts records with an error.

After each table: a usage line for the Claude run, `deal_board.cli.eval.usage_line(usage)` when the
file has `usage`, else `**Usage:** not recorded (this results file predates usage tracking).`; then
a provenance block:

```
**Provenance.** Regex: `<regex file>`, commit `<7>`, run `<timestamp>`. Claude: `<claude file>`,
`<model>`, prompt `<prompt>`, commit `<7>`, run `<timestamp>`. Both re-scored from their raw outputs
against the current labels.
```

then exactly one of: `**First and only look** at \`data/holdout_v3.jsonl\`: ...` (holdout_v3, no
file has `final_eval.second_look` true), `**Second look** ...` (holdout_v3 and some file has it),
or `**Holdout v2: not the final look.** ...` (the v2 set). A run whose `git.dirty` is true gets
`(uncommitted changes)` after its commit, and any other non-`false` value gets
`(dirty flag <value>, not provably clean)`; an incomplete one (`final_eval.complete` false) gets
`(INCOMPLETE)`, and then every Δ cell is `n/a` with a one-line note. For holdout_v3, a run whose
`final_eval.data_sha256` differs from the label file now gets a
`**WARNING: labels changed since this run.** ...` line before the provenance block.

Python API: `build_tables(regex: RunFile, claude: RunFile, holdout_v2: tuple[RunFile, RunFile] |
None = None) -> str` where `RunFile = tuple[str, JSONObject]` (source path as shown, parsed file),
raising `tables.TableError` for the exit-2 cases; `main(argv: list[str] | None = None) -> int`.
