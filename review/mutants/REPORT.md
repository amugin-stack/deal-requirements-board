# Mutation testing: licensing gate and match path

Branch `gate-mutants`, cut from `main` at `c47ee1f`. 160 deliberate bugs ("mutants") were applied,
one at a time, to scratchpad copies of the package, and the full suite was run on each. A mutant
is **killed** when at least one test fails, and **survives** when the whole suite stays green.
Every survivor was checked by a separate verifier, who wrote a short script showing an input
where the original and the mutant behave differently. One survivor could not be told apart from
the original and is marked equivalent. The worktree was never patched, and no existing file
changed. Mutant definitions and the runner are in `mutants.py`, per-mutant results in
`results.json`, and the round-by-round record in `LOG.md`.

## Result

| | count |
|---|---|
| mutants | 160 (159 non-equivalent) |
| killed | 114 |
| survived (verified) | 45: **13 high**, 14 medium, 18 low |
| equivalent | 1 (`api-13`) |
| rounds | 4 (the maximum). **Not converged**: round 4 still added 2 high and 1 medium survivor |

Severity: **high** = an unlicensed, expired, out-of-state or non-exactly-"Active" broker could
reach a client (directly, or via data a rebuild would produce); **medium** = wrong counts,
ranking, notes, status codes or fields a client would see; **low** = cosmetic, or only reachable
through an input the real entry points never produce.

| file | killed | survived | equivalent |
|---|---|---|---|
| `matcher.py` | 52 | 9 | 0 |
| `api.py` | 33 | 14 | 1 |
| `brokers.py` | 7 | 7 | 0 |
| `cli/build_brokers.py` | 0 | 10 | 0 |
| `schema.py` | 7 | 3 | 0 |
| `extractors/*` (not `frozen/`) | 7 | 2 | 0 |
| `followup.py`, `config.py`, `paths.py` | 8 | 0 | 0 |

## Verdict

The gate itself is well protected. Of the 21 mutants aimed at `is_licensed`, 20 were killed,
most by dozens of tests: always-True, year-only expiry, `>` vs `>=`, off-by-one days, case-folded
or prefix status, probation accepted, unknown expiry treated as licensed, dropped conjuncts,
string-compared dates. The red team's known gap, a gate that compares only expiration years, is
now caught by 52 tests. So are the core `match()` bypasses: ranking ungated brokers, padding
results, skipping the gate for large limits, a no-state deal matching everyone, and a mutated
broker list. The promise is weak around the gate instead. The suite never runs the real
production clock (every API test pins `get_as_of`, so a frozen, lagging or UTC date lets expired
licenses through: `api-10`, `gate-20`, `gate-22`). It never loads a CSV with padded or
duplicate values (`gate-25`, `gate-28`, `gate-53`). It has no tests at all for the builder that
produces `data/brokers.csv`, where five mutants would make expired, unknown-expiry or probation
licensees look licensed in a rebuild. It never exercises the matcher-unavailable fallback
(`api-21` serves every broker on a broken import). And it never sends a null-state Claude result
or two prefix-sharing posts through one extractor (`gate-69`, `gate-71`), both of which route a
deal to brokers from the wrong state. Each round found new gaps one layer further out, which is
why the loop did not converge in four rounds. The 13 high survivors each come with a proposed
test (and a fictional `fx-mut-*` fixture row where one is needed). Adding those tests, starting
with `test_get_as_of_is_today`, the builder tests in a new `tests/test_build_brokers.py`, and
`test_matcher_import_error_is_503_and_returns_no_brokers`, would close every high gap found here
without touching `deal_board/`.

## Proof step

The runner on the always-True gate mutant (`python review/mutants/mutants.py show --work <scratchpad>/mut gate-00`):

```
gate-00: Gate always returns True: every broker in every state passes, whatever the status or expiry.
suite: 389 failed, 613 passed, 2 skipped, 5 xfailed, 2 warnings in 11.28s
     1 failing in tests/test_api.py
   297 failing in tests/test_compliance.py
    10 failing in tests/test_matcher.py
    81 failing in tests/test_red_team.py
   389 failing in total -> KILLED
```

The 81 red-team failures match the red team's own measurement. On two survivors, the low
`match-11` and the high `api-10`, the suite stays green:

```
match-11: Negative limit slices scored[:limit], returning all but the last |limit| brokers instead of none.
suite: 1002 passed, 2 skipped, 5 xfailed, 2 warnings in 6.87s
     0 failing in total -> SURVIVED
api-10: get_as_of returns a date one year in the past, so licenses expired within the last year pass the gate.
suite: 1002 passed, 2 skipped, 5 xfailed, 2 warnings in 6.72s
     0 failing in total -> SURVIVED
```

Copies give 1002 passed / 2 skipped where the worktree gives 1003 / 1: copies have no `.git` on
purpose, so `test_frozen_baseline_matches_tag` also skips there. That test only reads
`extractors/frozen/`, which no mutant touches, so no kill is lost.

`check_success.py` on the final commit: PASS on (a), (b), (c), (e), (f), (g); **FAIL on (d)**,
because the last round added 3 high/medium survivors. The script is frozen and was not edited, so
the loop is reported as not converged rather than re-scored.

## Survivors by severity

### High (13)

**api-10** · `get_as_of` in `deal_board/api.py` · round 1

- Mutant: get_as_of returns a date one year in the past, so licenses expired within the last year pass the gate.
- Evidence: Synthetic broker fx-demo-01 ('Fictional Lapsed Broker', TX, Active) whose license expired 30 days before today, checked with is_licensed(b, 'TX', api.get_as_of()): original False (as_of = today), mutant True (as_of = a year ago), so any license that lapsed in the last 12 months passes the gate in production. Every API and red-team test overrides api.get_as_of with a fixed AS_OF (test_api.py:146, :509; test_red_team.py:290), so the real dependency is never called.
- Proposed test: `test_get_as_of_is_today` in `tests/test_api.py`: Calls the real api.get_as_of() with no dependency override and asserts it equals datetime.date.today(); optionally POSTs /match without the as_of override with an in-state Active broker that expired yesterday and asserts matches == [] and excluded_for_licensing == 1.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-20** · `get_as_of` in `deal_board/api.py` · round 2

- Mutant: get_as_of returns yesterday (off by one day), so a license that expired yesterday passes the gate in production.
- Evidence: POST /match via TestClient for a TX deal with one fictional Active TX broker whose license expired yesterday (2026-10-24), clock frozen at 2026-10-25: the original excludes the broker, the mutant (as_of = yesterday) returns it. Every API test overrides get_as_of with a fixed AS_OF and none compares the real get_as_of with date.today().
- Proposed test: `test_get_as_of_is_today` in `tests/test_api.py`: With date.today() patched, api.get_as_of() == date.today(); and POST /match without a get_as_of override excludes a broker whose license_expiration is date.today() - 1 day (matches empty, excluded_for_licensing == 1).
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-22** · `get_as_of` in `deal_board/api.py` · round 2

- Mutant: get_as_of returns a frozen constant date (the TREC snapshot date 2026-10-20), so every license expiring after that date but before today keeps passing forever.
- Evidence: POST /match for a TX deal with one fictional Active TX broker whose license expired 2026-10-22, today 2026-10-25: the original excludes it; the mutant's get_as_of is pinned to 2026-10-20 so the expired broker is returned, and every license expiring after that date stays valid forever. It survived because the constant equals the tests' AS_OF and every test overrides get_as_of anyway.
- Proposed test: `test_get_as_of_follows_the_clock` in `tests/test_api.py`: With date.today() patched to two different dates (e.g. 2026-10-25 and 2027-01-01), api.get_as_of() returns each patched date in turn, so it is not a constant.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-25** · `Broker._blank_to_none` in `deal_board/brokers.py` · round 2 (verifier rated medium; lead changed it, see LOG.md)

