"""The raw-output cache, per-call usage, cost, --repeats and save() rules in scripts/eval.py (P6 contract).
Written blind to the implementation. Every Claude extractor uses a fake client; nothing touches the network."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from deal_board.extractors.claude import PROMPT_VERSION, ClaudeExtractor, Usage
from deal_board.schema import DealRequirement, KEY_FIELDS
from deal_board.cli import eval as ev

ROOT = Path(__file__).parent.parent
MODEL = "claude-haiku-5-5"
N_DEV_POSTS = 24
IN_TOK, OUT_TOK = 100, 20
OUTPUT = {"is_deal_request": True, "state": "TX", "city": "Austin", "property_type": "single_family",
          "transaction_type": "buy", "deal_value_usd": 650000, "timeline": "31_to_90_days", "notes": None}
POSTS = [{"id": f"t{i}", "text": f"Need an agent in Austin, TX, post {i}",
          "label": {"is_deal_request": True, **dict.fromkeys(KEY_FIELDS)}} for i in range(3)]


class FakeClient:
    """Answers every request with the same tool call (or a refusal) and counts calls."""

    def __init__(self, output: dict | None = None, *, refuse: bool = False, usage: bool = True) -> None:
        self.output, self.refuse, self.usage, self.calls, self.messages = output or OUTPUT, refuse, usage, 0, self

    def create(self, **kwargs: Any) -> NS:
        self.calls += 1
        extra = {"usage": NS(input_tokens=IN_TOK, output_tokens=OUT_TOK)} if self.usage else {}
        if self.refuse:
            return NS(stop_reason="refusal", content=[], **extra)
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", input=dict(self.output))], **extra)


def entry(text: str, model: str = MODEL, prompt_version: str = PROMPT_VERSION) -> dict:
    """A well-formed cache entry for text."""
    return {"model": model, "prompt_version": prompt_version,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "raw": dict(OUTPUT),
            "error": None, "input_tokens": IN_TOK, "output_tokens": OUT_TOK, "latency_s": 0.5}


def cache(directory: Path, model: str = MODEL, prompt_version: str = PROMPT_VERSION) -> "ev.RawCache":
    return ev.RawCache(directory, model, prompt_version)


def results_listing() -> list[str]:
    return sorted(p.name for p in (ROOT / "results").iterdir())


@pytest.fixture
def claude_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Run ev.main for the claude extractor with a fresh counting fake; returns (client, results_dir)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-real-key")
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path / "results-default")
    before = results_listing()

    def run(*extra: str, results_dir: Path, cache_dir: Path) -> FakeClient:
        client = FakeClient()
        monkeypatch.setattr(ev, "make_extractor", lambda *a, **k: (
            ClaudeExtractor(client=client), {"model": MODEL, "prompt_version": PROMPT_VERSION}))
        ev.main(["--extractor", "claude", "--split", "dev", "--results-dir", str(results_dir),
                 "--cache-dir", str(cache_dir), *extra])
        return client

    yield run
    assert results_listing() == before


def load_results(directory: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]


# ---------- cache_key ----------

def test_cache_key_formula_and_determinism() -> None:
    """cache_key is sha256(json.dumps([model, prompt_version, sha256(text)])) and is deterministic."""
    text = "Need a buyer agent in Boise, ID"
    inner = hashlib.sha256(text.encode()).hexdigest()
    expected = hashlib.sha256(json.dumps([MODEL, "v1", inner]).encode()).hexdigest()
    assert ev.cache_key(MODEL, "v1", text) == expected == ev.cache_key(MODEL, "v1", text)


def test_cache_key_changes_with_each_part() -> None:
    """Changing model, prompt_version or text changes the key."""
    base = ev.cache_key("m", "v1", "text")
    assert len({base, ev.cache_key("m2", "v1", "text"), ev.cache_key("m", "v2", "text"),
                ev.cache_key("m", "v1", "text!")}) == 4


def test_cache_key_parts_cannot_collide_by_concatenation() -> None:
    """('ab', 'c') and ('a', 'bc') are different keys."""
    assert ev.cache_key("ab", "c", "t") != ev.cache_key("a", "bc", "t")


@pytest.mark.parametrize("model", ["org/model", "../../escape", "a/../../b", "..", "/abs/path"])
def test_model_name_never_becomes_a_path(tmp_path: Path, model: str) -> None:
    """Model names with / or .. write one flat file named <key>.json in the cache dir."""
    d = tmp_path / "cache"
    cache(d, model).put("hello", entry("hello", model))
    files = list(d.rglob("*"))
    assert [f.name for f in files] == [f"{ev.cache_key(model, PROMPT_VERSION, 'hello')}.json"]
    assert files[0].parent == d and files[0].is_file()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cache"]


# ---------- RawCache get/put ----------

def test_put_then_new_instance_hits(tmp_path: Path) -> None:
    """An entry written by one instance is served by a new instance on the same dir."""
    e = entry("post")
    cache(tmp_path).put("post", e)
    got = cache(tmp_path).get("post")
    assert got is not None and got["raw"] == e["raw"] and got["input_tokens"] == IN_TOK


def test_missing_file_is_a_miss(tmp_path: Path) -> None:
    """Nothing on disk means get returns None."""
    assert cache(tmp_path).get("never written") is None


def test_instance_serves_each_key_at_most_once(tmp_path: Path) -> None:
    """The second get of the same text on the same instance is a miss."""
    cache(tmp_path).put("post", entry("post"))
    c = cache(tmp_path)
    assert c.get("post") is not None
    assert c.get("post") is None
    assert cache(tmp_path).get("post") is not None  # still on disk for the next invocation


def test_other_prompt_version_entry_is_a_miss(tmp_path: Path) -> None:
    """An entry from another prompt_version is never served, even at the requested key's path."""
    cache(tmp_path, prompt_version="old").put("post", entry("post", prompt_version="old"))
    src = tmp_path / f"{ev.cache_key(MODEL, 'old', 'post')}.json"
    (tmp_path / f"{ev.cache_key(MODEL, 'new', 'post')}.json").write_text(src.read_text())
    assert cache(tmp_path, prompt_version="new").get("post") is None


