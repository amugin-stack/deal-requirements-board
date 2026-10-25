"""One look at data/holdout_v3.jsonl: run one extractor once, or refuse.

Why this exists instead of `deal-eval`: the final number is only worth anything if nobody could
have peeked first and nobody can quietly rerun after seeing it. So every precondition that would
make the look untrustworthy (dirty tree, wrong branch, wrong prompt, no key, an earlier look, a
cached output for a holdout post) is checked and refused *before* the extractor is built or
called, and a run that dies part-way still writes what it saw, because it was still a look.
The contract (flags, exit codes, refusal order, file shape) is tools/final_eval/PROTOCOL.md.

Scoring, saving, git provenance and usage accounting are reused from deal_board.cli.eval and
deal_board.scoring so the final file is scored exactly like every earlier results file.

Exit codes: 0 complete run written; 1 aborted part-way, partial file written; 2 refused (nothing
written) or an argparse usage error.

Before the first extractor call a `<extractor>_<stamp>.look` marker is created in the results dir.
It is never removed: if the process is killed (SIGKILL, power loss) or saving fails, the marker is
the evidence that a look happened, and the one-look guard refuses the next run because of it.
"""

import argparse
import functools
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deal_board import config, paths
from deal_board.cli import eval as ev
from deal_board.extractors import Extractor
from deal_board.schema import JSONObject
from deal_board.scoring import load_posts, score

ROOT = paths.REPO_ROOT
SPLIT = "holdout_v3"
DEFAULT_PROMPT = "ac1a145b"
PROTOCOL = "tools/final_eval/PROTOCOL.md"
REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class GitState:
    """Where HEAD is and whether the tree is clean, sampled once so every check sees one state."""

    branch: str
    commit: str
    dirty: bool


@dataclass(frozen=True)
class Hooks:
    """Every side effect run.py has on the outside world, injectable so tests never touch the real
    holdout, git, API key or results/ (and so a test can't accidentally make the real look)."""

    make_extractor: Callable[[str], tuple[Extractor, JSONObject]]
    git_state: Callable[[], GitState]
    has_api_key: Callable[[], bool]
    prompt_version: Callable[[], str]
    secret: Callable[[], str | None]
    data_path: Path
    guard_results_dirs: tuple[Path, ...]
    guard_cache_dirs: tuple[Path, ...]
    history_looks: Callable[[str, frozenset[str]], list[str]] = lambda extractor, ids: _history_looks(extractor, ids)


class Refusal(Exception):
    """A precondition failed; carries the rule name so the one stderr line names it."""

    def __init__(self, rule: str, explanation: str) -> None:
        super().__init__(f"{rule}: {explanation}")
        self.rule, self.explanation = rule, explanation


def _git_state() -> GitState:
    """Branch from git itself (detached HEAD prints "HEAD", which is then not "main"), and commit
    and dirtiness from eval.git_info() so the file's provenance matches every other results file."""
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    info = ev.git_info()
    return GitState(branch=branch, commit=str(info["commit"]), dirty=bool(info["dirty"]))


def _has_api_key() -> bool:
    """Read at call time (not import time) so .env loaded in __main__ is seen, and tests aren't."""
    return config.load_settings().has_api_key


def _prompt_version() -> str:
    """Imported lazily: importing run.py (e.g. in tests) shouldn't pull in the Anthropic SDK."""
    from deal_board.extractors.claude import PROMPT_VERSION

    return PROMPT_VERSION


def _secret() -> str | None:
    """The plaintext key, used only to scrub it from anything this script prints or saves."""
    return config.load_settings().api_key_value()


def default_hooks() -> Hooks:
    """The real hooks: the actual holdout, git, settings and the repo's results/ and cache."""
    return Hooks(
        make_extractor=ev.make_extractor,
        git_state=_git_state,
        has_api_key=_has_api_key,
        prompt_version=_prompt_version,
        secret=_secret,
        data_path=ROOT / "data/holdout_v3.jsonl",
        guard_results_dirs=(ev.RESULTS_DIR,),
        guard_cache_dirs=(ev.CACHE_DIR,),
    )


@functools.cache
def _history_candidates() -> tuple[tuple[str, str], ...]:
    """(rev:path, blob text) of every results/ file in any commit that mentions holdout_v3.

    Why history: the files on disk can be renamed, deleted or `git clean`ed after a look, but a
    committed look stays in the object store. Cached because history doesn't change mid-process.
    """

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout

    revs = git("rev-list", "--all").split()
    if not revs:
        return ()
    hits = git("grep", "-l", "-F", "-e", SPLIT, "-e", "v3-", *revs, "--", "results/").splitlines()
    return tuple((hit, git("show", hit)) for hit in hits)