- Mutant: Broker validator strips every string field, so a padded status like 'Active ' (or ' Active') is normalised to 'Active' and passes the exact-status gate, breaking the verbatim-status rule.
- Evidence: A fictional CSV row with license_status 'Active ' (trailing space) loaded with load_brokers(path), then a FL/Tampa deal through match(): the original keeps the status verbatim, fails closed and excludes the broker; the mutant strips every string field so the broker passes and is returned. Neither shipped CSV has padded values (all 252 rows checked) and no test loads a padded CSV. Verifier rated it medium (a padded 'Active' probably is active; 'Probation - Active ' stays rejected); the lead raised it to high because the suite's own test_status_must_be_exactly_active[Active ] defines that status as not licensed, and this mutant makes match() return such a broker.
- Proposed test: `test_padded_status_is_kept_verbatim_and_fails_closed` in `tests/test_brokers.py`: Write a tmp_path CSV with the fictional_brokers header and the proposed fixture row (license_status 'Active '), load it with load_brokers(path), and assert license_status == 'Active ' and that match() for an FL deal returns no matches with excluded_for_licensing == 1.
- Proposed fixture (fictional, a proposal only, not added to data/):

  ```csv
  broker_id,name,source,license_state,license_number,license_type,license_status,license_expiration,county,markets,property_types,transaction_types,min_deal_usd,max_deal_usd
  fx-mut-01,Fictional Broker FL-Padded,fictional,FL,FICT-FL-9001,Broker Individual,Active ,2028-06-30,,Tampa,retail,buy,1000000,10000000
  ```

**gate-28** · `load_brokers` in `deal_board/brokers.py` · round 2 (verifier rated medium; lead changed it, see LOG.md)

- Mutant: load_brokers reads the CSV with skipinitialspace=True, so a leading-space status like ' Active' is silently normalised to 'Active' and passes the gate.
- Evidence: A fictional CSV row with license_status ' Active' (leading space after the comma) loaded with load_brokers(path): the original keeps ' Active', is_licensed is False and match() excludes the broker; the mutant's DictReader(skipinitialspace=True) drops the space so the broker passes and is returned. Shipped CSVs have no padded fields and no test loads one. Verifier rated it medium; the lead raised it to high for the same reason as gate-25 (the suite treats a non-exact status as not licensed).
- Proposed test: `test_leading_space_status_is_not_trimmed_on_load` in `tests/test_brokers.py`: Write a tmp_path CSV with the fictional_brokers header and the proposed fixture row (license_status ' Active', leading space after the comma), load it, and assert license_status == ' Active' and match(DealRequirement(is_deal_request=True, state='FL'), brokers, date(2026,10,25)).matches == [].
- Proposed fixture (fictional, a proposal only, not added to data/):

  ```csv
  broker_id,name,source,license_state,license_number,license_type,license_status,license_expiration,county,markets,property_types,transaction_types,min_deal_usd,max_deal_usd
  fx-mut-02,Fictional Broker FL-LeadingSpace,fictional,FL,FICT-FL-9002,Broker Individual, Active,2028-06-30,,Tampa,retail,buy,1000000,10000000
  ```

**gate-41** · `parse_date` in `deal_board/cli/build_brokers.py` · round 3

- Mutant: Expiry parsed as %d/%m/%y (day/month swapped), so e.g. an expired 01/12/26 (Jan 12) is written as 2026-12-01 and an expired Active TREC licensee looks unexpired (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)
- Evidence: Builder main() (ROOT pointed at the scratchpad) on a synthetic Active Sales Agent row FICT-TX-9001 with raw expiration '05/11/26': the original reads 2026-05-11 (expired), the mutant swaps day and month to 2026-11-05 and is_licensed on the built row returns True. No test or runtime path calls the builder (only test_imports imports it; data/brokers.csv is prebuilt), but a rebuilt CSV would make expired licensees look licensed.
- Proposed test: `test_parse_date_is_month_first` in `tests/test_build_brokers.py`: parse_date('05/11/26') == date(2026, 5, 11), and is_valid on an Active row with that expiration is False against AS_OF 2026-10-20.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-42** · `parse_date` in `deal_board/cli/build_brokers.py` · round 3

- Mutant: An unreadable/blank expiry defaults to 2099-12-31 instead of None, so a licensee with unknown expiry is written as valid far into the future (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)
- Evidence: Synthetic Active row with an unreadable expiration: the original writes a blank (unknown = not licensed), the mutant writes 2099-12-31 and is_licensed returns True. The builder is never called by tests or at runtime, but a rebuilt CSV would license every unknown-expiry row.
- Proposed test: `test_parse_date_unreadable_is_none` in `tests/test_build_brokers.py`: parse_date('not-a-date') is None and parse_date('') is None, and is_valid on an Active row with an unreadable expiration is False.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-43** · `sample_trec` in `deal_board/cli/build_brokers.py` · round 3

- Mutant: Status mapping: any TREC status ending in 'Active' (e.g. 'Probation - Active') is written as plain 'Active', so probationary licensees pass the exact-'Active' gate (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)
- Evidence: main() on a synthetic raw CSV of 270 fictional rows (160 Active future, 30 Active expired, 60 Inactive, 20 'Probation - Active' future), ROOT redirected to the scratchpad: the original keeps the 6 sampled 'Probation - Active' rows verbatim so is_licensed rejects them; the mutant rewrites them to 'Active', so 156 TX rows pass instead of 150 and restricted licensees look licensed. Nothing calls the builder; tests only check the committed data/brokers.csv.
- Proposed test: `test_build_keeps_compound_status_verbatim` in `tests/test_build_brokers.py`: build_brokers.main on a tmp synthetic raw CSV (ROOT monkeypatched to tmp_path) whose invalid rows are 'Probation - Active' with a future expiration: every such output row keeps license_status == 'Probation - Active' and is_licensed(b, 'TX', AS_OF) is False.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-44** · `sample_trec` in `deal_board/cli/build_brokers.py` · round 3

- Mutant: Expiration written with month and day swapped (%Y-%d-%m), so for day<=12 dates a past expiry can become a future one (2026-03-10 -> 2026-10-03 still past, but 2026-01-12 -> 2026-12-01 future) and an expired licensee looks valid (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)
- Evidence: Synthetic Active row with raw expiration '05/11/26' (expired): the mutant writes '%Y-%d-%m', so the CSV says 2026-11-05, load_brokers reads 5 Nov 2026 and is_licensed returns True, while the builder's own summary still counts 0 valid and hides it. Days over 12 would make the CSV unloadable. No test or runtime path calls the builder.
- Proposed test: `test_built_row_expiration_is_iso` in `tests/test_build_brokers.py`: Building a tiny synthetic raw CSV (N_VALID/N_INVALID patched small) with expiration '05/11/26' gives license_expiration == '2026-05-11', and is_licensed on the loaded Broker is False as of 2026-10-20.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-45** · `sample_trec` in `deal_board/cli/build_brokers.py` · round 3

- Mutant: A missing expiry is written as the snapshot date AS_OF instead of blank, so unknown-expiry licensees pass the gate on any as_of up to 2026-10-20 (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)
- Evidence: Synthetic Active row with a blank expiration: the original writes '' (unknown = not licensed), the mutant fills in AS_OF 2026-10-20 so the row is licensed on the download date. No test or runtime path calls the builder, but a rebuilt CSV would license unknown-expiry rows.
- Proposed test: `test_built_row_unknown_expiration_blank` in `tests/test_build_brokers.py`: Building a synthetic raw CSV with an Active row with a blank expiration gives license_expiration == '', and the loaded Broker has license_expiration None and is_licensed False.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**api-21** · `get_matcher` in `deal_board/api.py` · round 3

