"""Regression tests for the confirmed findings of the P10 adversarial review.

R1: loop/check_success.py took its repository root from deal_board.paths, a module it didn't
    protect, so one edit there could redirect both the eval's data and the guard's checks.
R2: the Claude client Protocol typed `create` as (*args: Any, **kwargs: Any), which hid a real
    mismatch with the SDK's parameter types from mypy.
R3: ClaudeExtractor() without a key passed api_key=None, letting the SDK fall back to its own
    environment lookup (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN) behind deal_board.config's back.
"""

import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType

import anthropic
import pytest

from deal_board import paths
from deal_board.extractors import claude

CHECK_SUCCESS = Path(__file__).resolve().parent.parent / "loop" / "check_success.py"


def _load_check_success(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, CHECK_SUCCESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    return module


def test_r1_guard_root_ignores_deal_board_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An edited paths.REPO_ROOT must not move the guard: it finds the repo from its own file."""
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    cs = _load_check_success("check_success_r1")
    assert CHECK_SUCCESS.parent.parent == cs.ROOT
    assert tmp_path != cs.ROOT


def test_r1_guard_protects_paths_module_and_pytest_config() -> None:
    """paths.py decides where eval reads data; pyproject.toml holds the pytest config the guard runs."""
    cs = _load_check_success("check_success_r1b")
    assert {"deal_board/paths.py", "pyproject.toml"} <= set(cs.PROTECTED)


def test_r2_client_protocol_names_every_argument() -> None:
    """No catch-all *args/**kwargs: mypy must check extract_raw's call against the SDK's types."""
    params = inspect.signature(claude._Messages.create).parameters.values()
    kinds = {p.kind for p in params}
    assert inspect.Parameter.VAR_POSITIONAL not in kinds
    assert inspect.Parameter.VAR_KEYWORD not in kinds
    assert {"model", "max_tokens", "system", "tools", "tool_choice", "messages"} <= {p.name for p in params}


def test_r3_no_key_means_no_client_and_no_sdk_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ANTHROPIC_API_KEY, ClaudeExtractor() raises instead of letting the SDK find credentials."""
    built: list[dict[str, object]] = []

    def recorder(**kwargs: object) -> object:
        built.append(kwargs)
        return object()

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sdk-fallback-token-must-not-be-used")
    monkeypatch.setattr(anthropic, "Anthropic", recorder)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set") as exc:
        claude.ClaudeExtractor()
    assert built == []
    assert "sdk-fallback-token" not in str(exc.value)


def test_r3_key_from_config_is_passed_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a key, the client gets it as api_key=, never via the SDK's own lookup."""
    built: list[dict[str, object]] = []

    def recorder(**kwargs: object) -> object:
        built.append(kwargs)
        return object()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-r3")
    monkeypatch.setattr(anthropic, "Anthropic", recorder)
    claude.ClaudeExtractor()
    assert built == [{"api_key": "sk-test-r3"}]