def test_other_model_or_text_entry_is_a_miss(tmp_path: Path) -> None:
    """An entry whose model or text_sha256 doesn't match is a miss."""
    path = tmp_path / f"{ev.cache_key(MODEL, PROMPT_VERSION, 'post')}.json"
    path.write_text(json.dumps(entry("post", model="other-model")))
    assert cache(tmp_path).get("post") is None
    path.write_text(json.dumps(entry("different text")))
    assert cache(tmp_path).get("post") is None


@pytest.mark.parametrize("content", ["not json{", "", '{"model": '])
def test_corrupt_file_is_a_miss(tmp_path: Path, content: str) -> None:
    """A tampered or half-written file is a miss, not a crash."""
    (tmp_path / f"{ev.cache_key(MODEL, PROMPT_VERSION, 'post')}.json").write_text(content)
    assert cache(tmp_path).get("post") is None


def test_put_leaves_no_temp_files(tmp_path: Path) -> None:
    """put writes atomically: only the final <key>.json remains."""
    c = cache(tmp_path)
    for t in ("a", "b"):
        c.put(t, entry(t))
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        f"{ev.cache_key(MODEL, PROMPT_VERSION, t)}.json" for t in ("a", "b"))


# ---------- run_posts with a cache ----------

def test_run_posts_miss_then_hit_on_new_instance(tmp_path: Path) -> None:
    """First run calls the API per post; a new instance on the same dir replays without calls."""
    first = FakeClient()
    r1 = ev.run_posts(ClaudeExtractor(client=first), POSTS, cache(tmp_path))
    assert first.calls == len(POSTS)
    second = FakeClient()
    r2 = ev.run_posts(ClaudeExtractor(client=second), POSTS, cache(tmp_path))
    assert second.calls == 0
    assert {p: r["raw"] for p, r in r2.items()} == {p: r["raw"] for p, r in r1.items()}


def test_run_posts_same_instance_twice_calls_again(tmp_path: Path) -> None:
    """Reusing one cache instance (as --repeats does) makes fresh calls the second time."""
    c = cache(tmp_path)
    ev.run_posts(ClaudeExtractor(client=FakeClient()), POSTS, c)
    client = FakeClient()
    records = ev.run_posts(ClaudeExtractor(client=client), POSTS, c)
    assert client.calls == len(POSTS)
    assert all(r["call"]["cached"] is False for r in records.values())


def test_run_posts_without_cache_never_caches(tmp_path: Path) -> None:
    """cache=None always calls the API."""
    client = FakeClient()
    ext = ClaudeExtractor(client=client)
    ev.run_posts(ext, POSTS)
    ev.run_posts(ext, POSTS)
    assert client.calls == 2 * len(POSTS)


