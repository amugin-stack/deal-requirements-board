"""deal_board.config: env parsing, call-time reads, .env loading, and keeping the key secret.

No test here loads the repository's `.env` or builds a real Anthropic client: `.env` files are
written under tmp_path, and the extractor tests replace `anthropic.Anthropic` with a recorder.
"""

import dataclasses
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import anthropic
import pytest
from pydantic import SecretStr

from deal_board import api, config, paths
from deal_board.config import DEFAULT_MODEL, Settings, load_dotenv_file, load_settings
from deal_board.extractors.claude import ClaudeExtractor

ENV_VARS = ("ANTHROPIC_API_KEY", "DEAL_EXTRACTOR", "DEAL_MODEL")
# Distinctive enough that any substring hit can only be a leak.
SECRET = "sk-test-SECRET-do-not-leak-0123"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with none of the package's variables set; monkeypatch restores them."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# --- Defaults and parsing ------------------------------------------------------------------------


def test_defaults_when_nothing_is_set() -> None:
    s = load_settings()
    assert s.anthropic_api_key is None
    assert s.deal_extractor == ""
    assert s.deal_model == DEFAULT_MODEL == "claude-haiku-5-5"
    assert not s.has_api_key
    assert not s.use_claude
    assert s.api_key_value() is None


def test_dataclass_defaults_match_load_settings_defaults() -> None:
    assert Settings() == load_settings({})


def test_dotenv_path_is_repo_root() -> None:
    assert config.DOTENV_PATH == paths.REPO_ROOT / ".env"


@pytest.mark.parametrize("value", ["", None], ids=["empty", "unset"])
def test_empty_or_unset_key_is_none(value: str | None) -> None:
    env = {} if value is None else {"ANTHROPIC_API_KEY": value}
    s = load_settings(env)
    assert s.anthropic_api_key is None
    assert not s.has_api_key
    assert s.api_key_value() is None


def test_key_is_wrapped_in_secretstr() -> None:
    s = load_settings({"ANTHROPIC_API_KEY": SECRET})
    assert isinstance(s.anthropic_api_key, SecretStr)
    assert s.has_api_key
    assert s.api_key_value() == SECRET


def test_empty_secretstr_built_directly_counts_as_no_key() -> None:
    s = Settings(anthropic_api_key=SecretStr(""))
    assert not s.has_api_key
    assert s.api_key_value() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", ""), ("regex", "regex"), (" Regex ", "regex"), ("REGEX", "regex"), ("\tclaude\n", "claude")],
)
def test_deal_extractor_is_stripped_and_lowercased(raw: str, expected: str) -> None:
    assert load_settings({"DEAL_EXTRACTOR": raw}).deal_extractor == expected
    assert Settings(deal_extractor=raw).deal_extractor == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", DEFAULT_MODEL), ("   ", DEFAULT_MODEL), ("claude-other-1", "claude-other-1"), (" m-2 ", "m-2")],
)
def test_deal_model(raw: str, expected: str) -> None:
    assert load_settings({"DEAL_MODEL": raw}).deal_model == expected


@pytest.mark.parametrize(
    ("key", "forced", "expected"),
    [
        (SECRET, None, True),
        (SECRET, "", True),
        (SECRET, "claude", True),
        (SECRET, "anything-else", True),
        (SECRET, "regex", False),
        (SECRET, " ReGeX ", False),
        (None, None, False),
        ("", None, False),
        (None, "claude", False),
        (None, "regex", False),
    ],
)
def test_use_claude_truth_table(key: str | None, forced: str | None, expected: bool) -> None:
    env = {k: v for k, v in (("ANTHROPIC_API_KEY", key), ("DEAL_EXTRACTOR", forced)) if v is not None}
    assert load_settings(env).use_claude is expected


def test_settings_are_frozen() -> None:
    s = load_settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.deal_model = "other"  # type: ignore[misc]  # assigning to a frozen field is the point


# --- Call-time reads -----------------------------------------------------------------------------


def test_load_settings_reads_os_environ_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not load_settings().has_api_key  # module already imported, env still clean
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("DEAL_EXTRACTOR", "Regex")
    monkeypatch.setenv("DEAL_MODEL", "claude-later")
    s = load_settings()
    assert s.api_key_value() == SECRET
    assert s.deal_extractor == "regex"
    assert s.deal_model == "claude-later"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert not load_settings().has_api_key


def test_explicit_environ_ignores_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("DEAL_EXTRACTOR", "regex")
    monkeypatch.setenv("DEAL_MODEL", "from-os-environ")
    s = load_settings({"DEAL_MODEL": "from-mapping"})
    assert s == Settings(deal_model="from-mapping")