def _history_looks(extractor: str, ids: frozenset[str]) -> list[str]:
    """Committed results files (rev:path) anywhere in git history that record this extractor on holdout_v3."""
    out = []
    for where, text in _history_candidates():
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if is_look(data, extractor, ids):
            out.append(where)
    return out


def redact(text: str, secret: str | None) -> str:
    """Scrub the key from a message: exception text from an SDK or network layer can echo it."""
    return text.replace(secret, REDACTED) if secret else text


def _redact_obj(obj: Any, secret: str | None) -> Any:
    """Scrub the key from every string in the results dict, since the file is committed publicly."""
    if not secret:
        return obj
    if isinstance(obj, str):
        return redact(obj, secret)
    if isinstance(obj, dict):
        return {_redact_obj(k, secret): _redact_obj(v, secret) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_redact_obj(v, secret) for v in obj]
    return obj


def dataset_path_string(path: Path) -> str:
    """The path as cli/eval.py records it (relative to ROOT when inside it), so eval.rescore and
    tables.py can resolve the file via ROOT / path, and validate.py can compare like with like."""
    absolute = Path(path).absolute()
    return absolute.relative_to(ROOT).as_posix() if absolute.is_relative_to(ROOT) else str(absolute)


def _json_files(directory: Path) -> Iterator[Path]:
    """Recursive *.json under directory; a missing or unreadable directory just yields nothing,
    because a guard that crashes is a guard someone will be tempted to switch off.

    Matches `.json` case-insensitively and anywhere in the name (`x.json.bak`, `x.JSON`) and
    follows symlinked directories, so renaming or relinking an earlier file doesn't hide it.
    """
    yield from (p for p in _all_files(directory) if ".json" in p.name.lower())


def _all_files(directory: Path) -> Iterator[Path]:
    """Every file under directory, following symlinks once per real directory (no loops)."""
    if not directory.is_dir():
        return
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_file():
                yield path


