"""Contract tests for tools/final_eval/{run,validate,tables}.py (see tools/final_eval/PROTOCOL.md).

Blindness rule: these tests were written from PROTOCOL.md, the frozen check_success.py and the
shared deal_board modules only, never from the three scripts' source. They pin the contract, not
an implementation, so a failure here means one of the two disagrees with PROTOCOL.md.

Why fakes: data/holdout_v3.jsonl is the one-look final set, so no test may run any extractor on it
(not even the free regex baseline), touch the API, read .env, or write under results/ or .cache/.
Every run.py test drives `main(argv, hooks)` with a fake extractor on 25 synthetic posts written to
tmp_path (19 deal / 6 non-deal, like the real set), fake git/key/prompt/secret hooks, and tmp
results/cache dirs. tables.py tests may read the two committed holdout-v2 results files, since
tables only re-scores saved outputs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import math
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from deal_board import config
from deal_board.cli import eval as ev
from deal_board.schema import DealRequirement, JSONObject
from deal_board.scoring import SCORED_FIELDS, exact_mcnemar_p, score

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "final_eval"
PROMPT = "ac1a145b"
SECRET = "sk-ant-FAKE-test-secret-7f3c19"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
V2_REGEX = ROOT / "results" / "regex_20261024T224817Z.json"
V2_CLAUDE = ROOT / "results" / "claude_20261024T234359Z.json"
N_POSTS, N_DEAL = 25, 19
CHECK_ORDER = [
    "extractor",
    "split",
    "datasets",
    "records",
    "errors",
    "complete",
    "first_look",
    "labels",
    "provenance",
    "git_clean",
    "git_ancestor",
    "prompt",
    "model",
    "raw_consistent",
    "scores",
    "intervals",
    "non_deal",
]


# ---------------------------------------------------------------------------------------------
# Loading the scripts by path (they are not a package on sys.path; pytest runs in importlib mode)
# ---------------------------------------------------------------------------------------------


def _load(name: str) -> ModuleType:
    """Load tools/final_eval/<name>.py by path, registered in sys.modules first (dataclasses need it)."""
    spec = importlib.util.spec_from_file_location(f"final_eval_test_{name}", TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run_mod() -> ModuleType:
    return _load("run")


@pytest.fixture(scope="module")
def validate_mod() -> ModuleType:
    return _load("validate")


@pytest.fixture(scope="module")
def tables_mod() -> ModuleType:
    return _load("tables")


# ---------------------------------------------------------------------------------------------
# Synthetic data and fakes
# ---------------------------------------------------------------------------------------------


def make_posts() -> list[JSONObject]:
    """25 synthetic posts, t-01..t-19 deal requests and t-20..t-25 non-deal; timeline always null."""
    posts: list[JSONObject] = []
    for i in range(1, N_POSTS + 1):
        deal = i <= N_DEAL
        label: JSONObject = {
            "is_deal_request": deal,
            "state": "TX" if deal else None,
            "city": "Austin" if deal else None,
            "property_type": "single_family" if deal else None,
            "transaction_type": "buy" if deal else None,
            "deal_value_usd": 100_000 * i if deal else None,
            "timeline": None,
        }
        text = f"synthetic final-eval test post {i}: " + ("need a buyer agent" if deal else "congrats to the team")
        posts.append({"id": f"t-{i:02d}", "split": "holdout_v3", "text": text, "label": label})
    return posts


@dataclass
class Env:
    tmp: Path
    data: Path
    results: Path
    cache: Path
    posts: list[JSONObject]

    @property
    def by_text(self) -> dict[str, JSONObject]:
        return {p["text"]: p for p in self.posts}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.delenv("DEAL_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    posts = make_posts()
    data = tmp_path / "synthetic_holdout_v3.jsonl"
    data.write_text("".join(json.dumps(p) + "\n" for p in posts))
    return Env(tmp_path, data, tmp_path / "results", tmp_path / "cache", posts)


class FakeExtractor:
    """Returns each post's label as its raw output; can crash on the n-th call or mutate outputs."""

    name = "fake"

    def __init__(
        self,
        env: Env,
        *,
        fail_on: int | None = None,
        exc: Callable[[], BaseException] = lambda: ConnectionError("connection reset"),
        mutate: Callable[[str, JSONObject], JSONObject] | None = None,
    ) -> None:
        self.by_text = env.by_text
        self.fail_on, self.exc, self.mutate = fail_on, exc, mutate
        self.calls: list[str] = []

    def extract_raw(self, text: str) -> dict[str, object]:
        self.calls.append(text)
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise self.exc()
        post = self.by_text[text]
        raw: JSONObject = {**post["label"], "notes": None}
        return self.mutate(post["id"], raw) if self.mutate else raw

    def extract(self, text: str) -> DealRequirement:
        raise AssertionError("run.py should call extract_raw on a raw-capable extractor")


@dataclass
class Factory:
    """hooks.make_extractor: records every build, gives claude meta or regex's {}."""

    extractor: FakeExtractor
    prompt_version: str = PROMPT
    built: list[str] = field(default_factory=list)

    def __call__(self, name: str) -> tuple[FakeExtractor, JSONObject]:
        self.built.append(name)
        meta: JSONObject = {} if name == "regex" else {"model": "fake-model", "prompt_version": self.prompt_version}
        return self.extractor, meta


def make_hooks(
    run_mod: ModuleType,
    env: Env,
    factory: Factory,
    *,
    branch: str = "main",
    dirty: bool = False,
    key: bool = True,
    prompt: str = PROMPT,
    guard_results: tuple[Path, ...] = (),
    guard_cache: tuple[Path, ...] = (),
) -> object:
    return dataclasses.replace(
        run_mod.default_hooks(),
        make_extractor=factory,
        git_state=lambda: run_mod.GitState(branch=branch, commit=COMMIT, dirty=dirty),
        has_api_key=lambda: key,
        prompt_version=lambda: prompt,
        secret=lambda: SECRET,
        data_path=env.data,
        guard_results_dirs=guard_results,
        guard_cache_dirs=guard_cache,
    )


def argv(env: Env, extractor: str = "claude", *extra: str) -> list[str]:
    return [
        "--extractor",
        extractor,
        "--expect-prompt",
        PROMPT,
        "--results-dir",
        str(env.results),
        "--cache-dir",
        str(env.cache),
        *extra,
    ]


def call_run(run_mod: ModuleType, args: list[str], hooks: object) -> int:
    return int(run_mod.main(args, hooks))


def files_under(directory: Path) -> set[Path]:
    return {p for p in directory.rglob("*") if p.is_file()} if directory.exists() else set()


def result_jsons(env: Env) -> list[Path]:
    return sorted(env.results.glob("*.json"))


def load(path: Path) -> JSONObject:
    data: JSONObject = json.loads(path.read_text())
    return data


def good_run(run_mod: ModuleType, env: Env, extractor: str = "claude", fake: FakeExtractor | None = None) -> Path:
    """One run.py run with fakes; returns the written results JSON."""
    factory = Factory(fake or FakeExtractor(env))
    rc = call_run(run_mod, argv(env, extractor), make_hooks(run_mod, env, factory))
    assert rc == 0
    (path,) = result_jsons(env)
    return path


def refusal_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith("REFUSED: ")]