- Mutant: ImportError is swallowed and a pass-through matcher is returned that lists every broker ungated (score 0, excluded 0), so a broken deployment returns unlicensed/expired/out-of-state brokers with 200 instead of a 503.
- Evidence: sys.modules['deal_board.matcher']=None to force ImportError, then POST /match with a fake TX-deal extractor, as_of 2026-10-20, brokers fx-demo-01 (Active, expires 2028) and fx-demo-02 (Expired, 2025): the original returns 503, the mutant returns 200 listing both brokers, the expired one included, with no gate. tests/test_api.py never makes the matcher import fail: every test overrides get_matcher with a fake or uses the real matcher, so the except-ImportError branch never runs.
- Proposed test: `test_matcher_import_error_is_503_and_returns_no_brokers` in `tests/test_api.py`: With monkeypatch.setitem(sys.modules, 'deal_board.matcher', None) and get_matcher not overridden, POST /match returns 503 with body exactly {"detail": "matcher unavailable"} and the fake extractor is never called.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-69** · `ClaudeExtractor.extract` in `deal_board/extractors/claude.py` · round 4

- Mutant: When Claude returns a null state, extract infers it from the first standalone two-capital-letter token in the post that is a valid state code (e.g. 'IN', 'OK', 'ME'), so a no-state post gets brokers from a guessed state.
- Evidence: A fake Anthropic client (no network) returns a tool_use with state null for the post 'Fictional client wants a retail buy, call me IN the morning': the original keeps state None and match() excludes the Active IN fictional broker (fail closed); the mutant takes the word 'IN' as the state and returns that broker for a deal whose state was never given. This is on the real POST /match path whenever the Claude extractor is used. Existing fake-client tests never pair a null state with a post containing an uppercase word that is also a state code.
- Proposed test: `test_claude_null_state_is_not_guessed_from_text` in `tests/test_claude_strict.py`: ClaudeExtractor with a fake client returning state null on 'Fictional client wants a retail buy, call me IN the morning' gives requirement.state is None, and match() on it with the proposed fixture (Active, unexpired IN broker) returns no matches and excluded_for_licensing == 1.
- Proposed fixture (fictional, a proposal only, not added to data/):

  ```csv
  broker_id,name,source,license_state,license_number,license_type,license_status,license_expiration,county,markets,property_types,transaction_types,min_deal_usd,max_deal_usd
  fx-mut-69,Fictional Broker IN-Mut,fictional,IN,FICT-IN-9069,Broker Individual,Active,2028-01-31,,,retail,buy,,
  ```

**gate-71** · `RegexExtractor.extract` in `deal_board/extractors/regex_baseline.py` · round 4

- Mutant: Wrapper caches results keyed on the first 40 characters of the post, so two posts with the same opening ('Looking for a broker for my client...') reuse the first post's state and get the wrong state's brokers.
- Evidence: Two posts sharing their first 40 characters ('Looking for a broker for a retail deal: buy a retail center in Dallas, TX' and the same text ending 'in Tulsa, OK') through the app's real cached extractor (api._regex_extractor via override), as_of 2026-10-20: the original reads the second post as OK and returns the OK broker; the mutant reuses the cached TX result and returns fx-demo-01, licensed only in TX, for an Oklahoma deal. Golden tests build a fresh RegexExtractor() per post and no test sends two posts with a shared opening through one instance; in production the lru_cached singleton would leak results between clients.
- Proposed test: `test_regex_extractor_does_not_reuse_results_for_shared_prefix` in `tests/test_api.py`: With get_extractor overridden to the real cached api._regex_extractor and brokers [Active TX fictional, Active OK fictional], two posts sharing their first 40 characters but ending 'Dallas, TX' and 'Tulsa, OK': the second response has requirement.state == 'OK' and only the OK broker's id in matches.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

### Medium (14)

**gate-21** · `get_as_of` in `deal_board/api.py` · round 2

- Mutant: get_as_of uses the UTC calendar date instead of the local date, so for part of each day (US evenings) as_of is tomorrow/yesterday and a license expiring on the boundary day is wrongly accepted or rejected.
- Evidence: Clock frozen by patching api.date/api.datetime with a fixed local zone, POST /match with one fictional Active TX broker. On a US server in the evening the UTC date is already tomorrow, so a still-valid broker is wrongly excluded; on a server east of UTC the mutant's date lags the local one and a broker that expired yesterday is returned. Tests always override get_as_of and never pin local-vs-UTC. Rated medium assuming a US deployment (only wrong exclusions); it would be high on a server east of UTC.
- Proposed test: `test_get_as_of_uses_local_date_not_utc` in `tests/test_api.py`: With TZ=America/Chicago and the clock frozen at 2026-10-26T01:30Z (20:30 local on 2026-10-25), api.get_as_of() returns date(2026, 10, 25), not the UTC date 2026-10-26.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-23** · `get_brokers` in `deal_board/api.py` · round 2

- Mutant: get_brokers pre-filters the cache to Active-status records, so excluded_for_licensing undercounts (non-Active brokers silently vanish from the count).
- Evidence: Real get_brokers() and real matcher, only the extractor (full TX retail-buy deal) and as_of=2026-10-20 overridden: the original loads 226 records and reports excluded_for_licensing=76, the mutant drops the 50 non-'Active' records first and reports 26. Matches are identical (is_licensed rejects them anyway), so it is a wrong count, not a leak. Every API test overrides get_brokers or stubs load_brokers; the one real-data test only checks excluded_for_licensing >= 0.
- Proposed test: `test_real_get_brokers_keeps_every_record_and_counts_exclusions` in `tests/test_api.py`: After api.get_brokers.cache_clear(), len(api.get_brokers()) == len(load_brokers()) and some broker has license_status != 'Active'; a real-matcher POST /match for FULL_TX_DEAL at AS_OF returns excluded_for_licensing == len(load_brokers()) - sum(is_licensed(b, 'TX', AS_OF) for b in load_brokers()).
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-35** · `is_licensed` in `deal_board/matcher.py` · round 2 (verifier rated low; lead changed it, see LOG.md)

- Mutant: State checked by substring (state in license_state), so a deal state '' / 'T' / 'X' matches TX brokers.
- Evidence: With a deal state of '' or a single letter of the broker's code ('T'/'X'), the original refuses an Active TX broker and the mutant licenses it, returning it from match() and POST /match (via a model_construct requirement). DealRequirement validation turns ''/whitespace into None and rejects 'T', and parse_requirement validates every real extractor's output, so only a direct is_licensed call or a model_construct requirement reaches it. Verifier rated it low under the reachability rule; the lead raised it to medium because it is a gate bypass through the public is_licensed(state: str | None) signature, with no test that is_licensed itself rejects partial states.
- Proposed test: `test_is_licensed_rejects_partial_or_empty_state` in `tests/test_matcher.py`: For an Active, unexpired TX broker, is_licensed returns False for state '', 'T', 'X' and 'TXX' and True only for 'TX'; match() on DealRequirement.model_construct(is_deal_request=True, state='T', ...) returns no matches with excluded_for_licensing == 1.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**match-28** · `_transaction_point` in `deal_board/matcher.py` · round 2

- Mutant: Transaction reason drops the transaction type ('+2 handles deals'), so the explanation no longer says which deal type matched.
- Evidence: One synthetic TX broker (fx-demo-01) with transaction_types=[lease] and a TX lease deal: the original reason reads '+2 handles lease deals', the mutant '+2 handles deals'. Score and '+2' prefix are unchanged and the reason goes to clients in POST /match. Tests only parse the leading '+N' (point_amounts/point_total), never the transaction reason text.
- Proposed test: `test_transaction_reason_names_the_transaction_type` in `tests/test_matcher.py`: For a TX broker with transaction_types=[lease] and a lease deal, reasons == ['+2 handles lease deals']; parametrized over every TransactionType, its .value appears in the reason.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**match-30** · `match` in `deal_board/matcher.py` · round 2

