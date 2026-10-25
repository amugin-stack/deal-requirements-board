# Findings: correctness and API-contract review

Read-only review of `schema.py`, `matcher.py`, `followup.py`, `brokers.py`, `extractors/__init__.py`,
`extractors/claude.py` (correctness) and `api.py` against `docs/agent-graphs/api.md` and `README.md`
(API contract), at `main` `c4ae4be`. Generated from `findings.json`; the loop is in `graph.md` and `LOG.md`.

**Result:** converged in 2 rounds. 12 candidates: 8 confirmed, 4 rejected.

| dimension | confirmed | rejected |
|---|---|---|
| correctness | 5 | 2 |
| api_contract | 3 | 2 |

No finding returns an unlicensed broker or leaks post text: the gate and the logging held under every probe
(see `LOG.md`, round 2). Every confirmed finding is low severity after verification; four were filed as
medium and downgraded by their verifier because the path is rare or only reachable by a future caller.

## What I'd fix first

1. **C-1, truncated Claude tool call accepted.** The one confirmed bug on the live Claude path: a
   `max_tokens` cut-off silently becomes a requirement with nulls, so the poster is asked for data they gave
   and eval scores it as a prediction. One-line fix (accept only `stop_reason == "tool_use"`), cheap test.
2. **A-2, then A-1: make every error JSON and documented.** A catch-all after extraction gives a fixed
   `ErrorDetail` 500 with the post-id log line; declaring 422 as `HTTPValidationError | ErrorDetail` makes
   the published schema true. Together they make the OpenAPI a contract a generated client can rely on.
3. **C-6, city normalization.** The only finding that changes ranking for plausible Claude output
   ('Ft. Lauderdale', 'Austin, TX'). One shared `normalize_city()` on both sides of the comparison.

C-4, C-5, C-7 and A-3 are latent or cosmetic; fix them when touching those files.

## Confirmed

| id | severity | dimension | title | location |
|---|---|---|---|---|
| A-1 | low | api_contract | Extraction-failure 422 body is {detail: str}, but OpenAPI documents 422 only as HTTPValidationError (detail: array) | `api.py:341` |
| A-2 | low | api_contract | Errors after extraction (match, reshaping, follow-up) give a text/plain 500, not the documented JSON ErrorDetail | `api.py:363` |
| A-3 | low | api_contract | OpenAPI publishes minLength 1 / maxLength 4000 on the raw text, but the server applies them after stripping | `api.py:67` |
| C-1 | low | correctness | extract_raw returns a tool call truncated at max_tokens as a valid extraction | `extractors/claude.py:121` |
| C-4 | low | correctness | missing_fields goes stale after model_copy(update=...) or attribute assignment | `schema.py:105` |
| C-5 | low | correctness | Property-management deal requests are always asked the buy/sell/lease/1031 question | `followup.py:115` |
| C-6 | low | correctness | Market matching only strips and lowercases the city, so 'Austin, TX', 'Ft. Lauderdale' or a NBSP lose the +2 | `matcher.py:68` |
| C-7 | low | correctness | load_brokers fails on a UTF-8 BOM or an all-blank row, aborting the whole load with no row context | `brokers.py:62` |

### A-1 (low, api_contract): Extraction-failure 422 body is {detail: str}, but OpenAPI documents 422 only as HTTPValidationError (detail: array)

`api.py:341`: `detail="could not extract a deal requirement from this post",`

**Claim.** POST /match returns two 422 shapes: request validation gives detail as an array of error objects, while ExtractionError gives detail as a string. app.openapi() documents 422 only as HTTPValidationError, so a generated client fails to parse the extraction-failure 422.

**Verifier.** Reproduced. api.py's ErrorDetail docstring calls it 'the body of every non-validation error' and 413/500/503 declare it, but the extraction-failure 422 doesn't. The contract ('ExtractionError -> 422') is met, so this is a schema-documentation gap: low.

**Reproduction.**