def test_extraction_errors_are_cached_and_replayed(tmp_path: Path) -> None:
    """A refusal is cached like a success and replayed with the same error, no new call."""
    r1 = ev.run_posts(ClaudeExtractor(client=FakeClient(refuse=True)), POSTS, cache(tmp_path))
    assert all(r["error"] for r in r1.values())
    client = FakeClient()  # would succeed if it were called
    r2 = ev.run_posts(ClaudeExtractor(client=client), POSTS, cache(tmp_path))
    assert client.calls == 0
    assert {p: r["error"] for p, r in r2.items()} == {p: r["error"] for p, r in r1.items()}
    assert all(r["call"]["cached"] is True for r in r2.values())


def test_api_exceptions_are_not_cached(tmp_path: Path) -> None:
    """A network/API exception propagates and leaves nothing in the cache."""
    class Boom(FakeClient):
        def create(self, **kwargs: Any) -> NS:
            raise ConnectionError("network down")

    with pytest.raises(ConnectionError):
        ev.run_posts(ClaudeExtractor(client=Boom()), POSTS, cache(tmp_path))
    assert not list(tmp_path.glob("*.json"))


# ---------- call dict and last_usage ----------

def test_call_dict_on_fresh_and_cached_records(tmp_path: Path) -> None:
    """Fresh records carry tokens from usage and wall-clock latency; hits replay them with cached=True."""
    fresh = ev.run_posts(ClaudeExtractor(client=FakeClient()), POSTS, cache(tmp_path))
    hit = ev.run_posts(ClaudeExtractor(client=FakeClient()), POSTS, cache(tmp_path))
    for pid in fresh:
        f, h = fresh[pid]["call"], hit[pid]["call"]
        assert f["cached"] is False and h["cached"] is True
        assert (f["input_tokens"], f["output_tokens"]) == (IN_TOK, OUT_TOK)
        assert (h["input_tokens"], h["output_tokens"]) == (IN_TOK, OUT_TOK)
        assert isinstance(f["latency_s"], float) and f["latency_s"] >= 0
        assert h["latency_s"] == f["latency_s"]


def test_call_dict_without_usage_has_none_tokens() -> None:
    """A client without usage gives None tokens but still a latency."""
    records = ev.run_posts(ClaudeExtractor(client=FakeClient(usage=False)), POSTS)
    for r in records.values():
        assert r["call"]["cached"] is False
        assert r["call"]["input_tokens"] is None and r["call"]["output_tokens"] is None
        assert isinstance(r["call"]["latency_s"], float) and r["call"]["latency_s"] >= 0


def test_regex_records_have_call_with_no_tokens() -> None:
    """The regex baseline gets a call dict with None tokens."""
    from deal_board.extractors.regex_baseline import RegexExtractor
    records = ev.run_posts(RegexExtractor(), POSTS)
    assert all(r["call"]["input_tokens"] is None and r["call"]["cached"] is False for r in records.values())


def test_last_usage_from_response() -> None:
    """last_usage is a Usage built from response.usage; extract still returns a DealRequirement."""
    ext = ClaudeExtractor(client=FakeClient())
    assert isinstance(ext.extract("Need an agent in Austin, TX"), DealRequirement)
    assert ext.last_usage == Usage(input_tokens=IN_TOK, output_tokens=OUT_TOK)


def test_last_usage_none_without_usage() -> None:
    """A response without usage leaves last_usage None."""
    ext = ClaudeExtractor(client=FakeClient(usage=False))
    ext.extract("post")
    assert ext.last_usage is None


def test_last_usage_reset_each_call() -> None:
    """last_usage is reset per call, so a later call without usage clears it."""
    client = FakeClient()
    ext = ClaudeExtractor(client=client)
    ext.extract("post")
    client.usage = False
    ext.extract("post")
    assert ext.last_usage is None


def test_last_usage_set_on_refusal() -> None:
    """Refused calls still bill, so last_usage is set even when extract_raw raises."""
    ext = ClaudeExtractor(client=FakeClient(refuse=True))
    with pytest.raises(Exception):
        ext.extract_raw("post")
    assert ext.last_usage == Usage(input_tokens=IN_TOK, output_tokens=OUT_TOK)


def test_usage_is_frozen() -> None:
    """Usage is a frozen dataclass."""
    u = Usage(input_tokens=1, output_tokens=2)
    with pytest.raises(Exception):
        u.input_tokens = 5  # type: ignore[misc]


# ---------- cost and usage totals ----------

