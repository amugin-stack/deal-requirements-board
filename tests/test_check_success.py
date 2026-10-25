"""The loop's success test must reject what it should reject. Pure checks only; no git, no API."""

import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, TypedDict, Unpack

from deal_board.schema import KEY_FIELDS, JSONObject

CHECK_SUCCESS = Path(__file__).resolve().parent.parent / "loop" / "check_success.py"


def _load_check_success() -> ModuleType:
    """loop/ is a historical script directory, not a package, so load the script by its path.

    Registered in sys.modules before running, as the importlib recipe does, because its
    dataclass resolves annotations through its own module entry.
    """
    spec = importlib.util.spec_from_file_location("check_success", CHECK_SUCCESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if TYPE_CHECKING:
    # mypy checks loop/ as a source dir (pyproject `files`), so it knows this script as the
    # top-level module `check_success` and can type `cs` as the real script, no Any involved.
    # At runtime loop/ is not on sys.path, so the script is loaded by its file path instead.
    import check_success as cs
else:
    cs = _load_check_success()


class ScoreKw(TypedDict, total=False):
    acc: float
    errors: int
    is_deal: float
    non_deal_filled: int


def scores(acc: float = 1.0, errors: int = 0, is_deal: float = 1.0, non_deal_filled: int = 0) -> JSONObject:
    return {
        "is_deal_request": {"accuracy": is_deal, "error": errors},
        "fields": {f: {"accuracy": acc} for f in KEY_FIELDS},
        "non_deal_hallucinations": {"fields_filled": non_deal_filled},
    }


def dev_run(**kw: Unpack[ScoreKw]) -> JSONObject:
    return {"prompt_version": "v1", "datasets": {"posts_dev": {"scores": scores(**kw)}}}


V1 = cs.RunIdentity("v1", "m", "0123456789ab")


def final_run(version: str = "v1", **kw: Unpack[ScoreKw]) -> JSONObject:
    return {
        "prompt_version": version,
        "model": V1.model,
        "request_sha": V1.request_sha,
        "datasets": {"holdout": {"scores": scores(**kw)}, "posts": {"scores": scores()}},
    }


def failed(checks: Iterable[cs.Check]) -> set[str]:
    return {c.name for c in checks if not c.passed}


def test_dev_passes_only_with_three_clean_runs() -> None:
    assert failed(cs.dev_checks([dev_run()] * 3)) == set()
    assert failed(cs.dev_checks([dev_run()] * 2)) == {"repeats"}


def test_dev_fails_on_any_error_hallucination_or_weak_field() -> None:
    assert "schema errors" in failed(cs.dev_checks([dev_run(), dev_run(errors=1), dev_run()]))
    assert "non-deal hallucinations" in failed(cs.dev_checks([dev_run(non_deal_filled=1)] + [dev_run()] * 2))
    assert "weakest field" in failed(cs.dev_checks([dev_run(acc=0.8)] * 3))
    assert "is_deal_request" in failed(cs.dev_checks([dev_run(is_deal=0.96)] * 3))


def test_fixed_sections_ignore_editable_ones() -> None:
    before = "# T\n\n## Goal (FIXED)\nwin\n\n## Backlog (EDITABLE)\n1. a\n"
    after = "# T\n\n## Goal (FIXED)\nwin\n\n## Backlog (EDITABLE)\n1. b\n2. c\n"
    assert cs.fixed_sections(before) == cs.fixed_sections(after) == ["## Goal (FIXED)\nwin"]
    assert cs.fixed_sections(before.replace("win", "tie")) != cs.fixed_sections(before)


def test_final_allows_one_look_at_a_dev_passed_prompt() -> None:
    assert failed(cs.final_checks([final_run()], V1)) == set()
    assert "one look only" in failed(cs.final_checks([final_run(), final_run()], V1))
    assert "prompt passed dev first" in failed(cs.final_checks([final_run()], None))
    assert "holdout mean accuracy" in failed(cs.final_checks([final_run(acc=0.7)], V1))