```python
import os; os.environ.pop("ANTHROPIC_API_KEY", None); import sys; sys.path.insert(0, "/Users/annikamugin/Documents/deal-requirements-board-review-correctness")
import json
from datetime import date
from fastapi.testclient import TestClient
import api
from extractors import ExtractionError

class FailingExtractor:
    name = "fake"
    def extract(self, text):
        raise ExtractionError("bad output")

def fake_match(req, brokers, as_of):
    raise AssertionError("not reached")

api.app.dependency_overrides[api.get_extractor] = lambda: FailingExtractor()
api.app.dependency_overrides[api.get_brokers] = lambda: []
api.app.dependency_overrides[api.get_as_of] = lambda: date(2026, 1, 1)
api.app.dependency_overrides[api.get_matcher] = lambda: fake_match
c = TestClient(api.app)
r1 = c.post("/match", json={"text": "   "})
print("validation 422:", r1.status_code, r1.json())
r2 = c.post("/match", json={"text": "need a 10k sf warehouse in Austin"})
print("extraction 422:", r2.status_code, r2.json())
spec = api.app.openapi()
resp = spec["paths"]["/match"]["post"]["responses"]
print("documented 422:", json.dumps(resp.get("422")))
print("HTTPValidationError.detail:", json.dumps(spec["components"]["schemas"]["HTTPValidationError"]["properties"]["detail"]))
```

```bash
cd /private/tmp/claude-501/-Users-annikamugin-Documents-email-project/23e6bcdd-ffb9-4971-a45e-500380da38d7/scratchpad/verify/A-1 && /Users/annikamugin/Documents/deal-requirements-board/.venv/bin/python -I final.py
```

Output:

```text
validation 422: 422 {'detail': [{'type': 'string_too_short', 'loc': ['body', 'text'], 'msg': 'String should have at least 1 character', 'ctx': {'min_length': 1}}]}
extraction 422: 422 {'detail': 'could not extract a deal requirement from this post'}
documented 422: {"description": "Validation Error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}}}
HTTPValidationError.detail: {"items": {"$ref": "#/components/schemas/ValidationError"}, "type": "array", "title": "Detail"}
```

**Proposed fix.** Declare 422 in `responses` as anyOf HTTPValidationError | ErrorDetail (with a description covering extraction failure), or give the extraction failure the same array shape, e.g. [{'type': 'extraction_failed', 'loc': ['body', 'text'], 'msg': ...}].

**Regression test.** `test_extraction_422_body_matches_openapi_schema`: The JSON body of an ExtractionError response validates (jsonschema, with components resolved) against the 422 schema app.openapi() publishes for POST /match.

### A-2 (low, api_contract): Errors after extraction (match, reshaping, follow-up) give a text/plain 500, not the documented JSON ErrorDetail

`api.py:363`: `result = match(requirement, brokers, as_of)`

**Claim.** Only extractor.extract() is wrapped. An exception from match(), the BrokerMatch reshaping, follow_up_question() or MatchResponse(...) reaches Starlette, which returns text/plain 'Internal Server Error', while OpenAPI documents 500 as application/json ErrorDetail; the post-id log line is skipped.

**Verifier.** Reproduced the text/plain 500 and the missing post-id line; the claimed traceback leak of post-derived text depends on uvicorn's traceback logging and was not shown under TestClient, so it is not part of the confirmed claim. Today's matcher.match/follow_up_question have no raise sites on validated input, so this is a contract gap for a broken/future matcher: low.

**Reproduction.**