- Mutant: Final tie-break uses broker name instead of broker_id, so equal-score ties order by name and are not deterministic for duplicate names.
- Evidence: Two synthetic TX retail brokers with equal score and reason count whose ids and names sort opposite ways: the original ranks by id (fx-demo-01 first), the mutant by name (fx-demo-02 first). In the shipped data names and ids sort the same within every license_state (and only same-state brokers can tie), so POST /match can't show it today; make_broker names brokers 'Fictional Broker {id}', so the existing tie-break test's names sort like its ids.
- Proposed test: `test_tie_break_uses_broker_id_not_name` in `tests/test_matcher.py`: Two equally scored TX retail brokers, make_broker('b-1', name='Fictional Zeta') and make_broker('b-2', name='Fictional Alpha'), come back as ['b-1', 'b-2'] whichever order they are passed in.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-47** · `is_valid` in `deal_board/cli/build_brokers.py` · round 3

- Mutant: is_valid ignores expiry, so the '150 valid' stratum is drawn from all Active rows including expired ones; written data stays verbatim, so the gate still rejects them but the stratified counts are wrong (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)
- Evidence: Same synthetic raw CSV: is_valid ignoring expiry puts expired Active rows into the 'valid' pool, so the output has 131 truly valid rows instead of 150. Statuses and dates stay verbatim, so is_licensed still rejects expired rows; the effect is wrong strata and fewer matchable brokers. Nothing runs the builder.
- Proposed test: `test_build_strata_counts` in `tests/test_build_brokers.py`: build_brokers.main on a tmp synthetic raw CSV (160 valid, 30 Active-expired, 80 other invalid) outputs exactly 150 Active rows with expiration >= AS_OF and exactly 30 Active rows with expiration < AS_OF.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-48** · `main` in `deal_board/cli/build_brokers.py` · round 3

- Mutant: Build writes the first 5 TREC rows twice, so data/brokers.csv has duplicate broker_ids (the matcher may return the same broker twice) (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)
- Evidence: Same synthetic raw CSV: the mutant appends real[:5] again, so the CSV has 205 real rows and duplicate ids tx-001..tx-005 while the printed summary says 200; four duplicates are licensed, so match() could return the same broker twice and counts are off. No unlicensed broker gets through. Nothing runs the builder.
- Proposed test: `test_build_output_has_unique_ids` in `tests/test_build_brokers.py`: build_brokers.main on a tmp synthetic raw CSV (ROOT monkeypatched to tmp_path) writes exactly 200 trec rows plus the fictional rows, with every broker_id unique.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-49** · `sample_trec` in `deal_board/cli/build_brokers.py` · round 3

- Mutant: The PEOPLE filter is dropped, so company licenses (not people) enter the sample and can be matched as brokers (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)
- Evidence: build_brokers.main (ROOT in the scratchpad, N_VALID=2, N_INVALID=2) on a synthetic raw CSV with one 'Broker LLC' company row: the original keeps only Sales Agent / Broker Individual rows, the mutant drops the PEOPLE filter and the company row enters the output. On a rebuild, Active in-date company licenses could reach /match (is_licensed never checks license_type). No runtime path or test runs the builder.
- Proposed test: `test_build_excludes_non_person_license_types` in `tests/test_build_brokers.py`: sample_trec on a synthetic raw CSV in tmp_path that includes a 'Broker LLC' row (N_VALID/N_INVALID monkeypatched small) yields only rows whose license_type is in {'Sales Agent', 'Broker Individual'}.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-50** · `sample_trec` in `deal_board/cli/build_brokers.py` · round 3

- Mutant: Fictional/real distinction lost: TREC rows are written with source 'fictional', so the real sample is mislabeled and real-name/profile checks no longer apply to it (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)
- Evidence: The same synthetic raw CSV built into the scratchpad and loaded: the original labels TREC rows source='trec_2026-10-20', the mutant labels them 'fictional', so a rebuilt CSV would show clients source='fictional' for real license records. The gate doesn't read source. test_brokers would fail on such a rebuilt file but only reads the committed CSV; nothing runs the builder.
- Proposed test: `test_build_labels_trec_rows_with_trec_source` in `tests/test_build_brokers.py`: sample_trec on a synthetic raw CSV in tmp_path gives every TREC output row source == 'trec_2026-10-20' and none 'fictional'.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-53** · `load_brokers` in `deal_board/brokers.py` · round 3

- Mutant: load_brokers dedups by broker_id keeping the last row, so a licensed record shadowed by a later unlicensed duplicate (or vice versa) is silently dropped
- Evidence: Synthetic CSV with two fx-demo-01 rows (TX Active 2028-01-31 and Expired 2025-01-31) in both orders through load_brokers + match(TX, 2026-10-20): the original always returns the Active record with excluded=1; the mutant keeps only the last row per id, so it either drops the licensed broker or reports excluded=0. It never returns an unlicensed broker. test_compliance's duplicate-id test builds its pool in memory with model_copy and never goes through load_brokers.
- Proposed test: `test_load_brokers_keeps_rows_sharing_a_broker_id` in `tests/test_brokers.py`: A tmp_path CSV with the proposed fixture row plus an Active in-date twin with the same broker_id, in both orders: load_brokers returns 2 records and match(state='FL') returns only the Active record with excluded_for_licensing == 1.
- Proposed fixture (fictional, a proposal only, not added to data/):

  ```csv
  broker_id,name,source,license_state,license_number,license_type,license_status,license_expiration,county,markets,property_types,transaction_types,min_deal_usd,max_deal_usd
  fx-mut-03,Fictional Broker FL-Twin,fictional,FL,FICT-FL-9003,Broker Individual,Expired,2025-01-31,,Tampa,retail,buy,1000000,10000000
  ```

**match-41** · `TX_CITY_TO_COUNTY` in `deal_board/brokers.py` · round 3

- Mutant: Fort Worth mapped to Dallas County instead of Tarrant, so Fort Worth deals give the county +2 to Dallas County brokers and not to Tarrant ones.
- Evidence: A TX Fort Worth deal through match() with two licensed fictional brokers without markets, one in Tarrant and one in Dallas county: the original gives the Tarrant broker +2 and ranks it first; the mutant gives the +2 to the Dallas broker, flipping the ranking with a wrong-county reason. test_tx_county_fallback_scores_2 derives its cases from TX_CITY_TO_COUNTY itself, so a mutated table checks against itself.
- Proposed test: `test_fort_worth_maps_to_tarrant_county` in `tests/test_matcher.py`: TX_CITY_TO_COUNTY['fort worth'] == 'Tarrant', and a TX 'Fort Worth' deal scores +2 ('+2 based in Tarrant County, which covers Fort Worth') for a Tarrant broker and 0 for a Dallas broker.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**match-43** · `TX_CITY_TO_COUNTY` in `deal_board/brokers.py` · round 3

- Mutant: Non-TX city 'oklahoma city' -> 'Tarrant' added to the Texas map, so a TX deal mis-stating city 'Oklahoma City' gives Tarrant brokers a county +2.
- Evidence: A deal with state TX and city 'Oklahoma City' (the extractor can produce this pair; nothing checks the city belongs to the state) against a licensed fictional Tarrant broker: the original scores 0, the mutant awards a false +2. The county test is parametrized from the table itself and no test pins the table contents.
- Proposed test: `test_tx_city_to_county_table_is_exact` in `tests/test_matcher.py`: TX_CITY_TO_COUNTY equals a hard-coded dict of the 8 expected cities and counties, and a TX 'Oklahoma City' deal gives a Tarrant broker no market point.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**api-28** · `get_matcher` in `deal_board/api.py` · round 3

