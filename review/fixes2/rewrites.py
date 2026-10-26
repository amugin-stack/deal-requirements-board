"""Rewritten equivalents of the survivor diffs that no longer apply to the current tree.

The gate-mutants loop wrote its diffs against `main` at c47ee1f. The first fix loop then
changed the same lines (get_as_of now goes through `licensing_date`, load_brokers reads row by
row, build_brokers trims columns once in `_trimmed`, the Claude extractor checks the raw tool
input), so 18 stored diffs fail to apply. Each rewrite below makes the same bug, in the same
function, in today's code. They are exact `(old, new)` edits like `review/mutants/mutants.py`,
turned into unified diffs with difflib, so the diff always matches the real file.

  python review/fixes2/rewrites.py            # print id -> diff as JSON
  python review/fixes2/rewrites.py --write    # store each diff + reason in dispositions.json

Mutants are only rendered as text here; nothing in the worktree is modified except
dispositions.json with --write.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DISPOSITIONS = HERE / "dispositions.json"

API, BROKERS, BUILD = "deal_board/api.py", "deal_board/brokers.py", "deal_board/cli/build_brokers.py"
MATCHER, CLAUDE, REGEX = (
    "deal_board/matcher.py",
    "deal_board/extractors/claude.py",
    "deal_board/extractors/regex_baseline.py",
)

AS_OF_LINE = "    return licensing_date(datetime.now(UTC))\n"
EXP_LINE = '                "license_expiration": exp.isoformat() if exp else "",\n'
WHY_API = "get_as_of now returns licensing_date(datetime.now(UTC)) instead of date.today()"
WHY_BUILD = "build_brokers now trims the classified columns once in _trimmed, so the old lines changed"


@dataclass(frozen=True)
class Rewrite:
    """The same bug as survivor `id`, expressed as exact edits to today's `file`."""

    id: str
    file: str
    reason: str
    edits: tuple[tuple[str, str], ...]

    def diff(self) -> str:
        """Unified diff of the edits against the current file; raises if an edit target is not unique."""
        original = (ROOT / self.file).read_text(encoding="utf-8")
        text = original
        for old, new in self.edits:
            if text.count(old) != 1:
                raise ValueError(f"{self.id}: edit target occurs {text.count(old)} times in {self.file}: {old!r}")
            text = text.replace(old, new)
        return "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                text.splitlines(keepends=True),
                fromfile=f"a/{self.file}",
                tofile=f"b/{self.file}",
            )
        )