def test_cost_usd_known_model() -> None:
    """Cost uses the per-million-token price table."""
    assert MODEL in ev.PRICES_PER_MTOK
    pin, pout = ev.PRICES_PER_MTOK[MODEL]
    assert ev.cost_usd(MODEL, 1_000_000, 0) == pytest.approx(pin)
    assert ev.cost_usd(MODEL, 0, 1_000_000) == pytest.approx(pout)
    assert ev.cost_usd(MODEL, 2_000, 500) == pytest.approx((2_000 * pin + 500 * pout) / 1e6)


@pytest.mark.parametrize("model", [MODEL, "no-such-model", None])
def test_cost_usd_zero_tokens_is_zero(model: str | None) -> None:
    """Zero tokens cost 0.0 whatever the model (regex, fully cached run)."""
    assert ev.cost_usd(model, 0, 0) == 0.0


@pytest.mark.parametrize("model", ["no-such-model", None])
def test_cost_usd_unknown_model_never_guesses(model: str | None) -> None:
    """Non-zero tokens on a model not in the table give None."""
    assert ev.cost_usd(model, 10, 5) is None


def test_usage_totals_counts_only_fresh_calls() -> None:
    """Tokens, cost and latency count fresh calls; hits count as cache_hits; records without call are skipped."""
    records = {
        "a": {"call": {"cached": False, "input_tokens": 100, "output_tokens": 10, "latency_s": 1.0}},
        "b": {"call": {"cached": False, "input_tokens": 300, "output_tokens": 30, "latency_s": 3.0}},
        "c": {"call": {"cached": True, "input_tokens": 999, "output_tokens": 99, "latency_s": 9.0}},
        "d": {"raw": None, "error": None},
    }
    u = ev.usage_totals({"x": {"path": "data/posts.jsonl", "records": records}}, MODEL)
    assert (u["api_calls"], u["cache_hits"]) == (2, 1)
    assert (u["input_tokens"], u["output_tokens"]) == (400, 40)
    assert u["cost_usd"] == pytest.approx(ev.cost_usd(MODEL, 400, 40))
    assert u["latency_s_total"] == pytest.approx(4.0)
    assert u["latency_s_mean"] == pytest.approx(2.0)
    assert list(u["price_per_mtok"]) == list(ev.PRICES_PER_MTOK[MODEL])
    assert isinstance(u["prices_as_of"], str) and u["prices_as_of"]


def test_usage_totals_regex_style_records() -> None:
    """Fresh calls without tokens (regex) count no api_calls but do count latency."""
    records = {"a": {"call": {"cached": False, "input_tokens": None, "output_tokens": None, "latency_s": 0.25}}}
    u = ev.usage_totals({"x": {"path": "p", "records": records}}, None)
    assert (u["api_calls"], u["cache_hits"], u["input_tokens"], u["output_tokens"]) == (0, 0, 0, 0)
    assert u["cost_usd"] == 0.0
    assert u["latency_s_total"] == pytest.approx(0.25)


def test_usage_totals_unknown_model_cost_is_none() -> None:
    """Billed tokens on an unknown model report cost None and no price."""
    records = {"a": {"call": {"cached": False, "input_tokens": 5, "output_tokens": 5, "latency_s": 0.1}}}
    u = ev.usage_totals({"x": {"path": "p", "records": records}}, "mystery-model")
    assert u["cost_usd"] is None and u["price_per_mtok"] is None


# ---------- CLI: --repeats, --no-cache ----------

def test_repeats_empty_cache_every_run_calls(claude_main, tmp_path: Path, capsys) -> None:
    """--repeats 3 on an empty cache writes _r1.._r3 files and calls the API 3x per post."""
    out_dir, cache_dir = tmp_path / "out", tmp_path / "cache"
    client = claude_main("--repeats", "3", results_dir=out_dir, cache_dir=cache_dir)
    assert client.calls == 3 * N_DEV_POSTS
    names = sorted(p.stem for p in out_dir.glob("*.json"))
    assert len(names) == 3 and [n[-3:] for n in names] == ["_r1", "_r2", "_r3"]
    results = load_results(out_dir)
    assert [r["repeat"] for r in results] == [{"index": k, "of": 3} for k in (1, 2, 3)]
    for r in results:
        assert r["usage"]["api_calls"] == N_DEV_POSTS and r["usage"]["cache_hits"] == 0
        assert r["cache_enabled"] is True
    out = capsys.readouterr().out
    assert "mean" in out and "min–max" in out