- Mutant: get_matcher catches every Exception instead of ImportError, so a matcher bug raised at import time is masked as a 503 'matcher unavailable'.
- Evidence: sys.modules['deal_board.matcher'] replaced by an object whose attribute access raises RuntimeError: the original lets the non-ImportError through as a 500, as get_matcher's docstring requires; the mutant hides the bug as a 503. No broker is returned either way, so it is a wrong status code only; no test makes the import fail at all.
- Proposed test: `test_matcher_non_import_error_stays_500` in `tests/test_api.py`: With sys.modules['deal_board.matcher'] patched to an object whose __getattr__ raises RuntimeError and TestClient(raise_server_exceptions=False), POST /match returns 500, not 503.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**api-44** · `BodySizeLimit.__call__` in `deal_board/api.py` · round 4

- Mutant: Off-by-one: streaming check uses >=, so a body of exactly max_bytes is rejected with 413.
- Evidence: The real ASGI app called directly with a valid JSON body of exactly MAX_BODY_BYTES (65536) bytes, one chunk, correct Content-Length, extractor and matcher faked: the original returns 200, the mutant 413, so the cap is one byte lower than documented and a valid request is refused. Both 413 tests use bodies well over the limit; none sends exactly the limit. Reaching 64 KiB needs whitespace padding beyond any 4000-char post, so it is an edge case, but it is a client-visible validation boundary.
- Proposed test: `test_body_of_exactly_max_bytes_is_accepted` in `tests/test_api.py`: A JSON body padded with whitespace to exactly api.MAX_BODY_BYTES bytes with a matching Content-Length returns 200 and the extractor is called once; MAX_BODY_BYTES + 1 bytes returns 413.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

### Low (18)

**match-11** · `match` in `deal_board/matcher.py` · round 1

- Mutant: Negative limit slices scored[:limit], returning all but the last |limit| brokers instead of none.
- Evidence: TX deal, three licensed fictional brokers fx-demo-01..03, match(..., limit=-1): original returns [] (limit clamped to 0), mutant returns ['fx-demo-01','fx-demo-02'] (Python drops-from-the-end slicing). Only licensed brokers come back either way and api.py never passes a limit; tests use limits 1, 2, 3, 5, 10, 50, len(pool) but never 0 or negative.
- Proposed test: `test_negative_limit_returns_no_matches` in `tests/test_matcher.py`: With 3 licensed TX brokers from make_broker, match(make_req(state='TX'), brokers, AS_OF, limit=-1).matches == [] and excluded_for_licensing == 0.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**api-02** · `match_post` in `deal_board/api.py` · round 1

- Mutant: BrokerMatch built with model_construct, skipping field validation so a Sequence/tuple reasons or wrong-typed fields from a fake matcher pass through unvalidated (observable via direct match_post call returning non-list reasons).
- Evidence: A deliberately wrong-typed fake matcher returning score='87' and reasons=('licensed in TX',) for fx-demo-01: the original coerces to int 87 and a list; the mutant's model_construct skips validation, so the HTTP body carries "score":"87" as a string. With the real matcher (pydantic Match objects) the output is identical, which is why no test sees it.
- Proposed test: `test_match_post_validates_matcher_output_types` in `tests/test_api.py`: With a fake matcher returning SimpleNamespace matches (score='87', reasons=('r',)), POST /match returns matches[0]['score'] == 87 as an int; with score='not-a-number' a direct api.match_post call raises pydantic.ValidationError.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**api-07** · `match_post` in `deal_board/api.py` · round 1

- Mutant: API sorts the shared cached get_brokers() list in place by name before matching, mutating state across requests.
- Evidence: POST /match via TestClient with get_brokers overridden to one shared list [fx-demo-01 'Zeta Fictional', fx-demo-02 'Alpha Fictional'], both Active TX: responses identical (match() re-sorts by score/rules/broker_id) but after the mutant's request the shared cached list is reordered to [fx-demo-02, fx-demo-01], breaking get_brokers' read-only contract. No test snapshots the broker list around a request. Not shown but plausible: list.sort empties the list while the Python key runs, so a concurrent request could see zero brokers (fails closed: no matches).
- Proposed test: `test_match_does_not_mutate_shared_broker_list` in `tests/test_api.py`: Override get_brokers with one fixed list of fictional brokers whose names are not in broker_id order, POST /match with the real matcher, then assert the list has the same broker_ids in the same order and length as a snapshot taken before the request.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-26** · `Broker.license_state` in `deal_board/brokers.py` · round 2

- Mutant: Broker.license_state widened from the State Literal to str, so malformed broker states ('tx', 'Texas', 'TX ') load without error instead of being rejected at load time.
- Evidence: A fictional CSV row with license_state 'fl': the original rejects it at load time (State Literal), the mutant loads it as a plain str. Through match() the broker is still excluded because DealRequirement.state is always a valid State; only a direct is_licensed(b, 'fl', ...) returns True. Low: POST /match and match() can never produce the matching state; the visible effect is a malformed file loading silently and inflating excluded_for_licensing.
- Proposed test: `test_invalid_license_state_is_rejected_at_load` in `tests/test_brokers.py`: Write a tmp_path CSV with the fictional_brokers header and one row whose license_state is 'fl', and assert load_brokers(path) raises pydantic.ValidationError.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-31** · `ExtractedFields._upper_state` in `deal_board/schema.py` · round 2

- Mutant: Blank/whitespace state is no longer turned into None, so state '' or '  ' raises a validation error instead of becoming 'missing state'.
- Evidence: parse_requirement({'is_deal_request': True, 'state': ''}) (or '   '): the original treats a blank state as missing (state None, 'state' in missing_fields), the mutant raises ExtractionError, which POST /match turns into a 422 instead of a no-state result. Claude's strict schema only allows a State value or null and the regex extractor never emits '', so real entry points can't produce it; no test validates a blank state.
- Proposed test: `test_blank_state_becomes_none` in `tests/test_matcher.py`: DealRequirement.model_validate({'is_deal_request': True, 'state': s}) for s in ('', '   ') succeeds with state None and 'state' in missing_fields.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-34** · `ExtractedFields.is_deal_request` in `deal_board/schema.py` · round 2

- Mutant: is_deal_request gets a default of True, so a payload missing the flag is treated as a deal request instead of failing validation (and the Claude tool schema no longer requires it).
- Evidence: parse_requirement({'state': 'TX'}) with no is_deal_request: the original raises ExtractionError (422), the mutant silently treats the post as a deal request. Claude's INPUT_SCHEMA is unchanged (_strict_schema drops defaults and marks every property required), so real strict-mode output always carries the flag; only raw input real extractors never produce shows it.
- Proposed test: `test_missing_is_deal_request_is_rejected` in `tests/test_claude_strict.py`: parse_requirement({'state': 'TX'}) raises ExtractionError, and 'is_deal_request' is in ExtractedFields.model_json_schema()['required'] with no default.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**match-32** · `match` in `deal_board/matcher.py` · round 2

- Mutant: Limit floor is 1 instead of 0, so limit=0 (or negative) still returns the top broker.
- Evidence: Two licensed synthetic TX brokers, match(..., limit=0) and limit=-1: original [] , mutant one (licensed) broker, breaking 'at most limit'. POST /match never passes a limit (default 5), so only a direct call shows it; the smallest limit any test uses is 1.
- Proposed test: `test_limit_zero_or_negative_returns_no_matches` in `tests/test_matcher.py`: With three licensed TX brokers, match(req, brokers, AS_OF, limit=0).matches == [] and limit=-1 also gives [], with excluded_for_licensing == 0.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**api-11** · `MatchResponse` in `deal_board/api.py` · round 2

