"""Mutant definitions and the runner for the licensing-gate mutation-testing loop.

A mutant is one or more exact text edits to a file under `deal_board/`. Its unified diff is
derived from the edits with difflib, so the diff stored in results.json always matches the
real file. Mutants are never applied to the worktree: the runner works on throwaway copies.

  python review/mutants/mutants.py setup --work DIR [--workers 4]
      copy the worktree into DIR/w<k> (no .git, no .venv), give each copy its own venv with
      `pip install -e '.[dev]'`, and check an unpatched copy gives the baseline result.
  python review/mutants/mutants.py run --work DIR [--ids A B ...] [--round N]
      apply each mutant to a copy, run the full suite there, restore the copy, and record
      status / killed_by in results.json (annotation fields already there are kept).
  python review/mutants/mutants.py show --work DIR ID
      run one mutant and print its failure count per test file (the proof step).

Run it with the worktree's `.venv/bin/python`. Nothing here touches the network or a key.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RESULTS = HERE / "results.json"
COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", "*.egg-info"
)
# The worktree gives 1003 passed / 1 skipped / 5 xfailed. Copies have no .git on purpose (so
# nothing run in a copy can touch the real repo), so test_frozen_baseline_matches_tag skips there
# too. It checks deal_board/extractors/frozen/, which no mutant touches, so no kill is lost.
BASELINE = {"passed": 1002, "skipped": 2, "xfailed": 5}
TEST_TIMEOUT_SECONDS = 600
MATCHER, API, BROKERS = "deal_board/matcher.py", "deal_board/api.py", "deal_board/brokers.py"
SCHEMA, PATHS, FOLLOWUP = "deal_board/schema.py", "deal_board/paths.py", "deal_board/followup.py"


@dataclass(frozen=True)
class Mutant:
    """A deliberate bug: exact `(old, new)` edits to `file`, each `old` occurring exactly once."""

    id: str
    file: str
    function: str
    description: str
    edits: tuple[tuple[str, str], ...]
    round: int = 1
    author: str = "lead"

    def mutated(self, original: str) -> str:
        """The file text with every edit applied; raises if an edit is missing or ambiguous."""
        text = original
        for old, new in self.edits:
            count = text.count(old)
            if count != 1:
                raise ValueError(f"{self.id}: edit target occurs {count} times in {self.file}: {old!r}")
            text = text.replace(old, new)
        if text == original:
            raise ValueError(f"{self.id}: edits leave {self.file} unchanged")
        return text

    def diff(self, root: Path = ROOT) -> str:
        """Unified diff (a/ b/ paths, 3 lines of context) of this mutant against `root`'s file."""
        original = (root / self.file).read_text(encoding="utf-8")
        lines = difflib.unified_diff(
            original.splitlines(keepends=True),
            self.mutated(original).splitlines(keepends=True),
            fromfile=f"a/{self.file}",
            tofile=f"b/{self.file}",
        )
        return "".join(lines)


# --- Mutant definitions ------------------------------------------------------------------------
# Filled in round by round from the mutant writers' proposals; see LOG.md.