```python
import os; os.environ.pop("ANTHROPIC_API_KEY", None); import sys; sys.path.insert(0, "/Users/annikamugin/Documents/deal-requirements-board-review-correctness")
import io, logging
from datetime import date
from fastapi.testclient import TestClient
import api
from schema import DealRequirement

buf = io.StringIO()
h = logging.StreamHandler(buf); h.setLevel(logging.DEBUG)
logging.getLogger().addHandler(h); logging.getLogger().setLevel(logging.DEBUG)

REQ = DealRequirement(is_deal_request=True, state="TX", city="ZebraCity", property_type="retail")
class FakeExtractor:
    name = "fake"
    def extract(self, text): return REQ
def boom_matcher(req, brokers, as_of):
    raise ValueError(f"bad city {req.city}")
def bad_shape_matcher(req, brokers, as_of):
    class R: matches = [object()]; excluded_for_licensing = 0
    return R()

api.app.dependency_overrides[api.get_extractor] = lambda: FakeExtractor()
api.app.dependency_overrides[api.get_brokers] = lambda: []
api.app.dependency_overrides[api.get_as_of] = lambda: date(2026, 10, 20)
c = TestClient(api.app, raise_server_exceptions=False)
for label, m in [("matcher raises", boom_matcher), ("bad reshaping", bad_shape_matcher)]:
    api.app.dependency_overrides[api.get_matcher] = lambda m=m: m
    buf.truncate(0); buf.seek(0)
    r = c.post("/match", json={"text": "Need a TX broker ref ZEBRA-7731"})
    log = buf.getvalue()
    print(label, "->", r.status_code, r.headers.get("content-type"), repr(r.text))
    print("  post-id line:", "post=" in log)
spec = c.get("/openapi.json").json()["paths"]["/match"]["post"]["responses"]["500"]
print("openapi 500:", spec["description"], list(spec["content"]))
```

```bash
cd /private/tmp/claude-501/-Users-annikamugin-Documents-email-project/23e6bcdd-ffb9-4971-a45e-500380da38d7/scratchpad/verify/A-2 && /Users/annikamugin/Documents/deal-requirements-board/.venv/bin/python -I final.py
```

Output:

```text
matcher raises -> 500 text/plain; charset=utf-8 'Internal Server Error'
  post-id line: False
bad reshaping -> 500 text/plain; charset=utf-8 'Internal Server Error'
  post-id line: False
openapi 500: The extractor failed unexpectedly ['application/json']
```

**Proposed fix.** Wrap the post-extraction block in try/except Exception: log post_id and type(e).__name__ only and raise HTTPException(500, 'matching failed unexpectedly') from None; or add a global Exception handler returning ErrorDetail JSON. Widen the documented 500 description.

**Regression test.** `test_matcher_crash_returns_fixed_json_500`: With a matcher raising ValueError, the response is 500 application/json {'detail': <fixed message>} and a log line with post=<id> is emitted, with no post text in caplog.

### A-3 (low, api_contract): OpenAPI publishes minLength 1 / maxLength 4000 on the raw text, but the server applies them after stripping

`api.py:67`: `str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_POST_CHARS)`

**Claim.** The schema for MatchRequest.text is {type: string, minLength: 1, maxLength: 4000}; the server accepts a 4010-char raw post (10 spaces + 4000) with 200 and rejects '   \n' (raw 4) with 422, so a schema-validating client disagrees with the server both ways.

**Verifier.** Reproduced. The MatchRequest description (from its docstring) does say whitespace is stripped before the length check, and the server matches api.md ('1-4000 characters after stripping'), so this is imprecision in the machine-readable bounds, not a behavior bug: low.

**Reproduction.**

```python
import os; os.environ.pop("ANTHROPIC_API_KEY", None); import sys; sys.path.insert(0, "/Users/annikamugin/Documents/deal-requirements-board-review-correctness")
import json
from datetime import date
from types import SimpleNamespace
from fastapi.testclient import TestClient
import api
from schema import DealRequirement

REQ = DealRequirement(is_deal_request=False)
class FakeExtractor:
    name = "regex"
    def extract(self, text): return REQ

api.app.dependency_overrides[api.get_extractor] = lambda: FakeExtractor()
api.app.dependency_overrides[api.get_brokers] = lambda: []
api.app.dependency_overrides[api.get_as_of] = lambda: date(2026, 10, 20)
api.app.dependency_overrides[api.get_matcher] = lambda: (lambda r, b, d: SimpleNamespace(matches=[], excluded_for_licensing=0))
c = TestClient(api.app)
schema = c.get("/openapi.json").json()["components"]["schemas"]["MatchRequest"]
print("schema.text =", json.dumps(schema["properties"]["text"]))
for label, t in {"10 spaces + 4000 x (raw 4010)": " " * 10 + "x" * 4000, "4001 x": "x" * 4001, "'   \\n' (raw 4)": "   \n"}.items():
    print(label, "->", c.post("/match", json={"text": t}).status_code)
```

