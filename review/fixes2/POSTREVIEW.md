# Post-fix mutation round

A fresh reviewer (`post-reviewer`, which had not seen the fixes) wrote **39 new mutants** against the
fixed code, confined to the six files the 27 survivors lived in (`api.py`, `brokers.py`,
`cli/build_brokers.py`, `matcher.py`, `extractors/claude.py`, `extractors/regex_baseline.py`; never the
frozen baseline). Each one is verified real: `verifier_note` in `postreview.json` gives an input whose
output differs between the original and the mutant. Severity: 9 high, 23 medium, 7 low.

Both runs used `review/fixes2/rerun.py post` (each diff applied to a scratchpad copy filled from a
`git archive` of HEAD, full suite, unpatched copy green first).

## Round 1: suite at `b94081a` (before the round's tests)

`postreview_rerun_round1.json`: 26/39 killed. 13 survived: post-04, post-09, post-18, post-19, post-20, post-21, post-22, post-26, post-29, post-31, post-35, post-38, post-39.
High/medium among them: post-04 (high: a source like `fictional-trec_2019-01-01` skipped the
snapshot-freshness gate via `startswith`), post-09, post-18, post-19, post-20, post-21, post-26, post-29,
post-38. None was a live bug in shipped code; all were missing tests.

new_high_medium_survivors: 9

A blind test writer, who saw only each survivor's one-line description, wrote
`tests/test_postfix_mutants.py` (16 tests, commit `028d84f`), lows included.

## Round 2: suite at `a6ad069` (with those tests)

`postreview_rerun.json`: **39/39 killed**, unpatched copy 1514 passed / 1 skipped.

new_high_medium_survivors: 0