def assert_refused(
    capsys: pytest.CaptureFixture[str], rc: int, rule: str, env: Env, before: set[Path], factory: Factory
) -> None:
    """Exit 2, one `REFUSED: ` line naming the rule, nothing written, extractor never called."""
    err = capsys.readouterr().err
    assert rc == 2
    lines = refusal_lines(err)
    assert len(lines) == 1, err
    assert rule in lines[0]
    assert files_under(env.results) == before
    assert factory.extractor.calls == []


# ---------------------------------------------------------------------------------------------
# run.py: happy path
# ---------------------------------------------------------------------------------------------


def test_run_happy_path_writes_one_final_eval_file(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean claude run writes exactly one <extractor>_<stamp>.json + .md with every contract field."""
    path = good_run(run_mod, env)
    out = capsys.readouterr().out
    assert re.fullmatch(r"claude_\d{8}T\d{6}Z(-\d+)?\.json", path.name)
    # The .look marker (written before the first call) shares the stem; see the review regressions.
    assert sorted(p.name for p in env.results.iterdir()) == sorted(
        [path.name, path.with_suffix(".md").name, path.with_suffix(".look").name]
    )
    saved = load(path)
    assert saved["extractor"] == "claude"
    assert saved["model"] == "fake-model" and saved["prompt_version"] == PROMPT
    assert saved["split"] == "holdout_v3"
    assert set(saved["datasets"]) == {"holdout_v3"}
    assert saved["git"] == {"commit": COMMIT, "dirty": False}
    assert saved["scoring"] == ev.SCORING_NOTE
    assert saved["repeat"] == {"index": 1, "of": 1}
    assert saved["cache_enabled"] is False
    assert path.stem.startswith(f"claude_{saved['timestamp']}")
    assert out.rstrip().splitlines()[-1] == f"saved {path}"


def test_run_records_and_scores_match_run_post_and_score(run_mod: ModuleType, env: Env) -> None:
    """Records are run_post's shape for all 25 posts and scores equal scoring.score on them."""
    ds = load(good_run(run_mod, env))["datasets"]["holdout_v3"]
    assert Path(ds["path"]).resolve() == env.data.resolve()
    records = ds["records"]
    assert sorted(records) == [p["id"] for p in env.posts]
    assert all(set(r) == {"raw", "prediction", "error", "call"} for r in records.values())
    assert all(r["error"] is None for r in records.values())
    assert ds["scores"] == json.loads(json.dumps(score(env.posts, records)))


def test_run_final_eval_block(run_mod: ModuleType, env: Env) -> None:
    """final_eval names the protocol, branch, dataset sha256, and a complete first look."""
    fe = load(good_run(run_mod, env))["final_eval"]
    assert fe == {
        "protocol": "tools/final_eval/PROTOCOL.md",
        "branch": "main",
        "data_sha256": hashlib.sha256(env.data.read_bytes()).hexdigest(),
        "second_look": False,
        "expected_model": None,
        "marker": path_stem_look(fe),
        "complete": True,
        "aborted": None,
    }


def path_stem_look(fe: dict[str, object]) -> str:
    """The marker name the run reported; its shape is checked by the review regression tests."""
    name = fe["marker"]
    assert isinstance(name, str) and name.startswith("claude_") and name.endswith(".look")
    return name


def test_run_usage_is_usage_totals(run_mod: ModuleType, env: Env) -> None:
    """usage is present and equals eval.usage_totals over the saved datasets."""
    saved = load(good_run(run_mod, env))
    assert saved["usage"] == ev.usage_totals(saved["datasets"], saved["model"])


def test_run_markdown_title_replaced(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """The .md is eval.markdown(result) with the final-eval title, not 'holdout not scored'."""
    path = good_run(run_mod, env)
    out = capsys.readouterr().out
    md = path.with_suffix(".md").read_text()
    lines = md.splitlines()
    assert lines[0] == "# Final eval: claude on holdout_v3 (first and only look)"
    assert "holdout not scored" not in md
    assert lines[1:] == ev.markdown(load(path)).splitlines()[1:]
    assert lines[0] in out


def test_run_never_creates_or_writes_cache(run_mod: ModuleType, env: Env) -> None:
    """The cache dir is never written: absent or empty after a run."""
    good_run(run_mod, env)
    assert not env.cache.exists() or not any(env.cache.iterdir())


def test_run_regex_records_null_model_and_prompt(run_mod: ModuleType, env: Env) -> None:
    """A regex run (meta {}) records model and prompt_version null and needs no key."""
    factory = Factory(FakeExtractor(env))
    hooks = make_hooks(run_mod, env, factory, key=False, prompt="something-else")
    assert call_run(run_mod, argv(env, "regex"), hooks) == 0
    (path,) = result_jsons(env)
    saved = load(path)
    assert path.name.startswith("regex_")
    assert saved["extractor"] == "regex"
    assert saved["model"] is None and saved["prompt_version"] is None
    assert path.with_suffix(".md").read_text().splitlines()[0] == (
        "# Final eval: regex on holdout_v3 (first and only look)"
    )


# ---------------------------------------------------------------------------------------------
# run.py: refusals
# ---------------------------------------------------------------------------------------------


def test_refuse_dirty_tree(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 2: a dirty tree is refused before the extractor is built."""
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory, dirty=True))
    assert_refused(capsys, rc, "dirty", env, set(), factory)
    assert factory.built == []


@pytest.mark.parametrize("branch", ["final-eval-tools", "HEAD"])
def test_refuse_not_on_main(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str], branch: str) -> None:
    """Rule 2: a branch other than main, including detached HEAD, is refused."""
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory, branch=branch))
    assert_refused(capsys, rc, "branch", env, set(), factory)
    assert factory.built == []


def test_refuse_prompt_hook_mismatch(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """Claude's PROMPT_VERSION must equal --expect-prompt."""
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory, prompt="deadbeef"))
    assert_refused(capsys, rc, "prompt", env, set(), factory)
    assert factory.built == []


def test_refuse_meta_prompt_mismatch(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """A built claude extractor whose meta prompt_version differs is refused before any call."""
    factory = Factory(FakeExtractor(env), prompt_version="deadbeef")
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory))
    assert_refused(capsys, rc, "prompt", env, set(), factory)


def test_refuse_claude_without_key(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """Claude without an API key is refused."""
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory, key=False))
    assert_refused(capsys, rc, "key", env, set(), factory)
    assert factory.built == []


