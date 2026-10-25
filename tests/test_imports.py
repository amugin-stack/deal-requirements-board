"""Every module under ``deal_board`` imports cleanly (contract item 7).

Catches what a file move tends to break: stale imports, circular imports that only show up
when a module is imported first, import-time side effects (config, anthropic), the frozen
baseline becoming importable, and a top-level ``schema`` alias leaking from the loader.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import deal_board

EXPECTED_MODULES = frozenset(
    {
        "deal_board.api",
        "deal_board.brokers",
        "deal_board.config",
        "deal_board.followup",
        "deal_board.matcher",
        "deal_board.paths",
        "deal_board.schema",
        "deal_board.scoring",
        "deal_board.extractors",
        "deal_board.extractors.claude",
        "deal_board.extractors.regex_baseline",
        "deal_board.cli",
        "deal_board.cli.eval",
        "deal_board.cli.build_brokers",
    }
)

PACKAGE_DIR = Path(deal_board.__file__).resolve().parent


def _discover() -> list[str]:
    return sorted(m.name for m in pkgutil.walk_packages(deal_board.__path__, prefix="deal_board."))


ALL_MODULES = _discover()


def _env_without_key() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}


def _run_isolated(code: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter with ``-I`` from ``cwd`` (outside the repo), so only
    the installed package is importable and sys.modules starts empty."""
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=cwd,
        env=_env_without_key(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_discovered_modules_cover_contract_layout() -> None:
    """The contract's layout is the minimum; a missing module means the move lost a file."""
    missing = sorted(EXPECTED_MODULES - set(ALL_MODULES))
    assert not missing, f"modules in the contract layout but not discovered: {missing}"


@pytest.mark.parametrize("name", ALL_MODULES)
def test_module_imports(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every discovered module must import in-process without a key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    importlib.import_module(name)


@pytest.mark.parametrize("name", ALL_MODULES)
def test_module_imports_alone_in_fresh_interpreter(name: str, tmp_path: Path) -> None:
    """Importing a module first, alone, exposes circular imports that a warm sys.modules hides."""
    proc = _run_isolated(f"import {name}", tmp_path)
    assert proc.returncode == 0, f"`import {name}` failed in a fresh interpreter:\n{proc.stderr}"


def test_frozen_is_not_a_package() -> None:
    """The frozen baseline is data loaded by a wrapper; it must not be importable by accident."""
    assert not (PACKAGE_DIR / "extractors" / "frozen" / "__init__.py").exists()
    assert "deal_board.extractors.frozen" not in ALL_MODULES
    try:
        spec = importlib.util.find_spec("deal_board.extractors.frozen")
    except ImportError:
        spec = None
    if spec is not None:
        with pytest.raises(ImportError):
            importlib.import_module("deal_board.extractors.frozen.regex_baseline")


def test_frozen_not_importable_in_fresh_interpreter(tmp_path: Path) -> None:
    """Same check without this process's sys.modules: a namespace-package fallback must fail too."""
    code = (
        "import importlib.util, sys\n"
        "try:\n"
        "    spec = importlib.util.find_spec('deal_board.extractors.frozen')\n"
        "except ImportError:\n"
        "    spec = None\n"
        "if spec is None:\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    import deal_board.extractors.frozen.regex_baseline\n"
        "except ImportError:\n"
        "    sys.exit(0)\n"
        "sys.exit('deal_board.extractors.frozen.regex_baseline imported as a module')\n"
    )
    proc = _run_isolated(code, tmp_path)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_package_import_has_no_side_effects(tmp_path: Path) -> None:
    """``deal_board/__init__.py`` is docstring-only: importing it must not pull in the
    Anthropic SDK or the config module (no import-time environment reads)."""
    code = (
        "import sys\n"
        "import deal_board\n"
        "bad = sorted(m for m in sys.modules if m == 'anthropic' or m.startswith('anthropic.')"
        " or m == 'deal_board.config')\n"
        "print(','.join(bad))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    proc = _run_isolated(code, tmp_path)
    assert proc.returncode == 0, f"importing deal_board loaded: {proc.stdout.strip()}\n{proc.stderr}"


def test_regex_loader_does_not_leak_schema_alias(tmp_path: Path) -> None:
    """The loader maps the frozen file's ``from schema import`` with a scoped ``__import__``;
    it must not register a top-level ``schema`` module for everyone else."""
    code = (
        "import sys\n"
        "import deal_board.extractors.regex_baseline\n"
        "sys.exit('schema leaked into sys.modules' if 'schema' in sys.modules else 0)\n"
    )
    proc = _run_isolated(code, tmp_path)
    assert proc.returncode == 0, proc.stderr or proc.stdout
