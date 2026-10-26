"""Blind assertions for the docs pass (branch docs-v1). Frozen with check_success.py; never edit after commit 1.

Written from the brief and review/eval-quality/findings.md only, without seeing the new README.
`check_success.py` imports this module and calls:

- `em_checks(readme)`: one or more assertions per EM finding (EM-2, EM-3, EM-22, EM-25, EM-31), plus the
  brief's global wording rules (statistics, baseline caveat, labels, looks, limitations).
- `trace_checks(readme, ctx)`: number tracing against the check's own fresh runs and the files in the repo.

Each returns `(short_snake_name, ok, detail)` tuples. Stdlib only; reads files, never runs anything, never
touches `.env`.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Iterable, Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

Check = tuple[str, bool, str]

FIELDS = (
    "is_deal_request",
    "state",
    "city",
    "property_type",
    "transaction_type",
    "deal_value_usd",
    "timeline",
    "missing_fields",
)
KEY_FIELDS = FIELDS[1:7]

NEGATION = re.compile(
    r"\b(?:not|no|none|nothing|never|neither|nor|without|isn't|aren't|wasn't|weren't|doesn't|don't|"
    r"cannot|can't|insignificant|non-?significant|except|besides)\b|n't\b",
    re.I,
)

# --- text helpers ---------------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    """Remove fenced code blocks (``` or ~~~)."""
    return re.sub(r"(?ms)^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$", "\n", text)


def _prose(readme: str) -> str:
    """README without fenced code and without markdown table rows."""
    no_code = _strip_fences(readme)
    return "\n".join(line for line in no_code.splitlines() if not line.lstrip().startswith("|"))


def _near(text: str, a: str, b: str, window: int) -> bool:
    """True if some match of regex `a` has a match of regex `b` within `window` chars either side."""
    for m in re.finditer(a, text, re.I):
        lo, hi = max(0, m.start() - window), min(len(text), m.end() + window)
        if re.search(b, text[lo:hi], re.I):
            return True
    return False


def _windows(text: str, pat: str, window: int) -> Iterator[str]:
    for m in re.finditer(pat, text, re.I):
        yield text[max(0, m.start() - window) : min(len(text), m.end() + window)]


def _section(readme: str, heading_pat: str) -> str:
    """Body of the first '##'/'###' section whose heading matches, up to the next heading of the same or higher level."""
    lines = readme.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#{2,3})\s+(.*)$", line)
        if m and re.search(heading_pat, m.group(2), re.I):
            level = len(m.group(1))
            out: list[str] = []
            for nxt in lines[i + 1 :]:
                h = re.match(r"^(#{1,6})\s", nxt)
                if h and len(h.group(1)) <= level:
                    break
                out.append(nxt)
            return "\n".join(out)
    return ""


def _sentences(text: str) -> list[str]:
    """Rough sentence split that does not break on decimals like 0.0703 or on 'vs.'/'e.g.'."""
    flat = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<!\bvs)(?<!\be\.g)(?<!\bi\.e)(?<=[.!?;])\s+(?=[A-Z*`(\[])|\s*\n\s*", flat)
    return [p for p in parts if p.strip()]


def _limitations(readme: str) -> str:
    return _section(readme, r"limitation|next step")


# --- EM checks --------------------------------------------------------------------------------------


def em_checks(readme: str) -> list[Check]:
    low = readme.lower()
    prose = _prose(readme)
    lim = _limitations(readme)
    out: list[Check] = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append((name, ok, detail))

    # EM-2: keep rule wording
    stale = re.search(r"mean rose and no field regressed|no field regressed", low)
    add("em2_stale_keep_rule_gone", not stale, "absent: 'kept ... only if the mean rose and no field regressed'")
    tol = r"one post|a single post|\b1 post|~?\s?4\s?(?:points|pts|pt)\b|4-point|0\.045|4\.5\s?(?:points|pts)"
    add(
        "em2_tolerance_stated",
        _near(readme, r"\b(?:drop\w*|fell|fall\w*|regress\w*|declin\w*|dip\w*|los[et]\w*|lower\w*)\b", tol, 120)
        and _near(readme, r"\bkep[t]\b|\bkeep\w*", tol, 300),
        "a keep rule allowing a field to drop by about one post (~4 points / 0.045), near 'kept/keep'",
    )

    # EM-3: iteration noise, untuned posts, attribution
    iters = r"\biter(?:ation)?s?\.?\s*#?\s*2\s*(?:and|&|,|/)\s*#?\s*4\b"
    add(
        "em3_iters_2_4_within_noise",
        _near(
            readme, iters, r"noise|run-to-run|within (?:the )?(?:run )?(?:spread|variation|range)|not significant", 300
        ),
        "'iterations 2 and 4' said to be within run-to-run noise on dev",
    )
    untuned = r"\b11\b[^.\n]{0,60}(?:untuned|untouched|not tuned|never tuned|held[- ]out|holdout[- ]split)|(?:untuned|not tuned|never tuned)[^.\n]{0,60}\b11\b"
    add(
        "em3_first_vs_final_on_11_untuned",
        any(
            re.search(r"\bfirst\b|de789943|starting prompt", w, re.I) and re.search(r"\bfinal\b|ac1a145b", w, re.I)
            for w in _windows(readme, untuned, 400)
        ),
        "first vs final prompt reported on the 11 untuned posts",
    )
    add(
        "em3_holdout_gain_attributed",
        _near(readme, r"\bstrict\b", r"\bh0[12]\b", 300) and _near(readme, r"\blender", r"\bh12\b", 300),
        "holdout gain attributed to strict mode (h01/h02) and the lender rule (h12)",
    )

    # EM-22: timeline mechanism
    sub = list(
        re.finditer(r"(?:doesn't|does not|didn't|did not|fails? to|failed to|can't|cannot)\s+subtract", readme, re.I)
    )
    bad_sub = [
        m.group(0)
        for m in sub
        if not re.search(
            r"not because|isn't that|is not that|rather than|instead of|not from|not a|wrong|incorrect|n't come from|not come from",
            readme[max(0, m.start() - 60) : m.start()],
            re.I,
        )
    ]
    add("em22_no_elapsed_subtraction_claim", not bad_sub, f"stale 'doesn't subtract elapsed time' claims: {bad_sub}")
    shorter = (
        r"over-?shorten\w*|shorter\s+(?:timeline\s+)?bucket|bucket\w*\s+(?:that (?:is|was|are|were) )?too\s+short"
        r"|too[- ]short|shorter than (?:the )?label\w*|(?:land|fall|go)\w*\s+(?:in|into)\s+a\s+shorter"
    )
    add(
        "em22_misses_over_shorten",
        _near(readme, r"timeline", shorter, 400),
        "timeline misses described as over-shortening (every miss in a shorter bucket)",
    )

    # EM-25: stale counts, done items, data table
    add("em25_no_500_tests", not re.search(r"\b500\s+tests\b", low), "absent: '500 tests'")
    add("em25_limitations_section", bool(lim.strip()), "a '## ...Limitations / next steps' section exists")
    done_pats = [
        r"(?:next|todo|to do|add|report|implement|build|plan)\w*[^.\n]{0,50}(?:wilson )?intervals?\b",
        r"(?:next|todo|to do|add|report|implement|build|plan)\w*[^.\n]{0,50}paired comparison",
        r"resumable",
        r"\b(?:eval )?cache\b",
        r"token and cost tracking|cost tracking",
        r"--repeats",
        r"\bpackag\w*|pyproject|sys\.path",
        r"final review",
    ]
    stale_hits = [m.group(0) for p in done_pats for m in re.finditer(p, lim, re.I)]
    add(
        "em25_no_done_items_in_limitations",
        bool(lim.strip()) and not stale_hits,
        f"done items still listed as future in Limitations: {stale_hits}",
    )
    table_lines = [ln for ln in _strip_fences(readme).splitlines() if ln.lstrip().startswith("|")]
    jsonl = sorted(p.name for p in (ROOT / "data").glob("*.jsonl"))
    unlisted = [n for n in jsonl if not any(f"data/{n}" in ln for ln in table_lines)]
    add(
        "em25_data_table_lists_every_jsonl",
        bool(jsonl) and not unlisted,
        f"data/*.jsonl in repo {jsonl}; not on a README table row: {unlisted}",
    )
    v3_rows = [ln for ln in table_lines if "data/holdout_v3.jsonl" in ln]
    add(
        "em25_holdout_v3_described",
        any(re.search(r"sonnet|opus|same[- ](?:model[- ])?family|claude[- ]label", ln, re.I) for ln in v3_rows),
        "data-table row for data/holdout_v3.jsonl says it is labeled by Sonnet/Opus (same family)",
    )
    diff_model = (
        r"(?:different|non-claude|non-anthropic|another|other|second)[- ](?:model|family|vendor|provider|lab)"
        r"[^.\n]{0,80}holdout|holdout[^.\n]{0,80}(?:different|non-claude|non-anthropic|other)[- ](?:model|family|vendor|provider)"
    )
    add(
        "em25_different_model_holdout_next",
        bool(re.search(diff_model, lim, re.I)),
        "Limitations keeps a different-model (non-Claude) holdout as a next step",
    )

    # EM-31: holdout v2 blindness
    blind_claim = re.search(r"never the extractors or the other posts|seeing only `?schema\.py`?,?\s*never", low)
    add(
        "em31_blindness_claim_reworded",
        not blind_claim,
        "absent: 'seeing only schema.py, never the extractors or the other posts'",
    )
    add(
        "em31_h08_s04_shared_wording",
        any(
            re.search(r"\bs04\b", w, re.I)
            and re.search(r"shar\w*|same|verbatim|overlap\w*|cop(?:y|ied|ies)|identical|repeat\w*", w, re.I)
            for w in _windows(readme, r"\bh08\b", 250)
        ),
        "h08 and s04 disclosed as sharing wording",
    )
    add(
        "em31_other_overlaps_listed",
        _near(readme, r"\bh13\b", r"\bs06\b", 300) and _near(readme, r"\bh14\b", r"\bs13\b", 300),
        "overlapping pairs h13~s06 and h14~s13 listed (findings.md EM-31 fix)",
    )
    add(
        "em31_v3_as_independent_set",
        _near(readme, r"holdout[_ ]v3", r"\bblind\b|independent|recorded brief", 400),
        "holdout_v3 pointed to as the blind / independent set",
    )

    # Statistics
    add(
        "stats_intervals_overlap_every_field",
        bool(
            re.search(
                r"intervals?[^.\n]{0,100}overlap\w*[^.\n]{0,60}(?:every|all|each)\s+(?:\w+\s+){0,2}fields?"
                r"|overlap\w*\s+(?:on|for|in|across)\s+(?:every|all|each)\s+(?:\w+\s+){0,2}fields?",
                prose,
                re.I,
            )
        ),
        "prose says the per-field 95% intervals overlap on every field",
    )
    add(
        "stats_missing_fields_p",
        _near(prose, r"missing_fields", r"0?\.00781", 300) and bool(re.search(r"n\s?=\s?19\b|\b19 deal", prose, re.I)),
        "prose: missing_fields separates by exact McNemar (p 0.00781) at n=19",
    )
    add(
        "stats_deal_value_p",
        _near(prose, r"deal_value_usd", r"0?\.0703", 300),
        "prose: deal_value_usd p 0.0703 (not significant)",
    )
    add(
        "stats_mean_gain_summary",
        _near(prose, r"\+17\b", r"0?\.789", 300) and _near(prose, r"\+17\b", r"0?\.955", 300),
        "prose summary: mean +17 points (0.789 vs 0.955)",
    )
    bad_sig: list[str] = []
    other_fields = [f for f in FIELDS if f != "missing_fields"]
    for s in _sentences(prose):
        if not re.search(r"significan|statistically|\bp\s?<\s?0?\.05", s, re.I):
            continue
        if NEGATION.search(s):
            continue
        has_mf = "missing_fields" in s
        has_other = any(re.search(rf"\b{f}\b", s) for f in other_fields) or re.search(
            r"\bmean\b|overall|every field", s, re.I
        )
        if not has_mf or has_other:
            bad_sig.append(s.strip()[:120])
    add("stats_no_other_significance", not bad_sig, f"significance claimed outside missing_fields: {bad_sig}")

    # Baseline caveat
    add(
        "baseline_first_state_16_of_30",
        _near(readme, r"\b16\s?(?:of|/|out of)\s?(?:the\s+|all\s+)?30\b", r"red[- ]team", 200)
        and _near(readme, r"first state", r"regex|baseline", 400),
        "regex takes the first state named; 16 of 30 red-team posts route to the wrong state",
    )
    add(
        "baseline_claude_is_production",
        _near(readme, r"\bclaude\b", r"production", 200),
        "the Claude extractor is stated to be the production path",
    )

    # Known-ambiguous labels + sensitivity
    add(
        "labels_city_v3_09_v3_16",
        any(
            re.search(r"v3-16", w) and re.search(r"\bcity\b", w, re.I) and re.search(r"\bnear\b|\boutside\b", w, re.I)
            for w in _windows(readme, r"v3-09", 400)
        ),
        "v3-09 and v3-16 city labels ('near/outside' a city -> null) discussed together",
    )
    add(
        "labels_v3_15_property_management",
        _near(readme, r"v3-15", r"property[- ]manag", 300),
        "v3-15 (property management labeled a deal) discussed",
    )
    add(
        "labels_kept_as_shipped",
        _near(
            readme,
            r"v3-(?:09|15|16)",
            r"as shipped|kept|unchanged|not (?:re-?label|chang|edit|fix)\w*|left (?:as|alone)|leave|no relabel",
            400,
        ),
        "the ambiguous labels are kept as shipped (not relabeled)",
    )
    add(
        "sensitivity_labeled_secondary",
        _near(readme, r"\bsecondary\b", r"sensitiv|exclud\w*|v3-09|v3-16", 300),
        "the city sensitivity line (v3-09/v3-16 excluded) is labeled secondary",
    )

    # Looks
    add(
        "looks_v3_first_and_only",
        _near(readme, r"first and only look", r"holdout[_ ]v3|v3", 300),
        "holdout v3 labeled the first and only look",
    )
    add(
        "looks_v2_second_look",
        any(
            re.search(r"prompt development|during (?:prompt )?development|while (?:tuning|developing)|tuning", w, re.I)
            and re.search(r"\bv2\b|holdout\.jsonl", w, re.I)
            for w in _windows(readme, r"second look", 400)
        ),
        "holdout v2 stated as a second look made during prompt development",
    )
    add(
        "validation_fail_reported_as_result",
        _near(
            _strip_fences(readme),
            r"FAIL non_deal|non_deal|non-deal",
            r"\bresult\b|reported|not (?:a )?(?:re-?run|fix\w*|hid\w*)|no re-?run|never re-?run",
            400,
        ),
        "the regex FAIL non_deal line is presented as a reported result, not rerun",
    )

    # Limitations: real next steps
    add(
        "limits_ci_never_run_no_remote",
        any(
            re.search(r"never|not (?:been |yet )?(?:run|triggered|executed)|hasn't run|has not run|no run", w, re.I)
            and re.search(r"\bremote\b", w, re.I)
            for w in _windows(lim, r"\bCI\b", 250)
        ),
        "Limitations: CI has never run (no remote)",
    )
    add(
        "limits_timeline_arithmetic",
        _near(lim, r"timeline", r"arithmetic|bucket\w*|duration", 200),
        "Limitations: timeline arithmetic remains a next step",
    )
    add(
        "limits_seller_vs_buyer",
        bool(re.search(r"seller.{0,40}buyer|buyer.{0,40}seller", lim, re.I | re.S)),
        "Limitations: seller vs buyer remains a next step",
    )

    # First prompt column provenance
    # Judged outside the generated iterations block, which may name the starting prompt by hash.
    it_path = ROOT / "review" / "docs" / "iterations.txt"
    it_txt = it_path.read_text().strip() if it_path.is_file() else ""
    own = readme.replace(it_txt, "") if it_txt else readme
    if "de789943" in own:
        ok = "results/claude_20261024T231514Z.json" in readme and _near(
            own, r"de789943", r"first prompt|starting prompt|initial prompt", 400
        )
        add(
            "de789943_labeled_and_sourced",
            ok,
            "de789943 appears: labeled first prompt, sourced to results/claude_20261024T231514Z.json",
        )
    else:
        add("de789943_labeled_and_sourced", True, "de789943 not mentioned")
    return out


# --- number tracing ---------------------------------------------------------------------------------


def _norm_dec(s: str) -> str:
    try:
        d = Decimal(s.replace(",", ""))
    except InvalidOperation:
        return s
    t = format(d.normalize(), "f")
    return "0" if t in {"-0", ""} else t


def _norm_money(s: str) -> str:
    return re.sub(r"[\s,]", "", s).lower().replace("million", "m").replace("thousand", "k")


def _chunks_by_heading(text: str) -> list[str]:
    parts = re.split(r"(?m)^(?=### )", text)
    return [p.strip() for p in parts if p.strip()]


MONEY = r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|thousand|[kKmMbB])\b)?"
RANGE_PCT = r"(?<![\w.])(\d+(?:\.\d+)?)\s?[–\-]\s?(\d+(?:\.\d+)?)\s?%"
PCT = r"(?<![\w.])(\d+(?:\.\d+)?)\s?%"
SIGNED = r"(?<![\w.\-–/+])([+\-−±])(\d+(?:\.\d+)?)(%?)(?![\w.])"
SECONDS = r"(?<![\w.])(\d+(?:\.\d+)?)\s?(?:s|sec|secs|seconds)\b"
DECIMAL = r"(?<![\w.])(\d*\.\d+)(?![\w.])"
POINTS = r"(?<![\w.])(\d+)\s?(?:percentage points|points|pts|pt)\b"
BIGINT = r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d{4,})(?![\w.,])"
SKIP = [
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?",  # dates
    r"\d+\.\d+\.\d+(?:\.\d+)*",  # versions, IPs
    r"(?i:python|haiku|sonnet|opus|claude|pydantic|fastapi|ruff|mypy|pytest|version|v)\s?\d+(?:\.\d+)+",
    r"\b\d+\s?/\s?\d+\b",  # fractions like 12/12 (counts)
]