def test_only_config_reads_the_environment() -> None:
    """The contract: no other package module reads os.environ/os.getenv or imports dotenv."""
    pattern = re.compile(r"os\.environ|os\.getenv|getenv\(|^\s*(from|import)\s+dotenv\b", re.MULTILINE)
    package = paths.REPO_ROOT / "deal_board"
    offenders = [
        str(p.relative_to(paths.REPO_ROOT))
        for p in sorted(package.rglob("*.py"))
        if p != package / "config.py" and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# --- .env loading (tmp_path only; never the repository's .env) -----------------------------------


def test_load_dotenv_file_loads_without_overriding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEAL_EXTRACTOR", "regex")  # set in the shell: must win over the file
    dotenv = tmp_path / ".env"
    dotenv.write_text("DEAL_EXTRACTOR=claude\nDEAL_MODEL=claude-from-file\n", encoding="utf-8")

    assert load_dotenv_file(dotenv) is True

    assert os.environ["DEAL_EXTRACTOR"] == "regex"
    assert os.environ["DEAL_MODEL"] == "claude-from-file"
    assert load_settings().deal_model == "claude-from-file"
    monkeypatch.delenv("DEAL_MODEL")  # loaded by dotenv, not monkeypatch; remove it explicitly


def test_load_dotenv_file_missing_returns_false(tmp_path: Path) -> None:
    assert load_dotenv_file(tmp_path / "missing.env") is False
    assert "DEAL_MODEL" not in os.environ


# --- Secrecy -------------------------------------------------------------------------------------


@pytest.fixture
def secret_settings() -> Settings:
    return load_settings({"ANTHROPIC_API_KEY": SECRET, "DEAL_MODEL": "m"})


def test_key_never_rendered(secret_settings: Settings) -> None:
    renderings = [
        repr(secret_settings),
        str(secret_settings),
        format(secret_settings),
        f"{secret_settings}",
        f"{secret_settings!r}",
        "%s %r" % (secret_settings, secret_settings),  # noqa: UP031  # %-formatting is what logging uses
        str(dataclasses.asdict(secret_settings)),
        repr(dataclasses.astuple(secret_settings)),
        str(secret_settings.anthropic_api_key),
        repr(secret_settings.anthropic_api_key),
        str(vars(secret_settings)),
    ]
    for text in renderings:
        assert SECRET not in text
    assert "anthropic_api_key" not in repr(secret_settings)  # left out of the repr entirely


def test_asdict_keeps_key_wrapped(secret_settings: Settings) -> None:
    key = dataclasses.asdict(secret_settings)["anthropic_api_key"]
    assert isinstance(key, SecretStr)
    assert key.get_secret_value() == SECRET  # still the key, just not rendered


def test_key_not_in_log_records(secret_settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test_config")
    with caplog.at_level(logging.DEBUG, logger="test_config"):
        logger.info("%s", secret_settings)
        logger.info("settings=%r key=%s", secret_settings, secret_settings.anthropic_api_key)
        logger.info(secret_settings)
    assert len(caplog.records) == 3
    assert SECRET not in caplog.text
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_key_only_reachable_through_api_key_value(secret_settings: Settings) -> None:
    assert secret_settings.api_key_value() == SECRET
    public = {name for name in dir(secret_settings) if not name.startswith("_")}
    assert public == {
        "anthropic_api_key",
        "api_key_value",
        "deal_extractor",
        "deal_model",
        "has_api_key",
        "use_claude",
    }


# --- Wiring into the API and the Claude extractor ------------------------------------------------


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, object]]]:
    """Replaces anthropic.Anthropic with a recorder of its kwargs, so no real client is built."""
    calls: list[dict[str, object]] = []

    def fake_anthropic(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(anthropic, "Anthropic", fake_anthropic)  # the module api.py calls through
    api._claude_extractor.cache_clear()
    api._regex_extractor.cache_clear()
    yield calls
    api._claude_extractor.cache_clear()
    api._regex_extractor.cache_clear()


def test_get_extractor_forced_regex_builds_no_client(
    monkeypatch: pytest.MonkeyPatch, built: list[dict[str, object]]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("DEAL_EXTRACTOR", "regex")

    assert api.get_extractor().name == "regex"
    assert built == []


def test_get_extractor_without_key_builds_no_client(built: list[dict[str, object]]) -> None:
    assert api.get_extractor().name == "regex"
    assert built == []


def test_claude_client_gets_key_explicitly(monkeypatch: pytest.MonkeyPatch, built: list[dict[str, object]]) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("DEAL_MODEL", "claude-configured")

    extractor = api.get_extractor()

    assert extractor.name == "claude"
    assert built == [
        {
            "api_key": SECRET,
            "timeout": api.CLAUDE_TIMEOUT_SECONDS,
            "max_retries": api.CLAUDE_MAX_RETRIES,
            "open_telemetry": False,
        }
    ]
    assert isinstance(extractor, ClaudeExtractor)
    assert extractor.model == "claude-configured"


class _NoNetworkClient:
    """A fake client whose every request fails the test; constructing the extractor makes none."""

    def __init__(self) -> None:
        self.messages = self

    def create(self, **kwargs: object) -> NoReturn:
        raise AssertionError("no request expected")


def test_claude_extractor_model_defaults_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ClaudeExtractor(client=_NoNetworkClient()).model == DEFAULT_MODEL
    monkeypatch.setenv("DEAL_MODEL", "claude-env-model")
    assert ClaudeExtractor(client=_NoNetworkClient()).model == "claude-env-model"
    assert ClaudeExtractor(client=_NoNetworkClient(), model="explicit").model == "explicit"


def test_claude_module_reexports_default_model() -> None:
    from deal_board.extractors import claude

    assert claude.DEFAULT_MODEL == DEFAULT_MODEL