def test_repeats_prewarmed_cache_run1_hits_rest_fresh(claude_main, tmp_path: Path) -> None:
    """On a warm cache, run 1 replays every post and runs 2 and 3 call the API for every post."""
    out_dir, cache_dir = tmp_path / "out", tmp_path / "cache"
    claude_main(results_dir=tmp_path / "warmup", cache_dir=cache_dir)
    client = claude_main("--repeats", "3", results_dir=out_dir, cache_dir=cache_dir)
    assert client.calls == 2 * N_DEV_POSTS
    usage = [r["usage"] for r in load_results(out_dir)]
    assert [(u["api_calls"], u["cache_hits"]) for u in usage] == [
        (0, N_DEV_POSTS), (N_DEV_POSTS, 0), (N_DEV_POSTS, 0)]
    assert usage[0]["cost_usd"] == 0.0 and usage[0]["input_tokens"] == 0


def test_cached_run_records_are_marked(claude_main, tmp_path: Path) -> None:
    """A second invocation on a warm cache makes no calls and marks every record cached."""
    cache_dir = tmp_path / "cache"
    claude_main(results_dir=tmp_path / "a", cache_dir=cache_dir)
    client = claude_main(results_dir=tmp_path / "b", cache_dir=cache_dir)
    assert client.calls == 0
    (result,) = load_results(tmp_path / "b")
    assert all(r["call"]["cached"] for ds in result["datasets"].values() for r in ds["records"].values())


def test_no_cache_flag_bypasses_cache(claude_main, tmp_path: Path) -> None:
    """--no-cache calls the API every time and writes no cache files."""
    cache_dir = tmp_path / "cache"
    first = claude_main("--no-cache", results_dir=tmp_path / "a", cache_dir=cache_dir)
    second = claude_main("--no-cache", results_dir=tmp_path / "b", cache_dir=cache_dir)
    assert first.calls == second.calls == N_DEV_POSTS
    assert not cache_dir.exists() or not any(cache_dir.rglob("*"))
    (result,) = load_results(tmp_path / "b")
    assert result["cache_enabled"] is False


def test_claude_run_usage_and_cost(claude_main, tmp_path: Path) -> None:
    """A fresh Claude run reports tokens and cost from the price table."""
    claude_main(results_dir=tmp_path / "out", cache_dir=tmp_path / "cache")
    (result,) = load_results(tmp_path / "out")
    u = result["usage"]
    assert (u["input_tokens"], u["output_tokens"]) == (N_DEV_POSTS * IN_TOK, N_DEV_POSTS * OUT_TOK)
    assert u["cost_usd"] == pytest.approx(ev.cost_usd(MODEL, N_DEV_POSTS * IN_TOK, N_DEV_POSTS * OUT_TOK))
    assert u["cost_usd"] > 0
    assert result["repeat"] == {"index": 1, "of": 1}


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_repeats_must_be_positive(claude_main, tmp_path: Path, bad: str) -> None:
    """--repeats below 1 is rejected."""
    with pytest.raises(SystemExit):
        claude_main("--repeats", bad, results_dir=tmp_path / "out", cache_dir=tmp_path / "cache")
    assert not list((tmp_path / "out").glob("*.json"))


# ---------- CLI: regex run and save() ----------

def test_regex_run_new_keys_and_no_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A regex run records zero usage, repeat 1 of 1, cache_enabled, and never creates a cache dir."""
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path / "default")
    before = results_listing()
    out_dir, cache_dir = tmp_path / "out", tmp_path / "cache"
    ev.main(["--extractor", "regex", "--results-dir", str(out_dir), "--cache-dir", str(cache_dir)])
    (result,) = load_results(out_dir)
    assert result["usage"]["api_calls"] == 0 and result["usage"]["cost_usd"] == 0.0
    assert result["usage"]["cache_hits"] == 0
    assert result["repeat"] == {"index": 1, "of": 1}
    assert isinstance(result["cache_enabled"], bool)
    assert not cache_dir.exists()
    assert results_listing() == before


def test_save_never_overwrites(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Back-to-back runs (same timestamp second) produce distinct files, never an overwrite."""
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path / "default")
    out_dir = tmp_path / "out"
    for _ in range(3):
        ev.main(["--extractor", "regex", "--results-dir", str(out_dir), "--cache-dir", str(tmp_path / "c")])
    stems = sorted(p.stem for p in out_dir.glob("*.json"))
    assert len(stems) == 3 and all(s.startswith("regex_") for s in stems)
    by_stamp: dict[str, list[str]] = {}
    for s in stems:  # a taken stem gets -2, -3, ... appended
        stamp, _, suffix = s.partition("-")
        by_stamp.setdefault(stamp, []).append(suffix)
    for suffixes in by_stamp.values():
        assert sorted(suffixes) == sorted(["", *(str(k) for k in range(2, len(suffixes) + 1))])
