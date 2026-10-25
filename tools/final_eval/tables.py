"""README-ready markdown tables: the frozen regex baseline vs. the Claude extractor, one dataset per table.

  python tools/final_eval/tables.py REGEX_JSON CLAUDE_JSON [--holdout-v2 OLD_CLAUDE_JSON OLD_REGEX_JSON]

Why a script instead of hand-copying numbers into the README: the final look happens once, and a
typo in a hand-built table would be the one number nobody can re-derive. Everything here is
computed from the saved raw outputs, re-scored against the current labels with the same code the
eval CLI uses (`deal_board.cli.eval.rescore` / `compare_results`), so a label fixed after a run is
reflected instead of the run's stale stored scores, and old files without `ci95` still get Wilson
intervals.

Why each dataset gets its own table: the holdout v3 set is the final, one-look number and the
earlier holdout v2 set was looked at during prompt work. Putting both in one table would invite
reading them as comparable, so the v2 numbers only ever appear in their own, separately headed
section (`--holdout-v2`). Contract: tools/final_eval/PROTOCOL.md, section `tables.py`.

Exit 0 with the markdown on stdout; exit 2 with one `ERROR: <reason>` line on stderr otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from deal_board.cli import eval as ev
from deal_board.schema import KEY_FIELDS, JSONObject
from deal_board.scoring import SCORED_FIELDS

type RunFile = tuple[str, JSONObject]

V3 = "holdout_v3"
V2 = "holdout"
MEAN_FIELDS: tuple[str, ...] = ("is_deal_request", *KEY_FIELDS)  # missing_fields is excluded from the mean
NO_HALLUCINATION_COLUMN = ("is_deal_request", "missing_fields")
DASH = "–"
NO_USAGE = "**Usage:** not recorded (this results file predates usage tracking)."
RESCORE_NOTE = "Both re-scored from their raw outputs against the current labels."


class TableError(Exception):
    """A refusal that maps to exit 2: unreadable input, wrong extractor slot or mismatched datasets."""


def load_run(path: str) -> RunFile:
    """Read one results file as (path as given, parsed object), refusing anything that isn't a JSON object."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise TableError(f"cannot read {path}: {type(e).__name__}: {e}") from e
    if not isinstance(data, dict):
        raise TableError(f"{path} is not a JSON object")
    return path, data


def _datasets(run: RunFile) -> JSONObject:
    datasets = run[1].get("datasets")
    if not isinstance(datasets, dict):
        raise TableError(f"{run[0]} has no 'datasets' object, so it is not an eval run results file")
    return datasets


def _check_extractor(run: RunFile, expected: str, slot: str) -> None:
    """Swapped arguments would silently flip every Δ's sign, so each slot's extractor is checked."""
    got = run[1].get("extractor")
    if got != expected:
        raise TableError(f"{slot} must be a {expected} results file, but {run[0]} has extractor {got!r}")


def _positional_dataset(regex: RunFile, claude: RunFile) -> str:
    """holdout_v3 if both runs have it, else holdout (v2) if both have it; never a mix of the two."""
    r, c = _datasets(regex), _datasets(claude)
    if V3 in r and V3 in c:
        return V3
    if V2 in r and V2 in c:
        return V2
    raise TableError(
        f"{regex[0]} and {claude[0]} share no dataset to compare (both need '{V3}', or else both '{V2}'); "
        f"regex has {sorted(r)}, claude has {sorted(c)}"
    )


def _check_v2_file(run: RunFile) -> None:
    """The --holdout-v2 section must be pure v2: a file that also holds v3 results is refused outright."""
    ds = _datasets(run)
    if V3 in ds:
        raise TableError(f"--holdout-v2 file {run[0]} contains '{V3}'; the v2 section must not include v3 runs")
    if V2 not in ds:
        raise TableError(f"--holdout-v2 file {run[0]} has no '{V2}' dataset")


def _only(saved: JSONObject, name: str) -> JSONObject:
    """The run restricted to one dataset, so rescoring never touches (or needs labels for) the others."""
    return {**saved, "datasets": {name: saved["datasets"][name]}}