def _scan(text: str) -> dict[str, set[str]]:
    """Corpus tokens by kind (no exemptions)."""
    sets: dict[str, set[str]] = {k: set() for k in ("money", "range", "pct", "signed", "dec", "int", "points")}
    sets["money"] = {_norm_money(m) for m in re.findall(MONEY, text)}
    for a, b in re.findall(r"\[\s?(\d+(?:\.\d+)?)\s?[–\-]\s?(\d+(?:\.\d+)?)\s?\]", text):
        sets["range"].add(f"{_norm_dec(a)}-{_norm_dec(b)}")
    for a, b in re.findall(RANGE_PCT, text):
        sets["range"].add(f"{_norm_dec(a)}-{_norm_dec(b)}")
    sets["pct"] = {_norm_dec(m) for m in re.findall(PCT, text)}
    sets["signed"] = {f"{'-' if s == '−' else s}{_norm_dec(n)}{p}" for s, n, p in re.findall(SIGNED, text)}
    sets["dec"] = {_norm_dec(m) for m in re.findall(r"\d*\.\d+", text)}
    sets["int"] = {_norm_dec(m) for m in re.findall(r"\d[\d,]*\d|\d", text)}
    sets["points"] = {_norm_dec(m) for m in re.findall(POINTS, text)}
    return sets