REWRITES: tuple[Rewrite, ...] = (
    Rewrite(
        "api-10",
        API,
        WHY_API,
        (
            (
                AS_OF_LINE,
                "    today = licensing_date(datetime.now(UTC))\n    return today.replace(year=today.year - 1)\n",
            ),
        ),
    ),
    Rewrite(
        "gate-20",
        API,
        WHY_API,
        ((AS_OF_LINE, "    return date.fromordinal(licensing_date(datetime.now(UTC)).toordinal() - 1)\n"),),
    ),
    Rewrite(
        "gate-21",
        API,
        WHY_API + "; the bug is taking the UTC calendar date",
        ((AS_OF_LINE, "    return datetime.now(UTC).date()\n"),),
    ),
    Rewrite("gate-22", API, WHY_API, ((AS_OF_LINE, "    return date(2026, 10, 20)\n"),)),
    Rewrite(
        "gate-23",
        API,
        "get_brokers' docstring changed, so the stored diff's context no longer matches",
        (
            (
                "    return broker_store.load_brokers()\n",
                '    return [b for b in broker_store.load_brokers() if b.license_status == "Active"]\n',
            ),
        ),
    ),
    Rewrite(
        "gate-28",
        BROKERS,
        "load_brokers now loops over a named csv.DictReader to report the bad line number",
        (("        reader = csv.DictReader(f)\n", "        reader = csv.DictReader(f, skipinitialspace=True)\n"),),
    ),
    Rewrite(
        "gate-53",
        BROKERS,
        "load_brokers now loops over rows and appends, so the dedup moves to the return",
        (("    return brokers\n", "    return list({b.broker_id: b for b in brokers}.values())\n"),),
    ),
    Rewrite(
        "gate-35",
        MATCHER,
        "is_licensed now checks the snapshot age first, so the surrounding lines changed",
        (("        and broker.license_state == state\n", "        and state in broker.license_state\n"),),
    ),
    Rewrite(
        "gate-41",
        BUILD,
        "parse_date now strips first and rejects 4-digit years, so the strptime line changed",
        (
            (
                '        return datetime.strptime(s, "%m/%d/%y").date()\n',
                '        return datetime.strptime(s, "%d/%m/%y").date()\n',
            ),
        ),
    ),
    Rewrite(
        "gate-42",
        BUILD,
        "parse_date now strips first and rejects 4-digit years, so the context changed",
        (
            (
                "        return None  # an unreadable date is treated as no date, which fails the license check\n",
                "        return date(2099, 12, 31)  # an unreadable date is treated as no date, which fails the license check\n",
            ),
        ),
    ),
    Rewrite(
        "gate-43",
        BUILD,
        WHY_BUILD,
        (
            (
                '                "license_status": r["Status"],  # trimmed of surrounding whitespace, otherwise verbatim\n',
                '                "license_status": "Active" if r["Status"].endswith("Active") else r["Status"],\n',
            ),
        ),
    ),
    Rewrite(
        "gate-44",
        BUILD,
        WHY_BUILD,
        ((EXP_LINE, '                "license_expiration": exp.strftime("%Y-%d-%m") if exp else "",\n'),),
    ),
    Rewrite(
        "gate-45",
        BUILD,
        WHY_BUILD,
        ((EXP_LINE, '                "license_expiration": (exp or AS_OF).isoformat(),\n'),),
    ),
    Rewrite(
        "gate-47",
        BUILD,
        WHY_BUILD,
        (
            (
                '    return row["Status"] == "Active" and exp is not None and exp >= AS_OF\n',
                '    return row["Status"] == "Active"\n',
            ),
        ),
    ),
    Rewrite(
        "gate-49",
        BUILD,
        WHY_BUILD,
        (
            (
                '        people = [t for r in csv.DictReader(f) if (t := _trimmed(r))["License Type"] in PEOPLE]\n',
                "        people = [_trimmed(r) for r in csv.DictReader(f)]\n",
            ),
        ),
    ),
    Rewrite(
        "gate-50",
        BUILD,
        WHY_BUILD,
        (('                "source": f"trec_{AS_OF.isoformat()}",\n', '                "source": "fictional",\n'),),
    ),
    Rewrite(
        "gate-69",
        CLAUDE,
        "extract now runs check_claude_raw on the raw input before parse_requirement",
        (
            (
                "        check_claude_raw(raw)\n        return parse_requirement(raw)\n",
                "        check_claude_raw(raw)\n"
                '        if raw.get("state") is None:\n'
                "            import re\n"
                "            from typing import get_args\n"
                "\n"
                "            from deal_board.schema import State\n"
                "\n"
                '            guess = next((t for t in re.findall(r"\\b[A-Z]{2}\\b", text) if t in get_args(State)), None)\n'
                '            raw = {**raw, "state": guess}\n'
                "        return parse_requirement(raw)\n",
            ),
        ),
    ),
    Rewrite(
        "gate-71",
        REGEX,
        "the wrapper gained __reduce__ and an isinstance check, so the stored context moved",
        (
            (
                "        self._impl = _FROZEN_CLASS()\n",
                "        self._impl = _FROZEN_CLASS()\n        self._cache: dict[str, object] = {}\n",
            ),
            (
                "        result = self._impl.extract(text)\n",
                "        result = self._cache.setdefault(text[:40], self._impl.extract(text))\n",
            ),
        ),
    ),
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--write", action="store_true", help="store diff + rewrite_reason in dispositions.json")
    args = ap.parse_args(argv)
    diffs = {r.id: r.diff() for r in REWRITES}
    if not args.write:
        print(json.dumps(diffs, indent=2))
        return 0
    disp = json.loads(DISPOSITIONS.read_text(encoding="utf-8"))
    for r in REWRITES:
        disp[r.id]["diff"] = diffs[r.id]
        disp[r.id]["rewrite_reason"] = r.reason
    DISPOSITIONS.write_text(json.dumps(disp, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