def test_refusals_follow_contract_order(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """With every rule broken, the first check (dirty) is the one reported."""
    env.results.mkdir()
    (env.results / "old.json").write_text(json.dumps({"extractor": "claude", "split": "holdout_v3"}))
    factory = Factory(FakeExtractor(env))
    hooks = make_hooks(run_mod, env, factory, dirty=True, branch="HEAD", prompt="x", key=False)
    rc = call_run(run_mod, argv(env), hooks)
    assert_refused(capsys, rc, "dirty", env, files_under(env.results), factory)


def _write_prior(directory: Path, data: JSONObject, name: str = "claude_20261025T000000Z.json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(data))
    return path


def test_one_look_refuses_existing_file_in_results_dir(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 1: a holdout_v3 file for the same extractor in --results-dir blocks a second run."""
    _write_prior(env.results, {"extractor": "claude", "split": "holdout_v3"})
    before = files_under(env.results)
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory))
    assert_refused(capsys, rc, "one-look", env, before, factory)
    assert factory.built == []


def test_one_look_searches_results_dir_recursively(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 1: the search is recursive, so a file in a subdirectory still counts."""
    _write_prior(env.results / "archive" / "deep", {"extractor": "claude", "split": "holdout_v3"}, "x.json")
    before = files_under(env.results)
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory))
    assert_refused(capsys, rc, "one-look", env, before, factory)


def test_one_look_guard_dir_cannot_be_bypassed(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 1: a file only in a guard dir (the repo's results/) blocks a run into a different --results-dir."""
    guard = env.tmp / "repo_results"
    _write_prior(guard, {"extractor": "claude", "split": "holdout_v3"})
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory, guard_results=(guard,)))
    assert_refused(capsys, rc, "one-look", env, set(), factory)


def test_one_look_detects_datasets_key(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 1: a 'holdout_v3' key in datasets counts even when split says otherwise."""
    _write_prior(env.results, {"extractor": "claude", "split": "all", "datasets": {"holdout_v3": {}}})
    before = files_under(env.results)
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory))
    assert_refused(capsys, rc, "one-look", env, before, factory)


def test_one_look_is_per_extractor(run_mod: ModuleType, env: Env) -> None:
    """Rule 1 is per extractor: an existing Claude holdout_v3 file does not block the regex run."""
    prior = _write_prior(env.results, {"extractor": "claude", "split": "holdout_v3"})
    factory = Factory(FakeExtractor(env))
    assert call_run(run_mod, argv(env, "regex"), make_hooks(run_mod, env, factory)) == 0
    new = [p for p in result_jsons(env) if p != prior]
    assert len(new) == 1 and new[0].name.startswith("regex_")


def test_one_look_ignores_other_sets_and_junk(run_mod: ModuleType, env: Env) -> None:
    """Rule 1 ignores holdout-v2 files, non-JSON and non-object files."""
    _write_prior(env.results, {"extractor": "claude", "split": "all", "datasets": {"holdout": {}}}, "v2.json")
    (env.results / "junk.json").write_text("not json {")
    (env.results / "list.json").write_text("[1, 2]")
    factory = Factory(FakeExtractor(env))
    assert call_run(run_mod, argv(env), make_hooks(run_mod, env, factory)) == 0
    assert len(factory.extractor.calls) == N_POSTS


def test_allow_second_look_permits_and_is_recorded(run_mod: ModuleType, env: Env) -> None:
    """--allow-second-look skips rule 1 and the file records second_look true and a SECOND look title."""
    prior = _write_prior(env.results, {"extractor": "claude", "split": "holdout_v3"})
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env, "claude", "--allow-second-look"), make_hooks(run_mod, env, factory))
    assert rc == 0
    (path,) = [p for p in result_jsons(env) if p != prior]
    assert load(path)["final_eval"]["second_look"] is True
    assert path.with_suffix(".md").read_text().splitlines()[0] == "# Final eval: claude on holdout_v3 (SECOND look)"


def test_second_look_defaults_off(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """--allow-second-look is off by default: a second plain run after a first is refused."""
    good_run(run_mod, env)
    capsys.readouterr()
    before = files_under(env.results)
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory))
    assert_refused(capsys, rc, "one-look", env, before, factory)


def test_two_second_looks_never_overwrite(run_mod: ModuleType, env: Env) -> None:
    """Two allowed runs (likely the same second) get distinct stems via eval.save's -2 suffix."""
    for _ in range(2):
        factory = Factory(FakeExtractor(env))
        rc = call_run(run_mod, argv(env, "claude", "--allow-second-look"), make_hooks(run_mod, env, factory))
        assert rc == 0
    jsons = result_jsons(env)
    assert len(jsons) == 2 and len(list(env.results.glob("*.md"))) == 2
    stamps = [load(p)["timestamp"] for p in jsons]
    if stamps[0] == stamps[1]:
        assert {p.stem for p in jsons} == {f"claude_{stamps[0]}", f"claude_{stamps[0]}-2"}


# ---------------------------------------------------------------------------------------------
# run.py: cache rule
# ---------------------------------------------------------------------------------------------


def test_cache_refuses_entry_named_by_cache_key(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 3: a <cache_key(DEFAULT_MODEL, prompt, text)>.json for a holdout post refuses the run."""
    env.cache.mkdir()
    key = ev.cache_key(config.DEFAULT_MODEL, PROMPT, env.posts[4]["text"])
    (env.cache / f"{key}.json").write_text("{}")
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory))
    assert_refused(capsys, rc, "cache", env, set(), factory)
    assert factory.built == []


def test_cache_key_uses_configured_deal_model(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 3: the name check also uses the configured DEAL_MODEL."""
    monkeypatch.setenv("DEAL_MODEL", "claude-custom-test-model")
    env.cache.mkdir()
    key = ev.cache_key("claude-custom-test-model", PROMPT, env.posts[0]["text"])
    (env.cache / f"{key}.json").write_text("{}")
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory))
    assert_refused(capsys, rc, "cache", env, set(), factory)


def test_cache_refuses_text_sha256_anywhere(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 3: any *.json (subdir, other name) whose text_sha256 matches a post text refuses, for regex too."""
    sub = env.cache / "old" / "nested"
    sub.mkdir(parents=True)
    (sub / "whatever.json").write_text(json.dumps({"text_sha256": ev.text_sha256(env.posts[22]["text"])}))
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env, "regex"), make_hooks(run_mod, env, factory))
    assert_refused(capsys, rc, "cache", env, set(), factory)


def test_cache_guard_dir_cannot_be_bypassed(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 3: an entry only in a guard cache dir refuses even with an empty --cache-dir."""
    guard = env.tmp / "repo_cache"
    guard.mkdir()
    (guard / "e.json").write_text(json.dumps({"text_sha256": ev.text_sha256(env.posts[1]["text"])}))
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory, guard_cache=(guard,)))
    assert_refused(capsys, rc, "cache", env, set(), factory)


def test_cache_unrelated_entry_allowed_and_untouched(run_mod: ModuleType, env: Env) -> None:
    """Rule 3: an entry for an unrelated text does not refuse, and is neither modified nor joined by new files."""
    env.cache.mkdir()
    other = "a post that is not in the holdout"
    entry = env.cache / f"{ev.cache_key(config.DEFAULT_MODEL, PROMPT, other)}.json"
    entry.write_text(json.dumps({"text_sha256": ev.text_sha256(other), "raw": None}))
    before = {p: p.read_bytes() for p in files_under(env.cache)}
    mtime = entry.stat().st_mtime_ns
    good_run(run_mod, env)
    assert {p: p.read_bytes() for p in files_under(env.cache)} == before
    assert entry.stat().st_mtime_ns == mtime


# ---------------------------------------------------------------------------------------------
# run.py: aborted runs and the secret
# ---------------------------------------------------------------------------------------------


