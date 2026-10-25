"""Compatibility promises of the P10 packaging move (contract items 1-6).

The move to an installed ``deal_board`` package must not break anything that already exists:
the permanent ``results/`` log must still rescore, the CLI and console script must still work
without an API key, the frozen baseline must stay byte-identical to ``baseline-v1``, the
historical loop script must still run, and uvicorn must still find the app.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

import pytest

from deal_board.paths import REPO_ROOT

RESULTS_DIR = REPO_ROOT / "results"
RESULT_JSONS = sorted(RESULTS_DIR.glob("*.json"))
RESULT_MDS = sorted(RESULTS_DIR.glob("*.md"))
RESCORE_SOURCE = "results/claude_20261024T234359Z.json"


def _env_without_key() -> dict[str, str]:
    """A copy of the current environment with the API key removed, for subprocesses."""
    return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}


def _snapshot(directory: Path) -> dict[str, str]:
    """Map every file name under ``directory`` (recursively) to the sha256 of its bytes."""
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any socket connection attempt fail loudly so 'offline' is enforced, not assumed."""

    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError(f"network access attempted: args={args!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _dataset_entries(saved: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return (name, entry) pairs for the ``datasets`` field, whether it is a dict or a list."""
    datasets = saved["datasets"]
    if isinstance(datasets, dict):
        return [(str(k), v) for k, v in datasets.items()]
    assert isinstance(datasets, list), f"datasets must be a dict or list, got {type(datasets).__name__}"
    return [(str(i), v) for i, v in enumerate(datasets)]


# ---------------------------------------------------------------------------------------------
# 1. results/ still parses and rescores
# ---------------------------------------------------------------------------------------------


def test_results_dir_is_not_empty() -> None:
    """Guards the parametrized tests below: an empty glob would make them pass vacuously."""
    assert RESULT_JSONS, f"no results/*.json found under {RESULTS_DIR}"
    assert RESULT_MDS, f"no results/*.md found under {RESULTS_DIR}"


@pytest.mark.parametrize("path", RESULT_JSONS, ids=lambda p: p.name)
def test_results_json_parses(path: Path) -> None:
    """Every file in the permanent results log must still be valid JSON after the move."""
    json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "path",
    [p for p in RESULT_JSONS if isinstance(json.loads(p.read_text(encoding="utf-8")), dict)],
    ids=lambda p: p.name,
)
def test_eval_run_rescores_offline(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Eval-run files store relative dataset paths; the move must keep them resolvable and
    rescorable from the repo root with no key and no network."""
    saved: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if "datasets" not in saved:
        pytest.skip(f"{path.name} is not an eval-run file (no 'datasets')")

    for name, entry in _dataset_entries(saved):
        assert isinstance(entry, dict), f"{path.name}: dataset {name!r} is not an object"
        assert "path" in entry, f"{path.name}: dataset {name!r} has no 'path'"
        rel = Path(entry["path"])
        assert not rel.is_absolute(), f"{path.name}: dataset {name!r} path {rel} is absolute"
        assert (REPO_ROOT / rel).is_file(), f"{path.name}: dataset {name!r} path {rel} missing under REPO_ROOT"

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _no_network(monkeypatch)
    from deal_board.cli.eval import rescore

    rescored = rescore(saved)
    assert isinstance(rescored, dict), f"{path.name}: rescore returned {type(rescored).__name__}"


@pytest.mark.parametrize("path", RESULT_MDS, ids=lambda p: p.name)
def test_results_md_is_utf8(path: Path) -> None:
    """Markdown reports are part of the permanent log; they must still decode as UTF-8."""
    path.read_bytes().decode("utf-8")


# ---------------------------------------------------------------------------------------------
# 2. CLI rescore end to end, results/ untouched
# ---------------------------------------------------------------------------------------------


def test_cli_rescore_writes_only_to_results_dir(tmp_path: Path) -> None:
    """``python -m deal_board.cli.eval --rescore`` must work without a key, write exactly one
    report pair to ``--results-dir``, and never touch the permanent ``results/`` log.

    Run with ``-I`` so neither the cwd nor the environment can put the repo on sys.path: the
    module must resolve from the installed package."""
    before = _snapshot(RESULTS_DIR)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    proc = subprocess.run(
        [sys.executable, "-I", "-m", "deal_board.cli.eval", "--rescore", RESCORE_SOURCE, "--results-dir", str(out_dir)],
        cwd=REPO_ROOT,
        env=_env_without_key(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    after = _snapshot(RESULTS_DIR)

    assert proc.returncode == 0, f"exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    written = sorted(p.name for p in out_dir.iterdir())
    jsons = [n for n in written if n.endswith(".json")]
    mds = [n for n in written if n.endswith(".md")]
    assert len(jsons) == 1, f"expected exactly one .json in results-dir, got {written}"
    assert len(mds) == 1, f"expected exactly one .md in results-dir, got {written}"
    data = json.loads((out_dir / jsons[0]).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "datasets" in data, f"rescored JSON lacks 'datasets': keys={sorted(data)}"

    assert sorted(after) == sorted(before), (
        f"results/ file names changed: added={sorted(set(after) - set(before))} "
        f"removed={sorted(set(before) - set(after))}"
    )
    changed = sorted(n for n in before if before[n] != after[n])
    assert not changed, f"results/ contents changed: {changed}"


def test_deal_eval_console_script_installed() -> None:
    """The ``deal-eval`` entry point must be installed next to this interpreter and runnable."""
    script = Path(sys.executable).parent / "deal-eval"
    assert script.is_file(), f"console script not found at {script} (is the package pip-installed?)"
    proc = subprocess.run(
        [str(script), "--help"],
        cwd=REPO_ROOT,
        env=_env_without_key(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"deal-eval --help exit {proc.returncode}\nstderr:\n{proc.stderr}"


# ---------------------------------------------------------------------------------------------
# 3. Typed wrapper around the frozen baseline (golden output itself is test_baseline_golden.py)
# ---------------------------------------------------------------------------------------------


def test_regex_extractor_wrapper_is_typed() -> None:
    """The loader must expose the frozen baseline under the old name and return the new
    package's ``DealRequirement`` (proves ``schema`` was mapped to ``deal_board.schema``)."""
    from deal_board.extractors.regex_baseline import RegexExtractor
    from deal_board.schema import DealRequirement

    extractor = RegexExtractor()
    assert extractor.name == "regex"
    result = extractor.extract("Need a listing agent in Columbus, Ohio for a 3bd ranch, about $275k")
    assert isinstance(result, DealRequirement), f"extract() returned {type(result).__module__}.{type(result).__name__}"


# ---------------------------------------------------------------------------------------------
# 4. Frozen baseline is byte-identical to the tag
# ---------------------------------------------------------------------------------------------


def test_frozen_baseline_matches_tag() -> None:
    """The baseline is frozen by owner rule; the move may relocate it but not change a byte."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not on PATH")
    inside = subprocess.run(
        [git, "-C", str(REPO_ROOT), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        pytest.skip(f"{REPO_ROOT} is not a git work tree")

    tag = subprocess.run(
        [git, "-C", str(REPO_ROOT), "rev-parse", "-q", "--verify", "refs/tags/baseline-v1"],
        capture_output=True,
        check=False,
    )
    assert tag.returncode == 0, "tag baseline-v1 missing: fetch full history with tags (CI must use fetch-depth: 0)"

    shown = subprocess.run(
        [git, "-C", str(REPO_ROOT), "show", "baseline-v1:extractors/regex_baseline.py"],
        capture_output=True,
        check=False,
    )
    assert shown.returncode == 0, f"git show failed: {shown.stderr.decode(errors='replace')}"
    frozen = (REPO_ROOT / "deal_board" / "extractors" / "frozen" / "regex_baseline.py").read_bytes()
    assert frozen == shown.stdout, "deal_board/extractors/frozen/regex_baseline.py differs from baseline-v1"


# ---------------------------------------------------------------------------------------------
# 5. loop/check_success.py still works in the new layout
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def check_success() -> ModuleType:
    """Load the historical script by file path (it is not part of the package).

    Registered in sys.modules before exec because it defines a dataclass. Never call its
    ``guard_checks()`` or ``main()``: they run the whole pytest suite (infinite recursion)."""
    path = REPO_ROOT / "loop" / "check_success.py"
    name = "_p10_check_success_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def test_check_success_protected_paths_exist(check_success: ModuleType) -> None:
    """The loop guard refuses changes to protected paths; if those paths no longer exist after
    the move, the guard silently protects nothing."""
    protected = check_success.PROTECTED
    assert isinstance(protected, list | tuple), f"PROTECTED is {type(protected).__name__}"
    assert protected, "PROTECTED is empty"
    missing: list[str] = []
    for entry in protected:
        text = str(entry)
        target = REPO_ROOT / text
        ok = target.is_dir() if text.endswith("/") else target.exists()
        if not ok:
            missing.append(text)
    assert not missing, f"PROTECTED entries missing from the working tree: {missing}"


def test_check_success_prompt_version(check_success: ModuleType) -> None:
    """The guard compares the prompt version; it must still find the moved Claude extractor."""
    assert check_success.current_prompt_version() == "ac1a145b"


def test_check_success_runs_as_script() -> None:
    """The script must run directly (``python loop/check_success.py``) without sys.path hacks."""
    proc = subprocess.run(
        [sys.executable, "loop/check_success.py", "--help"],
        cwd=REPO_ROOT,
        env=_env_without_key(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"exit {proc.returncode}\nstderr:\n{proc.stderr}"


# ---------------------------------------------------------------------------------------------
# 6. uvicorn target and API behaviour
# ---------------------------------------------------------------------------------------------


def test_uvicorn_resolves_app() -> None:
    """``uvicorn deal_board.api:app`` is the documented run command; it must resolve."""
    from fastapi import FastAPI
    from uvicorn.importer import import_from_string

    app = import_from_string("deal_board.api:app")
    assert isinstance(app, FastAPI)


def test_api_match_uses_regex_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no key (or DEAL_EXTRACTOR=regex) the API must fall back to the regex extractor and
    still answer, offline."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DEAL_EXTRACTOR", "regex")
    from fastapi.testclient import TestClient

    from deal_board.api import app

    _no_network(monkeypatch)
    with TestClient(app) as client:
        resp = client.post(
            "/match", json={"text": "Need a listing agent in Columbus, Ohio for a 3bd ranch, about $275k"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("extractor") == "regex", f"expected extractor 'regex', body={body}"


def test_prompt_version_unchanged() -> None:
    """The move must not change the Claude prompt or tool, so its version hash is fixed."""
    from deal_board.extractors.claude import PROMPT_VERSION

    assert PROMPT_VERSION == "ac1a145b"