| id | severity | file | failing tests, round 1 | failing tests, round 2 | description |
|---|---|---|---|---|---|
| post-01 | high | `deal_board/matcher.py` | 2 | 2 | Snapshot source matched with re.search instead of fullmatch, so any source merely containing 'trec_<date>' (e.g. 'trec_2026-10-20_revoked', 'old_trec_2026-10-20') is read as a fresh snapshot and passes the allow-list. |
| post-02 | high | `deal_board/matcher.py` | 2 | 2 | Lower bound of the snapshot age check dropped, so a snapshot dated AFTER as_of (says nothing about the license on that earlier day) is accepted. |
| post-03 | high | `deal_board/matcher.py` | 1 | 1 | An impossible snapshot date (e.g. trec_2026-02-30) fails open: the ValueError branch returns True, so a record whose age can't be read is vouched for forever. |
| post-04 | high | `deal_board/matcher.py` | 0 | 3 | Fictional check uses startswith('fictional'), so any source beginning with 'fictional' ('fictional_import', 'fictional-trec_2019-01-01') skips the snapshot-freshness gate entirely. |
| post-05 | high | `deal_board/matcher.py` | 4 | 4 | MAX_SNAPSHOT_AGE_DAYS raised from 90 to 365, so a TREC status snapshot up to a year stale (missing suspensions/revocations) is still vouched for. |
| post-06 | medium | `deal_board/matcher.py` | 1 | 1 | Broker markets are cut at the first comma before keying, so a NY broker listing 'Hoboken, NJ' gets the +2 local-fit point on a NY deal in Hoboken (other-state market counted as local). |
| post-07 | medium | `deal_board/matcher.py` | 136 | 137 | excluded_for_licensing counts only in-state brokers that failed the gate, so out-of-state brokers are silently dropped from the excluded count. |
| post-08 | high | `deal_board/matcher.py` | 1 | 1 | Sales Agent referral-fee compliance note only added when the agent earned at least one point, so a zero-fit licensed Sales Agent is returned without the 'paid through sponsoring broker' note. |
| post-09 | medium | `deal_board/matcher.py` | 0 | 1 | limit=0 treated as 'no limit' (scored[:limit] if limit else scored), so a caller asking for zero matches gets every licensed broker. |
| post-10 | medium | `deal_board/brokers.py` | 72 | 72 | _is_blank uses any() instead of all(), so any row with a single blank field (every TREC row: blank markets) is skipped as blank; brokers silently vanish from the pool and from excluded_for_licensing. |
| post-11 | medium | `deal_board/brokers.py` | 1 | 1 | _split_list splits on commas as well as semicolons, so a market written 'Austin, TX' becomes two markets 'Austin' and 'TX' (and a broker can match a deal city literally named after a state code token). |
| post-12 | medium | `deal_board/brokers.py` | 2 | 2 | A bad broker row is skipped (continue) instead of failing the load, so a malformed record (e.g. state 'Texas' or a bad license_type) silently disappears from the pool and from excluded_for_licensing. |
| post-13 | medium | `deal_board/brokers.py` | 6 | 6 | _blank_to_none uses falsiness (None if not v), so a programmatic min_deal_usd=0 becomes None (a fully known $0-$X range is treated as open-ended, losing the +1 deal-size point) and a Broker built with an empty list (markets=[]) fails validation. |
| post-14 | medium | `deal_board/brokers.py` | 1 | 1 | 'Broker Organization' added to LicenseType, so company licenses load as brokers and (via PEOPLE = LICENSE_TYPES) the TREC build samples companies as people to match with. |
| post-15 | high | `deal_board/cli/build_brokers.py` | 5 | 5 | TREC rows are stamped source 'trec_<today>' (build date) instead of the download date AS_OF, so the matcher's snapshot-freshness clock restarts on every rebuild and a stale download is vouched for as fresh. |
| post-16 | medium | `deal_board/cli/build_brokers.py` | 1 | 1 | parse_date no longer raises on a 4-digit year, so if TREC switches to MM/DD/YYYY every expiry silently becomes unknown (None) and the build fails with a misleading 'need 150 valid rows' or writes blank expirations. |
| post-17 | low | `deal_board/cli/build_brokers.py` | 1 | 1 | County 'Pending' is no longer blanked, so the placeholder 'Pending' is written as a real county name. |
| post-18 | medium | `deal_board/cli/build_brokers.py` | 0 | 1 | 'Status' dropped from _TRIMMED_COLUMNS, so a raw ' Active ' is classified invalid (and as non-Active, not 'Active but expired') and written padded; the strata and the value written diverge from what was classified on. |
| post-19 | medium | `deal_board/cli/build_brokers.py` | 0 | 2 | is_valid uses exp > AS_OF, so an Active license expiring exactly on the download date is put in the 'Active but expired' stratum although it is valid that day (strata mislabeled vs the matcher's >=). |
| post-20 | medium | `deal_board/cli/build_brokers.py` | 0 | 1 | Valid stratum takes the first N_VALID rows (valid[:N_VALID]) instead of a seeded random sample, so the real sample is biased to file order and every later rng draw shifts (different invalid rows, different shuffle). |
| post-21 | medium | `deal_board/cli/build_brokers.py` | 0 | 1 | Final rng.shuffle removed, so licensee numbers reveal the stratum (tx-001..150 are exactly the valid rows), leaking the label into the broker_id. |
| post-22 | low | `deal_board/cli/build_brokers.py` | 0 | 1 | Build summary counts valid rows with > instead of >=, so the printed 'N valid as of AS_OF' undercounts licenses expiring on the download day (log only). |
| post-23 | high | `deal_board/api.py` | 1 | 1 | revalidate_requirement uses bool(is_deal_request) instead of 'is True', so a model_construct'ed requirement with is_deal_request='no' is treated as a deal and gets licensed matches. |
| post-24 | medium | `deal_board/api.py` | 31 | 31 | revalidate_requirement keeps a state if its strip().upper() is a code, so an unvalidated ' tx ' from a buggy extractor is normalised to TX and routed instead of failing closed to None. |
| post-25 | high | `deal_board/api.py` | 1 | 1 | LICENSING_TZ set to America/Chicago instead of America/New_York, so for the first hour of each Eastern day as_of is still yesterday and a NY/FL/GA license that expired yesterday passes the gate. |
| post-26 | medium | `deal_board/api.py` | 0 | 1 | licensing_date no longer rejects a naive datetime, so a naive now is silently interpreted in the host's local zone and the licensing date depends on server TZ. |
| post-27 | medium | `deal_board/api.py` | 2 | 2 | _declared_length_ok drops the digit-count guard, so a Content-Length of >4300 digits makes int() raise ValueError inside the middleware (500 instead of 413). |
| post-28 | medium | `deal_board/api.py` | 1 | 1 | _ClaudeKey fingerprint ignores the secret, so every key has the same fingerprint and a rotated ANTHROPIC_API_KEY keeps reusing the cached client built with the old key. |
| post-29 | medium | `deal_board/api.py` | 0 | 1 | Body-size middleware checks each chunk's size instead of the running total, so a chunked upload of many sub-cap chunks bypasses the 64 KiB limit. |
| post-30 | medium | `deal_board/api.py` | 4 | 4 | Route catches (ValidationError, TypeError) around revalidation, so an extractor returning a non-requirement (None/dict - an extractor bug) is reported as a 422 'could not extract' instead of the logged fixed 500. |
| post-31 | low | `deal_board/api.py` | 0 | 1 | Auto-fallback warning condition inverted (== FORCE_REGEX), so an explicit DEAL_EXTRACTOR=regex logs the downgrade warning and the real auto-mode downgrade is silent (log only). |
| post-32 | medium | `deal_board/extractors/claude.py` | 2 | 2 | _strict_type_ok accepts isinstance(value, int) for deal_value_usd, so a JSON boolean (True) passes the strict check and becomes deal_value_usd=1. |
| post-33 | medium | `deal_board/extractors/claude.py` | 2 | 2 | check_claude_raw no longer rejects unexpected keys, so a tool input with extra keys (e.g. 'license_state') is accepted and pydantic silently drops them. |
| post-34 | medium | `deal_board/extractors/claude.py` | 3 | 3 | A stop_reason of 'max_tokens' is accepted like 'tool_use', so a tool call cut off mid-object is returned by extract_raw as if complete. |
| post-35 | low | `deal_board/extractors/claude.py` | 0 | 2 | A tool_use block with no name is accepted (name check only when name is not None), so a nameless block is trusted as our tool. |
| post-36 | low | `deal_board/extractors/claude.py` | 2 | 2 | _strict_schema no longer recurses into lists, so 'minimum' inside deal_value_usd's anyOf survives in the strict tool schema (strict mode may reject the tool) and PROMPT_VERSION changes. |
| post-37 | low | `deal_board/extractors/claude.py` | 1 | 1 | last_usage is no longer reset at the start of extract_raw, so a later call whose response has no usage reports the previous call's tokens (cost accounting). |
| post-38 | medium | `deal_board/extractors/regex_baseline.py` | 0 | 1 | Wrapper's output check weakened to 'result is None', so a frozen impl returning a dict/other object is passed through as if it were a validated DealRequirement. |
| post-39 | low | `deal_board/extractors/regex_baseline.py` | 0 | 1 | _scoped_import aliases 'schema' at any import level, so a relative 'from .schema import' (level 1) inside the frozen file would also be redirected to deal_board.schema instead of resolving normally. |
