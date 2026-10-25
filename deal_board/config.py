"""Runtime settings from the environment: the only module that reads env vars or `.env`.

Three variables configure the package:

- `ANTHROPIC_API_KEY`: enables the Claude extractor. Unset or empty means no key, so the API and
  the eval CLI fall back to the regex baseline instead of failing.
- `DEAL_EXTRACTOR`: set to `regex` (any case, surrounding spaces ignored) to force the regex
  baseline even when a key is present, e.g. to run the API without spending tokens. Any other
  value means "use Claude if there is a key".
- `DEAL_MODEL`: the Claude model id. Unset or empty means `DEFAULT_MODEL`.

Why one module: the key is a secret, so every read goes through one place that wraps it in
`pydantic.SecretStr` straight away. `Settings` never shows it in `repr()`, `str()`, f-strings,
`dataclasses.asdict()` or log records; `Settings.api_key_value()` is the only way to the
plaintext, and it exists to hand the key to the Anthropic client.

Why read at call time: nothing here touches the environment at import time. `load_settings()`
reads `os.environ` each time it is called, so tests (and a long-running server after a config
change) see the current values, and importing the package never depends on the environment.

`.env` is loaded only by CLI entry points, via `load_dotenv_file()`. Library code and tests never
load it, so a test can never pick up a real key from the checkout.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from dotenv import load_dotenv
from pydantic import SecretStr

from deal_board import paths

# A cheap model. Note: Haiku 5.5 accepts a forced tool_choice, but Opus 5.5 and Sonnet 5.5
# reject it with a 400. Switching to those would need tool_choice "auto" plus a prompt instruction.
DEFAULT_MODEL: Final = "claude-haiku-5-5"

# The checkout's `.env`. Only CLI entry points load it (see `load_dotenv_file`).
DOTENV_PATH: Final[Path] = paths.REPO_ROOT / ".env"

# The one DEAL_EXTRACTOR value with a meaning: force the regex baseline.
FORCE_REGEX: Final = "regex"


@dataclass(frozen=True)
class Settings:
    """The package's configuration, as read by `load_settings()`.

    Frozen, so a settings object handed around can't be changed underneath its users. Build it
    with `load_settings()`; constructing it directly is for tests.

    The key is a `SecretStr` (renders as `**********`) and is also left out of the dataclass
    repr entirely, as defense in depth: `repr(settings)`, `str(settings)`, f-strings and
    `logger.info("%s", settings)` never contain it, and `dataclasses.asdict(settings)` keeps it
    wrapped in a `SecretStr`.
    """

    anthropic_api_key: SecretStr | None = field(default=None, repr=False)
    """ANTHROPIC_API_KEY; unset or empty -> None."""
    deal_extractor: str = ""
    """DEAL_EXTRACTOR, stripped and lowercased; only "regex" means anything."""
    deal_model: str = DEFAULT_MODEL
    """DEAL_MODEL, stripped; unset or empty -> DEFAULT_MODEL."""

    def __post_init__(self) -> None:
        # Normalize here as well as in load_settings, so a directly built Settings behaves the
        # same way. object.__setattr__ because the dataclass is frozen.
        object.__setattr__(self, "deal_extractor", self.deal_extractor.strip().lower())
        object.__setattr__(self, "deal_model", self.deal_model.strip() or DEFAULT_MODEL)

    @property
    def has_api_key(self) -> bool:
        """True when a non-empty Anthropic API key is configured."""
        return self.anthropic_api_key is not None and self.anthropic_api_key.get_secret_value() != ""

    @property
    def use_claude(self) -> bool:
        """Use the Claude extractor: a key is set and DEAL_EXTRACTOR doesn't force regex."""
        return self.has_api_key and self.deal_extractor != FORCE_REGEX

    def api_key_value(self) -> str | None:
        """The plaintext key, or None. The only way to it; meant for `anthropic.Anthropic(api_key=...)`.

        Never log or print the result.
        """
        if not self.has_api_key or self.anthropic_api_key is None:
            return None
        return self.anthropic_api_key.get_secret_value()


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read settings from `environ`, or from `os.environ` at the time of the call.

    Pass a mapping to build settings from something other than the process environment (tests);
    `os.environ` is then not consulted at all. Missing variables take the defaults documented on
    `Settings`. Never raises for odd values: an unknown DEAL_EXTRACTOR just means "not regex".
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    key = env.get("ANTHROPIC_API_KEY", "")
    return Settings(
        anthropic_api_key=SecretStr(key) if key else None,
        deal_extractor=env.get("DEAL_EXTRACTOR", ""),
        deal_model=env.get("DEAL_MODEL", ""),
    )


def load_dotenv_file(path: Path = DOTENV_PATH) -> bool:
    """Load `path` into `os.environ` with python-dotenv; True if the file existed and was read.

    `override=False`: a variable already set in the shell wins over the file, so
    `DEAL_EXTRACTOR=regex deal-eval` works even when `.env` says otherwise. Call this only from CLI
    entry points, never from library code or tests (which must not pick up a real key). Nothing
    is printed, and a missing file is not an error (returns False).
    """
    if not path.is_file():
        return False
    return load_dotenv(path, override=False)