```bash
cd /private/tmp/claude-501/-Users-annikamugin-Documents-email-project/23e6bcdd-ffb9-4971-a45e-500380da38d7/scratchpad/verify/A-3 && /Users/annikamugin/Documents/deal-requirements-board/.venv/bin/python -I final.py
```

Output:

```text
schema.text = {"type": "string", "maxLength": 4000, "minLength": 1, "title": "Text"}
10 spaces + 4000 x (raw 4010) -> 200
4001 x -> 422
'   \n' (raw 4) -> 422
```

**Proposed fix.** Override the field's JSON schema: drop minLength/maxLength from the raw-string schema (the 64 KiB body cap is the raw bound) and state the stripped 1-4000 rule in the field description, or add a pattern requiring a non-whitespace character.

**Regression test.** `test_openapi_text_constraints_match_stripping_semantics`: ' '*10 + 'a'*4000, which the server accepts with 200, validates against the published MatchRequest schema.

### C-1 (low, correctness): extract_raw returns a tool call truncated at max_tokens as a valid extraction

`extractors/claude.py:121`: `if response.stop_reason == "refusal":`

**Claim.** Only stop_reason 'refusal' is rejected. A response with stop_reason='max_tokens' whose tool_use input is incomplete is returned and validates (every field but is_deal_request is optional), so a cut-off extraction silently becomes a requirement with nulls and extra missing_fields instead of raising ExtractionError.

**Verifier.** Reproduced with a fake client returning stop_reason='max_tokens' and a partial tool_use block: extract() returned a DealRequirement with five nulls listed as missing, contradicting ExtractionError's documented scope ('bad output'). Downgraded medium -> low: max_tokens=1024 against ~8 short fields makes truncation rare.

**Reproduction.**

```python
import os; os.environ.pop("ANTHROPIC_API_KEY", None); import sys; sys.path.insert(0, "/Users/annikamugin/Documents/deal-requirements-board-review-correctness")
from types import SimpleNamespace as NS
from extractors.claude import ClaudeExtractor
from extractors import ExtractionError

class FakeMessages:
    def create(self, **kw):
        # Truncated tool call: only is_deal_request + state made it before max_tokens hit
        return NS(stop_reason="max_tokens",
                  usage=NS(input_tokens=500, output_tokens=1024),
                  content=[NS(type="tool_use", name="record_deal_requirement",
                              input={"is_deal_request": True, "state": "TX"})])
class FakeClient: messages = FakeMessages()

ex = ClaudeExtractor(client=FakeClient())
try:
    r = ex.extract("Buyer needs 4-plex in Austin TX, $1.2M, closing in 60 days")
    print("NO ERROR RAISED; result:", r.model_dump())
except ExtractionError as e:
    print("ExtractionError:", e)
```

```bash
cd /private/tmp/claude-501/-Users-annikamugin-Documents-email-project/23e6bcdd-ffb9-4971-a45e-500380da38d7/scratchpad/verify/C-1 && /Users/annikamugin/Documents/deal-requirements-board/.venv/bin/python -I final.py
```

Output:

```text
NO ERROR RAISED; result: {'is_deal_request': True, 'state': 'TX', 'city': None, 'property_type': None, 'transaction_type': None, 'deal_value_usd': None, 'timeline': None, 'notes': None, 'missing_fields': ['city', 'property_type', 'transaction_type', 'deal_value_usd', 'timeline']}
```

**Proposed fix.** Accept only stop_reason == 'tool_use'; for any other stop_reason (max_tokens included) raise ExtractionError(f'truncated (stop_reason=...)') after recording last_usage.

**Regression test.** `test_claude_extractor_rejects_max_tokens_truncated_tool_call`: A fake response with stop_reason='max_tokens' and a tool_use block makes extract() and extract_raw() raise ExtractionError, and last_usage is still set.

### C-4 (low, correctness): missing_fields goes stale after model_copy(update=...) or attribute assignment

`schema.py:105`: `self.missing_fields = (`

