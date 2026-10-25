"""Blind regression tests for fix set D (packaging): one test per finding, named after its id.

Written from the agreed contracts in review/fixes/dispositions.json, before any fix, so each
test pins the promised behavior rather than an implementation.
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
import pickle
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final

from packaging.requirements import Requirement
from packaging.utils import NormalizedName, canonicalize_name

from deal_board.extractors.regex_baseline import RegexExtractor

ROOT: Final = Path(__file__).resolve().parent.parent
PACKAGE_DIR: Final = ROOT / "deal_board"
AGENT_FILE: Final = ROOT / ".claude" / "agents" / "extractor-improver.md"
PATH_SUFFIXES: Final = (".py", ".md", ".json", ".jsonl", ".csv")

# The transport packages the review named; the closure walk must at least reach these.
TRANSPORT_FLOOR: Final = frozenset({"httpx2", "httpcore2", "truststore", "h11", "opentelemetry-api"})


def _pre_packaging_paths() -> frozenset[str]:
    """Flat paths the package move retired: `x.py`, `extractors/x.py`, `cli/x.py`, `scripts/x.py`."""
    flat = {str(p.relative_to(PACKAGE_DIR)) for p in PACKAGE_DIR.rglob("*.py") if "frozen" not in p.parts}
    flat |= {f"scripts/{p.name}" for p in (PACKAGE_DIR / "cli").glob("*.py") if p.name != "__init__.py"}
    flat |= {"scripts/eval.py", "scripts/build_brokers.py"}
    return frozenset(flat)


def _path_like_tokens(markdown: str) -> list[str]:
    """Path-like words: in backtick spans, any with '/' or a file suffix; in prose, `dir/file.ext` only.

    Prose is held to the stricter shape so phrases like "kept/reverted" or a bare "DRAFT.md"
    used as a name are not mistaken for paths, while a path written without backticks still counts.
    """
    tokens: list[str] = []
    for span in re.findall(r"`([^`]+)`", markdown):
        for word in span.split():
            word = word.strip("\"'(),:;")
            if "/" in word or word.endswith(PATH_SUFFIXES):
                tokens.append(word)
    prose = re.sub(r"`[^`]+`", " ", markdown)
    for word in prose.split():
        word = word.strip("\"'(),:;.*")
        if "/" in word and word.endswith(PATH_SUFFIXES):
            tokens.append(word)
    return tokens


def _is_repo_path(token: str) -> bool:
    """Exclude things that are paths but not repo files: the local venv and URLs."""
    return not (token.startswith((".venv/", "http://", "https://")) or token.startswith("<"))


# ---------------------------------------------------------------- compliance-security/S-6


def _runtime_requirements(dist: str) -> list[Requirement]:
    """Non-extra requirements of `dist` whose environment markers hold on this interpreter."""
    reqs: list[Requirement] = []
    for line in metadata.requires(dist) or []:
        req = Requirement(line)
        if req.marker is None or req.marker.evaluate({"extra": ""}):
            reqs.append(req)
    return reqs


def _runtime_closure(root: str) -> set[NormalizedName]:
    """Every distribution reachable from `root` through runtime requirements, `root` included."""
    seen: set[NormalizedName] = set()
    todo = [root]
    while todo:
        name = canonicalize_name(todo.pop())
        if name in seen:
            continue
        seen.add(name)
        todo.extend(req.name for req in _runtime_requirements(name))
    return seen


def _exact_pins() -> dict[NormalizedName, str]:
    """[project].dependencies of pyproject.toml as name -> version, for `==` pins only."""
    with (ROOT / "pyproject.toml").open("rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    pins: dict[NormalizedName, str] = {}
    for line in deps:
        req = Requirement(line)
        specs = list(req.specifier)
        if len(specs) == 1 and specs[0].operator == "==":
            pins[canonicalize_name(req.name)] = specs[0].version
    return pins


def test_s6_anthropic_transport_closure_pinned_exactly() -> None:
    """compliance-security/S-6: pyproject pins the anthropic SDK's runtime closure with == at installed versions."""
    closure = _runtime_closure("anthropic")
    assert {canonicalize_name(n) for n in TRANSPORT_FLOOR} <= closure, sorted(closure)
    pins = _exact_pins()
    missing = sorted(closure - pins.keys())
    assert not missing, f"unpinned (or not pinned with ==) in [project].dependencies: {missing}"
    wrong = {name: (pins[name], metadata.version(name)) for name in closure if pins[name] != metadata.version(name)}
    assert not wrong, f"pin != installed version (pin, installed): {wrong}"


# ---------------------------------------------------------------- compliance-security/S-7


