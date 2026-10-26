"""Prompt-loop numbers for the README (finding EM-3), recomputed from committed results files.

Reads loop/state.json (which results files belong to which iteration), the results/ files it names, and
the label files. No API calls. Every run is re-scored from its raw outputs against the current labels
with deal_board.cli.eval, the same path tables.py uses. Output is committed as review/docs/iterations.txt
and quoted verbatim in README.

Run: .venv/bin/python review/docs/iterations.py > review/docs/iterations.txt
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any

from deal_board.cli import eval as ev
from deal_board.scoring import KEY_FIELDS, exact_mcnemar_p, load_posts, post_correctness, score

ROOT = Path(__file__).resolve().parents[2]
MEAN_FIELDS = ("is_deal_request", *KEY_FIELDS)
FIRST = "results/claude_20261024T231514Z.json"  # prompt de789943, before the loop
FINAL = "results/claude_20261024T234359Z.json"  # prompt ac1a145b, after the loop


def load(rel: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((ROOT / rel).read_text())
    return data


def scored(saved: dict[str, Any], ds_name: str, split: str | None) -> tuple[float, dict[str, dict[str, bool]]]:
    """7-field mean and per-field {post_id: correct} for one dataset, optionally one split of it."""
    ds = saved["datasets"][ds_name]
    with contextlib.redirect_stderr(io.StringIO()):  # legacy-hash warnings are not part of the output
        records = ev.rescore_records(ds["records"], ev.run_raw_check(saved))
    posts = [p for p in load_posts(ROOT / ds["path"]) if split is None or p.get("split") == split]
    recs = {p["id"]: records[p["id"]] for p in posts}
    s = score(posts, recs)
    mean = sum(ev.field_cell(s, f)["accuracy"] for f in MEAN_FIELDS) / len(MEAN_FIELDS)
    return mean, post_correctness(posts, recs)


def discordant(a: dict[str, dict[str, bool]], b: dict[str, dict[str, bool]]) -> tuple[int, int, set[str]]:
    """(b-only correct, a-only correct, post ids involved) over the 7 mean fields."""
    b_only = a_only = 0
    ids: set[str] = set()
    for f in MEAN_FIELDS:
        for pid in a[f].keys() & b[f].keys():
            if b[f][pid] and not a[f][pid]:
                b_only += 1
                ids.add(pid)
            elif a[f][pid] and not b[f][pid]:
                a_only += 1
                ids.add(pid)
    return b_only, a_only, ids


def post_level(a: dict[str, dict[str, bool]], b: dict[str, dict[str, bool]]) -> tuple[list[str], list[str]]:
    """Posts with more / fewer correct cells (over the 7 mean fields) in b than in a."""
    ids = set().union(*(a[f].keys() & b[f].keys() for f in MEAN_FIELDS))

    def n(c: dict[str, dict[str, bool]], pid: str) -> int:
        return sum(c[f].get(pid, False) for f in MEAN_FIELDS)

    better = sorted(p for p in ids if n(b, p) > n(a, p))
    worse = sorted(p for p in ids if n(b, p) < n(a, p))
    return better, worse


def main() -> None:
    state = load("loop/state.json")
    print("Prompt loop on the 24 dev posts (data/posts.jsonl split dev), 3 runs per prompt")
    print("Source: loop/state.json and the claude_*.json files it names in results/, re-scored from raw outputs against current labels")
    print("p: exact McNemar vs the previous kept prompt, discordant (post, field) cells over the 7 mean fields,")
    print("   pooled over the 3 run pairs (run k vs run k); repeated posts, and cells within one post moving together,")
    print("   make this p optimistic, not conservative")
    print()
    print("| Iteration | Prompt | Dev mean (3 runs) | Per-run min–max | vs | Better / worse cells | p | Decision |")
    print("|---|---|---:|---:|---|---:|---:|---|")
    kept: tuple[str, list[dict[str, dict[str, bool]]]] | None = None
    for it in state["history"]:
        runs = [scored(load(f"results/{f}"), "posts_dev", "dev") for f in it["results_files"]]
        means = [m for m, _ in runs]
        mean = sum(means) / len(means)
        if kept is None:
            vs, cells, p = "–", "–", "–"
        else:
            b = a = 0
            for prev, (_, cur) in zip(kept[1], runs, strict=True):
                bo, ao, _ = discordant(prev, cur)
                b, a = b + bo, a + ao
            vs, cells, p = kept[0], f"{b} / {a}", f"{exact_mcnemar_p(b, a):.3g}"
        print(
            f"| {it['iteration']} | `{it['prompt_version']}` | {mean:.3f} | {min(means):.3f}–{max(means):.3f} "
            f"| {vs if vs == '–' else f'`{vs}`'} | {cells} | {p} | {it['decision']} |"
        )
        if it["decision"] in ("baseline", "kept"):
            kept = (it["prompt_version"], [c for _, c in runs])
    print()
    print(f"First prompt vs final prompt, one run each: {FIRST} (de789943) vs {FINAL} (ac1a145b)")
    first, final = load(FIRST), load(FINAL)
    for label, ds_name, split in (
        ("11 untuned author posts (data/posts.jsonl split holdout)", "posts", "holdout"),
        ("holdout v2 (data/holdout.jsonl, 15 posts)", "holdout", None),
    ):
        m1, c1 = scored(first, ds_name, split)
        m2, c2 = scored(final, ds_name, split)
        b, a, ids = discordant(c1, c2)
        print(
            f"- {label}: mean {m1:.3f} -> {m2:.3f}; better / worse cells {b} / {a}, "
            f"p = {exact_mcnemar_p(b, a):.3g}; posts that changed: {', '.join(sorted(ids)) or 'none'}"
        )
        better, worse = post_level(c1, c2)
        print(
            f"  post level (cells within a post move together, so this is the fairer test): "
            f"posts with more / fewer correct fields {len(better)} / {len(worse)}, "
            f"p = {exact_mcnemar_p(len(better), len(worse)):.3g} "
            f"(more: {', '.join(better) or 'none'}; fewer: {', '.join(worse) or 'none'})"
        )


if __name__ == "__main__":
    main()