**Claim.** missing_fields is computed only in an after-validator; validate_assignment is off and model_copy skips validation, so setting state to None leaves 'state' out of missing_fields and follow_up_question (which promises it 'never disagrees with the gaps') asks about transaction_type while state is null.

**Verifier.** Reproduced via both model_copy(update=) and attribute assignment. No production path copies or mutates a requirement today (tests/test_api.py:220 already works around it), so it's a latent hazard contradicting the followup docstring: low.

**Reproduction.**

```python
import os; os.environ.pop("ANTHROPIC_API_KEY", None); import sys; sys.path.insert(0, "/Users/annikamugin/Documents/deal-requirements-board-review-correctness")
from schema import DealRequirement
from followup import follow_up_question
req = DealRequirement(is_deal_request=True, state="TX", city="Austin", property_type="office", deal_value_usd=1000000, timeline="within_30_days")
print("base missing:", req.missing_fields)
c = req.model_copy(update={"state": None})
print("copy state:", c.state, "missing:", c.missing_fields, "q:", follow_up_question(c))
req2 = req.model_copy(); req2.state = None
print("assign state:", req2.state, "missing:", req2.missing_fields, "q:", follow_up_question(req2))
print("revalidated missing:", DealRequirement.model_validate(c.model_dump()).missing_fields)
```

```bash
cd /private/tmp/claude-501/-Users-annikamugin-Documents-email-project/23e6bcdd-ffb9-4971-a45e-500380da38d7/scratchpad/verify/C-4 && /Users/annikamugin/Documents/deal-requirements-board/.venv/bin/python -I final.py
```

Output:

```text
base missing: ['transaction_type']
copy state: None missing: ['transaction_type'] q: Is the client buying, selling, leasing, or doing a 1031 exchange for the office property in Austin?
assign state: None missing: ['transaction_type'] q: Is the client buying, selling, leasing, or doing a 1031 exchange for the office property in Austin?
revalidated missing: ['state', 'transaction_type']
```

**Proposed fix.** Make missing_fields a @computed_field derived from the current fields, or set model_config = ConfigDict(validate_assignment=True, frozen=True); have followup derive gaps from the fields rather than the stored list.

**Regression test.** `test_missing_fields_tracks_field_changes`: After model_copy(update={'state': None}) (or assignment), 'state' is in missing_fields and follow_up_question starts with 'Which state'.

### C-5 (low, correctness): Property-management deal requests are always asked the buy/sell/lease/1031 question

`followup.py:115`: `if field in missing:`

**Claim.** The system prompt and schema tell the extractor to leave transaction_type null for non-transaction deal requests (e.g. a property manager), but _compute_missing counts that null as a gap and PRIORITY puts it second, so such posts get a question with no correct answer; 'not applicable' is handled as 'not stated'.

**Verifier.** Reproduced. extractors/claude.py:33-35 and schema.py:72 prescribe transaction_type null for PM requests; no test pins the resulting question as intended. Low: rare post type (holdout has one, v3-15) and the cost is one irrelevant question, not a bad match.

**Reproduction.**

```python
import os; os.environ.pop("ANTHROPIC_API_KEY", None); import sys; sys.path.insert(0, "/Users/annikamugin/Documents/deal-requirements-board-review-correctness")
from schema import DealRequirement, PropertyType
from followup import follow_up_question
# Property-management request extracted per SYSTEM_PROMPT: deal request, transaction_type null.
req = DealRequirement(is_deal_request=True, state="NC", city="Raleigh",
                      property_type=PropertyType.multifamily, transaction_type=None)
print("missing:", req.missing_fields)
print("question:", follow_up_question(req))
```

```bash
cd /private/tmp/claude-501/-Users-annikamugin-Documents-email-project/23e6bcdd-ffb9-4971-a45e-500380da38d7/scratchpad/verify/C-5 && /Users/annikamugin/Documents/deal-requirements-board/.venv/bin/python -I final.py
```

Output:

```text
missing: ['transaction_type', 'deal_value_usd', 'timeline']
question: Is the client buying, selling, leasing, or doing a 1031 exchange for the multifamily property in Raleigh?
```