def test_crash_writes_partial_file_and_exits_1(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 5: a crash on post 3 keeps posts 1-2 in a partial file marked incomplete, exit 1."""
    factory = Factory(FakeExtractor(env, fail_on=3))
    assert call_run(run_mod, argv(env), make_hooks(run_mod, env, factory)) == 1
    (path,) = result_jsons(env)
    saved = load(path)
    assert saved["final_eval"]["complete"] is False
    assert saved["final_eval"]["aborted"] == {"completed": 2, "total": N_POSTS, "error_type": "ConnectionError"}
    assert sorted(saved["datasets"]["holdout_v3"]["records"]) == ["t-01", "t-02"]
    assert path.with_suffix(".md").read_text().splitlines()[0] == (
        f"# Final eval: claude on holdout_v3 (INCOMPLETE: 2 of {N_POSTS} posts)"
    )


def test_partial_file_still_counts_as_a_look(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 5: after a crashed run, the next run is refused by the one-look rule."""
    call_run(run_mod, argv(env), make_hooks(run_mod, env, Factory(FakeExtractor(env, fail_on=3))))
    capsys.readouterr()
    before = files_under(env.results)
    factory = Factory(FakeExtractor(env))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory))
    assert_refused(capsys, rc, "one-look", env, before, factory)


def test_ctrl_c_is_an_aborted_look(run_mod: ModuleType, env: Env) -> None:
    """Rule 5: Ctrl-C part-way also writes a partial file and exits 1."""
    factory = Factory(FakeExtractor(env, fail_on=3, exc=KeyboardInterrupt))
    try:
        rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, factory))
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped run.main instead of producing a partial file")
    assert rc == 1
    (path,) = result_jsons(env)
    assert load(path)["final_eval"]["aborted"]["error_type"] == "KeyboardInterrupt"