def _rescore(run: RunFile, name: str) -> JSONObject:
    """One dataset of a run re-scored from its raw outputs; a missing label file is a refusal, not a traceback."""
    try:
        rescored: JSONObject = ev.rescore(_only(run[1], name))[name]
    except OSError as e:
        raise TableError(f"cannot re-score {run[0]} '{name}': label file unreadable: {e}") from e
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise TableError(f"cannot re-score {run[0]} '{name}': malformed results file ({type(e).__name__}: {e})") from e
    return rescored


def _compare(regex: RunFile, claude: RunFile, name: str, path: str) -> JSONObject:
    """Per-field exact McNemar (A = regex, B = Claude) on the posts both runs scored."""
    try:
        cmp = ev.compare_results(_only(regex[1], name), _only(claude[1], name))
    except OSError as e:
        raise TableError(f"cannot compare on '{name}': label file unreadable: {e}") from e
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise TableError(f"cannot compare on '{name}': malformed results file ({type(e).__name__}: {e})") from e
    if path not in cmp:
        raise TableError(f"no paired comparison for {path}")
    fields: JSONObject = cmp[path]["fields"]
    return fields


def _delta(regex: float | None, claude: float | None) -> str:
    """Points, signed: `+7`, `-8`, `+0`; `n/a` when either side has nothing to score."""
    if regex is None or claude is None:
        return "n/a"
    return f"{round((claude - regex) * 100):+d}"


def _mean(scores: JSONObject) -> float | None:
    """Mean accuracy over is_deal_request and the 6 key fields; n/a if any of them is unscored."""
    accs = [ev.field_cell(scores, f)["accuracy"] for f in MEAN_FIELDS]
    if any(a is None for a in accs):
        return None
    return float(sum(accs)) / len(accs)