def _load_json(path: Path) -> Any:
    """Parsed JSON, or None for anything unreadable: such files can't be a results file or cache entry."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None


def is_look(data: Any, extractor: str, ids: frozenset[str]) -> bool:
    """Whether a parsed results file records `extractor` on holdout_v3.

    Matches on split, dataset name, dataset path *or* record ids, so a file from any code path
    (this script, a hand-run deal-eval with DATASETS repointed, a rescore) counts as a look.
    """
    if not isinstance(data, dict) or data.get("extractor") != extractor:
        return False
    if data.get("split") == SPLIT:
        return True
    datasets = data.get("datasets")
    if not isinstance(datasets, dict):
        return False
    for name, ds in datasets.items():
        if name == SPLIT:
            return True
        if not isinstance(ds, dict):
            continue
        if str(ds.get("path", "")).endswith(f"{SPLIT}.jsonl"):
            return True
        records = ds.get("records")
        if isinstance(records, dict) and ids & records.keys():
            return True
    return False


def find_earlier_look(extractor: str, dirs: list[Path], ids: frozenset[str] = frozenset()) -> Path | None:
    """The first file in dirs that shows this extractor already looked at holdout_v3, if any.

    Besides parseable results files this counts a `<extractor>_*.look` marker (a run started,
    whatever happened next) and an unparseable `<extractor>_*` JSON file (a save killed half-way
    can't be proven not to be a look).
    """
    prefix = f"{extractor}_"
    for directory in dirs:
        for p in _all_files(directory):
            name = p.name.lower()
            if name.startswith(prefix) and name.endswith(".look"):
                return p
            if ".json" not in name:
                continue
            data = _load_json(p)
            if data is None and name.startswith(prefix):
                return p
            if is_look(data, extractor, ids):
                return p
    return None


def find_cached_post(texts: list[str], prompt: str, dirs: list[Path]) -> Path | None:
    """The first cache entry for a holdout_v3 post text, if any.

    Two independent tests, because either alone can be fooled: the filename is the cache key
    (only matches the models and prompt we know of), the stored text_sha256 matches any model
    or prompt but needs a parseable entry.
    """
    models = {config.load_settings().deal_model, config.DEFAULT_MODEL}
    names = {f"{ev.cache_key(m, prompt, t)}.json" for m in models for t in texts}
    hashes = {ev.text_sha256(t) for t in texts}
    for directory in dirs:
        for p in _json_files(directory):
            if p.name in names:
                return p
            data = _load_json(p)
            if isinstance(data, dict) and data.get("text_sha256") in hashes:
                return p
    return None


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Flags per PROTOCOL.md; argparse's own exit 2 doubles as the usage-error code."""
    ap = argparse.ArgumentParser(description="One look at data/holdout_v3.jsonl (see tools/final_eval/PROTOCOL.md).")
    ap.add_argument("--extractor", choices=["regex", "claude"], required=True)
    ap.add_argument(
        "--expect-prompt", default=DEFAULT_PROMPT, help=f"required Claude PROMPT_VERSION ({DEFAULT_PROMPT})"
    )
    ap.add_argument("--results-dir", type=Path, default=ev.RESULTS_DIR, help="where the .json + .md are written")
    ap.add_argument("--cache-dir", type=Path, default=ev.CACHE_DIR, help="raw-output cache to inspect (never written)")
    ap.add_argument("--allow-second-look", action="store_true", help="skip the one-look refusal (owner approval only)")
    ap.add_argument(
        "--expect-model",
        default=None,
        help="required Claude model id, e.g. claude-haiku-5-5 (PROTOCOL.md always passes it; a DEAL_MODEL "
        "override would otherwise change the model silently)",
    )
    return ap.parse_args(argv)


def check_preconditions(args: argparse.Namespace, hooks: Hooks, posts: list[JSONObject]) -> GitState:
    """Raise Refusal on the first failed rule, in PROTOCOL.md's order; nothing has run yet.

    The order puts cheap, obvious mistakes (dirty tree, wrong branch) first so the message
    names the thing to fix, and the one-look and cache scans last.
    """
    git = hooks.git_state()
    if git.dirty:
        raise Refusal("dirty", "the working tree has uncommitted or untracked changes; commit or remove them first")
    if git.branch != "main":
        raise Refusal("branch", f"HEAD is {git.branch or 'unknown'!r}, not the branch 'main'; run `git switch main`")
    if args.extractor == "claude":
        actual = hooks.prompt_version()
        if actual != args.expect_prompt:
            raise Refusal("prompt", f"Claude PROMPT_VERSION is {actual!r}, expected {args.expect_prompt!r}")
        if not hooks.has_api_key():
            raise Refusal("key", "no Anthropic API key is configured (set it in the shell or .env); nothing was run")
    if not args.allow_second_look:
        ids = frozenset(str(p["id"]) for p in posts)
        earlier = find_earlier_look(args.extractor, [args.results_dir, *hooks.guard_results_dirs], ids)
        where = str(earlier) if earlier is not None else next(iter(hooks.history_looks(args.extractor, ids)), None)
        if where is not None:
            raise Refusal(
                "one-look",
                f"{where} already records a {SPLIT} look for {args.extractor}; a second run needs the "
                "owner's written approval and --allow-second-look",
            )
    # A look written outside the guarded dirs would be invisible to the next run's guard.
    target = Path(args.results_dir).resolve()
    guards = [Path(d).resolve() for d in hooks.guard_results_dirs]
    if guards and not any(target.is_relative_to(g) for g in guards):
        raise Refusal("results-dir", f"{args.results_dir} is outside {', '.join(map(str, guards))}")
    cached = find_cached_post(
        [str(p["text"]) for p in posts], args.expect_prompt, [args.cache_dir, *hooks.guard_cache_dirs]
    )
    if cached is not None:
        raise Refusal("cache", f"{cached} is a cached extractor output for a {SPLIT} post, so it was already seen")
    return git


def write_marker(args: argparse.Namespace, stamp: str, git: GitState, data_sha256: str) -> Path:
    """Create `<extractor>_<stamp>.look` before the first extractor call, never overwriting.

    Why: if the process dies before eval.save (kill -9, disk full), there would otherwise be no
    trace on disk and the guard would allow a silent rerun. The marker is committed with the results.
    """
    directory = Path(args.results_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "extractor": args.extractor,
        "split": SPLIT,
        "timestamp": stamp,
        "git": {"commit": git.commit, "dirty": git.dirty},
        "data_sha256": data_sha256,
        "second_look": bool(args.allow_second_look),
        "note": "a look at holdout_v3 started; the results file with the same extractor and timestamp records it",
    }
    for i in range(1, 1000):
        path = directory / (f"{args.extractor}_{stamp}.look" if i == 1 else f"{args.extractor}_{stamp}-{i}.look")
        try:
            with path.open("x", encoding="utf-8") as f:
                f.write(json.dumps(payload, indent=2))
        except FileExistsError:
            continue
        return path
    raise OSError(f"no free marker name for {args.extractor}_{stamp}")


def title(extractor: str, second_look: bool, completed: int, total: int, complete: bool) -> str:
    """The .md heading. markdown() would call any non-'all' split "holdout not scored", which is
    the opposite of what this file is; a second or incomplete look says so in the heading."""
    line = f"# Final eval: {extractor} on {SPLIT}"
    if not second_look and complete:
        return f"{line} (first and only look)"
    if second_look:
        line += " (SECOND look)"
    if not complete:
        line += f" (INCOMPLETE: {completed} of {total} posts)"
    return line


def main(argv: list[str] | None = None, hooks: Hooks | None = None) -> int:
    """Refuse or run once. Returns the exit code instead of exiting so tests can drive it."""
    args = parse_args(argv)
    hooks = default_hooks() if hooks is None else hooks
    secret = hooks.secret()

    def err(msg: str) -> None:
        print(redact(msg, secret), file=sys.stderr)

    try:
        posts = load_posts(hooks.data_path)
        data_sha256 = ev.file_sha256(hooks.data_path)
    except (OSError, ValueError) as e:
        err(f"ERROR: can't read {hooks.data_path}: {type(e).__name__}: {e}")
        return 2

    try:
        git = check_preconditions(args, hooks, posts)
        extractor, meta = hooks.make_extractor(args.extractor)
        if args.extractor == "claude" and meta.get("prompt_version") != args.expect_prompt:
            raise Refusal(
                "prompt",
                f"the built extractor reports prompt {meta.get('prompt_version')!r}, expected {args.expect_prompt!r}",
            )
        if args.extractor == "claude" and args.expect_model is not None and meta.get("model") != args.expect_model:
            raise Refusal("model", f"the built extractor uses {meta.get('model')!r}, expected {args.expect_model!r}")
    except Refusal as r:
        err(f"REFUSED: {r.rule}: {r.explanation}")
        return 2
    except Exception as e:  # building the extractor failed: nothing ran, nothing to write
        err(f"ERROR: could not build the {args.extractor} extractor: {type(e).__name__}: {e}")
        return 2

    stamp = ev.timestamp()
    try:
        marker = write_marker(args, stamp, git, data_sha256)
    except OSError as e:
        err(f"ERROR: can't write the look marker in {args.results_dir}: {type(e).__name__}: {e}")
        return 2
    records: dict[str, JSONObject] = {}
    failure: BaseException | None = None
    try:
        for p in posts:  # one at a time, so a crash keeps every finished record
            records[str(p["id"])] = ev.run_post(extractor, str(p["text"]), None)
    except BaseException as e:  # Ctrl-C and SystemExit too: a partial run is still a look
        failure = e

    try:
        done = [p for p in posts if str(p["id"]) in records]
        complete = failure is None
        datasets: JSONObject = {
            SPLIT: {"path": dataset_path_string(hooks.data_path), "records": records, "scores": score(done, records)}
        }
        model = meta.get("model") if args.extractor == "claude" else None
        result: JSONObject = {
            "extractor": args.extractor,
            "model": model,
            "prompt_version": meta.get("prompt_version") if args.extractor == "claude" else None,
            "split": SPLIT,
            "timestamp": stamp,
            "git": {"commit": git.commit, "dirty": git.dirty},
            "scoring": ev.SCORING_NOTE,
            "repeat": {"index": 1, "of": 1},
            "cache_enabled": False,
            "usage": ev.usage_totals(datasets, model),
            "final_eval": {
                "protocol": PROTOCOL,
                "branch": git.branch,
                "data_sha256": data_sha256,
                "second_look": bool(args.allow_second_look),
                "expected_model": args.expect_model,
                "marker": marker.name,
                "complete": complete,
                "aborted": None
                if failure is None
                else {"completed": len(done), "total": len(posts), "error_type": type(failure).__name__},
            },
            "datasets": datasets,
        }
        result = _redact_obj(result, secret)
        json_path = ev.save(result, args.results_dir)
        md_lines = ev.markdown(result).split("\n")
        md_lines[0] = title(args.extractor, bool(args.allow_second_look), len(done), len(posts), complete)
        md = "\n".join(md_lines)
        json_path.with_suffix(".md").write_text(md, encoding="utf-8")

    except Exception as e:  # the look happened; the marker stays as its evidence
        err(f"ERROR: the run finished but saving failed ({marker} records the look): {type(e).__name__}: {e}")
        return 1

    print(redact(md, secret))
    print(redact(f"saved {json_path}", secret))
    if failure is not None:
        err(f"ABORTED: {type(failure).__name__}: {failure}")
        return 1
    return 0


if __name__ == "__main__":
    config.load_dotenv_file()
    sys.exit(main())