**Proposed fix.** Distinguish 'not applicable' from 'not stated' (e.g. a TransactionType value or an is_transaction flag the extractor sets for PM-style requests that _compute_missing doesn't count), or skip transaction_type in the follow-up when the request is flagged non-transactional.

**Regression test.** `test_property_management_request_not_asked_transaction_type`: A PM-style deal with the non-transaction marker has no transaction_type in missing_fields, and its follow-up is not the buy/sell/lease/1031 question.

### C-6 (low, correctness): Market matching only strips and lowercases the city, so 'Austin, TX', 'Ft. Lauderdale' or a NBSP lose the +2

`matcher.py:68`: `city = req.city.strip().lower()`

**Claim.** A city like 'Austin, TX', 'Ft. Lauderdale' or one with a non-breaking space never equals a listed market or TX_CITY_TO_COUNTY key, so the +2 market/county point is lost and the ranking changes.

**Verifier.** Reproduced for all three variants. The regex baseline can't produce them (it drops the state suffix), but the Claude prompt's 'city name as written' plausibly passes abbreviations like 'Ft.' through, and fx-02 lists 'Fort Lauderdale'. Not documented as intended: low.

**Reproduction.**

```python
import os; os.environ.pop("ANTHROPIC_API_KEY", None); import sys; sys.path.insert(0, "/Users/annikamugin/Documents/deal-requirements-board-review-correctness")
from datetime import date
from brokers import Broker
from matcher import match
from schema import DealRequirement

def broker(bid, state, county=None, markets=()):
    return Broker(broker_id=bid, name=bid, source="fictional", license_state=state, license_number="F-"+bid,
                  license_type="Broker Individual", license_status="Active", license_expiration=date(2030,1,1),
                  county=county, markets=list(markets), property_types=[], transaction_types=[],
                  min_deal_usd=None, max_deal_usd=None)

AS_OF = date(2026,10,20)
tx = [broker("tx-travis","TX",county="Travis"), broker("tx-harris","TX",county="Harris")]
fl = [broker("fl-ftl","FL",markets=["Miami","Fort Lauderdale"])]
bexar = [broker("tx-bexar","TX",county="Bexar")]
for st, city, bs in [("TX","Austin",tx),("TX","Austin, TX",tx),("TX","San\u00a0Antonio",bexar),
                     ("TX","San Antonio",bexar),("FL","Fort Lauderdale",fl),("FL","Ft. Lauderdale",fl)]:
    r = match(DealRequirement(is_deal_request=True, state=st, city=city), bs, AS_OF)
    print(repr(city), [(m.broker.broker_id, m.score) for m in r.matches])
```

```bash
cd /private/tmp/claude-501/-Users-annikamugin-Documents-email-project/23e6bcdd-ffb9-4971-a45e-500380da38d7/scratchpad/verify/C-6 && /Users/annikamugin/Documents/deal-requirements-board/.venv/bin/python -I final.py
```

Output:

```text
'Austin' [('tx-travis', 2), ('tx-harris', 0)]
'Austin, TX' [('tx-harris', 0), ('tx-travis', 0)]
'San\xa0Antonio' [('tx-bexar', 0)]
'San Antonio' [('tx-bexar', 2)]
'Fort Lauderdale' [('fl-ftl', 2)]
'Ft. Lauderdale' [('fl-ftl', 0)]
```

**Proposed fix.** One normalize_city() used on both sides (req.city, broker.markets, TX_CITY_TO_COUNTY keys): NFKC, collapse internal whitespace, casefold, drop a trailing ', <req.state>', expand 'ft.'->'fort' and 'st.'->'saint'.

**Regression test.** `test_market_point_normalizes_city_variants`: For TX, 'Austin, TX' and 'San\u00a0Antonio' earn the '+2 based in ... County' reason; for FL, 'Ft. Lauderdale' earns '+2 serves' from a 'Fort Lauderdale' market.

### C-7 (low, correctness): load_brokers fails on a UTF-8 BOM or an all-blank row, aborting the whole load with no row context

`brokers.py:62`: `with open(path, newline="") as f:`

**Claim.** The CSV is opened with the locale encoding and no BOM handling, so a BOM makes the first header '\ufeffbroker_id' and the load raises 'broker_id Field required'; a row of only commas also raises; either way a bare ValidationError with no line number aborts the entire broker list.

**Verifier.** Reproduced with test CSVs in the scratchpad. Only reachable with hand-edited or Excel-saved input: scripts/build_brokers.py writes no BOM and the real file loads 226 rows. Low; 'every row fails' is really 'the first row raises and the load stops'.

**Reproduction.**

```python
import os; os.environ.pop("ANTHROPIC_API_KEY", None); import sys; sys.path.insert(0, "/Users/annikamugin/Documents/deal-requirements-board-review-correctness")
from pathlib import Path
from brokers import load_brokers, BROKERS_CSV
D = Path(__file__).parent
raw = BROKERS_CSV.read_bytes()
print("real file BOM:", raw.startswith(b"\xef\xbb\xbf"), "loads:", len(load_brokers()))
lines = raw.decode("utf-8").splitlines(keepends=True)
cases = {"bom": b"\xef\xbb\xbf" + raw,
         "commas": ("".join(lines[:2]) + "," * (lines[0].count(",")) + "\n").encode()}
for name, data in cases.items():
    p = D / f"{name}.csv"; p.write_bytes(data)
    try:
        print(name, "OK", len(load_brokers(p)))
    except Exception as e:
        msg = str(e).splitlines()
        print(name, type(e).__name__, msg[0], "|", msg[1:3])
```

```bash
cd /private/tmp/claude-501/-Users-annikamugin-Documents-email-project/23e6bcdd-ffb9-4971-a45e-500380da38d7/scratchpad/verify/C-7 && /Users/annikamugin/Documents/deal-requirements-board/.venv/bin/python -I final.py
```

Output:

```text
real file BOM: False loads: 226
bom ValidationError 1 validation error for Broker | ['broker_id', "  Field required [type=missing, input_value={'\\ufeffbroker_id': 'tx-0... '', 'max_deal_usd': ''}, input_type=dict]"]
commas ValidationError 7 validation errors for Broker | ['broker_id', '  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]']
```

**Proposed fix.** open(path, newline='', encoding='utf-8-sig'); skip rows whose values are all empty; wrap model_validate to re-raise with reader.line_num and the broker_id.

**Regression test.** `test_load_brokers_handles_bom_and_blank_rows`: A BOM-prefixed CSV loads with broker_id 'fx-01'; an all-comma row is skipped; an invalid row raises an error naming its line number.

## Rejected

Each was reproduced mechanically, then rejected because the path is unreachable or the behavior
is intended and documented. Full scripts are in `findings.json`.

| id | dimension | title | why rejected |
|---|---|---|---|
| C-2 | correctness | is_licensed raises TypeError when as_of is a datetime | Mechanism reproduces, but no caller can reach it: the only production caller is api.py, whose get_as_of() returns date.today(); every test pins a date; scripts never call the matcher with a datetime. Rejected as unreachable (hardening note at most). |
| C-3 | correctness | parse_requirement coerces wrongly typed values instead of raising | All coercions reproduce, but tests/test_red_team.py pins this leniency as intended (deal-value-string-coerced, is-deal-request-yes-validated, and 'Non-string states are turned into None by the validator: fail closed'), and live Claude output comes from a strict:true tool call that can't send these types. Rejected: intended behavior. |
| A-4 | api_contract | Malformed Content-Length gets 413 instead of 400 | 413 reproduces only when calling the middleware directly. In the documented deployment (uvicorn; only h11 installed), h11 0.16 rejects non-digit Content-Length with 400 before the app runs and strips whitespace, so ' 13' arrives as b'13'. Rejected: unreachable. |
| A-5 | api_contract | Cached Claude extractor keeps the API key from its first build | Reproduces with fake keys, but the full docstring (api.py:133-135) says '...the choice follows the current config, but the Claude extractor itself is cached': a documented trade-off, and the claude/regex choice does follow the env. Rejected: intended. |