def test_secret_never_printed_or_saved(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4: an exception message carrying the key is redacted in stdout, stderr and the files."""
    fake = FakeExtractor(env, fail_on=3, exc=lambda: ConnectionError(f"401 invalid x-api-key: {SECRET}"))
    rc = call_run(run_mod, argv(env), make_hooks(run_mod, env, Factory(fake)))
    captured = capsys.readouterr()
    assert rc == 1
    assert SECRET not in captured.out and SECRET not in captured.err
    assert "ConnectionError" in captured.out + captured.err
    for p in files_under(env.results):
        assert SECRET not in p.read_text()


# ---------------------------------------------------------------------------------------------
# validate.py
# ---------------------------------------------------------------------------------------------


@dataclass
class Validated:
    rc: int
    lines: list[str]
    out: str

    @property
    def fails(self) -> list[str]:
        return [line.split()[1].rstrip(":") for line in self.lines if line.startswith("FAIL ")]

    def line(self, name: str) -> str:
        (match,) = [x for x in self.lines if x.startswith((f"PASS {name}:", f"FAIL {name}:"))]
        return match


def vhooks(validate_mod: ModuleType, env: Env, ancestor: bool = True) -> object:
    return dataclasses.replace(validate_mod.default_hooks(), data_path=env.data, is_ancestor=lambda commit: ancestor)


def validate_file(
    validate_mod: ModuleType,
    capsys: pytest.CaptureFixture[str],
    path: Path,
    hooks: object,
    *extra: str,
) -> Validated:
    capsys.readouterr()
    rc = int(validate_mod.main([str(path), *extra], hooks))
    cap = capsys.readouterr()
    out = cap.out + cap.err
    return Validated(rc, out.splitlines(), out)


@pytest.fixture
def good_file(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> Path:
    path = good_run(run_mod, env)
    capsys.readouterr()
    return path


def variant(env: Env, good: Path, change: Callable[[JSONObject], None]) -> Path:
    data = load(good)
    change(data)
    path = env.tmp / "variant.json"
    path.write_text(json.dumps(data))
    return path


def test_validate_good_file(
    validate_mod: ModuleType, env: Env, good_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean run.py file passes every check, in contract order, and prints VALID."""
    commits: list[str] = []

    def is_ancestor(commit: str) -> bool:
        commits.append(commit)
        return True

    hooks = dataclasses.replace(validate_mod.default_hooks(), data_path=env.data, is_ancestor=is_ancestor)
    v = validate_file(validate_mod, capsys, good_file, hooks)
    assert v.rc == 0, v.out
    assert [x.split()[1].rstrip(":") for x in v.lines if x.startswith("PASS ")] == CHECK_ORDER
    assert v.fails == []
    assert v.lines[-1] == "VALID"
    assert commits == [COMMIT]


def test_validate_good_regex_file(
    run_mod: ModuleType, validate_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean regex file (prompt_version null) is VALID."""
    path = good_run(run_mod, env, "regex")
    v = validate_file(validate_mod, capsys, path, vhooks(validate_mod, env))
    assert v.rc == 0, v.out


def _set(path: tuple[str, ...], value: object) -> Callable[[JSONObject], None]:
    def change(data: JSONObject) -> None:
        target = data
        for k in path[:-1]:
            target = target[k]
        target[path[-1]] = value

    return change


def _del(path: tuple[str, ...]) -> Callable[[JSONObject], None]:
    def change(data: JSONObject) -> None:
        target = data
        for k in path[:-1]:
            target = target[k]
        del target[path[-1]]

    return change


def _extra_dataset(data: JSONObject) -> None:
    data["datasets"]["posts"] = data["datasets"]["holdout_v3"]


def _error_record(data: JSONObject) -> None:
    data["datasets"]["holdout_v3"]["records"]["t-07"]["error"] = "invalid output: boom"


@pytest.mark.parametrize(
    ("change", "check"),
    [
        (_set(("git", "dirty"), True), "git_clean"),
        (_del(("git", "dirty")), "git_clean"),
        (_del(("datasets", "holdout_v3", "records", "t-25")), "records"),
        (_error_record, "errors"),
        (_set(("split",), "holdout"), "split"),
        (_extra_dataset, "datasets"),
        (_set(("datasets", "holdout_v3", "path"), "data/holdout_v3.jsonl"), "datasets"),
        (_set(("extractor",), "fake"), "extractor"),
        (_set(("prompt_version",), "deadbeef"), "prompt"),
        (_set(("datasets", "holdout_v3", "scores", "fields", "state", "wrong"), 99), "scores"),
        (_set(("datasets", "holdout_v3", "scores", "fields", "city", "ci95"), None), "intervals"),
        (_set(("final_eval", "complete"), False), "complete"),
        (_set(("final_eval", "aborted"), {"completed": 24, "total": 25, "error_type": "X"}), "complete"),
    ],
    ids=[
        "dirty-true",
        "dirty-missing",
        "24-records",
        "error-record",
        "wrong-split",
        "extra-dataset",
        "wrong-dataset-path",
        "bad-extractor",
        "wrong-prompt",
        "tampered-score",
        "missing-ci95",
        "incomplete",
        "aborted",
    ],
)
def test_validate_rejects_variant(
    validate_mod: ModuleType,
    env: Env,
    good_file: Path,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[JSONObject], None],
    check: str,
) -> None:
    """Each broken property fails its named check, exit 1, INVALID summary."""
    v = validate_file(validate_mod, capsys, variant(env, good_file, change), vhooks(validate_mod, env))
    assert v.rc == 1, v.out
    assert check in v.fails, v.out
    assert v.lines[-1] == f"INVALID ({len(v.fails)} failed)"


def test_validate_expect_prompt_flag(
    validate_mod: ModuleType, env: Env, good_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--expect-prompt different from the file's claude prompt fails `prompt`."""
    v = validate_file(validate_mod, capsys, good_file, vhooks(validate_mod, env), "--expect-prompt", "deadbeef")
    assert v.rc == 1 and v.fails == ["prompt"], v.out


def test_validate_regex_with_prompt_fails(
    run_mod: ModuleType, validate_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """A regex file must have prompt_version null."""
    path = variant(env, good_run(run_mod, env, "regex"), _set(("prompt_version",), PROMPT))
    v = validate_file(validate_mod, capsys, path, vhooks(validate_mod, env))
    assert v.rc == 1 and "prompt" in v.fails, v.out


def test_validate_not_ancestor(
    validate_mod: ModuleType, env: Env, good_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A commit that is not an ancestor of main fails git_ancestor."""
    v = validate_file(validate_mod, capsys, good_file, vhooks(validate_mod, env, ancestor=False))
    assert v.rc == 1 and v.fails == ["git_ancestor"], v.out


def test_validate_non_deal_guesses_reported(
    run_mod: ModuleType, validate_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """Filled fields on non-deal posts fail non_deal with the count; scores stay consistent."""

    def guess(pid: str, raw: JSONObject) -> JSONObject:
        return {**raw, "state": "TX"} if pid in ("t-20", "t-21") else raw

    path = good_run(run_mod, env, fake=FakeExtractor(env, mutate=guess))
    v = validate_file(validate_mod, capsys, path, vhooks(validate_mod, env))
    assert v.rc == 1
    assert v.fails == ["non_deal"], v.out
    assert "2 fields filled on non-deal posts" in v.line("non_deal")
    assert "t-20" in v.line("non_deal") and "t-21" in v.line("non_deal")


def test_validate_partial_run_file(
    run_mod: ModuleType, validate_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real partial file from a crashed run fails complete and records."""
    call_run(run_mod, argv(env), make_hooks(run_mod, env, Factory(FakeExtractor(env, fail_on=3))))
    (path,) = result_jsons(env)
    v = validate_file(validate_mod, capsys, path, vhooks(validate_mod, env))
    assert v.rc == 1
    assert {"complete", "records"} <= set(v.fails), v.out


@pytest.mark.parametrize("content", [None, "not json {", "[1, 2, 3]"], ids=["missing", "not-json", "not-object"])
def test_validate_unreadable_exits_2(
    validate_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str], content: str | None
) -> None:
    """A file that can't be read or isn't a JSON object exits 2 with one ERROR: line."""
    path = env.tmp / "bad.json"
    if content is not None:
        path.write_text(content)
    v = validate_file(validate_mod, capsys, path, vhooks(validate_mod, env))
    assert v.rc == 2
    assert sum(line.startswith("ERROR: ") for line in v.lines) == 1, v.out


# ---------------------------------------------------------------------------------------------
# tables.py
# ---------------------------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Independent Wilson score interval (not the scripts' helper)."""
    p, z2 = k / n, z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    lo = 0.0 if k == 0 else max(0.0, centre - half)
    hi = 1.0 if k == n else min(1.0, centre + half)
    return lo, hi


def acc_cell(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k / n:.0%} [{lo * 100:.0f}–{hi * 100:.0f}]"


def delta(r: float, c: float) -> str:
    return f"{round((c - r) * 100):+d}"


def table_rows(text: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows[cells[0]] = cells
    return rows


def sections(text: str) -> dict[str, str]:
    """Split output by '### ' headings: {'v3': ..., 'v2': ...}."""
    out: dict[str, str] = {}
    current = ""
    for line in text.splitlines(keepends=True):
        if line.startswith("### Final holdout v3"):
            current = "v3"
        elif line.startswith("### Earlier holdout v2"):
            current = "v2"
        if current:
            out[current] = out.get(current, "") + line
    return out


def call_tables(
    tables_mod: ModuleType, capsys: pytest.CaptureFixture[str], args: list[str | Path]
) -> tuple[int, str, str]:
    capsys.readouterr()
    rc = int(tables_mod.main([str(a) for a in args]))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def v3_result(
    env: Env,
    extractor: str,
    raws: dict[str, JSONObject | None],
    *,
    usage: bool = True,
    second_look: bool = False,
    dirty: bool = False,
    complete: bool = True,
) -> JSONObject:
    """A synthetic holdout_v3 results dict with an absolute label path in tmp_path."""
    claude = extractor == "claude"
    records: dict[str, JSONObject] = {}
    for p in env.posts:
        raw = raws.get(p["id"], {**p["label"], "notes": None})
        call = {"cached": False, "input_tokens": 1000 if claude else None, "output_tokens": 50 if claude else None}
        records[p["id"]] = {
            **ev.to_record(raw, None if raw is not None else "invalid output: no tool call"),
            "call": {**call, "latency_s": 1.5},
        }
    datasets: JSONObject = {
        "holdout_v3": {"path": str(env.data), "records": records, "scores": score(env.posts, records)}
    }
    model = "claude-haiku-5-5" if claude else None
    result: JSONObject = {
        "extractor": extractor,
        "model": model,
        "prompt_version": PROMPT if claude else None,
        "split": "holdout_v3",
        "timestamp": "20261025T120000Z" if claude else "20261025T110000Z",
        "git": {"commit": ("c1aude00" if claude else "4e9e0000") + "0" * 32, "dirty": dirty},
        "scoring": ev.SCORING_NOTE,
        "repeat": {"index": 1, "of": 1},
        "cache_enabled": False,
        "final_eval": {
            "protocol": "tools/final_eval/PROTOCOL.md",
            "branch": "main",
            "data_sha256": hashlib.sha256(env.data.read_bytes()).hexdigest(),
            "second_look": second_look,
            "complete": complete,
            "aborted": None if complete else {"completed": 25, "total": 25, "error_type": "X"},
        },
        "datasets": json.loads(json.dumps(datasets)),
    }
    if usage:
        result["usage"] = ev.usage_totals(result["datasets"], model)
    return result


def label_raw(env: Env, pid: str, **changes: object) -> JSONObject:
    (post,) = [p for p in env.posts if p["id"] == pid]
    return {**post["label"], "notes": None, **changes}


def regex_raws(env: Env) -> dict[str, JSONObject | None]:
    """Regex: state wrong on t-01..t-05, timeline guessed on t-01..t-03 (deal posts, label null)."""
    raws: dict[str, JSONObject | None] = {}
    for i in range(1, 6):
        pid = f"t-{i:02d}"
        raws[pid] = label_raw(env, pid, state="OK", **({"timeline": "within_30_days"} if i <= 3 else {}))
    return raws


def claude_raws(env: Env) -> dict[str, JSONObject | None]:
    """Claude: state wrong on t-06 only, and a city filled on non-deal post t-20."""
    return {"t-06": label_raw(env, "t-06", state="OK"), "t-20": label_raw(env, "t-20", city="Dallas")}


@dataclass
class V3Pair:
    regex: Path
    claude: Path
    claude_data: JSONObject


def write_pair(env: Env, **claude_kw: bool) -> V3Pair:
    regex = env.tmp / "regex_v3.json"
    claude = env.tmp / "claude_v3.json"
    claude_data = v3_result(env, "claude", claude_raws(env), **claude_kw)
    regex.write_text(json.dumps(v3_result(env, "regex", regex_raws(env))))
    claude.write_text(json.dumps(claude_data))
    return V3Pair(regex, claude, claude_data)


@pytest.fixture
def pair(env: Env) -> V3Pair:
    return write_pair(env)


def test_tables_v2_real_files_readme_numbers(tables_mod: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """On the real holdout-v2 files: 8 field rows, each once, with the README's numbers."""
    rc, out, _ = call_tables(tables_mod, capsys, [V2_REGEX, V2_CLAUDE])
    assert rc == 0
    field_lines = [line for line in out.splitlines() if re.match(r"^\|\s*(" + "|".join(SCORED_FIELDS) + r")\b", line)]
    assert len(field_lines) == 8
    rows = table_rows(out)
    assert all(f in rows for f in SCORED_FIELDS)
    # README Results table: regex baseline vs Claude final prompt, counts out of 15 / 12.
    expected = {
        "is_deal_request": ((14, 15), (15, 15)),
        "state": ((9, 12), (12, 12)),
        "city": ((9, 12), (12, 12)),
        "property_type": ((9, 12), (11, 12)),
        "transaction_type": ((9, 12), (12, 12)),
        "deal_value_usd": ((7, 12), (12, 12)),
        "timeline": ((10, 12), (9, 12)),
        "missing_fields": ((4, 12), (11, 12)),
    }
    for f, ((rk, rn), (ck, cn)) in expected.items():
        assert rows[f][1] == acc_cell(rk, rn), f
        assert rows[f][2] == acc_cell(ck, cn), f
        assert rows[f][3] == delta(rk / rn, ck / cn), f
    mean = rows["**Mean of the 7 fields**"]
    assert mean[1:4] == ["0.764", "0.952", "+19"]
    assert rows["Fields guessed on deal posts"][1:3] == ["3", "1"]
    assert rows["Fields filled on non-deal posts"][1:3] == ["0", "0"]
    assert rows["Posts that errored"][1:3] == ["0", "0"]


def test_tables_v2_heading_and_provenance(tables_mod: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """A v2 pair is titled 'Earlier holdout v2', is never called the first look, and has no usage."""
    _, out, _ = call_tables(tables_mod, capsys, [V2_REGEX, V2_CLAUDE])
    assert "### Earlier holdout v2: `data/holdout.jsonl`, 15 posts (12 deal, 3 non-deal)" in out
    assert "**Holdout v2: not the final look.**" in out
    assert "First and only look" not in out
    assert "**Usage:** not recorded (this results file predates usage tracking)." in out
    assert "Final holdout v3" not in out


def test_wilson_reference_matches_contract_example() -> None:
    """The test's own Wilson formula reproduces the contract's 14/15 -> 93% [70–99] example."""
    assert acc_cell(14, 15) == "93% [70–99]"
    assert acc_cell(15, 15) == "100% [80–100]"


def test_tables_v3_cells(tables_mod: ModuleType, pair: V3Pair, capsys: pytest.CaptureFixture[str]) -> None:
    """v3 accuracy cells are Wilson-formatted, Δ signed, hallucinated regex / Claude, mean 3 decimals."""
    rc, out, err = call_tables(tables_mod, capsys, [pair.regex, pair.claude])
    assert rc == 0, err
    rows = table_rows(out)
    assert rows["state"][1:4] == [acc_cell(14, 19), acc_cell(18, 19), delta(14 / 19, 18 / 19)]
    assert rows["timeline"][1:4] == [acc_cell(16, 19), acc_cell(19, 19), delta(16 / 19, 1.0)]
    assert rows["missing_fields"][1] == acc_cell(16, 19)
    assert rows["is_deal_request"][1:4] == [acc_cell(25, 25), acc_cell(25, 25), "+0"]
    assert [x.strip() for x in rows["timeline"][4].split("/")] == ["3", "0"]
    assert rows["is_deal_request"][4] == "–" and rows["missing_fields"][4] == "–"
    regex_mean = (1 + (14 + 19 + 19 + 19 + 19 + 16) / 19) / 7
    claude_mean = (1 + (18 + 19 * 5) / 19) / 7
    mean = rows["**Mean of the 7 fields**"]
    assert mean[1:4] == [f"{regex_mean:.3f}", f"{claude_mean:.3f}", delta(regex_mean, claude_mean)]
    assert rows["Fields guessed on deal posts"][1:3] == ["3", "0"]
    assert rows["Fields filled on non-deal posts"][1:3] == ["0", "1"]
    assert rows["Posts that errored"][1:3] == ["0", "0"]


def test_tables_mcnemar_p(tables_mod: ModuleType, pair: V3Pair, capsys: pytest.CaptureFixture[str]) -> None:
    """p cells equal exact_mcnemar_p on the constructed discordant counts, formatted {:.3g}."""
    _, out, _ = call_tables(tables_mod, capsys, [pair.regex, pair.claude])
    rows = table_rows(out)
    assert rows["state"][5] == f"{exact_mcnemar_p(5, 1):.3g}"  # Claude-only right t-01..05, regex-only t-06
    assert rows["timeline"][5] == f"{exact_mcnemar_p(3, 0):.3g}"
    assert rows["is_deal_request"][5] == f"{exact_mcnemar_p(0, 0):.3g}"


def test_tables_v3_heading_usage_and_first_look(
    tables_mod: ModuleType, env: Env, pair: V3Pair, capsys: pytest.CaptureFixture[str]
) -> None:
    """A v3 pair: Final holdout v3 heading, Claude's usage_line, provenance, 'First and only look'."""
    _, out, _ = call_tables(tables_mod, capsys, [pair.regex, pair.claude])
    (heading,) = [line for line in out.splitlines() if line.startswith("### ")]
    assert heading.startswith("### Final holdout v3:")
    assert f"`{env.data}`" in heading and f"{N_POSTS} posts ({N_DEAL} deal, 6 non-deal)" in heading
    assert ev.usage_line(pair.claude_data["usage"]) in out
    assert "**Provenance.**" in out
    assert "c1aude0" in out and "4e9e000" in out and "claude-haiku-5-5" in out and PROMPT in out
    assert "**First and only look**" in out
    assert "Second look" not in out and "Holdout v2: not the final look" not in out
    assert "(uncommitted changes)" not in out and "(INCOMPLETE)" not in out


def test_tables_v3_no_usage_line(tables_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """A Claude file without usage gets the 'not recorded' line."""
    pair = write_pair(env, usage=False)
    _, out, _ = call_tables(tables_mod, capsys, [pair.regex, pair.claude])
    assert "**Usage:** not recorded (this results file predates usage tracking)." in out


def test_tables_second_look_dirty_incomplete_flags(
    tables_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """second_look, dirty and incomplete runs are flagged in the provenance."""
    pair = write_pair(env, second_look=True, dirty=True, complete=False)
    rc, out, _ = call_tables(tables_mod, capsys, [pair.regex, pair.claude])
    assert rc == 0
    assert "**Second look**" in out and "First and only look" not in out
    assert "(uncommitted changes)" in out and "(INCOMPLETE)" in out


def test_tables_errored_posts_counted(tables_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """'Posts that errored' counts records with an error."""
    regex = env.tmp / "r.json"
    claude = env.tmp / "c.json"
    regex.write_text(json.dumps(v3_result(env, "regex", {})))
    claude.write_text(json.dumps(v3_result(env, "claude", {"t-07": None, "t-21": None})))
    _, out, _ = call_tables(tables_mod, capsys, [regex, claude])
    assert table_rows(out)["Posts that errored"][1:3] == ["0", "2"]


def test_tables_with_holdout_v2_separate_sections(
    tables_mod: ModuleType, pair: V3Pair, capsys: pytest.CaptureFixture[str]
) -> None:
    """--holdout-v2 adds its own v2 section; the v3 table carries no v2 numbers."""
    rc, out, err = call_tables(tables_mod, capsys, [pair.regex, pair.claude, "--holdout-v2", V2_CLAUDE, V2_REGEX])
    assert rc == 0, err
    parts = sections(out)
    assert set(parts) == {"v3", "v2"}
    assert out.index("### Final holdout v3") < out.index("### Earlier holdout v2")
    assert "0.764" not in parts["v3"] and "data/holdout.jsonl" not in parts["v3"]
    assert "0.764" in parts["v2"] and "0.952" in parts["v2"]
    assert "**First and only look**" in parts["v3"]
    assert "**Holdout v2: not the final look.**" in parts["v2"]
    assert len([line for line in out.splitlines() if re.match(r"^\|\s*state\b", line)]) == 2


@pytest.mark.parametrize(
    "case",
    ["swapped", "swapped-v2", "mixed", "v2-flag-given-v3", "missing-file", "not-json"],
)
def test_tables_errors_exit_2(
    tables_mod: ModuleType, env: Env, pair: V3Pair, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    """Swapped slots, mixed v2/v3, a v3 file under --holdout-v2 and unreadable files all exit 2 with ERROR:."""
    junk = env.tmp / "junk.json"
    junk.write_text("not json {")
    args: dict[str, list[str | Path]] = {
        "swapped": [pair.claude, pair.regex],
        "swapped-v2": [pair.regex, pair.claude, "--holdout-v2", V2_REGEX, V2_CLAUDE],
        "mixed": [V2_REGEX, pair.claude],
        "v2-flag-given-v3": [pair.regex, pair.claude, "--holdout-v2", pair.claude, pair.regex],
        "missing-file": [env.tmp / "nope.json", pair.claude],
        "not-json": [junk, pair.claude],
    }
    rc, _out, err = call_tables(tables_mod, capsys, args[case])
    assert rc == 2
    assert sum(line.startswith("ERROR: ") for line in err.splitlines()) == 1, err


def test_tables_build_tables_raises_table_error(tables_mod: ModuleType, pair: V3Pair) -> None:
    """The Python API raises tables.TableError for a swapped pair."""
    regex, claude = load(pair.regex), load(pair.claude)
    with pytest.raises(tables_mod.TableError):
        tables_mod.build_tables((str(pair.claude), claude), (str(pair.regex), regex))
    text = tables_mod.build_tables((str(pair.regex), regex), (str(pair.claude), claude))
    assert isinstance(text, str) and "### Final holdout v3" in text


def test_tools_never_touch_repo_results_or_cache() -> None:
    """Guard: no test above may leave a holdout_v3 results file in the repo's results/."""
    for p in (ROOT / "results").rglob("*.json"):
        try:
            data = json.loads(p.read_text())
        except ValueError:
            continue
        if isinstance(data, dict):
            assert data.get("split") != "holdout_v3" and "holdout_v3" not in data.get("datasets", {}), p


# ---------------------------------------------------------------------------------------------
# Regression tests for the adversarial review's confirmed findings (one or more per finding).
# Unlike the tests above, these were written by the lead after reading the implementations.
# ---------------------------------------------------------------------------------------------

V3_CLAUDE_PRIOR: JSONObject = {"extractor": "claude", "split": "holdout_v3", "datasets": {}}


def run_refused_by(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str], rule: str, hooks: object, *extra: str
) -> None:
    """Exit 2 with a REFUSED line naming `rule`, and no results file written."""
    capsys.readouterr()
    before = set(result_jsons(env))
    rc = call_run(run_mod, argv(env, "claude", *extra), hooks)
    err = capsys.readouterr().err
    assert rc == 2, err
    assert any(rule in line for line in refusal_lines(err)), err
    assert set(result_jsons(env)) == before


def test_review_results_dir_outside_guard_is_refused(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 1a: a look written outside the guarded results dir would be invisible to the next run."""
    guard = env.tmp / "guarded"
    hooks = make_hooks(run_mod, env, Factory(FakeExtractor(env)), guard_results=(guard,))
    run_refused_by(run_mod, env, capsys, "results-dir", hooks)


def test_review_results_dir_inside_guard_is_allowed(run_mod: ModuleType, env: Env) -> None:
    """Finding 1a: the confinement rule still allows a results dir inside a guard dir."""
    hooks = make_hooks(run_mod, env, Factory(FakeExtractor(env)), guard_results=(env.tmp,))
    assert call_run(run_mod, argv(env), hooks) == 0


@pytest.mark.parametrize(
    "name", ["claude_20261025T000000Z.json.bak", "claude_20261025T000000Z.JSON", "claude_x.Json.old"]
)
def test_review_renamed_prior_look_still_refuses(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """Finding 1b/1c: renaming an earlier look's file (suffix or case) doesn't hide it."""
    _write_prior(env.results, V3_CLAUDE_PRIOR, name)
    run_refused_by(run_mod, env, capsys, "one-look", make_hooks(run_mod, env, Factory(FakeExtractor(env))))


def test_review_prior_look_in_symlinked_dir_refuses(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 1d: an earlier look reachable only through a symlinked directory is still found."""
    elsewhere = env.tmp / "elsewhere"
    _write_prior(elsewhere, V3_CLAUDE_PRIOR)
    env.results.mkdir(parents=True)
    (env.results / "linked").symlink_to(elsewhere, target_is_directory=True)
    run_refused_by(run_mod, env, capsys, "one-look", make_hooks(run_mod, env, Factory(FakeExtractor(env))))


def test_review_truncated_prior_file_refuses(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """Finding 1e: a half-written claude_*.json (killed mid-save) can't be proven not a look."""
    env.results.mkdir(parents=True)
    (env.results / "claude_20261025T000000Z.json").write_text('{"extractor": "claude", "split": "hol')
    run_refused_by(run_mod, env, capsys, "one-look", make_hooks(run_mod, env, Factory(FakeExtractor(env))))


def test_review_marker_exists_before_first_call(run_mod: ModuleType, env: Env) -> None:
    """Finding 1f: the .look marker is on disk before the extractor is first called."""
    seen: list[list[str]] = []

    class Watching(FakeExtractor):
        def extract_raw(self, text: str) -> dict[str, object]:
            seen.append(sorted(p.name for p in env.results.glob("*.look")))
            return super().extract_raw(text)

    good_run(run_mod, env, fake=Watching(env))
    assert seen and len(seen[0]) == 1 and seen[0][0].startswith("claude_")


def test_review_save_failure_leaves_marker_and_blocks_rerun(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1f/9: if saving fails after the calls, exit 1, redacted error, and the marker blocks a rerun."""
    hooks = make_hooks(run_mod, env, Factory(FakeExtractor(env)))

    def broken_save(result: JSONObject, results_dir: Path | None = None) -> Path:
        raise OSError(f"disk full while writing (key {SECRET})")

    monkeypatch.setattr(run_mod.ev, "save", broken_save)
    capsys.readouterr()
    assert call_run(run_mod, argv(env), hooks) == 1
    cap = capsys.readouterr()
    assert SECRET not in cap.out + cap.err
    assert "ERROR:" in cap.err
    assert result_jsons(env) == [] and len(list(env.results.glob("*.look"))) == 1
    monkeypatch.undo()
    run_refused_by(run_mod, env, capsys, "one-look", make_hooks(run_mod, env, Factory(FakeExtractor(env))))


def test_review_git_history_look_refuses(run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    """Finding 1b: a look that was committed and later deleted from disk is found in git history."""
    base: Any = make_hooks(run_mod, env, Factory(FakeExtractor(env)))
    hooks = dataclasses.replace(
        base,
        history_looks=lambda extractor, ids: ["abc1234:results/claude_old.json"] if extractor == "claude" else [],
    )
    run_refused_by(run_mod, env, capsys, "one-look", hooks)


def test_review_history_scan_parses_blobs(run_mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 1b: the default history scan applies the same look rule to committed blobs."""
    blobs = (
        ("r1:results/claude_a.json", json.dumps(V3_CLAUDE_PRIOR)),
        ("r1:results/regex_a.json", json.dumps({**V3_CLAUDE_PRIOR, "extractor": "regex"})),
        ("r1:results/claude_b.json", "not json"),
    )
    monkeypatch.setattr(run_mod, "_history_candidates", lambda: blobs)
    assert run_mod._history_looks("claude", frozenset()) == ["r1:results/claude_a.json"]


@pytest.mark.parametrize(
    "prior",
    [
        {"extractor": "claude", "split": "all", "datasets": {"holdout": {"path": "data/holdout_v3.jsonl"}}},
        {"extractor": "claude", "split": "all", "datasets": {"x": {"path": "p", "records": {"t-03": {}}}}},
    ],
)
def test_review_look_detected_by_path_or_ids(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str], prior: JSONObject
) -> None:
    """Finding 7: a peek via a repointed DATASETS entry is caught by dataset path or record ids."""
    _write_prior(env.results, prior)
    run_refused_by(run_mod, env, capsys, "one-look", make_hooks(run_mod, env, Factory(FakeExtractor(env))))


def test_review_expect_model_mismatch_refuses(
    run_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 8: --expect-model pins the model, so a DEAL_MODEL override can't swap it silently."""
    hooks = make_hooks(run_mod, env, Factory(FakeExtractor(env)))
    run_refused_by(run_mod, env, capsys, "model", hooks, "--expect-model", "claude-haiku-5-5")


@pytest.mark.parametrize(
    ("change", "check"),
    [
        (_set(("final_eval", "second_look"), True), "first_look"),
        (_set(("final_eval", "branch"), "feature"), "first_look"),
        (_del(("final_eval", "protocol")), "first_look"),
        (_set(("final_eval", "data_sha256"), "0" * 64), "labels"),
        (_set(("rescored_from",), "results/x.json"), "provenance"),
        (_set(("cache_enabled",), True), "provenance"),
        (_set(("usage", "cache_hits"), 25), "provenance"),
        (_set(("datasets", "holdout_v3", "records", "t-01", "call", "cached"), True), "provenance"),
        (_set(("model",), None), "model"),
    ],
)
def test_review_validate_rejects_unclean_look(
    validate_mod: ModuleType,
    env: Env,
    good_file: Path,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[JSONObject], None],
    check: str,
) -> None:
    """Finding 2: second looks, other branches, changed labels, rescores, cache use and a null model all FAIL."""
    v = validate_file(validate_mod, capsys, variant(env, good_file, change), vhooks(validate_mod, env))
    assert v.rc == 1 and check in v.fails, v.out


def test_review_validate_expect_model(
    validate_mod: ModuleType, env: Env, good_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 8: validate.py --expect-model rejects a file from another model."""
    v = validate_file(validate_mod, capsys, good_file, vhooks(validate_mod, env), "--expect-model", "claude-haiku-5-5")
    assert v.rc == 1 and v.fails == ["model"], v.out


def test_review_validate_prediction_tamper(
    validate_mod: ModuleType, env: Env, good_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 4: editing predictions and the stored scores consistently still FAILs raw_consistent."""

    def tamper(data: JSONObject) -> None:
        ds = data["datasets"]["holdout_v3"]
        for pid in ("t-01", "t-02", "t-03"):
            ds["records"][pid]["prediction"]["city"] = "Dallas"
        ds["scores"] = json.loads(json.dumps(score(env.posts, ds["records"])))

    v = validate_file(validate_mod, capsys, variant(env, good_file, tamper), vhooks(validate_mod, env))
    assert v.rc == 1 and v.fails == ["raw_consistent"], v.out


def test_review_is_ancestor_has_no_head_fallback(validate_mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 5: a commit that is HEAD but not on main is not accepted."""
    commit = "ab" * 20

    def fake_git(*args: str) -> subprocess.CompletedProcess[str]:
        if args[0] == "merge-base":
            return subprocess.CompletedProcess(list(args), 1, "", "")
        return subprocess.CompletedProcess(list(args), 0, commit + "\n", "")

    monkeypatch.setattr(validate_mod, "_git_ok", fake_git)
    assert validate_mod.is_ancestor(commit) is False


def test_review_tables_refuses_rescore(
    tables_mod: ModuleType, env: Env, pair: V3Pair, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 3: a rescored file is not a look, so tables.py refuses it."""
    pair.claude.write_text(json.dumps({**pair.claude_data, "rescored_from": "results/claude_x.json"}))
    rc, _, err = call_tables(tables_mod, capsys, [pair.regex, pair.claude])
    assert rc == 2 and err.startswith("ERROR:")


def test_review_tables_warns_on_label_change(
    tables_mod: ModuleType, env: Env, pair: V3Pair, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 3: labels edited after the run produce a loud warning in the v3 section."""
    with env.data.open("a") as f:
        f.write("\n")
    rc, out, _ = call_tables(tables_mod, capsys, [pair.regex, pair.claude])
    assert rc == 0 and out.count("WARNING: labels changed since this run") == 2


def test_review_tables_requires_final_eval_block(
    tables_mod: ModuleType, env: Env, pair: V3Pair, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 6: a v3 file without final_eval wasn't made by run.py and can't be called a final look."""
    pair.claude.write_text(json.dumps({k: v for k, v in pair.claude_data.items() if k != "final_eval"}))
    rc, _, err = call_tables(tables_mod, capsys, [pair.regex, pair.claude])
    assert rc == 2 and "final_eval" in err


def test_review_tables_flags_non_boolean_dirty(
    tables_mod: ModuleType, env: Env, pair: V3Pair, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 6: a dirty flag that isn't exactly false is shown as not provably clean."""
    pair.claude.write_text(json.dumps({**pair.claude_data, "git": {**pair.claude_data["git"], "dirty": "true"}}))
    rc, out, _ = call_tables(tables_mod, capsys, [pair.regex, pair.claude])
    assert rc == 0 and "not provably clean" in out


def test_review_tables_incomplete_run_has_no_delta(
    tables_mod: ModuleType, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finding 10: Δ is n/a when a run is incomplete, since the accuracies cover different posts."""
    p = write_pair(env, complete=False)
    rc, out, _ = call_tables(tables_mod, capsys, [p.regex, p.claude])
    assert rc == 0
    rows = table_rows(sections(out)["v3"])
    assert all(rows[f][3] == "n/a" for f in SCORED_FIELDS), rows