- Mutant: Field(ge=0) removed from excluded_for_licensing, so a negative count from a buggy matcher is returned instead of failing validation.
- Evidence: An overridden get_matcher returning excluded_for_licensing=-1: the original fails MatchResponse validation (500), the mutant returns 200 with the negative count. The real matcher's len(brokers) - len(licensed) is never negative, so only a fake matcher or a direct MatchResponse(...) shows it; no test feeds a negative count.
- Proposed test: `test_match_response_rejects_negative_excluded_count` in `tests/test_api.py`: MatchResponse(extractor='regex', requirement=FULL_TX_DEAL, matches=[], excluded_for_licensing=-1, follow_up_question=None) raises pydantic.ValidationError; with a fake matcher returning excluded=-1, POST /match via TestClient(raise_server_exceptions=False) does not return 200.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-46** · `sample_trec` in `deal_board/cli/build_brokers.py` · round 3

- Mutant: The 'Active but expired' stratum is emptied, so the hardest case for a status-only filter disappears from data/brokers.csv (no leak, but the test data stops exercising it) (build CLI: no runtime or test path executes it; effect is on a rebuilt data/brokers.csv)
- Evidence: Same synthetic raw CSV: the original keeps all 30 Active-but-expired rows in the invalid stratum, the mutant keeps none. The licensed set is unchanged (150) so clients see nothing, but a rebuilt CSV loses the hardest case for a status-only filter (test_sample_is_stratified would then fail). Nothing runs the builder.
- Proposed test: `test_build_keeps_active_but_expired_stratum` in `tests/test_build_brokers.py`: build_brokers.main on a tmp synthetic raw CSV with 30 Active-but-expired rows outputs exactly 30 rows with license_status 'Active' and license_expiration < AS_OF.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-55** · `Broker.is_fictional` in `deal_board/brokers.py` · round 3

- Mutant: is_fictional checks 'trec' not in source, so any row whose source is mislabeled or blank counts as fictional and escapes the real-data checks
- Evidence: A Broker built with sources 'fictional', 'trec_2026-10-20', 'TREC_2026-10-20', 'manual_2026-10-20': the original is True only for exactly 'fictional', the mutant for any source without lowercase 'trec'. is_fictional is used only in tests, and every committed row is exactly 'fictional' or 'trec_...', so no test sees it.
- Proposed test: `test_is_fictional_requires_exact_fictional_source` in `tests/test_brokers.py`: A Broker with source 'manual_2026-10-20' or 'TREC_2026-10-20' has is_fictional False; only source == 'fictional' gives True.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**match-44** · `_market_point` in `deal_board/matcher.py` · round 3

- Mutant: Broker markets are lower-cased but not stripped, so a market stored as ' Austin' or 'Austin ' no longer matches an Austin deal.
- Evidence: Broker._split_list strips markets only when they arrive as a ';'-joined string (the CSV path); a list is kept as passed, so Broker(markets=[' Austin ']) keeps the spaces. For a TX Austin deal the original still scores '+2 serves Austin', the mutant 0. Every real path loads from CSV, so only directly built brokers show it.
- Proposed test: `test_market_match_ignores_whitespace_in_list_markets` in `tests/test_matcher.py`: make_broker(markets=[' Austin ']) scores 2 with reason '+2 serves Austin' for a TX deal with city 'Austin'.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**match-45** · `_market_point` in `deal_board/matcher.py` · round 3

- Mutant: Deal city is lower-cased but not stripped, so ' Austin ' misses both the market match and the county fallback.
- Evidence: Validation strips city on every validated path (constructor, model_validate, extractor/API output), so there the two agree. A DealRequirement.model_construct with city '  Austin ' differs: the original still awards the market +2, the mutant 0. Tests always build validated requirements.
- Proposed test: `test_market_point_strips_unvalidated_city` in `tests/test_matcher.py`: DealRequirement.model_construct(is_deal_request=True, state='TX', city='  Austin ', missing_fields=[]) against make_broker(markets=['Austin']) scores 2 from the market rule.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**match-46** · `_score` in `deal_board/matcher.py` · round 3

- Mutant: Sales Agent note is inserted first instead of appended, so it comes before the point reasons.
- Evidence: match() on a TX retail deal with an Active retail Sales Agent fx-demo-01: the original appends the referral note after the point reasons, the mutant puts it first, and that order reaches clients in BrokerMatch.reasons. Tests only check membership, note count and point totals, never position.
- Proposed test: `test_sales_agent_note_follows_point_reasons` in `tests/test_matcher.py`: For a retail Sales Agent matched to a TX retail deal, m.reasons == ['+3 specializes in retail', SALES_AGENT_NOTE] exactly.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**match-48** · `Match` in `deal_board/matcher.py` · round 3

- Mutant: Match.score typed float, so scores serialize/compare as floats (3.0) and the int contract is loosened.
- Evidence: TX retail deal, one Active retail broker: a direct match() returns score 3 (int) vs 3.0 (float) and model_dump_json shows "score":3.0; over HTTP BrokerMatch.score: int coerces it back to 3. Tests compare with == and 3.0 == 3.
- Proposed test: `test_match_score_is_int` in `tests/test_matcher.py`: For a matched broker, type(m.score) is int and m.model_dump_json() contains '"score":3' and not '3.0'.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**gate-63** · `ExtractedFields._upper_state` in `deal_board/schema.py` · round 4

- Mutant: State validator keeps only the last token of the value, so a multi-state or prose answer like 'TX or OK' / 'near KS' is silently repaired to one state instead of being rejected, routing the deal to that state's brokers.
- Evidence: DealRequirement(state='TX or OK') or state='near KS' raises ValidationError in the original; the mutant keeps the last word and returns OK / KS, turning an unclear state into a definite one. Neither real extractor can send a multi-word state (the strict Claude tool only allows 2-letter enum codes or null; the frozen regex extractor only returns get_args(State) codes or None), so only a direct schema call shows it.
- Proposed test: `test_state_validator_rejects_multi_word_state` in `tests/test_compliance.py`: DealRequirement(is_deal_request=True, state='TX or OK') and state='near KS' each raise pydantic.ValidationError, while state=' tx ' still gives 'TX'.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**api-45** · `BodySizeLimit.__call__` in `deal_board/api.py` · round 4

- Mutant: Non-numeric Content-Length is no longer rejected, so int() raises inside the middleware and the client gets a 500 instead of 413.
- Evidence: ASGI call with header Content-Length: abc: the original rejects it with 413 and a fixed detail; in the mutant int(b'abc') raises inside the middleware and the client gets a plain-text 500. Under the project's uvicorn 0.54 + h11 0.16, h11 rejects a non-numeric Content-Length before the app sees it, and TestClient/httpx always send a numeric length, so a real client can't reach it.
- Proposed test: `test_non_numeric_content_length_is_rejected_not_500` in `tests/test_api.py`: Calling the BodySizeLimit-wrapped app over ASGI with header (b'content-length', b'abc') returns 413 with body {"detail": "request body too large"} and no exception propagates.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**api-49** · `_post_id` in `deal_board/api.py` · round 4

- Mutant: Post-id hash truncated to 4 hex chars, so distinct posts collide in logs and correlation breaks.
- Evidence: One fictional post via TestClient with a handler on deal_board.api's logger: the original logs a 12-hex post id, the mutant a 4-hex one (65,536 values), so ids start colliding after a few hundred posts. Nothing reaches clients and no post text leaks; tests only check the post text is absent from logs.
- Proposed test: `test_logged_post_id_is_12_hex_sha256_prefix` in `tests/test_api.py`: After a successful POST /match with caplog at INFO, the 'matched' line contains post=<sha256(POST.strip().encode()).hexdigest()[:12]> and the post= value matches [0-9a-f]{12}.
- Proposed fixture: none needed (the test builds synthetic brokers in code)