def _json_floats(obj: object, out: list[float]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k != "records":
                _json_floats(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _json_floats(v, out)
    elif isinstance(obj, float):
        out.append(obj)


def _json_means(obj: object, out: list[float]) -> None:
    """Mean of the 7 fields (is_deal_request + 6 key fields) for every scores block / accuracy dict found."""
    if isinstance(obj, dict):
        acc: dict[str, float] = {}
        isd = obj.get("is_deal_request")
        fields = obj.get("fields")
        if isinstance(isd, dict) and isinstance(fields, dict):
            if isinstance(isd.get("accuracy"), float | int):
                acc["is_deal_request"] = float(isd["accuracy"])
            for f in KEY_FIELDS:
                fv = fields.get(f)
                if isinstance(fv, dict) and isinstance(fv.get("accuracy"), float | int):
                    acc[f] = float(fv["accuracy"])
        elif all(isinstance(obj.get(f), float | int) and not isinstance(obj.get(f), bool) for f in FIELDS[:7]):
            acc = {f: float(obj[f]) for f in FIELDS[:7]}
        if len(acc) == 7:
            out.append(sum(acc.values()) / 7)
        for k, v in obj.items():
            if k != "records":
                _json_means(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _json_means(v, out)


def _corpus(ctx: dict[str, object], root: Path) -> tuple[dict[str, set[str]], set[str]]:
    texts = [
        str(ctx.get(k, ""))
        for k in (
            "tables_stdout",
            "validate_regex",
            "validate_claude",
            "sensitivity_txt",
            "iterations_txt",
            "pytest_summary",
        )
    ]
    floats: list[float] = []
    for p in sorted((root / "results").glob("*")):
        if p.suffix not in {".md", ".json"} or not p.is_file():
            continue
        txt = p.read_text(errors="replace")
        texts.append(txt)
        if p.suffix == ".json":
            try:
                data = json.loads(txt)
            except ValueError:
                continue
            _json_floats(data, floats)
            _json_means(data, floats)
    texts.extend(p.read_text(errors="replace") for p in sorted((root / "review" / "docs").glob("*.txt")))
    sets = _scan("\n".join(texts))
    for f in floats:
        for k in (2, 3, 4):
            sets["dec"].add(_norm_dec(f"{f:.{k}f}"))
        sets["dec"].add(_norm_dec(f"{f:.3g}"))
    # money quoted from post texts / broker data
    data_money: set[str] = set()
    for p in sorted((root / "data").glob("*")):
        if p.suffix in {".jsonl", ".csv"} and p.is_file():
            txt = p.read_text(errors="replace")
            data_money |= {_norm_money(m) for m in re.findall(MONEY, txt)}
            data_money |= {f"${n}" for n in re.findall(r"(?<![\w.])\d{3,}(?![\w])", txt)}
    return sets, data_money


def _blank(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def _sub_blank(pattern: str, text: str, flags: int = 0) -> str:
    return re.sub(pattern, lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=flags)


def _runtime_ok(value: float, actual: float) -> bool:
    return actual > 0 and abs(value - actual) <= max(0.5 * actual, 10.0)


def _untraced(readme: str, ctx: dict[str, object], root: Path) -> tuple[list[str], int]:
    text = readme
    blocks = _chunks_by_heading(str(ctx.get("tables_stdout", "")))
    blocks += [
        str(ctx.get(k, "")).strip() for k in ("validate_regex", "validate_claude", "sensitivity_txt", "iterations_txt")
    ]
    for b in blocks:
        if b:
            text = text.replace(b, re.sub(r"[^\n]", " ", b))
    text = _sub_blank(r"(?ms)^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$", text)
    text = _sub_blank(r"<!--.*?-->", text, re.S)
    text = _sub_blank(r"\]\([^)]*\)", text)  # link targets
    text = _sub_blank(r"https?://\S+", text)
    for p in SKIP:
        text = _sub_blank(p, text)

    sets, data_money = _corpus(ctx, root)
    seconds = ctx.get("pytest_seconds", -1.0)
    actual_secs = float(seconds) if isinstance(seconds, int | float) else -1.0
    passed = ctx.get("pytest_passed", -1)
    bad: list[str] = []
    n = 0

    def consume(pattern: str, judge: Callable[[re.Match[str]], bool], flags: int = 0) -> None:
        nonlocal text, n
        spans = []
        for m in re.finditer(pattern, text, flags):
            n += 1
            if not judge(m):
                ctx_line = text[max(0, m.start() - 25) : m.end() + 10].replace("\n", " ").strip()
                bad.append(f"{m.group(0).strip()!r} (…{ctx_line}…)")
            spans.append(m.span())
        text = _blank(text, spans)

    consume(MONEY, lambda m: _norm_money(m.group(0)) in sets["money"] | data_money)
    consume(RANGE_PCT, lambda m: f"{_norm_dec(m.group(1))}-{_norm_dec(m.group(2))}" in sets["range"])

    def pct_ok(m: re.Match[str]) -> bool:
        v = _norm_dec(m.group(1))
        if v == "95" and re.match(
            r"\s*(?:wilson\s+)?(?:confidence\s+)?(?:interval|ci\b|confidence|wilson)",
            text[m.end() : m.end() + 40],
            re.I,
        ):
            return True
        return v in sets["pct"]

    consume(PCT, pct_ok)

    def signed_ok(m: re.Match[str]) -> bool:
        sign, num, pct = m.group(1), m.group(2), m.group(3)
        if (
            "." not in num
            and not pct
            and int(num) <= 5
            and not re.match(r"\s?(?:points|pts|pp)\b", text[m.end() :], re.I)
        ):
            return True  # small design constants such as matcher scores "+3", "+2"
        return f"{'-' if sign == '−' else sign}{_norm_dec(num)}{pct}" in sets["signed"]

    consume(SIGNED, signed_ok)

    def secs_ok(m: re.Match[str]) -> bool:
        v = m.group(1)
        return (
            ("." in v and _norm_dec(v) in sets["dec"])
            or ("." not in v and _norm_dec(v) in sets["int"])
            or _runtime_ok(float(v), actual_secs)
        )

    consume(SECONDS, secs_ok)

    def dec_ok(m: re.Match[str]) -> bool:
        v = _norm_dec(m.group(1))
        if v in sets["dec"]:
            return True
        before = text[max(0, m.start() - 6) : m.start()]
        return v in {"0.05", "0.01", "0.001", "0.1"} and bool(re.search(r"[<≤>≥]\s*$|α\s*=?\s*$", before))

    consume(DECIMAL, dec_ok)

    def points_ok(m: re.Match[str]) -> bool:
        v = _norm_dec(m.group(1))
        return int(v) <= 5 or f"+{v}" in sets["signed"] or f"-{v}" in sets["signed"] or v in sets["points"]

    consume(POINTS, points_ok, re.I)

    def big_ok(m: re.Match[str]) -> bool:
        v = _norm_dec(m.group(1))
        if re.fullmatch(r"(?:19|20)\d\d", m.group(1)):
            return True  # a year
        return v in sets["int"] or v == str(passed)

    consume(BIGINT, big_ok)
    return bad, n


def _red_team_numbers(root: Path) -> tuple[int, int, str]:
    src = (root / "tests" / "test_red_team.py").read_text()
    m = re.search(r"KNOWN_REGEX_STATE_LEAKS[^=]*=\s*frozenset\(\s*(\{.*?\})\s*\)", src, re.S)
    if not m:
        return -1, -1, "KNOWN_REGEX_STATE_LEAKS not found in tests/test_red_team.py"
    leaks = ast.literal_eval(m.group(1))
    total = sum(1 for line in (root / "data" / "red_team.jsonl").read_text().splitlines() if line.strip())
    return len(leaks), total, f"{len(leaks)} leaks in KNOWN_REGEX_STATE_LEAKS, {total} posts in data/red_team.jsonl"


def trace_checks(readme: str, ctx: dict[str, object]) -> list[Check]:
    root_obj = ctx.get("root", ROOT)
    root = root_obj if isinstance(root_obj, Path) else Path(str(root_obj))
    out: list[Check] = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append((name, ok, detail))

    passed, skipped = ctx.get("pytest_passed", -1), ctx.get("pytest_skipped", -1)
    secs_obj = ctx.get("pytest_seconds", -1.0)
    actual_secs = float(secs_obj) if isinstance(secs_obj, int | float) else -1.0

    # Test count and runtime
    counts = [
        (m, int(m.group(1).replace(",", "")), int(m.group(2)))
        for m in re.finditer(r"(\d[\d,]*)\s+passed,\s+(\d+)\s+skipped", readme)
    ]
    wrong = [m.group(0) for m, p, s in counts if (p, s) != (passed, skipped)]
    add(
        "test_count",
        bool(counts) and not wrong,
        f"README '<N> passed, <M> skipped' must equal this run ({passed} passed, {skipped} skipped); found {[m.group(0) for m, _, _ in counts]}",
    )
    other_counts = [
        m.group(0)
        for m in re.finditer(r"(\d[\d,]*)\s+(?:tests|test cases)\b", readme)
        if int(m.group(1).replace(",", "")) >= 100
        and int(m.group(1).replace(",", ""))
        not in {passed, (passed if isinstance(passed, int) else 0) + (skipped if isinstance(skipped, int) else 0)}
    ]
    add(
        "test_count_no_other",
        not other_counts,
        f"suite-size '<N> tests' claims (N >= 100) not matching this run: {other_counts}",
    )
    stated: list[str] = []
    rt_ok = bool(counts)
    for m, _, _ in counts:
        tail = readme[m.end() : m.end() + 100]
        r = re.search(r"(\d+(?:\.\d+)?)\s?(?:[–\-]|to)\s?(\d+(?:\.\d+)?)\s?(?:s|sec|secs|seconds)\b", tail)
        one = re.search(r"(\d+(?:\.\d+)?)\s?(?:s|sec|secs|seconds)\b", tail)
        if r:
            lo, hi = float(r.group(1)), float(r.group(2))
            tol = max(0.5 * actual_secs, 10.0)
            rt_ok &= actual_secs > 0 and lo - tol <= actual_secs <= hi + tol
            stated.append(r.group(0))
        elif one:
            rt_ok &= _runtime_ok(float(one.group(1)), actual_secs)
            stated.append(one.group(0))
        else:
            rt_ok = False
    add(
        "test_runtime",
        rt_ok,
        f"runtime stated next to the test count {stated} vs this run {actual_secs}s (tolerance ±max(50%, 10 s))",
    )

    # Generated blocks
    it = str(ctx.get("iterations_txt", "")).strip()
    add("iterations_verbatim", bool(it) and it in readme, "review/docs/iterations.txt appears verbatim")
    rest = readme.replace(it, "") if it else readme
    hand_rows = [
        ln.strip()[:80]
        for ln in _strip_fences(rest).splitlines()
        if re.match(r"^\s*\|\s*[0-4]\s*\|.*\|\s*0?\.\d{3}\s*\|", ln)
    ]
    add(
        "iteration_numbers_generated",
        not hand_rows,
        f"hand-typed iteration table rows outside iterations.txt: {hand_rows}",
    )
    sens = str(ctx.get("sensitivity_txt", "")).strip()
    at = readme.find(sens) if sens else -1
    v3_chunks = _chunks_by_heading(str(ctx.get("tables_stdout", "")))
    v3_at = readme.find(v3_chunks[0]) if v3_chunks else -1
    add(
        "sensitivity_secondary_placed",
        at >= 0
        and bool(re.search(r"\bsecondary\b", readme[max(0, at - 600) : at + len(sens) + 300], re.I))
        and (v3_at < 0 or v3_at < at),
        f"sensitivity.txt found at {at}, after the v3 table (at {v3_at}), with 'secondary' within 600 chars",
    )

    # Red-team numbers
    leaks, total, how = _red_team_numbers(root)
    pairs = [
        (int(m.group(1)), int(m.group(2)))
        for w in [readme]
        for m in re.finditer(
            r"\b(\d+)\s?(?:of|/|out of)\s?(?:the\s+|all\s+)?(\d+)\b(?=[^.\n]{0,60}red[- ]team)", w, re.I
        )
    ]
    near_pairs = [
        (int(m.group(1)), int(m.group(2)))
        for m in re.finditer(r"\b(\d+)\s?(?:of|/|out of)\s?(?:the\s+|all\s+)?(\d+)\b", readme, re.I)
        if re.search(r"red[- ]team", readme[max(0, m.start() - 150) : m.end() + 150], re.I)
    ]
    wrong_pairs = [p for p in set(pairs + near_pairs) if p[1] == total and p[0] != leaks and p[0] != total]
    add(
        "red_team_16_of_30",
        leaks > 0 and (leaks, total) in set(pairs + near_pairs) and not wrong_pairs,
        f"{how}; README pairs near 'red-team': {sorted(set(pairs + near_pairs))}; conflicting: {wrong_pairs}",
    )

    # Paths named in README exist
    path_re = r"(?<![\w/.])((?:deal_board|tools|tests|loop|data|review|docs|results|\.github)/[\w./\-]*\w)"
    named = sorted(set(re.findall(path_re, readme)))
    missing = [p for p in named if not (root / p).exists() and not p.startswith("data/raw")]
    add("paths_exist", bool(named) and not missing, f"{len(named)} repo paths named; missing: {missing}")
    for name, needle in [
        ("arch_frozen_baseline", "deal_board/extractors/frozen/regex_baseline.py"),
        ("arch_baseline_loader", "deal_board/extractors/regex_baseline.py"),
        ("arch_eval_cli", "deal_board/cli/eval.py"),
        ("arch_config", "deal_board/config.py"),
        ("arch_final_eval_tools", "tools/final_eval/"),
    ]:
        add(name, needle in readme, f"architecture names {needle}")

    # Prose numbers
    bad, n = _untraced(readme, ctx, root)
    add(
        "prose_numbers_traced",
        not bad,
        f"{n} measurement tokens outside generated blocks; untraced: {bad}"
        if bad
        else f"{n} measurement tokens, all traced",
    )
    return out
