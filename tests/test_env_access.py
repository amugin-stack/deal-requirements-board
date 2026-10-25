"""Source-level guards: one env reader, no import-path hacks.

``deal_board/config.py`` is the only place allowed to read the environment or dotenv, so key
handling can be audited in one file. And nothing may edit ``sys.path``/PYTHONPATH, so tests
pass only when ``deal_board`` is a properly installed package.

Patterns are assembled by concatenation so this file never matches itself; it is also skipped
explicitly.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from pathlib import Path

import pytest

from deal_board.paths import REPO_ROOT

THIS_FILE = Path(__file__).resolve()
CONFIG_FILE = (REPO_ROOT / "deal_board" / "config.py").resolve()

# Word-boundary regexes: ``dotenv`` and ``load_dotenv`` must not match the sanctioned
# ``config.load_dotenv_file()`` call that CLI entry points make.
_OS = r"\bos" + r"\."
ENV_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        _OS + "environ",
        _OS + "getenv",
        r"\bget" + r"env\(",
        r"\benviron" + r"\[",
        r"from\s+os\s+import\s+.*\benv" + "iron",
        r"\bdot" + r"env\b",
        r"\bload_" + r"dotenv\b",
    )
)

_SP = r"\bsys" + r"\.path"
PATH_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        _SP + r"\.insert",
        _SP + r"\.append",
        _SP + r"\.extend",
        _SP + r"\s*\+=",
        _SP + r"\s*\[",
        _SP + r"\s*=(?!=)",
        r"\bPYTHON" + "PATH",
    )
)
_SYS_PATH_ANY = re.compile(_SP + r"\b")


def _py_files(*dirs: str) -> list[Path]:
    files: list[Path] = []
    for d in dirs:
        files.extend(sorted((REPO_ROOT / d).rglob("*.py")))
    return [f for f in files if f.resolve() != THIS_FILE and "__pycache__" not in f.parts]


def _hits(files: Iterable[Path], patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    found: list[str] = []
    for f in files:
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), start=1):
            for pat in patterns:
                if pat.search(line):
                    found.append(f"{f.relative_to(REPO_ROOT)}:{lineno}: /{pat.pattern}/: {line.strip()}")
    return found


def test_scanned_trees_are_not_empty() -> None:
    """Guards against a vacuous pass if the globs stop matching."""
    assert _py_files("deal_board"), "no .py files under deal_board/"
    assert _py_files("loop"), "no .py files under loop/"


def test_only_config_reads_environment() -> None:
    """Every env/dotenv read outside config.py is a second, unaudited path to the API key."""
    files = [f for f in _py_files("deal_board", "loop") if f.resolve() != CONFIG_FILE]
    offenders = _hits(files, ENV_PATTERNS)
    assert not offenders, "environment access outside deal_board/config.py:\n" + "\n".join(offenders)


def test_config_module_exists() -> None:
    """The exclusion above only makes sense if the single env reader exists."""
    assert CONFIG_FILE.is_file(), f"{CONFIG_FILE} missing"


def test_no_sys_path_manipulation() -> None:
    """A sys.path or PYTHONPATH edit would let tests pass against an uninstalled checkout."""
    offenders = _hits(_py_files("deal_board", "loop", "tests"), PATH_PATTERNS)
    assert not offenders, "sys.path / PYTHONPATH manipulation found:\n" + "\n".join(offenders)


def test_no_conftest_touches_sys_path() -> None:
    """conftest.py is the classic hiding place for path hacks; none may touch sys.path."""
    conftests = [
        p
        for p in REPO_ROOT.rglob("conftest.py")
        if not any(part in {".venv", "venv", ".git", "node_modules", "build", "dist"} for part in p.parts)
    ]
    offenders = _hits(conftests, (*PATH_PATTERNS, _SYS_PATH_ANY))
    assert not offenders, "conftest.py touches sys.path:\n" + "\n".join(offenders)


def test_pytest_config_has_no_pythonpath() -> None:
    """pytest's ``pythonpath`` option is a sys.path edit in disguise."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_cfg = data.get("tool", {}).get("pytest", {})
    for section_name, section in [
        ("tool.pytest", pytest_cfg),
        ("tool.pytest.ini_options", pytest_cfg.get("ini_options", {})),
    ]:
        assert "pythonpath" not in section, f"[{section_name}] sets pythonpath"


@pytest.mark.parametrize("name", ["pytest.ini", "setup.cfg", "tox.ini"])
def test_no_alternate_pytest_config(name: str) -> None:
    """A second config file could override pyproject's pytest settings (and add pythonpath)."""
    assert not (REPO_ROOT / name).exists(), f"{name} exists; pytest config must live only in pyproject.toml"