MUTANTS: list[Mutant] = [
    Mutant(
        id="gate-00",
        file=MATCHER,
        function="is_licensed",
        description="Gate always returns True: every broker in every state passes, whatever the status or expiry.",
        edits=(("    return (\n        state is not None\n", "    return True or (\n        state is not None\n"),),
    ),
    Mutant(
        id="gate-01",
        file=MATCHER,
        function="is_licensed",
        description="Expiry compared by year only, so a license that expired earlier in the as_of year still counts as licensed.",
        edits=(
            (
                "        and broker.license_expiration >= as_of\n",
                "        and broker.license_expiration.year >= as_of.year\n",
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-02",
        file=MATCHER,
        function="is_licensed",
        description="Uses > instead of >=, so a license expiring exactly on as_of is wrongly rejected.",
        edits=(
            ("        and broker.license_expiration >= as_of\n", "        and broker.license_expiration > as_of\n"),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-03",
        file=MATCHER,
        function="is_licensed",
        description="Status compared case-insensitively, so 'ACTIVE' or 'active' pass the exact-'Active' gate.",
        edits=(
            (
                '        and broker.license_status == "Active"\n',
                '        and broker.license_status.lower() == "active"\n',
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-04",
        file=MATCHER,
        function="is_licensed",
        description="Status checked with startswith('Active'), so variants like 'Active - Inactive Sponsor' or 'Active (restricted)' pass.",
        edits=(
            (
                '        and broker.license_status == "Active"\n',
                '        and broker.license_status.startswith("Active")\n',
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-05",
        file=MATCHER,
        function="is_licensed",
        description="Status checked with substring 'Active' in status, so 'Probation - Active' and other compound statuses are licensed.",
        edits=(('        and broker.license_status == "Active"\n', '        and "Active" in broker.license_status\n'),),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-06",
        file=MATCHER,
        function="is_licensed",
        description="Missing expiration treated as licensed (fail-open), so brokers with unknown expiry pass.",
        edits=(
            (
                "        and broker.license_expiration is not None\n        and broker.license_expiration >= as_of\n",
                "        and (broker.license_expiration is None or broker.license_expiration >= as_of)\n",
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-07",
        file=MATCHER,
        function="is_licensed",
        description="State compared case-insensitively, so a lowercase/mis-cased state string matches a broker's state.",
        edits=(
            (
                "        and broker.license_state == state\n",
                "        and broker.license_state.lower() == state.lower()\n",
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-08",
        file=MATCHER,
        function="is_licensed",
        description="State compared by first letter only, so a TX broker matches deals in TN, etc.",
        edits=(("        and broker.license_state == state\n", "        and broker.license_state[0] == state[0]\n"),),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-09",
        file=MATCHER,
        function="is_licensed",
        description="Compares expiry to date.today() instead of as_of, making results clock-dependent and wrong for historical/future as_of.",
        edits=(
            (
                "        and broker.license_expiration >= as_of\n",
                "        and broker.license_expiration >= date.today()\n",
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-10",
        file=MATCHER,
        function="is_licensed",
        description="Off-by-one day: accepts a license that expired the day before as_of.",
        edits=(
            (
                "from datetime import date\n\nfrom pydantic",
                "from datetime import date, datetime, timedelta\n\nfrom pydantic",
            ),
            (
                "        and broker.license_expiration >= as_of\n",
                "        and broker.license_expiration >= as_of - timedelta(days=1)\n",
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-11",
        file=MATCHER,
        function="is_licensed",
        description="Empty or None deal state matches every broker instead of failing closed.",
        edits=(
            (
                "        state is not None\n        and broker.license_state == state\n",
                "        (not state or broker.license_state == state)\n",
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-12",
        file=MATCHER,
        function="is_licensed",
        description="Expiry compared by (year, month) only, so a license that expired earlier in the as_of month still counts.",
        edits=(
            (
                "        and broker.license_expiration >= as_of\n",
                "        and (broker.license_expiration.year, broker.license_expiration.month) >= (as_of.year, as_of.month)\n",
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-13",
        file=MATCHER,
        function="is_licensed",
        description="Timezone-style shift: as_of converted to a midnight datetime and shifted by a -6h UTC offset, landing on the previous day, so a license expired the day before passes.",
        edits=(
            (
                "from datetime import date\n\nfrom pydantic",
                "from datetime import date, datetime, timedelta\n\nfrom pydantic",
            ),
            (
                "        and broker.license_expiration >= as_of\n",
                "        and broker.license_expiration >= (datetime.combine(as_of, datetime.min.time()) - timedelta(hours=6)).date()\n",
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-14",
        file=MATCHER,
        function="is_licensed",
        description="Status conjunct dropped entirely, so Expired/Inactive/Probation brokers with a future expiration date are licensed.",
        edits=(('        and broker.license_status == "Active"\n', ""),),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-15",
        file=MATCHER,
        function="is_licensed",
        description="'or' instead of 'and' before the status check, so (state matches) alone or (Active and unexpired in any state) is enough.",
        edits=(('        and broker.license_status == "Active"\n', '        or broker.license_status == "Active"\n'),),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-16",
        file=MATCHER,
        function="is_licensed",
        description="Expiration date comparison dropped, so any Active in-state broker with a non-null but past expiration is licensed.",
        edits=(
            (
                "        and broker.license_expiration is not None\n        and broker.license_expiration >= as_of\n",
                "        and broker.license_expiration is not None\n",
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-17",
        file=BROKERS,
        function="Broker._blank_to_none",
        description="Loader normalizes strings with strip().title(), so 'ACTIVE', ' active ' etc. become 'Active' and pass the exact-status gate (also rewrites 'verbatim' status).",
        edits=(
            (
                '        return None if v == "" else v\n',
                '        return None if v == "" else (v.strip().title() if isinstance(v, str) else v)\n',
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="gate-18",
        file=MATCHER,
        function="is_licensed",
        description="Expiry compared against as_of + 1 day (off-by-one the other way), so a license expiring exactly on as_of is rejected.",
        edits=(
            (
                "from datetime import date\n\nfrom pydantic",
                "from datetime import date, datetime, timedelta\n\nfrom pydantic",
            ),
            (
                "        and broker.license_expiration >= as_of\n",
                "        and broker.license_expiration >= as_of + timedelta(days=1)\n",
            ),
        ),
        round=1,
        author="gate-writer",
    ),
    Mutant(
        id="match-01",
        file=MATCHER,
        function="match",
        description="When limit >= len(brokers) the gate is skipped and every broker is ranked, so unlicensed brokers are returned for large limits.",
        edits=(
            (
                "    licensed = [b for b in brokers if is_licensed(b, req.state, as_of)]\n",
                "    licensed = brokers if limit >= len(brokers) else [b for b in brokers if is_licensed(b, req.state, as_of)]\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-02",
        file=MATCHER,
        function="match",
        description="excluded_for_licensing counts the licensed brokers instead of the rejected ones.",
        edits=(
            (
                "        excluded_for_licensing=len(brokers) - len(licensed),\n",
                "        excluded_for_licensing=len(licensed),\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-03",
        file=MATCHER,
        function="match",
        description="Ranking sorts over all brokers instead of the licensed list, returning ungated brokers.",
        edits=(("        (_score(req, b) for b in licensed),\n", "        (_score(req, b) for b in brokers),\n"),),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-04",
        file=MATCHER,
        function="match",
        description="When fewer than limit brokers are licensed, results are padded with unlicensed brokers to fill the page.",
        edits=(
            (
                "        matches=[m for m, _ in scored[: max(limit, 0)]],\n",
                "        matches=[m for m, _ in scored[: max(limit, 0)]]\n        + [_score(req, b)[0] for b in brokers if b not in licensed][: max(limit - len(scored), 0)],\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-05",
        file=MATCHER,
        function="_score",
        description="Sales Agent note is never appended, so referral-fee compliance note is dropped.",
        edits=(
            (
                '    if broker.license_type == "Sales Agent":\n        reasons.append(SALES_AGENT_NOTE)\n',
                '    if broker.license_type == "Sales agent":\n        reasons.append(SALES_AGENT_NOTE)\n',
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-06",
        file=MATCHER,
        function="match",
        description="Matcher sorts the shared cached broker list in place by broker_id before gating, mutating get_brokers() state across requests.",
        edits=(
            (
                "    licensed = [b for b in brokers if is_licensed(b, req.state, as_of)]\n",
                "    brokers.sort(key=lambda b: b.broker_id)\n    licensed = [b for b in brokers if is_licensed(b, req.state, as_of)]\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-07",
        file=MATCHER,
        function="match",
        description="Unlicensed brokers are removed from the caller's list in place, mutating the shared cache (and making excluded count 0).",
        edits=(
            (
                "    licensed = [b for b in brokers if is_licensed(b, req.state, as_of)]\n",
                "    licensed = [b for b in brokers if is_licensed(b, req.state, as_of)]\n    brokers[:] = licensed\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-08",
        file=MATCHER,
        function="match",
        description="excluded_for_licensing computed as len(brokers) - len(matches), so licensed brokers cut by limit are counted as excluded.",
        edits=(
            (
                "        matches=[m for m, _ in scored[: max(limit, 0)]],\n        excluded_for_licensing=len(brokers) - len(licensed),\n",
                "        matches=[m for m, _ in scored[: max(limit, 0)]],\n        excluded_for_licensing=len(brokers) - len(scored[: max(limit, 0)]),\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-09",
        file=MATCHER,
        function="match",
        description="Tie-break on rules-matched is flipped, so brokers with fewer matched rules rank first among equal scores.",
        edits=(
            (
                "        key=lambda pair: (-pair[0].score, -pair[1], pair[0].broker.broker_id),\n",
                "        key=lambda pair: (-pair[0].score, pair[1], pair[0].broker.broker_id),\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-10",
        file=MATCHER,
        function="match",
        description="limit is ignored; every licensed broker is returned.",
        edits=(
            ("        matches=[m for m, _ in scored[: max(limit, 0)]],\n", "        matches=[m for m, _ in scored],\n"),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-11",
        file=MATCHER,
        function="match",
        description="Negative limit slices scored[:limit], returning all but the last |limit| brokers instead of none.",
        edits=(
            (
                "        matches=[m for m, _ in scored[: max(limit, 0)]],\n",
                "        matches=[m for m, _ in scored[:limit]],\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-12",
        file=MATCHER,
        function="match",
        description="Non-deal posts report every broker as excluded instead of 0.",
        edits=(
            (
                "        return MatchResult(matches=[], excluded_for_licensing=0)\n",
                "        return MatchResult(matches=[], excluded_for_licensing=len(brokers))\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-13",
        file=MATCHER,
        function="match",
        description="Non-deal check removed, so non-deal posts with a state get matched against licensed brokers.",
        edits=(
            ("    if not req.is_deal_request:\n        return MatchResult(matches=[], excluded_for_licensing=0)\n", ""),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-14",
        file=MATCHER,
        function="match",
        description="A deal with no state skips the gate and matches every broker.",
        edits=(
            (
                "    licensed = [b for b in brokers if is_licensed(b, req.state, as_of)]\n",
                "    licensed = [b for b in brokers if req.state is None or is_licensed(b, req.state, as_of)]\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-15",
        file=MATCHER,
        function="match",
        description="broker_id tie-break is descending, so equal-score/equal-rule ties order Z..A instead of A..Z.",
        edits=(
            (
                "        key=lambda pair: (-pair[0].score, -pair[1], pair[0].broker.broker_id),\n    )\n",
                "        key=lambda pair: (-pair[0].score, -pair[1]),\n    )\n    scored = sorted(scored, key=lambda pair: pair[0].broker.broker_id, reverse=True)\n    scored = sorted(scored, key=lambda pair: (-pair[0].score, -pair[1]))\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-16",
        file=MATCHER,
        function="match",
        description="excluded_for_licensing is off by one (len(brokers) - len(licensed) - 1).",
        edits=(
            (
                "        excluded_for_licensing=len(brokers) - len(licensed),\n",
                "        excluded_for_licensing=max(len(brokers) - len(licensed) - 1, 0),\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-17",
        file=MATCHER,
        function="match",
        description="Ranking sorts by score ascending, so worst-fit licensed brokers come first.",
        edits=(
            (
                "        key=lambda pair: (-pair[0].score, -pair[1], pair[0].broker.broker_id),\n",
                "        key=lambda pair: (pair[0].score, -pair[1], pair[0].broker.broker_id),\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-18",
        file=MATCHER,
        function="_score",
        description="Rules-matched tie-breaker counts all rules rather than those that earned points, so it never breaks ties.",
        edits=(("reasons=reasons), len(points)\n", "reasons=reasons), len(RULES)\n"),),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-19",
        file=MATCHER,
        function="_score",
        description="Sales Agent note replaces the scoring reasons instead of being appended.",
        edits=(("        reasons.append(SALES_AGENT_NOTE)\n", "        reasons = [SALES_AGENT_NOTE]\n"),),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="match-20",
        file=MATCHER,
        function="match",
        description="Gate checks the broker's own license_state instead of the deal state, so any active in-date broker from any state passes.",
        edits=(
            (
                "    licensed = [b for b in brokers if is_licensed(b, req.state, as_of)]\n",
                "    licensed = [b for b in brokers if req.state is not None and is_licensed(b, b.license_state, as_of)]\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="api-01",
        file=API,
        function="match_post",
        description="API builds MatchResponse.model_construct with a sign-flipped excluded count, skipping the ge=0 validator inside the route (FastAPI response_model revalidation may still 500; calling match_post directly returns a negative count).",
        edits=(
            (
                "    return MatchResponse(\n        # Extractor",
                "    return MatchResponse.model_construct(\n        # Extractor",
            ),
            (
                "        excluded_for_licensing=result.excluded_for_licensing,\n        follow_up",
                "        excluded_for_licensing=-result.excluded_for_licensing,\n        follow_up",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="api-02",
        file=API,
        function="match_post",
        description="BrokerMatch built with model_construct, skipping field validation so a Sequence/tuple reasons or wrong-typed fields from a fake matcher pass through unvalidated (observable via direct match_post call returning non-list reasons).",
        edits=(
            (
                "        BrokerMatch(\n            broker_id",
                "        BrokerMatch.model_construct(\n            broker_id",
            ),
            ("            reasons=list(m.reasons),\n", "            reasons=m.reasons,\n"),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="api-03",
        file=API,
        function="match_post",
        description="API passes date.today() instead of the injected as_of, so pinned dates are ignored and expired-as-of brokers can be returned.",
        edits=(
            (
                "    result = match(requirement, brokers, as_of)\n",
                "    result = match(requirement, brokers, date.today())\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="api-04",
        file=API,
        function="match_post",
        description="API drops reasons (returns empty list), losing the Sales Agent note and fit explanations.",
        edits=(("            reasons=list(m.reasons),\n", "            reasons=[],\n"),),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="api-05",
        file=API,
        function="match_post",
        description="license_number is filled from broker_id instead of the license number.",
        edits=(
            (
                "            license_number=m.broker.license_number,\n",
                "            license_number=m.broker.broker_id,\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="api-06",
        file=API,
        function="match_post",
        description="API reports excluded_for_licensing as 0 always.",
        edits=(
            (
                "        excluded_for_licensing=result.excluded_for_licensing,\n        follow_up",
                "        excluded_for_licensing=0,\n        follow_up",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="api-07",
        file=API,
        function="match_post",
        description="API sorts the shared cached get_brokers() list in place by name before matching, mutating state across requests.",
        edits=(
            (
                "    result = match(requirement, brokers, as_of)\n",
                "    brokers.sort(key=lambda b: b.name)\n    result = match(requirement, brokers, as_of)\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="api-08",
        file=API,
        function="match_post",
        description="API reshapes only the first match's broker for every row (uses result.matches[0]), duplicating one broker.",
        edits=(
            (
                "            broker_id=m.broker.broker_id,\n",
                "            broker_id=result.matches[0].broker.broker_id,\n",
            ),
        ),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="api-09",
        file=API,
        function="match_post",
        description="license_type taken from broker.source, so Sales Agents are not identifiable to the client.",
        edits=(("            license_type=m.broker.license_type,\n", "            license_type=m.broker.source,\n"),),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="api-10",
        file=API,
        function="get_as_of",
        description="get_as_of returns a date one year in the past, so licenses expired within the last year pass the gate.",
        edits=(("    return date.today()\n", "    return date.today().replace(year=date.today().year - 1)\n"),),
        round=1,
        author="match-api-writer",
    ),
    Mutant(
        id="gate-20",
        file=API,
        function="get_as_of",
        description="get_as_of returns yesterday (off by one day), so a license that expired yesterday passes the gate in production.",
        edits=(("    return date.today()\n", "    return date.fromordinal(date.today().toordinal() - 1)\n"),),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-21",
        file=API,
        function="get_as_of",
        description="get_as_of uses the UTC calendar date instead of the local date, so for part of each day (US evenings) as_of is tomorrow/yesterday and a license expiring on the boundary day is wrongly accepted or rejected.",
        edits=(
            ("from datetime import date\n", "from datetime import UTC, date, datetime\n"),
            ("    return date.today()\n", "    return datetime.now(UTC).date()\n"),
        ),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-22",
        file=API,
        function="get_as_of",
        description="get_as_of returns a frozen constant date (the TREC snapshot date 2026-10-20), so every license expiring after that date but before today keeps passing forever.",
        edits=(("    return date.today()\n", "    return date(2026, 10, 20)\n"),),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-23",
        file=API,
        function="get_brokers",
        description="get_brokers pre-filters the cache to Active-status records, so excluded_for_licensing undercounts (non-Active brokers silently vanish from the count).",
        edits=(
            (
                "    return broker_store.load_brokers()\n",
                '    return [b for b in broker_store.load_brokers() if b.license_status == "Active"]\n',
            ),
        ),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-24",
        file=API,
        function="match_post",
        description="API passes the requirement to the matcher with a lower-cased state, so the exact-match gate rejects every broker and every deal returns zero matches with all brokers 'excluded'.",
        edits=(
            (
                "    result = match(requirement, brokers, as_of)\n",
                '    result = match(requirement.model_copy(update={"state": requirement.state.lower() if requirement.state else None}), brokers, as_of)\n',
            ),
        ),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-25",
        file=BROKERS,
        function="Broker._blank_to_none",
        description="Broker validator strips every string field, so a padded status like 'Active ' (or ' Active') is normalised to 'Active' and passes the exact-status gate, breaking the verbatim-status rule.",
        edits=(
            (
                '        return None if v == "" else v\n',
                '        return None if v == "" else (v.strip() if isinstance(v, str) else v)\n',
            ),
        ),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-26",
        file=BROKERS,
        function="Broker.license_state",
        description="Broker.license_state widened from the State Literal to str, so malformed broker states ('tx', 'Texas', 'TX ') load without error instead of being rejected at load time.",
        edits=(("    license_state: State\n", "    license_state: str\n"),),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-27",
        file=BROKERS,
        function="Broker._split_list",
        description="_split_list drops already-parsed lists (returns [] for any non-string), so brokers built programmatically lose markets/property/transaction types and their fit points.",
        edits=(("if isinstance(v, str) else (v or [])", "if isinstance(v, str) else []"),),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-28",
        file=BROKERS,
        function="load_brokers",
        description="load_brokers reads the CSV with skipinitialspace=True, so a leading-space status like ' Active' is silently normalised to 'Active' and passes the gate.",
        edits=(("csv.DictReader(f)", "csv.DictReader(f, skipinitialspace=True)"),),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-29",
        file=SCHEMA,
        function="ExtractedFields._upper_state",
        description="_upper_state no longer upper-cases, so a lowercase extracted state like 'tx' fails validation (extraction error) instead of matching TX brokers.",
        edits=(
            (
                "        return v.strip().upper() if isinstance(v, str) and v.strip() else None\n",
                "        return v.strip() if isinstance(v, str) and v.strip() else None\n",
            ),
        ),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-30",
        file=SCHEMA,
        function="ExtractedFields._upper_state",
        description="_upper_state no longer strips, so a padded state like ' tx ' fails validation instead of normalising to 'TX'.",
        edits=(
            (
                "        return v.strip().upper() if isinstance(v, str) and v.strip() else None\n",
                "        return v.upper() if isinstance(v, str) and v.strip() else None\n",
            ),
        ),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-31",
        file=SCHEMA,
        function="ExtractedFields._upper_state",
        description="Blank/whitespace state is no longer turned into None, so state '' or '  ' raises a validation error instead of becoming 'missing state'.",
        edits=(
            (
                "        return v.strip().upper() if isinstance(v, str) and v.strip() else None\n",
                "        return v.strip().upper() if isinstance(v, str) else None\n",
            ),
        ),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-32",
        file=SCHEMA,
        function="ExtractedFields.state",
        description="Deal state widened from the State Literal to any string, so 'Texas', 'T' or 'X' are accepted as a deal state (fails closed at the exact gate, but the 2-letter contract and the Claude tool schema enum are lost).",
        edits=(("    state: State | None = Field(\n", "    state: str | None = Field(\n"),),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-33",
        file=SCHEMA,
        function="DealRequirement._compute_missing",
        description="missing_fields is computed for non-deal posts too, so news/hiring posts report missing fields (and may trigger follow-up questions).",
        edits=(("getattr(self, f) is None] if self.is_deal_request else []", "getattr(self, f) is None]"),),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-34",
        file=SCHEMA,
        function="ExtractedFields.is_deal_request",
        description="is_deal_request gets a default of True, so a payload missing the flag is treated as a deal request instead of failing validation (and the Claude tool schema no longer requires it).",
        edits=(("    is_deal_request: bool = Field(\n", "    is_deal_request: bool = Field(\n        True,\n"),),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-35",
        file=MATCHER,
        function="is_licensed",
        description="State checked by substring (state in license_state), so a deal state '' / 'T' / 'X' matches TX brokers.",
        edits=(("        and broker.license_state == state\n", "        and state in broker.license_state\n"),),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-36",
        file=MATCHER,
        function="is_licensed",
        description="Status compared after .strip(), so padded statuses like 'Active ' or ' Active' pass the exact-'Active' gate.",
        edits=(
            (
                '        and broker.license_status == "Active"\n',
                '        and broker.license_status.strip() == "Active"\n',
            ),
        ),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-37",
        file=MATCHER,
        function="is_licensed",
        description="Expiration compared as US-format MM/DD/YYYY strings, so e.g. a license that expired 12/01/2025 sorts after 10/20/2026 and passes, while valid January expirations are rejected.",
        edits=(
            (
                "        and broker.license_expiration >= as_of\n",
                '        and broker.license_expiration.strftime("%m/%d/%Y") >= as_of.strftime("%m/%d/%Y")\n',
            ),
        ),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="gate-38",
        file=API,
        function="match_post",
        description="API forces is_deal_request=True before matching, so non-deal posts (news, hiring) with a state return licensed brokers and nonzero excluded counts.",
        edits=(
            (
                "    result = match(requirement, brokers, as_of)\n",
                '    result = match(requirement.model_copy(update={"is_deal_request": True}), brokers, as_of)\n',
            ),
        ),
        round=2,
        author="gate-writer",
    ),
    Mutant(
        id="match-22",
        file=MATCHER,
        function="_market_point",
        description="County fallback applied to non-TX deals, so an Arlington VA deal gives +2 to a Tarrant County TX broker (same-state licensing still gates, but ranking is wrong).",
        edits=(
            (
                'county = TX_CITY_TO_COUNTY.get(city) if req.state == "TX" else None',
                "county = TX_CITY_TO_COUNTY.get(city)",
            ),
        ),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-23",
        file=MATCHER,
        function="_market_point",
        description="Drops the county-is-not-None guard, so a city missing from the map 'matches' any broker whose county is also unknown (None == None) and gets +2.",
        edits=(("if county is not None and county == broker.county:", "if county == broker.county:"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-24",
        file=MATCHER,
        function="_market_point",
        description="City compared case-sensitively against raw market strings, so 'austin' or 'Austin ' no longer match a broker listing 'Austin'.",
        edits=(("if city in {m.strip().lower() for m in broker.markets}:", "if req.city in set(broker.markets):"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-25",
        file=MATCHER,
        function="_deal_size_point",
        description="Deal-size bounds made exclusive, so a deal exactly at a broker's min or max no longer earns the +1.",
        edits=(("not lo <= value <= hi", "not lo < value < hi"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-26",
        file=MATCHER,
        function="_deal_size_point",
        description="Open-ended ranges are filled with 0 / 10**12, so a broker with an unknown min or max gets +1 for any deal size.",
        edits=(
            (
                "lo, hi, value = broker.min_deal_usd, broker.max_deal_usd, req.deal_value_usd",
                "lo, hi, value = broker.min_deal_usd or 0, broker.max_deal_usd or 10**12, req.deal_value_usd",
            ),
        ),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-27",
        file=MATCHER,
        function="_property_point",
        description="Property match scores 2 points while the reason still says +3, so score and reasons disagree and property ties with transaction/market.",
        edits=(('return 3, f"+3 specializes in', 'return 2, f"+3 specializes in'),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-28",
        file=MATCHER,
        function="_transaction_point",
        description="Transaction reason drops the transaction type ('+2 handles deals'), so the explanation no longer says which deal type matched.",
        edits=(('f"+2 handles {req.transaction_type.value} deals"', 'f"+2 handles deals"'),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-29",
        file=MATCHER,
        function="_score",
        description="Sales Agent detection checks license_type != 'Broker', so 'Broker Individual' licensees also get the sponsoring-broker referral note.",
        edits=(('if broker.license_type == "Sales Agent":', 'if broker.license_type != "Broker":'),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-30",
        file=MATCHER,
        function="match",
        description="Final tie-break uses broker name instead of broker_id, so equal-score ties order by name and are not deterministic for duplicate names.",
        edits=(("pair[0].broker.broker_id),", "pair[0].broker.name),"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-31",
        file=MATCHER,
        function="match",
        description="Limit applied before ranking: only the first `limit` licensed brokers (in input order) are scored, so better-fit brokers later in the list are dropped.",
        edits=(
            ("(_score(req, b) for b in licensed),", "(_score(req, b) for b in licensed[: max(limit, 0)]),"),
            ("scored[: max(limit, 0)]", "scored"),
        ),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-32",
        file=MATCHER,
        function="match",
        description="Limit floor is 1 instead of 0, so limit=0 (or negative) still returns the top broker.",
        edits=(("scored[: max(limit, 0)]", "scored[: max(limit, 1)]"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-33",
        file=MATCHER,
        function="match",
        description="Default limit changed from 5 to 10, so callers that rely on the default (the API) get up to 10 matches.",
        edits=(("limit: int = 5", "limit: int = 10"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="match-34",
        file=MATCHER,
        function="_score",
        description="Score counts rules matched instead of summing their points, so every matching rule is worth 1 and property/transaction weighting is lost.",
        edits=(("score=sum(n for n, _ in points)", "score=len(points)"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="api-11",
        file=API,
        function="MatchResponse",
        description="Field(ge=0) removed from excluded_for_licensing, so a negative count from a buggy matcher is returned instead of failing validation.",
        edits=(("excluded_for_licensing: int = Field(ge=0)", "excluded_for_licensing: int"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="api-12",
        file=API,
        function="match_post",
        description="follow_up_question always None, so posts missing state or other key fields never get the follow-up question.",
        edits=(("follow_up_question=follow_up_question(requirement),", "follow_up_question=None,"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="api-13",
        file=API,
        function="match_post",
        description="Response requirement has missing_fields cleared, so the client is told nothing is missing even when state/city are null.",
        edits=(("requirement=requirement,", 'requirement=requirement.model_copy(update={"missing_fields": []}),'),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="api-14",
        file=API,
        function="match_post",
        description="API reshapes only the first 3 matches, silently truncating the matcher's ranked list.",
        edits=(("for m in result.matches\n", "for m in result.matches[:3]\n"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="api-15",
        file=API,
        function="match_post",
        description="BrokerMatch.score is len(reasons) instead of the matcher's score, so reported scores are wrong (and inflated by the Sales Agent note).",
        edits=(("score=m.score,", "score=len(m.reasons),"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="api-16",
        file=API,
        function="match_post",
        description="Reasons are sorted alphabetically, so the Sales Agent compliance note and point lines lose their order.",
        edits=(("reasons=list(m.reasons),", "reasons=sorted(m.reasons),"),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="api-17",
        file=API,
        function="match_post",
        description="Extractor name hardcoded to 'regex', so Claude-extracted responses are mislabeled.",
        edits=(('cast(Literal["claude", "regex"], extractor.name)', '"regex"'),),
        round=2,
        author="match-api-writer",
    ),
    Mutant(
        id="gate-41",
        file="deal_board/cli/build_brokers.py",
        function="parse_date",
        description="Expiry parsed as %d/%m/%y (day/month swapped), so e.g. an expired 01/12/26 (Jan 12) is written as 2026-12-01 and an expired Active TREC licensee looks unexpired (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)",
        edits=(('"%m/%d/%y"', '"%d/%m/%y"'),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-42",
        file="deal_board/cli/build_brokers.py",
        function="parse_date",
        description="An unreadable/blank expiry defaults to 2099-12-31 instead of None, so a licensee with unknown expiry is written as valid far into the future (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)",
        edits=(("return None  # an unreadable date", "return date(2099, 12, 31)  # an unreadable date"),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-43",
        file="deal_board/cli/build_brokers.py",
        function="sample_trec",
        description="Status mapping: any TREC status ending in 'Active' (e.g. 'Probation - Active') is written as plain 'Active', so probationary licensees pass the exact-'Active' gate (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)",
        edits=(
            (
                '"license_status": r["Status"].strip(),',
                '"license_status": "Active" if r["Status"].strip().endswith("Active") else r["Status"].strip(),',
            ),
        ),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-44",
        file="deal_board/cli/build_brokers.py",
        function="sample_trec",
        description="Expiration written with month and day swapped (%Y-%d-%m), so for day<=12 dates a past expiry can become a future one (2026-03-10 -> 2026-10-03 still past, but 2026-01-12 -> 2026-12-01 future) and an expired licensee looks valid (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)",
        edits=(('exp.isoformat() if exp else ""', 'exp.strftime("%Y-%d-%m") if exp else ""'),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-45",
        file="deal_board/cli/build_brokers.py",
        function="sample_trec",
        description="A missing expiry is written as the snapshot date AS_OF instead of blank, so unknown-expiry licensees pass the gate on any as_of up to 2026-10-20 (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)",
        edits=(('exp.isoformat() if exp else ""', "(exp or AS_OF).isoformat()"),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-46",
        file="deal_board/cli/build_brokers.py",
        function="sample_trec",
        description="The 'Active but expired' stratum is emptied, so the hardest case for a status-only filter disappears from data/brokers.csv (no leak, but the test data stops exercising it) (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)",
        edits=(('[r for r in invalid if r["Status"] == "Active"][:N_INVALID]', "[]"),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-47",
        file="deal_board/cli/build_brokers.py",
        function="is_valid",
        description="is_valid ignores expiry, so the '150 valid' stratum is drawn from all Active rows including expired ones; written data stays verbatim, so the gate still rejects them but the stratified counts are wrong (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)",
        edits=(
            (
                'return row["Status"] == "Active" and exp is not None and exp >= AS_OF',
                'return row["Status"] == "Active"',
            ),
        ),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-48",
        file="deal_board/cli/build_brokers.py",
        function="main",
        description="Build writes the first 5 TREC rows twice, so data/brokers.csv has duplicate broker_ids (the matcher may return the same broker twice) (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)",
        edits=(("w.writerows(real + fictional)", "w.writerows(real + fictional + real[:5])"),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-49",
        file="deal_board/cli/build_brokers.py",
        function="sample_trec",
        description="The PEOPLE filter is dropped, so company licenses (not people) enter the sample and can be matched as brokers (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)",
        edits=(
            (
                'people = [r for r in csv.DictReader(f) if r["License Type"] in PEOPLE]',
                "people = list(csv.DictReader(f))",
            ),
        ),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-50",
        file="deal_board/cli/build_brokers.py",
        function="sample_trec",
        description="Fictional/real distinction lost: TREC rows are written with source 'fictional', so the real sample is mislabeled and real-name/profile checks no longer apply to it (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)",
        edits=(('"source": f"trec_{AS_OF.isoformat()}",', '"source": "fictional",'),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-51",
        file=BROKERS,
        function="Broker.license_expiration",
        description="license_expiration typed datetime | None, so the loaded value is a datetime and the gate's datetime >= date comparison raises TypeError (or behaves differently) at match time",
        edits=(
            ("license_expiration: date | None", "license_expiration: datetime | None"),
            ("from datetime import date", "from datetime import date, datetime"),
        ),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-52",
        file=BROKERS,
        function="Broker.license_expiration",
        description="license_expiration typed str | None, so expiry stays a raw string and is compared against a date (TypeError) or accepted in any format, e.g. '12/01/2025'",
        edits=(("license_expiration: date | None", "license_expiration: str | None"),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-53",
        file=BROKERS,
        function="load_brokers",
        description="load_brokers dedups by broker_id keeping the last row, so a licensed record shadowed by a later unlicensed duplicate (or vice versa) is silently dropped",
        edits=(
            (
                "return [Broker.model_validate(row) for row in csv.DictReader(f)]",
                'return [Broker.model_validate(row) for row in {r["broker_id"]: r for r in csv.DictReader(f)}.values()]',
            ),
        ),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-54",
        file=BROKERS,
        function="load_brokers",
        description="load_brokers skips the first data row, so one broker silently disappears from the pool (and from excluded_for_licensing)",
        edits=(("for row in csv.DictReader(f)]", "for row in list(csv.DictReader(f))[1:]]"),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-55",
        file=BROKERS,
        function="Broker.is_fictional",
        description="is_fictional checks 'trec' not in source, so any row whose source is mislabeled or blank counts as fictional and escapes the real-data checks",
        edits=(('return self.source == "fictional"', 'return "trec" not in self.source'),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-56",
        file=BROKERS,
        function="BROKERS_CSV",
        description="BROKERS_CSV points at data/fictional_brokers.csv, so the app loads only the fictional pool and every real TREC licensee disappears",
        edits=(('BROKERS_CSV = DATA_DIR / "brokers.csv"', 'BROKERS_CSV = DATA_DIR / "fictional_brokers.csv"'),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="gate-57",
        file=PATHS,
        function="DATA_DIR",
        description="DATA_DIR resolved one level too high (outside the repo), so brokers.csv is looked for in the wrong place and loading fails or picks up a stray file",
        edits=(('DATA_DIR: Final = REPO_ROOT / "data"', 'DATA_DIR: Final = REPO_ROOT.parent / "data"'),),
        round=3,
        author="gate-writer",
    ),
    Mutant(
        id="match-41",
        file=BROKERS,
        function="TX_CITY_TO_COUNTY",
        description="Fort Worth mapped to Dallas County instead of Tarrant, so Fort Worth deals give the county +2 to Dallas County brokers and not to Tarrant ones.",
        edits=(('"fort worth": "Tarrant"', '"fort worth": "Dallas"'),),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="match-42",
        file=BROKERS,
        function="TX_CITY_TO_COUNTY",
        description="Plano key stored as 'Plano', so the lower-cased lookup misses and Collin County brokers never get the county fallback for Plano deals.",
        edits=(('"plano": "Collin"', '"Plano": "Collin"'),),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="match-43",
        file=BROKERS,
        function="TX_CITY_TO_COUNTY",
        description="Non-TX city 'oklahoma city' -> 'Tarrant' added to the Texas map, so a TX deal mis-stating city 'Oklahoma City' gives Tarrant brokers a county +2.",
        edits=(('    "el paso": "El Paso",\n}', '    "el paso": "El Paso",\n    "oklahoma city": "Tarrant",\n}'),),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="match-44",
        file=MATCHER,
        function="_market_point",
        description="Broker markets are lower-cased but not stripped, so a market stored as ' Austin' or 'Austin ' no longer matches an Austin deal.",
        edits=(("{m.strip().lower() for m in broker.markets}", "{m.lower() for m in broker.markets}"),),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="match-45",
        file=MATCHER,
        function="_market_point",
        description="Deal city is lower-cased but not stripped, so ' Austin ' misses both the market match and the county fallback.",
        edits=(("city = req.city.strip().lower()", "city = req.city.lower()"),),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="match-46",
        file=MATCHER,
        function="_score",
        description="Sales Agent note is inserted first instead of appended, so it comes before the point reasons.",
        edits=(("reasons.append(SALES_AGENT_NOTE)", "reasons.insert(0, SALES_AGENT_NOTE)"),),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="match-47",
        file=MATCHER,
        function="_score",
        description="Sales Agent note is appended twice, so the reasons list repeats the compliance line.",
        edits=(
            (
                "reasons.append(SALES_AGENT_NOTE)",
                "reasons.append(SALES_AGENT_NOTE)\n        reasons.append(SALES_AGENT_NOTE)",
            ),
        ),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="match-48",
        file=MATCHER,
        function="Match",
        description="Match.score typed float, so scores serialize/compare as floats (3.0) and the int contract is loosened.",
        edits=(("    score: int\n    reasons", "    score: float\n    reasons"),),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="match-49",
        file=MATCHER,
        function="Match",
        description="Match.reasons typed tuple[str, ...], so the matcher returns tuples instead of the documented list.",
        edits=(("    reasons: list[str]  # one line", "    reasons: tuple[str, ...]  # one line"),),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="match-50",
        file=MATCHER,
        function="match",
        description="The non-deal early return gives back the previous call's MatchResult (cached at module level), so a news/hiring post gets brokers ranked and gated for some earlier deal and that deal's state.",
        edits=(
            (
                'SALES_AGENT_NOTE = "Referral fee must be paid through sponsoring broker"\n',
                'SALES_AGENT_NOTE = "Referral fee must be paid through sponsoring broker"\n\n_LAST: dict = {}\n',
            ),
            (
                "        return MatchResult(matches=[], excluded_for_licensing=0)",
                '        return _LAST.get("result", MatchResult(matches=[], excluded_for_licensing=0))',
            ),
            (
                "    return MatchResult(\n        matches=[m for m, _ in scored[: max(limit, 0)]],\n        excluded_for_licensing=len(brokers) - len(licensed),\n    )",
                '    result = MatchResult(\n        matches=[m for m, _ in scored[: max(limit, 0)]],\n        excluded_for_licensing=len(brokers) - len(licensed),\n    )\n    _LAST["result"] = result\n    return result',
            ),
        ),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="api-21",
        file=API,
        function="get_matcher",
        description="ImportError is swallowed and a pass-through matcher is returned that lists every broker ungated (score 0, excluded 0), so a broken deployment returns unlicensed/expired/out-of-state brokers with 200 instead of a 503.",
        edits=(
            (
                '        logger.error("matcher import failed: %s", type(e).__name__)\n        raise MatcherUnavailable from e',
                '        logger.error("matcher import failed: %s", type(e).__name__)\n        from types import SimpleNamespace\n\n        def match(req, brokers, as_of):  # type: ignore[no-redef]\n            return SimpleNamespace(\n                matches=[SimpleNamespace(broker=b, score=0, reasons=[]) for b in brokers],\n                excluded_for_licensing=0,\n            )\n\n        return match',
            ),
        ),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="api-22",
        file=API,
        function="match_post",
        description="ExtractionError is swallowed and replaced by an empty non-deal requirement, so an unparseable post gets 200 with no matches instead of 422.",
        edits=(
            (
                '        raise HTTPException(\n            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,\n            detail="could not extract a deal requirement from this post",\n        ) from e',
                "        requirement = DealRequirement(is_deal_request=False)",
            ),
        ),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="api-23",
        file=API,
        function="match_post",
        description="A crashing extractor falls back to a default TX deal requirement and runs the matcher, so a post about any state (or none) returns TX-licensed brokers with 200 instead of 500.",
        edits=(
            (
                '        raise HTTPException(\n            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,\n            detail="extraction failed unexpectedly",\n        ) from None',
                '        requirement = DealRequirement(is_deal_request=True, state="TX")',
            ),
        ),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="api-24",
        file=API,
        function="BrokerMatch",
        description="BrokerMatch drops `source`, so a client can no longer tell fictional brokers from real TREC license records.",
        edits=(
            ("    name: str\n    source: str\n", "    name: str\n"),
            ("            source=m.broker.source,\n", ""),
        ),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="api-25",
        file=API,
        function="match_post",
        description="license_number is taken from the top-ranked broker for every row, so each other broker is shown with someone else's license number and can't be verified.",
        edits=(("license_number=m.broker.license_number,", "license_number=result.matches[0].broker.license_number,"),),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="api-26",
        file=API,
        function="match_post",
        description="Response requirement echoes a copy with city cleared, so the client sees a different requirement than the one actually matched.",
        edits=(
            (
                "        requirement=requirement,\n        matches=matches,",
                '        requirement=requirement.model_copy(update={"city": None}),\n        matches=matches,',
            ),
        ),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="api-27",
        file=API,
        function="match_post",
        description="excluded_for_licensing reported as len(result.matches), so the client gets the match count instead of the excluded count.",
        edits=(
            (
                "        excluded_for_licensing=result.excluded_for_licensing,\n        follow_up",
                "        excluded_for_licensing=len(result.matches),\n        follow_up",
            ),
        ),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="api-28",
        file=API,
        function="get_matcher",
        description="get_matcher catches every Exception instead of ImportError, so a matcher bug raised at import time is masked as a 503 'matcher unavailable'.",
        edits=(
            (
                '    except ImportError as e:\n        logger.error("matcher import failed',
                '    except Exception as e:\n        logger.error("matcher import failed',
            ),
        ),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="api-30",
        file=API,
        function="match_post",
        description="Reasons are taken from the first match for every row, so each broker shows another broker's explanation (and Sales Agent note) instead of its own.",
        edits=(("reasons=list(m.reasons),", "reasons=list(result.matches[0].reasons),"),),
        round=3,
        author="match-api-writer",
    ),
    Mutant(
        id="gate-60",
        file=SCHEMA,
        function="ExtractedFields._upper_state",
        description="State is repaired to its first two letters, so a spelled-out 'Missouri' or 'Minnesota' becomes 'MI' and Michigan brokers are returned for a Missouri/Minnesota deal.",
        edits=(
            (
                'return v.strip().upper() if isinstance(v, str) and v.strip() else None\n\n    @field_validator("city"',
                'return v.strip().upper()[:2] if isinstance(v, str) and v.strip() else None\n\n    @field_validator("city"',
            ),
        ),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-61",
        file=SCHEMA,
        function="State",
        description="'KS' is replaced by a duplicate 'KY' in the State Literal, so Kansas becomes an invalid state and every Kansas deal fails extraction (Kansas brokers unreachable).",
        edits=(('    "KS",\n', '    "KY",\n'),),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-62",
        file=SCHEMA,
        function="KEY_FIELDS",
        description="'state' dropped from KEY_FIELDS, so a deal with no state never lists state as missing and never gets the follow-up question asking for it.",
        edits=(('KEY_FIELDS = ("state", "city", ', 'KEY_FIELDS = ("city", '),),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-63",
        file=SCHEMA,
        function="ExtractedFields._upper_state",
        description="State validator keeps only the last token of the value, so a multi-state or prose answer like 'TX or OK' / 'near KS' is silently repaired to one state instead of being rejected, routing the deal to that state's brokers.",
        edits=(
            (
                'return v.strip().upper() if isinstance(v, str) and v.strip() else None\n\n    @field_validator("city"',
                'return v.replace(",", " ").replace("/", " ").split()[-1].upper() if isinstance(v, str) and v.strip() else None\n\n    @field_validator("city"',
            ),
        ),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-64",
        file="deal_board/extractors/__init__.py",
        function="parse_requirement",
        description="On a validation failure, parse_requirement repairs the state to its first two letters and retries instead of raising, so 'Missouri' becomes 'MI' and Michigan brokers are matched.",
        edits=(
            (
                "    except ValueError as e:  # pydantic.ValidationError subclasses ValueError\n        raise ExtractionError",
                '    except ValueError as e:  # pydantic.ValidationError subclasses ValueError\n        if isinstance(raw.get("state"), str) and len(raw["state"].strip()) > 2:\n            try:\n                return DealRequirement.model_validate({**raw, "state": raw["state"].strip()[:2].upper()})\n            except ValueError:\n                pass\n        raise ExtractionError',
            ),
        ),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-65",
        file="deal_board/extractors/__init__.py",
        function="parse_requirement",
        description="A missing/null state defaults to 'TX' before validation, so a deal post that names no state (or another state only by city) gets Texas-licensed brokers and no follow-up question.",
        edits=(
            (
                "        return DealRequirement.model_validate(raw)",
                '        return DealRequirement.model_validate({**raw, "state": raw.get("state") or "TX"})',
            ),
        ),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-66",
        file="deal_board/extractors/__init__.py",
        function="parse_requirement",
        description="Catches TypeError instead of ValueError, so invalid extractor output (bad state) escapes as a raw pydantic ValidationError instead of ExtractionError and crashes callers expecting the documented error.",
        edits=(("    except ValueError as e:", "    except TypeError as e:"),),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-67",
        file="deal_board/extractors/claude.py",
        function="INPUT_SCHEMA",
        description="The Claude tool's state enum is truncated to the first 40 states, so the API-enforced schema can never return states after 'NC' (ND..WY) and those deals come back with a null or forced-wrong state.",
        edits=(
            (
                "INPUT_SCHEMA: dict[str, Any] = _strict_schema(ExtractedFields.model_json_schema())\n",
                'INPUT_SCHEMA: dict[str, Any] = _strict_schema(ExtractedFields.model_json_schema())\nINPUT_SCHEMA["properties"]["state"]["anyOf"][0]["enum"] = INPUT_SCHEMA["properties"]["state"]["anyOf"][0]["enum"][:40]\n',
            ),
        ),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-68",
        file="deal_board/extractors/claude.py",
        function="TOOL",
        description="Tool strict mode turned off, so the API no longer enforces the state enum/required fields and Claude may return 'Texas' or omit fields (Pydantic still rejects, so it surfaces as extraction errors).",
        edits=(('    "strict": True,', '    "strict": False,'),),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-69",
        file="deal_board/extractors/claude.py",
        function="ClaudeExtractor.extract",
        description="When Claude returns a null state, extract infers it from the first standalone two-capital-letter token in the post that is a valid state code (e.g. 'IN', 'OK', 'ME'), so a no-state post gets brokers from a guessed state.",
        edits=(
            (
                "    def extract(self, text: str) -> DealRequirement:\n        return parse_requirement(self.extract_raw(text))",
                '    def extract(self, text: str) -> DealRequirement:\n        raw = self.extract_raw(text)\n        if raw.get("state") is None:\n            import re\n            from typing import get_args\n            from deal_board.schema import State\n            guess = next((t for t in re.findall(r"\\b[A-Z]{2}\\b", text) if t in get_args(State)), None)\n            raw = {**raw, "state": guess}\n        return parse_requirement(raw)',
            ),
        ),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-70",
        file="deal_board/extractors/claude.py",
        function="_strict_schema",
        description="Strict schema lists only the first property as required, so Claude may omit state (and other fields) entirely and the omission is indistinguishable from 'post did not say'.",
        edits=(
            (
                '                node["required"] = list(node["properties"])',
                '                node["required"] = list(node["properties"])[:1]',
            ),
        ),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-71",
        file="deal_board/extractors/regex_baseline.py",
        function="RegexExtractor.extract",
        description="Wrapper caches results keyed on the first 40 characters of the post, so two posts with the same opening ('Looking for a broker for my client...') reuse the first post's state and get the wrong state's brokers.",
        edits=(
            (
                "        self._impl = _FROZEN_CLASS()\n",
                "        self._impl = _FROZEN_CLASS()\n        self._cache: dict[str, object] = {}\n",
            ),
            (
                "        result = self._impl.extract(text)\n",
                "        result = self._cache.setdefault(text[:40], self._impl.extract(text))\n",
            ),
        ),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-72",
        file="deal_board/extractors/regex_baseline.py",
        function="RegexExtractor.extract",
        description="Wrapper fills a null extracted state with 'TX' via model_copy (no revalidation), so a deal post naming no state is matched against Texas-licensed brokers while missing_fields still says state is missing.",
        edits=(
            (
                "        return result\n",
                '        if result.is_deal_request and result.state is None:\n            result = result.model_copy(update={"state": "TX"})\n        return result\n',
            ),
        ),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="gate-73",
        file="deal_board/config.py",
        function="Settings.use_claude",
        description="use_claude requires DEAL_EXTRACTOR to be empty, so any non-empty value (not just 'regex', e.g. 'claude') silently forces the regex baseline.",
        edits=(
            (
                "return self.has_api_key and self.deal_extractor != FORCE_REGEX",
                'return self.has_api_key and self.deal_extractor == ""',
            ),
        ),
        round=4,
        author="gate-writer",
    ),
    Mutant(
        id="api-41",
        file=API,
        function="MatchRequest",
        description="max_length is MAX_POST_CHARS + 1, so a 4001-char post is accepted and sent to the extractor instead of a 422.",
        edits=(("max_length=MAX_POST_CHARS)]", "max_length=MAX_POST_CHARS + 1)]"),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-42",
        file=API,
        function="MatchRequest",
        description="strip_whitespace disabled, so a whitespace-only post passes validation and is sent to the extractor (and padded posts count whitespace toward the 4000 cap).",
        edits=(("strip_whitespace=True", "strip_whitespace=False"),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-43",
        file=API,
        function="BodySizeLimit.__call__",
        description="Streaming size check only runs when a Content-Length was declared, so a chunked body with no length header bypasses the 64 KiB cap.",
        edits=(("if size > self.max_bytes:", "if declared is not None and size > self.max_bytes:"),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-44",
        file=API,
        function="BodySizeLimit.__call__",
        description="Off-by-one: streaming check uses >=, so a body of exactly max_bytes is rejected with 413.",
        edits=(("if size > self.max_bytes:", "if size >= self.max_bytes:"),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-45",
        file=API,
        function="BodySizeLimit.__call__",
        description="Non-numeric Content-Length is no longer rejected, so int() raises inside the middleware and the client gets a 500 instead of 413.",
        edits=(("(not declared.isdigit() or int(declared) > self.max_bytes)", "int(declared) > self.max_bytes"),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-46",
        file=API,
        function="_validation_error",
        description="Handler drops `ctx` instead of `input`, so a 422 echoes the rejected post text back to the client (and proxy logs).",
        edits=(('if k != "input"', 'if k != "ctx"'),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-47",
        file=API,
        function="match_post",
        description="Success log line records the post text instead of its hash, writing client details to the logs.",
        edits=(
            (
                '        "matched post=%s chars=%d extractor=%s matches=%d excluded=%d",\n        post_id,',
                '        "matched post=%s chars=%d extractor=%s matches=%d excluded=%d",\n        text,',
            ),
        ),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-48",
        file=API,
        function="match_post",
        description="Extraction-failure warning logs the exception text (pydantic errors embed post fragments) instead of only its class name.",
        edits=(
            (
                "post_id, extractor.name, type(e).__name__)\n        raise HTTPException(\n            status_code=status.HTTP_422",
                "post_id, extractor.name, str(e))\n        raise HTTPException(\n            status_code=status.HTTP_422",
            ),
        ),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-49",
        file=API,
        function="_post_id",
        description="Post-id hash truncated to 4 hex chars, so distinct posts collide in logs and correlation breaks.",
        edits=(("hexdigest()[:12]", "hexdigest()[:4]"),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-50",
        file=API,
        function="match_post",
        description="Matches are reshaped from reversed(result.matches), so the client sees the worst-fit licensed broker first.",
        edits=(("for m in result.matches", "for m in reversed(result.matches)"),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-51",
        file=API,
        function="match_post",
        description="ExtractionError mapped to 503 instead of 422, so an unparseable post is reported as a service outage (clients retry).",
        edits=(
            (
                '            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,\n            detail="could not extract',
                '            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,\n            detail="could not extract',
            ),
        ),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-52",
        file=API,
        function="match_post",
        description="anthropic.APIError mapped to 500 instead of 503, so an upstream outage looks like an extractor bug.",
        edits=(
            (
                '            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,\n            detail="extraction service unavailable",',
                '            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,\n            detail="extraction service unavailable",',
            ),
        ),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-53",
        file=API,
        function="match_post",
        description="Generic extractor crash returns the exception text in the 500 detail, which may quote the post.",
        edits=(('detail="extraction failed unexpectedly",', 'detail=f"extraction failed unexpectedly: {e}",'),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-54",
        file=API,
        function="match_post",
        description="API reports excluded_for_licensing as len(brokers) - len(matches) instead of the matcher's count, so licensed brokers cut by the limit are reported as unlicensed.",
        edits=(
            (
                "excluded_for_licensing=result.excluded_for_licensing,\n        follow_up",
                "excluded_for_licensing=len(brokers) - len(matches),\n        follow_up",
            ),
        ),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-55",
        file=API,
        function="match_post",
        description="Follow-up question only sent when there are no matches, so a matched deal missing city/value/timeline never gets its question.",
        edits=(
            (
                "follow_up_question=follow_up_question(requirement),",
                "follow_up_question=follow_up_question(requirement) if not matches else None,",
            ),
        ),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="api-56",
        file=API,
        function="BodySizeLimit.__call__",
        description="Body is replayed without deferring to the real receive, so after the body the app sees an immediate disconnect instead of waiting on the client.",
        edits=(("_replay(replay, then=receive)", "_replay(replay)"),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="match-61",
        file=FOLLOWUP,
        function="PRIORITY",
        description="transaction_type ranked above state, so a deal missing both is asked about deal type while it can't match any broker for lack of a state.",
        edits=(('    "state",\n    "transaction_type",\n', '    "transaction_type",\n    "state",\n'),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="match-62",
        file=FOLLOWUP,
        function="follow_up_question",
        description="State is always asked first even when present, so a complete TX deal is asked 'Which state...?' and a deal missing city never gets the city question.",
        edits=(("if field in missing:", 'if field in missing or field == "state":'),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="match-63",
        file=FOLLOWUP,
        function="follow_up_question",
        description="Priority iterated in reverse, so the least important gap (timeline) is asked before a missing state.",
        edits=(("for field in PRIORITY:", "for field in reversed(PRIORITY):"),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="match-64",
        file=FOLLOWUP,
        function="_QUESTIONS",
        description="Missing city mapped to the state question, so a deal with a known state is asked which state it is in.",
        edits=(('"city": _ask_city,', '"city": _ask_state,'),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="match-65",
        file=FOLLOWUP,
        function="_where",
        description="_where uses the state instead of the city, so questions read ' in TX' and drop the city anchor.",
        edits=(('return f" in {req.city}" if req.city else ""', 'return f" in {req.state}" if req.state else ""'),),
        round=4,
        author="match-api-writer",
    ),
    Mutant(
        id="match-66",
        file=FOLLOWUP,
        function="_PROPERTY_LABELS",
        description="'other' property type labeled 'other', so questions say 'other property' instead of 'property'.",
        edits=(('PropertyType.other: "",', 'PropertyType.other: "other",'),),
        round=4,
        author="match-api-writer",
    ),
    # @@ROUND5@@
]


# --- Runner ------------------------------------------------------------------------------------


@dataclass
class RunOutcome:
    """What the full suite did on one patched copy."""

    id: str
    failed: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    note: str = ""


def base_python() -> str:
    """The interpreter the worktree venv was built from, used to build each copy's own venv."""
    return str(getattr(sys, "_base_executable", sys.executable))


def setup_copy(dest: Path) -> None:
    """Fresh copy of the worktree at `dest` with its own editable install."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(ROOT, dest, ignore=COPY_IGNORE)
    subprocess.run([base_python(), "-m", "venv", str(dest / ".venv")], check=True)
    pip = [str(dest / ".venv/bin/pip"), "install", "-q", "--disable-pip-version-check", "-e", ".[dev]"]
    subprocess.run(pip, cwd=dest, check=True)
    where = subprocess.run(
        [str(dest / ".venv/bin/python"), "-c", "import deal_board; print(deal_board.__file__)"],
        cwd="/",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not Path(where).resolve().is_relative_to(dest.resolve()):
        raise RuntimeError(f"copy {dest} imports deal_board from {where}, not from the copy")


def _nodeid(case: ET.Element, copy: Path) -> str:
    """Turn a junit testcase's dotted classname into a pytest node id like tests/x.py::Cls::name."""
    parts = [p for p in case.get("classname", "").split(".") if p]
    if not parts:  # a collection error has no class; pytest puts the module path in `name`
        module = case.get("name", "<unknown>")
        path = Path(*module.split(".")).with_suffix(".py")
        return f"{path.as_posix() if (copy / path).is_file() else module}::<collection-error>"
    for i in range(len(parts), 0, -1):
        path = Path(*parts[:i]).with_suffix(".py")
        if (copy / path).is_file():
            return "::".join([path.as_posix(), *parts[i:], case.get("name", "")])
    return f"{case.get('classname')}::{case.get('name')}"


def run_suite(copy: Path, tag: str) -> RunOutcome:
    """Full pytest run in `copy` with its own venv; failures and errors become node ids."""
    report = copy / f".junit-{tag}.xml"
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "ANTHROPIC_API_KEY", "VIRTUAL_ENV"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [str(copy / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}"]
    outcome = RunOutcome(id=tag)
    try:
        proc = subprocess.run(cmd, cwd=copy, env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        outcome.note = f"suite timed out after {TEST_TIMEOUT_SECONDS}s"
        outcome.failed = ["<timeout>::whole-suite"]
        return outcome
    summary = (proc.stdout.strip().splitlines() or [""])[-1]
    outcome.counts = {
        k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?)", summary)
    }
    outcome.note = summary
    if report.is_file():
        for case in ET.parse(report).getroot().iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                outcome.failed.append(_nodeid(case, copy))
        report.unlink()
    if proc.returncode != 0 and not outcome.failed:
        outcome.failed = ["<collection>::suite-error"]
        outcome.note = f"pytest exit {proc.returncode}: {summary or proc.stderr.strip()[-200:]}"
    return outcome


def run_mutant(mutant: Mutant, copy: Path) -> RunOutcome:
    """Patch `copy`, run the suite, and always restore the pristine file from the worktree."""
    target = copy / mutant.file
    original = (ROOT / mutant.file).read_text(encoding="utf-8")
    try:
        target.write_text(mutant.mutated(original), encoding="utf-8")
        shutil.rmtree(target.parent / "__pycache__", ignore_errors=True)
        outcome = run_suite(copy, mutant.id)
    finally:
        target.write_text(original, encoding="utf-8")
    outcome.id = mutant.id
    return outcome


def copies(work: Path) -> list[Path]:
    """The prepared worker copies under `work`."""
    found = sorted(p for p in work.glob("w*") if (p / ".venv/bin/python").is_file())
    if not found:
        raise SystemExit(f"no prepared copies under {work}; run `setup` first")
    return found


def run_many(mutants: list[Mutant], work: Path) -> list[RunOutcome]:
    """Run mutants in parallel, one copy per worker thread."""
    pool: Queue[Path] = Queue()
    for c in copies(work):
        pool.put(c)

    def task(m: Mutant) -> RunOutcome:
        copy = pool.get()
        try:
            out = run_mutant(m, copy)
            print(
                f"  {m.id}: {'KILLED' if out.failed else 'survived'} ({len(out.failed)} failing) {out.note}", flush=True
            )
            return out
        finally:
            pool.put(copy)

    with ThreadPoolExecutor(max_workers=pool.qsize()) as ex:
        return list(ex.map(task, mutants))


def load_results() -> list[dict[str, Any]]:
    """Current results.json, or an empty list before the first run."""
    return json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.is_file() else []


def record(outcomes: list[RunOutcome], by_id: dict[str, Mutant]) -> None:
    """Merge run outcomes into results.json, keeping verifier / proposal fields already present."""
    existing = {e["id"]: e for e in load_results()}
    for out in outcomes:
        m = by_id[out.id]
        entry = existing.get(m.id, {})
        status = "killed" if out.failed else entry.get("status") if entry.get("status") == "equivalent" else "survived"
        entry.update(
            id=m.id,
            round=m.round,
            author=m.author,
            target={"function": m.function, "file": m.file},
            description=m.description,
            diff=m.diff(),
            status=status,
            killed_by=sorted(out.failed),
            suite=out.note,
        )
        existing[m.id] = entry
    order = {m.id: i for i, m in enumerate(MUTANTS)}
    rows = sorted(existing.values(), key=lambda e: order.get(e["id"], len(order)))
    RESULTS.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--work", type=Path, required=True)
    s.add_argument("--workers", type=int, default=4)
    r = sub.add_parser("run")
    r.add_argument("--work", type=Path, required=True)
    r.add_argument("--ids", nargs="*")
    r.add_argument("--round", type=int)
    r.add_argument("--no-record", action="store_true", help="print only; leave results.json alone")
    sh = sub.add_parser("show")
    sh.add_argument("--work", type=Path, required=True)
    sh.add_argument("id")
    args = ap.parse_args(argv)
    if args.work.resolve().is_relative_to(ROOT.resolve()):
        raise SystemExit("--work must be outside the worktree (use the scratchpad)")

    if args.cmd == "setup":
        dests = [args.work / f"w{k}" for k in range(args.workers)]
        with ThreadPoolExecutor(max_workers=len(dests)) as ex:
            list(ex.map(setup_copy, dests))
        base = run_suite(dests[0], "baseline")
        ok = not base.failed and all(base.counts.get(k) == v for k, v in BASELINE.items())
        print(f"baseline on unpatched copy: {base.note} -> {'OK' if ok else 'MISMATCH'}")
        return 0 if ok else 1

    by_id = {m.id: m for m in MUTANTS}
    if args.cmd == "show":
        out = run_mutant(by_id[args.id], copies(args.work)[0])
        print(f"{args.id}: {by_id[args.id].description}")
        print(f"suite: {out.note}")
        per_file = Counter(n.split("::")[0] for n in out.failed)
        for f, n in sorted(per_file.items()):
            print(f"  {n:4d} failing in {f}")
        print(f"  {len(out.failed):4d} failing in total -> {'KILLED' if out.failed else 'SURVIVED'}")
        return 0

    chosen = [by_id[i] for i in args.ids] if args.ids else [m for m in MUTANTS if args.round in (None, m.round)]
    outcomes = run_many(chosen, args.work)
    if not args.no_record:
        record(outcomes, by_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