def _ignored(name: str) -> int:
    """Return code of `git check-ignore --no-index -q name` at the repo root (0 ignored, 1 not)."""
    return subprocess.run(["git", "check-ignore", "--no-index", "-q", name], cwd=ROOT, check=False).returncode


def test_s7_gitignore_covers_secret_files_but_not_env_example() -> None:
    """compliance-security/S-7: .env variants, *.pem and *.key are ignored; .env.example is not."""
    secrets = [".env", ".env.local", ".env.production", ".env.development.local", "x.pem", "x.key"]
    codes = {name: _ignored(name) for name in [*secrets, ".env.example"]}
    assert codes == {**dict.fromkeys(secrets, 0), ".env.example": 1}


# ---------------------------------------------------------------- p10/P10-1

_IMPORT_PROBE: Final = """
import sys
try:
    import deal_board.paths
    import deal_board.brokers
except Exception as exc:
    print("FILE", getattr(sys.modules.get("deal_board"), "__file__", None))
    print("ERR", type(exc).__name__, exc)
    sys.exit(3)
print("FILE", sys.modules["deal_board"].__file__)
print("OK")
"""


def _probe_import(cwd: Path) -> subprocess.CompletedProcess[str]:
    """Import deal_board.paths and .brokers in a clean interpreter that finds deal_board in `cwd` first.

    `-c` puts the working directory first on the module path; `-E -s` drop the environment's
    path variables and the user site, so only `cwd` (then the venv) can supply `deal_board`.
    (`-I` would also drop `cwd`, and the repo's own guard forbids path edits in tests.)
    """
    return subprocess.run(
        [sys.executable, "-E", "-s", "-c", _IMPORT_PROBE],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_p10_1_package_without_data_fails_import_with_editable_hint(tmp_path: Path) -> None:
    """p10/P10-1: a deal_board copy with no data/ next to it fails import, saying to `pip install -e`."""
    shutil.copytree(PACKAGE_DIR, tmp_path / "deal_board", ignore=shutil.ignore_patterns("__pycache__"))
    assert not (tmp_path / "data").exists()
    proc = _probe_import(tmp_path)
    assert f"FILE {tmp_path / 'deal_board' / '__init__.py'}" in proc.stdout, proc.stdout + proc.stderr
    assert proc.returncode == 3, proc.stdout + proc.stderr
    err = next(line for line in proc.stdout.splitlines() if line.startswith("ERR "))
    assert "pip install -e" in err, err

    in_checkout = _probe_import(ROOT)
    assert in_checkout.returncode == 0, in_checkout.stdout + in_checkout.stderr
    assert f"FILE {PACKAGE_DIR / '__init__.py'}" in in_checkout.stdout


# ---------------------------------------------------------------- p10/P10-2


def test_p10_2_regex_extractor_pickles_and_extracts_the_same() -> None:
    """p10/P10-2: RegexExtractor survives a pickle round trip and extracts identically."""
    original = RegexExtractor()
    clone = pickle.loads(pickle.dumps(original))
    assert isinstance(clone, RegexExtractor)
    with (ROOT / "data" / "posts.jsonl").open(encoding="utf-8") as fh:
        texts = [str(json.loads(line)["text"]) for line in fh if line.strip()][:5]
    assert len(texts) == 5
    for text in texts:
        assert clone.extract(text) == original.extract(text)


# ---------------------------------------------------------------- p10/P10-4


def _architecture_table(readme: str) -> str:
    """The first markdown table under the README's architecture heading ("How it works")."""
    lines = readme.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(r"^#+\s+(How it works|Architecture)\b", line))
    table: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        if line.startswith("|"):
            table.append(line)
        elif table:
            break
    assert table, "no markdown table under the architecture heading"
    return "\n".join(table)


def test_p10_4_readme_architecture_table_uses_packaged_paths() -> None:
    """p10/P10-4: every path in the README architecture table exists; no pre-packaging path appears."""
    table = _architecture_table((ROOT / "README.md").read_text(encoding="utf-8"))
    tokens = [t for t in _path_like_tokens(table) if _is_repo_path(t)]
    assert tokens, "architecture table names no files"
    assert not [t for t in tokens if not (ROOT / t).exists()], tokens
    assert not sorted(set(tokens) & _pre_packaging_paths())


# ---------------------------------------------------------------- p10/P10-5


def test_p10_5_extractor_improver_agent_uses_packaged_paths() -> None:
    """p10/P10-5: every repo path in .claude/agents/extractor-improver.md exists; no pre-packaging path appears."""
    tokens = [t for t in _path_like_tokens(AGENT_FILE.read_text(encoding="utf-8")) if _is_repo_path(t)]
    assert tokens, "agent file names no files"
    assert not [t for t in tokens if not (ROOT / t).exists()], tokens
    assert not sorted(set(tokens) & _pre_packaging_paths())