**api-56** · `BodySizeLimit.__call__` in `deal_board/api.py` · round 4

- Mutant: Body is replayed without deferring to the real receive, so after the body the app sees an immediate disconnect instead of waiting on the client.
- Evidence: Through TestClient every response is identical (the only route is a sync def returning JSON, nothing calls receive() after the body). Calling BodySizeLimit directly with a probe app that calls receive() a second time while the client is connected: the original keeps waiting on the real receive, the mutant immediately returns a fake http.disconnect, which would break disconnect detection for a future streaming route. No test drives BodySizeLimit/_replay directly.
- Proposed test: `test_body_size_limit_defers_to_real_receive_after_body` in `tests/test_api.py`: A probe ASGI app wrapped in BodySizeLimit, driven by a hand-written receive that sends one body message then blocks: the probe's second receive() does not return within 0.2s (no synthetic http.disconnect).
- Proposed fixture: none needed (the test builds synthetic brokers in code)

## All mutants

| id | round | target | status | killed_by | severity |
|---|---|---|---|---|---|
| gate-00 | 1 | `is_licensed` (matcher.py) | killed | 389 |  |
| gate-01 | 1 | `is_licensed` (matcher.py) | killed | 52 |  |
| gate-02 | 1 | `is_licensed` (matcher.py) | killed | 13 |  |
| gate-03 | 1 | `is_licensed` (matcher.py) | killed | 2 |  |
| gate-04 | 1 | `is_licensed` (matcher.py) | killed | 1 |  |
| gate-05 | 1 | `is_licensed` (matcher.py) | killed | 52 |  |
| gate-06 | 1 | `is_licensed` (matcher.py) | killed | 42 |  |
| gate-07 | 1 | `is_licensed` (matcher.py) | killed | 32 |  |
| gate-08 | 1 | `is_licensed` (matcher.py) | killed | 47 |  |
| gate-09 | 1 | `is_licensed` (matcher.py) | killed | 15 |  |
| gate-10 | 1 | `is_licensed` (matcher.py) | killed | 40 |  |
| gate-11 | 1 | `is_licensed` (matcher.py) | killed | 27 |  |
| gate-12 | 1 | `is_licensed` (matcher.py) | killed | 40 |  |
| gate-13 | 1 | `is_licensed` (matcher.py) | killed | 40 |  |
| gate-14 | 1 | `is_licensed` (matcher.py) | killed | 97 |  |
| gate-15 | 1 | `is_licensed` (matcher.py) | killed | 224 |  |
| gate-16 | 1 | `is_licensed` (matcher.py) | killed | 57 |  |
| gate-17 | 1 | `Broker._blank_to_none` (brokers.py) | killed | 5 |  |
| gate-18 | 1 | `is_licensed` (matcher.py) | killed | 13 |  |
| match-01 | 1 | `match` (matcher.py) | killed | 53 |  |
| match-02 | 1 | `match` (matcher.py) | killed | 123 |  |
| match-03 | 1 | `match` (matcher.py) | killed | 135 |  |
| match-04 | 1 | `match` (matcher.py) | killed | 122 |  |
| match-05 | 1 | `_score` (matcher.py) | killed | 3 |  |
| match-06 | 1 | `match` (matcher.py) | killed | 1 |  |
| match-07 | 1 | `match` (matcher.py) | killed | 16 |  |
| match-08 | 1 | `match` (matcher.py) | killed | 13 |  |
| match-09 | 1 | `match` (matcher.py) | killed | 2 |  |
| match-10 | 1 | `match` (matcher.py) | killed | 15 |  |
| match-11 | 1 | `match` (matcher.py) | survived | 0 | low |
| match-12 | 1 | `match` (matcher.py) | killed | 15 |  |
| match-13 | 1 | `match` (matcher.py) | killed | 15 |  |
| match-14 | 1 | `match` (matcher.py) | killed | 17 |  |
| match-15 | 1 | `match` (matcher.py) | killed | 1 |  |
| match-16 | 1 | `match` (matcher.py) | killed | 124 |  |
| match-17 | 1 | `match` (matcher.py) | killed | 3 |  |
| match-18 | 1 | `_score` (matcher.py) | killed | 2 |  |
| match-19 | 1 | `_score` (matcher.py) | killed | 4 |  |
| match-20 | 1 | `match` (matcher.py) | killed | 114 |  |
| api-01 | 1 | `match_post` (api.py) | killed | 82 |  |
| api-02 | 1 | `match_post` (api.py) | survived | 0 | low |
| api-03 | 1 | `match_post` (api.py) | killed | 13 |  |
| api-04 | 1 | `match_post` (api.py) | killed | 1 |  |
| api-05 | 1 | `match_post` (api.py) | killed | 36 |  |
| api-06 | 1 | `match_post` (api.py) | killed | 81 |  |
| api-07 | 1 | `match_post` (api.py) | survived | 0 | low |
| api-08 | 1 | `match_post` (api.py) | killed | 21 |  |
| api-09 | 1 | `match_post` (api.py) | killed | 36 |  |
| api-10 | 1 | `get_as_of` (api.py) | survived | 0 | high |
| gate-20 | 2 | `get_as_of` (api.py) | survived | 0 | high |
| gate-21 | 2 | `get_as_of` (api.py) | survived | 0 | medium |
| gate-22 | 2 | `get_as_of` (api.py) | survived | 0 | high |
| gate-23 | 2 | `get_brokers` (api.py) | survived | 0 | medium |
| gate-24 | 2 | `match_post` (api.py) | killed | 38 |  |
| gate-25 | 2 | `Broker._blank_to_none` (brokers.py) | survived | 0 | high |
| gate-26 | 2 | `Broker.license_state` (brokers.py) | survived | 0 | low |
| gate-27 | 2 | `Broker._split_list` (brokers.py) | killed | 14 |  |
| gate-28 | 2 | `load_brokers` (brokers.py) | survived | 0 | high |
| gate-29 | 2 | `ExtractedFields._upper_state` (schema.py) | killed | 34 |  |
| gate-30 | 2 | `ExtractedFields._upper_state` (schema.py) | killed | 2 |  |
| gate-31 | 2 | `ExtractedFields._upper_state` (schema.py) | survived | 0 | low |
| gate-32 | 2 | `ExtractedFields.state` (schema.py) | killed | 13 |  |
| gate-33 | 2 | `DealRequirement._compute_missing` (schema.py) | killed | 12 |  |
| gate-34 | 2 | `ExtractedFields.is_deal_request` (schema.py) | survived | 0 | low |
| gate-35 | 2 | `is_licensed` (matcher.py) | survived | 0 | medium |
| gate-36 | 2 | `is_licensed` (matcher.py) | killed | 2 |  |
| gate-37 | 2 | `is_licensed` (matcher.py) | killed | 123 |  |
| gate-38 | 2 | `match_post` (api.py) | killed | 5 |  |
| match-22 | 2 | `_market_point` (matcher.py) | killed | 1 |  |
| match-23 | 2 | `_market_point` (matcher.py) | killed | 2 |  |
| match-24 | 2 | `_market_point` (matcher.py) | killed | 4 |  |
| match-25 | 2 | `_deal_size_point` (matcher.py) | killed | 2 |  |
| match-26 | 2 | `_deal_size_point` (matcher.py) | killed | 6 |  |
| match-27 | 2 | `_property_point` (matcher.py) | killed | 7 |  |
| match-28 | 2 | `_transaction_point` (matcher.py) | survived | 0 | medium |
| match-29 | 2 | `_score` (matcher.py) | killed | 2 |  |
| match-30 | 2 | `match` (matcher.py) | survived | 0 | medium |
| match-31 | 2 | `match` (matcher.py) | killed | 1 |  |
| match-32 | 2 | `match` (matcher.py) | survived | 0 | low |
| match-33 | 2 | `match` (matcher.py) | killed | 13 |  |
| match-34 | 2 | `_score` (matcher.py) | killed | 24 |  |
| api-11 | 2 | `MatchResponse` (api.py) | survived | 0 | low |
| api-12 | 2 | `match_post` (api.py) | killed | 5 |  |
| api-13 | 2 | `match_post` (api.py) | equivalent | 0 |  |
| api-14 | 2 | `match_post` (api.py) | killed | 12 |  |
| api-15 | 2 | `match_post` (api.py) | killed | 1 |  |
| api-16 | 2 | `match_post` (api.py) | killed | 1 |  |
| api-17 | 2 | `match_post` (api.py) | killed | 1 |  |
| gate-41 | 3 | `parse_date` (cli/build_brokers.py) | survived | 0 | high |
| gate-42 | 3 | `parse_date` (cli/build_brokers.py) | survived | 0 | high |
| gate-43 | 3 | `sample_trec` (cli/build_brokers.py) | survived | 0 | high |
| gate-44 | 3 | `sample_trec` (cli/build_brokers.py) | survived | 0 | high |
| gate-45 | 3 | `sample_trec` (cli/build_brokers.py) | survived | 0 | high |
| gate-46 | 3 | `sample_trec` (cli/build_brokers.py) | survived | 0 | low |
| gate-47 | 3 | `is_valid` (cli/build_brokers.py) | survived | 0 | medium |
| gate-48 | 3 | `main` (cli/build_brokers.py) | survived | 0 | medium |
| gate-49 | 3 | `sample_trec` (cli/build_brokers.py) | survived | 0 | medium |
| gate-50 | 3 | `sample_trec` (cli/build_brokers.py) | survived | 0 | medium |
| gate-51 | 3 | `Broker.license_expiration` (brokers.py) | killed | 141 |  |
| gate-52 | 3 | `Broker.license_expiration` (brokers.py) | killed | 1 |  |
| gate-53 | 3 | `load_brokers` (brokers.py) | survived | 0 | medium |
| gate-54 | 3 | `load_brokers` (brokers.py) | killed | 1 |  |
| gate-55 | 3 | `Broker.is_fictional` (brokers.py) | survived | 0 | low |
| gate-56 | 3 | `BROKERS_CSV` (brokers.py) | killed | 6 |  |
| gate-57 | 3 | `DATA_DIR` (paths.py) | killed | 4 |  |
| match-41 | 3 | `TX_CITY_TO_COUNTY` (brokers.py) | survived | 0 | medium |
| match-42 | 3 | `TX_CITY_TO_COUNTY` (brokers.py) | killed | 1 |  |
| match-43 | 3 | `TX_CITY_TO_COUNTY` (brokers.py) | survived | 0 | medium |
| match-44 | 3 | `_market_point` (matcher.py) | survived | 0 | low |
| match-45 | 3 | `_market_point` (matcher.py) | survived | 0 | low |
| match-46 | 3 | `_score` (matcher.py) | survived | 0 | low |
| match-47 | 3 | `_score` (matcher.py) | killed | 1 |  |
| match-48 | 3 | `Match` (matcher.py) | survived | 0 | low |
| match-49 | 3 | `Match` (matcher.py) | killed | 1 |  |
| match-50 | 3 | `match` (matcher.py) | killed | 17 |  |
| api-21 | 3 | `get_matcher` (api.py) | survived | 0 | high |
| api-22 | 3 | `match_post` (api.py) | killed | 15 |  |
| api-23 | 3 | `match_post` (api.py) | killed | 3 |  |
| api-24 | 3 | `BrokerMatch` (api.py) | killed | 37 |  |
| api-25 | 3 | `match_post` (api.py) | killed | 21 |  |
| api-26 | 3 | `match_post` (api.py) | killed | 2 |  |
| api-27 | 3 | `match_post` (api.py) | killed | 81 |  |
| api-28 | 3 | `get_matcher` (api.py) | survived | 0 | medium |
| api-30 | 3 | `match_post` (api.py) | killed | 1 |  |
| gate-60 | 4 | `ExtractedFields._upper_state` (schema.py) | killed | 1 |  |
| gate-61 | 4 | `State` (schema.py) | killed | 4 |  |
| gate-62 | 4 | `KEY_FIELDS` (schema.py) | killed | 4 |  |
| gate-63 | 4 | `ExtractedFields._upper_state` (schema.py) | survived | 0 | low |
| gate-64 | 4 | `parse_requirement` (extractors/__init__.py) | killed | 1 |  |
| gate-65 | 4 | `parse_requirement` (extractors/__init__.py) | killed | 5 |  |
| gate-66 | 4 | `parse_requirement` (extractors/__init__.py) | killed | 34 |  |
| gate-67 | 4 | `INPUT_SCHEMA` (extractors/claude.py) | killed | 2 |  |
| gate-68 | 4 | `TOOL` (extractors/claude.py) | killed | 3 |  |
| gate-69 | 4 | `ClaudeExtractor.extract` (extractors/claude.py) | survived | 0 | high |
| gate-70 | 4 | `_strict_schema` (extractors/claude.py) | killed | 3 |  |
| gate-71 | 4 | `RegexExtractor.extract` (extractors/regex_baseline.py) | survived | 0 | high |
| gate-72 | 4 | `RegexExtractor.extract` (extractors/regex_baseline.py) | killed | 9 |  |
| gate-73 | 4 | `Settings.use_claude` (config.py) | killed | 2 |  |
| api-41 | 4 | `MatchRequest` (api.py) | killed | 1 |  |
| api-42 | 4 | `MatchRequest` (api.py) | killed | 2 |  |
| api-43 | 4 | `BodySizeLimit.__call__` (api.py) | killed | 1 |  |
| api-44 | 4 | `BodySizeLimit.__call__` (api.py) | survived | 0 | medium |
| api-45 | 4 | `BodySizeLimit.__call__` (api.py) | survived | 0 | low |
| api-46 | 4 | `_validation_error` (api.py) | killed | 5 |  |
| api-47 | 4 | `match_post` (api.py) | killed | 1 |  |
| api-48 | 4 | `match_post` (api.py) | killed | 1 |  |
| api-49 | 4 | `_post_id` (api.py) | survived | 0 | low |
| api-50 | 4 | `match_post` (api.py) | killed | 1 |  |
| api-51 | 4 | `match_post` (api.py) | killed | 15 |  |
| api-52 | 4 | `match_post` (api.py) | killed | 3 |  |
| api-53 | 4 | `match_post` (api.py) | killed | 3 |  |
| api-54 | 4 | `match_post` (api.py) | killed | 17 |  |
| api-55 | 4 | `match_post` (api.py) | killed | 1 |  |
| api-56 | 4 | `BodySizeLimit.__call__` (api.py) | survived | 0 | low |
| match-61 | 4 | `PRIORITY` (followup.py) | killed | 4 |  |
| match-62 | 4 | `follow_up_question` (followup.py) | killed | 12 |  |
| match-63 | 4 | `follow_up_question` (followup.py) | killed | 10 |  |
| match-64 | 4 | `_QUESTIONS` (followup.py) | killed | 2 |  |
| match-65 | 4 | `_where` (followup.py) | killed | 6 |  |
| match-66 | 4 | `_PROPERTY_LABELS` (followup.py) | killed | 1 |  |

Equivalent (excluded from the counts):

- **api-13**: Equivalent: building MatchResponse re-runs DealRequirement's after-validator _compute_missing on the passed instance (pydantic 2.14.1), so missing_fields is recomputed from the unchanged other fields; follow_up_question uses the original object in both versions. No client-visible difference on POST /match or a direct match_post call.

Totals: 160 mutants, 114 killed, 45 survived, 1 equivalent.
