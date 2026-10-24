"""The loop's success test must reject what it should reject. Pure checks only; no git, no API."""
from loop import check_success as cs
from schema import KEY_FIELDS


def scores(acc=1.0, errors=0, is_deal=1.0, non_deal_filled=0):
    return {
        "is_deal_request": {"accuracy": is_deal, "error": errors},
        "fields": {f: {"accuracy": acc} for f in KEY_FIELDS},
        "non_deal_hallucinations": {"fields_filled": non_deal_filled},
    }


def dev_run(**kw):
    return {"prompt_version": "v1", "datasets": {"posts_dev": {"scores": scores(**kw)}}}


def final_run(version="v1", **kw):
    return {"prompt_version": version, "datasets": {"holdout": {"scores": scores(**kw)}, "posts": {"scores": scores()}}}


def failed(checks):
    return {c.name for c in checks if not c.passed}


def test_dev_passes_only_with_three_clean_runs():
    assert failed(cs.dev_checks([dev_run()] * 3)) == set()
    assert failed(cs.dev_checks([dev_run()] * 2)) == {"repeats"}


def test_dev_fails_on_any_error_hallucination_or_weak_field():
    assert "schema errors" in failed(cs.dev_checks([dev_run(), dev_run(errors=1), dev_run()]))
    assert "non-deal hallucinations" in failed(cs.dev_checks([dev_run(non_deal_filled=1)] + [dev_run()] * 2))
    assert "weakest field" in failed(cs.dev_checks([dev_run(acc=0.8)] * 3))
    assert "is_deal_request" in failed(cs.dev_checks([dev_run(is_deal=0.96)] * 3))


def test_fixed_sections_ignore_editable_ones():
    before = "# T\n\n## Goal (FIXED)\nwin\n\n## Backlog (EDITABLE)\n1. a\n"
    after = "# T\n\n## Goal (FIXED)\nwin\n\n## Backlog (EDITABLE)\n1. b\n2. c\n"
    assert cs.fixed_sections(before) == cs.fixed_sections(after) == ["## Goal (FIXED)\nwin"]
    assert cs.fixed_sections(before.replace("win", "tie")) != cs.fixed_sections(before)


def test_final_allows_one_look_at_a_dev_passed_prompt():
    assert failed(cs.final_checks([final_run()], "v1")) == set()
    assert "one look only" in failed(cs.final_checks([final_run(), final_run()], "v1"))
    assert "prompt passed dev first" in failed(cs.final_checks([final_run()], None))
    assert "holdout mean accuracy" in failed(cs.final_checks([final_run(acc=0.7)], "v1"))