def _fmt_mean(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def _guessed_on_deal(scores: JSONObject) -> int:
    return sum(int(scores["fields"][f]["hallucinated"]) for f in KEY_FIELDS)


def _errored(records: JSONObject) -> int:
    return sum(1 for r in records.values() if r.get("error"))


def _commit(saved: JSONObject) -> str:
    """Short commit plus the run-state markers the reader must see next to it."""
    git = saved.get("git") or {}
    commit = git.get("commit")
    out = f"commit `{commit[:7] if isinstance(commit, str) and commit else 'unknown'}`"
    if git.get("dirty") is True:
        out += " (uncommitted changes)"
    final = saved.get("final_eval")
    if isinstance(final, dict) and final.get("complete") is False:
        out += " (INCOMPLETE)"
    return out


def _provenance(regex: RunFile, claude: RunFile) -> str:
    r, c = regex[1], claude[1]
    return (
        f"**Provenance.** Regex: `{regex[0]}`, {_commit(r)}, run `{r.get('timestamp') or 'unknown'}`. "
        f"Claude: `{claude[0]}`, `{c.get('model') or 'unknown'}`, prompt `{c.get('prompt_version') or 'unknown'}`, "
        f"{_commit(c)}, run `{c.get('timestamp') or 'unknown'}`. {RESCORE_NOTE}"
    )


def _look_note(name: str, regex: RunFile, claude: RunFile) -> str:
    """Says plainly which kind of look these numbers are, so a v2 or second-look table can't pass as final."""
    if name == V2:
        return (
            "**Holdout v2: not the final look.** These posts were scored during prompt development, so they "
            "are not an unseen test set; the final number is the holdout v3 table."
        )

    def second(saved: JSONObject) -> bool:
        final = saved.get("final_eval")
        return isinstance(final, dict) and final.get("second_look") is True

    if second(regex[1]) or second(claude[1]):
        return (
            "**Second look** at `data/holdout_v3.jsonl`: at least one of these runs was made with "
            "`--allow-second-look` (`final_eval.second_look` is true), so this is not a first, unseen look."
        )
    return (
        "**First and only look** at `data/holdout_v3.jsonl`: each extractor was run on these posts exactly once, "
        "and no run was repeated after seeing a result."
    )


def _section(regex: RunFile, claude: RunFile, name: str) -> str:
    """Heading, table, usage line, provenance and look note for one dataset."""
    r_ds, c_ds = _rescore(regex, name), _rescore(claude, name)
    path = r_ds["path"]
    if c_ds["path"] != path:
        raise TableError(f"'{name}' points at different label files: regex {path}, claude {c_ds['path']}")
    rs, cs = r_ds["scores"], c_ds["scores"]
    paired = _compare(regex, claude, name, path)

    title = "Final holdout v3" if name == V3 else "Earlier holdout v2"
    heading = (
        f"### {title}: `{path}`, {rs['n_posts']} posts ({rs['n_deal_posts']} deal, {rs['n_non_deal_posts']} non-deal)"
    )
    if cs["n_posts"] != rs["n_posts"]:  # a partial Claude run: say so rather than hide it in the denominators
        heading += f"; the Claude run scored {cs['n_posts']} of them"
    prompt = claude[1].get("prompt_version") or "unknown"
    lines = [
        heading,
        "",
        f"| Field | Regex baseline | Claude `{prompt}` | Δ (pts) | Hallucinated (regex / Claude) | p (exact McNemar) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for field in SCORED_FIELDS:
        rc, cc = ev.field_cell(rs, field), ev.field_cell(cs, field)
        halluc = DASH if field in NO_HALLUCINATION_COLUMN else f"{rc['hallucinated']} / {cc['hallucinated']}"
        lines.append(
            f"| {field} | {ev.with_ci(rc['accuracy'], rc.get('ci95'))} | {ev.with_ci(cc['accuracy'], cc.get('ci95'))} "
            f"| {_delta(rc['accuracy'], cc['accuracy'])} | {halluc} | {paired[field]['p_value']:.3g} |"
        )
    r_mean, c_mean = _mean(rs), _mean(cs)
    lines += [
        f"| **Mean of the {len(MEAN_FIELDS)} fields** | {_fmt_mean(r_mean)} | {_fmt_mean(c_mean)} "
        f"| {_delta(r_mean, c_mean)} | | |",
        f"| Fields guessed on deal posts | {_guessed_on_deal(rs)} | {_guessed_on_deal(cs)} | | | |",
        f"| Fields filled on non-deal posts | {rs['non_deal_hallucinations']['fields_filled']} "
        f"| {cs['non_deal_hallucinations']['fields_filled']} | | | |",
        f"| Posts that errored | {_errored(r_ds['records'])} | {_errored(c_ds['records'])} | | | |",
        "",
    ]
    usage = claude[1].get("usage")
    lines += [ev.usage_line(usage) if isinstance(usage, dict) else NO_USAGE, ""]
    lines += [_provenance(regex, claude), "", _look_note(name, regex, claude)]
    return "\n".join(lines)


def build_tables(regex: RunFile, claude: RunFile, holdout_v2: tuple[RunFile, RunFile] | None = None) -> str:
    """The full markdown: the positional pair's section, then (if given) the separate holdout v2 section.

    `holdout_v2` is (old Claude run, old regex run), Claude first as on the command line. All checks run
    before any output is built, so a refusal never leaves a half-printed table behind.
    """
    _check_extractor(regex, "regex", "REGEX_JSON (first positional)")
    _check_extractor(claude, "claude", "CLAUDE_JSON (second positional)")
    name = _positional_dataset(regex, claude)
    if holdout_v2 is not None:
        old_claude, old_regex = holdout_v2
        _check_extractor(old_claude, "claude", "OLD_CLAUDE_JSON (first --holdout-v2 file)")
        _check_extractor(old_regex, "regex", "OLD_REGEX_JSON (second --holdout-v2 file)")
        _check_v2_file(old_claude)
        _check_v2_file(old_regex)
    sections = [_section(regex, claude, name)]
    if holdout_v2 is not None:
        sections.append(_section(holdout_v2[1], holdout_v2[0], V2))
    return "\n\n".join(sections)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="README tables: regex baseline vs. Claude, one dataset per table.")
    ap.add_argument("regex", metavar="REGEX_JSON", help="regex baseline results file")
    ap.add_argument("claude", metavar="CLAUDE_JSON", help="Claude results file")
    ap.add_argument(
        "--holdout-v2",
        nargs=2,
        metavar=("OLD_CLAUDE_JSON", "OLD_REGEX_JSON"),
        help="add a separate holdout v2 section from these two files (Claude first)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        regex, claude = load_run(args.regex), load_run(args.claude)
        v2 = (load_run(args.holdout_v2[0]), load_run(args.holdout_v2[1])) if args.holdout_v2 else None
        out = build_tables(regex, claude, v2)
    except TableError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
