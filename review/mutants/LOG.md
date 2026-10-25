# Mutation-testing loop log

Each round: mutant writers propose diffs (read-only), the lead runs every mutant with
`review/mutants/mutants.py run` (full suite on a scratchpad copy with its own venv), and one
verifier per survivor shows an input where original and mutant differ (or marks it equivalent).
Copy baseline (no `.git` in copies, so `test_frozen_baseline_matches_tag` also skips):
1002 passed / 2 skipped / 5 xfailed. Worktree baseline: 1003 passed / 1 skipped / 5 xfailed.

## Round 1

Writers: gate writer (18 mutants on `is_licensed` and `brokers.py`) and match/API writer
(20 on `match` / `_score`, 10 on `api.py`), plus the lead's always-True gate (`gate-00`).

- mutants: 49
- killed: 45
- survived: 4 (all confirmed `differs` by a verifier; none equivalent)
- new survivors by severity: high 1 (`api-10`), medium 0, low 3 (`api-02`, `api-07`, `match-11`)
- new_high_medium_survivors: 1

Notes:
- The year-only expiry gate (`gate-01`, the red team's known gap) is now killed by 52 tests,
  39 of them in `test_compliance.py`.
- `gate-00` (always-True gate) fails 389 tests: 81 in `test_red_team.py`, 297 in
  `test_compliance.py`, 10 in `test_matcher.py`, 1 in `test_api.py`.
- Thinly killed (<= 3 tests): `gate-03` (2), `gate-04` (1, only the `"Active "` case),
  `match-06` (1), `match-15` (1), `api-04` (1), `match-09` (2), `match-18` (2), `match-05` (3),
  `match-17` (3).
- `api-10` is the one high gap: every API/red-team test overrides `get_as_of`, so the real
  production clock is never tested. A broker whose license expired within the last year would
  pass the gate in production.

| id | target | status | killed_by | killing test files | severity |
|---|---|---|---|---|---|
| gate-00 | `is_licensed` | killed | 389 | test_api.py (1), test_compliance.py (297), test_matcher.py (10), test_red_team.py (81) |  |
| gate-01 | `is_licensed` | killed | 52 | test_compliance.py (39), test_matcher.py (1), test_red_team.py (12) |  |
| gate-02 | `is_licensed` | killed | 13 | test_compliance.py (1), test_red_team.py (12) |  |
| gate-03 | `is_licensed` | killed | 2 | test_compliance.py (2) |  |
| gate-04 | `is_licensed` | killed | 1 | test_compliance.py (1) |  |
| gate-05 | `is_licensed` | killed | 52 | test_compliance.py (39), test_matcher.py (1), test_red_team.py (12) |  |
| gate-06 | `is_licensed` | killed | 42 | test_compliance.py (37), test_matcher.py (2), test_red_team.py (3) |  |
| gate-07 | `is_licensed` | killed | 32 | test_compliance.py (1), test_red_team.py (31) |  |
| gate-08 | `is_licensed` | killed | 47 | test_api.py (1), test_compliance.py (22), test_matcher.py (2), test_red_team.py (22) |  |
| gate-09 | `is_licensed` | killed | 15 | test_compliance.py (3), test_red_team.py (12) |  |
| gate-10 | `is_licensed` | killed | 40 | test_compliance.py (39), test_matcher.py (1) |  |
| gate-11 | `is_licensed` | killed | 27 | test_compliance.py (17), test_matcher.py (1), test_red_team.py (9) |  |
| gate-12 | `is_licensed` | killed | 40 | test_compliance.py (39), test_matcher.py (1) |  |
| gate-13 | `is_licensed` | killed | 40 | test_compliance.py (39), test_matcher.py (1) |  |
| gate-14 | `is_licensed` | killed | 97 | test_compliance.py (71), test_matcher.py (4), test_red_team.py (22) |  |
| gate-15 | `is_licensed` | killed | 224 | test_api.py (1), test_compliance.py (132), test_matcher.py (10), test_red_team.py (81) |  |
| gate-16 | `is_licensed` | killed | 57 | test_compliance.py (39), test_matcher.py (2), test_red_team.py (16) |  |
| gate-17 | `Broker._blank_to_none` | killed | 5 | test_api.py (1), test_brokers.py (1), test_compliance.py (1), test_matcher.py (1), test_red_team.py (1) |  |
| gate-18 | `is_licensed` | killed | 13 | test_compliance.py (1), test_red_team.py (12) |  |
| match-01 | `match` | killed | 53 | test_compliance.py (44), test_matcher.py (9) |  |
| match-02 | `match` | killed | 123 | test_compliance.py (41), test_matcher.py (3), test_red_team.py (79) |  |
| match-03 | `match` | killed | 135 | test_api.py (1), test_compliance.py (44), test_matcher.py (9), test_red_team.py (81) |  |
| match-04 | `match` | killed | 122 | test_compliance.py (44), test_matcher.py (9), test_red_team.py (69) |  |
| match-05 | `_score` | killed | 3 | test_matcher.py (3) |  |
| match-06 | `match` | killed | 1 | test_compliance.py (1) |  |
| match-07 | `match` | killed | 16 | test_compliance.py (3), test_matcher.py (10), test_red_team.py (3) |  |
| match-08 | `match` | killed | 13 | test_matcher.py (1), test_red_team.py (12) |  |
| match-09 | `match` | killed | 2 | test_matcher.py (2) |  |
| match-10 | `match` | killed | 15 | test_matcher.py (3), test_red_team.py (12) |  |
| match-11 | `match` | survived | 0 | - | low |
| match-12 | `match` | killed | 15 | test_compliance.py (9), test_matcher.py (2), test_red_team.py (4) |  |
| match-13 | `match` | killed | 15 | test_compliance.py (9), test_matcher.py (2), test_red_team.py (4) |  |
| match-14 | `match` | killed | 17 | test_compliance.py (7), test_matcher.py (1), test_red_team.py (9) |  |
| match-15 | `match` | killed | 1 | test_matcher.py (1) |  |
| match-16 | `match` | killed | 124 | test_compliance.py (42), test_matcher.py (3), test_red_team.py (79) |  |
| match-17 | `match` | killed | 3 | test_matcher.py (3) |  |
| match-18 | `_score` | killed | 2 | test_matcher.py (2) |  |
| match-19 | `_score` | killed | 4 | test_matcher.py (4) |  |
| match-20 | `match` | killed | 114 | test_api.py (1), test_compliance.py (34), test_matcher.py (5), test_red_team.py (74) |  |
| api-01 | `match_post` | killed | 82 | test_api.py (3), test_red_team.py (79) |  |
| api-02 | `match_post` | survived | 0 | - | low |
| api-03 | `match_post` | killed | 13 | test_api.py (1), test_red_team.py (12) |  |
| api-04 | `match_post` | killed | 1 | test_api.py (1) |  |
| api-05 | `match_post` | killed | 36 | test_api.py (1), test_red_team.py (35) |  |
| api-06 | `match_post` | killed | 81 | test_api.py (2), test_red_team.py (79) |  |
| api-07 | `match_post` | survived | 0 | - | low |
| api-08 | `match_post` | killed | 21 | test_api.py (1), test_red_team.py (20) |  |
| api-09 | `match_post` | killed | 36 | test_api.py (1), test_red_team.py (35) |  |
| api-10 | `get_as_of` | survived | 0 | - | high |

## Round 2

Writers were given the round-1 kill list (`kill_list_r1.json`: id, status, killer count and
sample node ids) and asked to target survivor neighbourhoods, thinly killed spots and regions no
round-1 mutant touched: gate writer on what feeds the gate (`get_as_of`, `get_brokers`, broker
loading, `DealRequirement` state handling, new gate-expression variants), match/API writer on the
scoring rules (untouched in round 1), tie-breaks, limit handling and the response model.

- mutants: 39 (gate-20..38, match-22..34, api-11..17)
- killed: 24
- survived: 15 -> verified: 14 `differs`, 1 `equivalent` (`api-13`: building `MatchResponse`
  re-runs `_compute_missing`, so clearing `missing_fields` is undone; excluded from the count)
- new survivors by severity: high 4 (`gate-20`, `gate-22`, `gate-25`, `gate-28`),
  medium 5 (`gate-21`, `gate-23`, `gate-35`, `match-28`, `match-30`),
  low 5 (`gate-26`, `gate-31`, `gate-34`, `match-32`, `api-11`)
- new_high_medium_survivors: 9

Severity calls the lead changed (both ratings are stored in results.json as
`severity` / `verifier_severity`):
- `gate-25`, `gate-28` medium -> high: a padded `"Active "` / `" Active"` status is "not
  licensed" by the suite's own `test_status_must_be_exactly_active[Active ]`, and these loader
  mutants make `match()` return such a broker. Shipped CSVs have no padded values today.
- `gate-35` low -> medium: a real gate bypass (`"T" in "TX"`) through the public
  `is_licensed(state: str | None)`, though validation keeps it off the POST /match path.

Gaps found:
1. The production clock is untested. Every API/red-team test overrides `get_as_of`, so a lagging,
   frozen or UTC `as_of` (gate-20/21/22, round-1 api-10) lets expired licenses through.
2. Loading is untested for padded values. No test loads a CSV whose status has spaces, so a loader
   that trims (gate-25/28) silently turns "not exactly Active" into "Active".
3. `get_brokers()` with real data is only checked for `excluded >= 0` (gate-23).
4. Reason text, the name-vs-id tie-break and `limit <= 0` are thinly covered.

| id | target | status | killed_by | killing test files | severity |
|---|---|---|---|---|---|
| gate-20 | `get_as_of` | survived | 0 | - | high |
| gate-21 | `get_as_of` | survived | 0 | - | medium |
| gate-22 | `get_as_of` | survived | 0 | - | high |
| gate-23 | `get_brokers` | survived | 0 | - | medium |
| gate-24 | `match_post` | killed | 38 | test_api.py (1), test_red_team.py (37) |  |
| gate-25 | `Broker._blank_to_none` | survived | 0 | - | high |
| gate-26 | `Broker.license_state` | survived | 0 | - | low |
| gate-27 | `Broker._split_list` | killed | 14 | test_matcher.py (14) |  |
| gate-28 | `load_brokers` | survived | 0 | - | high |
| gate-29 | `ExtractedFields._upper_state` | killed | 34 | test_eval.py (1), test_red_team.py (33) |  |
| gate-30 | `ExtractedFields._upper_state` | killed | 2 | test_red_team.py (2) |  |
| gate-31 | `ExtractedFields._upper_state` | survived | 0 | - | low |
| gate-32 | `ExtractedFields.state` | killed | 13 | test_eval.py (1), test_packaging_compat.py (2), test_red_team.py (8), test_scoring_stats.py (2) |  |
| gate-33 | `DealRequirement._compute_missing` | killed | 12 | test_api.py (1), test_baseline_golden.py (10), test_scoring_stats.py (1) |  |
| gate-34 | `ExtractedFields.is_deal_request` | survived | 0 | - | low |
| gate-35 | `is_licensed` | survived | 0 | - | medium |
| gate-36 | `is_licensed` | killed | 2 | test_compliance.py (2) |  |
| gate-37 | `is_licensed` | killed | 123 | test_compliance.py (35), test_matcher.py (51), test_red_team.py (37) |  |
| gate-38 | `match_post` | killed | 5 | test_api.py (1), test_red_team.py (4) |  |
| match-22 | `_market_point` | killed | 1 | test_matcher.py (1) |  |
| match-23 | `_market_point` | killed | 2 | test_matcher.py (2) |  |
| match-24 | `_market_point` | killed | 4 | test_matcher.py (4) |  |
| match-25 | `_deal_size_point` | killed | 2 | test_matcher.py (2) |  |
| match-26 | `_deal_size_point` | killed | 6 | test_matcher.py (6) |  |
| match-27 | `_property_point` | killed | 7 | test_matcher.py (7) |  |
| match-28 | `_transaction_point` | survived | 0 | - | medium |
| match-29 | `_score` | killed | 2 | test_matcher.py (2) |  |
| match-30 | `match` | survived | 0 | - | medium |
| match-31 | `match` | killed | 1 | test_matcher.py (1) |  |
| match-32 | `match` | survived | 0 | - | low |
| match-33 | `match` | killed | 13 | test_matcher.py (1), test_red_team.py (12) |  |
| match-34 | `_score` | killed | 24 | test_matcher.py (24) |  |
| api-11 | `MatchResponse` | survived | 0 | - | low |
| api-12 | `match_post` | killed | 5 | test_api.py (2), test_red_team.py (3) |  |
| api-13 | `match_post` | equivalent | 0 | - |  |
| api-14 | `match_post` | killed | 12 | test_red_team.py (12) |  |
| api-15 | `match_post` | killed | 1 | test_api.py (1) |  |
| api-16 | `match_post` | killed | 1 | test_api.py (1) |  |
| api-17 | `match_post` | killed | 1 | test_api.py (1) |  |

## Round 3

Writers got the round-2 kill list (`kill_list_r2.json`) and were told not to write more variants
of gaps already recorded (any `get_as_of` change, loader whitespace trimming, `get_brokers`
pre-filtering, `limit <= 0`, substring state, reason wording, name-vs-id tie-break,
`model_construct`, `ge=0`, in-place sort), so this round measures new gaps only. The gate writer
took the data pipeline that produces what the gate reads: `deal_board/cli/build_brokers.py`
(builds `data/brokers.csv` from the raw TREC download), the `Broker` field types,
`load_brokers` and `paths.py`. The match/API writer took the county table, market
normalisation, `Match` model types, the non-deal early return, `get_matcher`'s import fallback,
`match_post`'s exception mapping and the response fields.
`api-29` (inverted extractor choice) was proposed and dropped before running: it could make the
suite build a real Claude client, and the owner's rule is no API calls.

- mutants: 36 (gate-41..57, match-41..50, api-21..28, api-30)
- killed: 16
- survived: 20, all verified `differs`; none equivalent
- new survivors by severity:
  high 6 (`gate-41`, `gate-42`, `gate-43`, `gate-44`, `gate-45`, `api-21`),
  medium 8 (`gate-47`, `gate-48`, `gate-49`, `gate-50`, `gate-53`, `api-28`, `match-41`, `match-43`),
  low 6 (`gate-46`, `gate-55`, `match-44`, `match-45`, `match-46`, `match-48`)
- new_high_medium_survivors: 14

Gaps found:
1. The builder that produces `data/brokers.csv` has no tests. Ten of its ten mutants survived. Five
   would make an unlicensed row look licensed in a rebuilt CSV: day/month swap, unknown expiry
   becoming 2099 or the snapshot date, `%Y-%d-%m` output, and "Probation - Active" mapped to
   "Active". Today's committed CSV is fine (the data tests check it), but a rebuild would not be
   checked.
2. The matcher-unavailable path is never exercised. With `api-21`, a failed matcher import serves
   every broker, expired ones included, with a 200 instead of a 503.
3. `TX_CITY_TO_COUNTY` is only tested against itself (the county test reads its cases from the
   table), so a wrong or added entry survives.
4. `load_brokers` is never fed duplicate ids (the duplicate-id compliance test builds its pool in
   memory).

| id | target | status | killed_by | killing test files | severity |
|---|---|---|---|---|---|
| gate-41 | `parse_date` | survived | 0 | - | high |
| gate-42 | `parse_date` | survived | 0 | - | high |
| gate-43 | `sample_trec` | survived | 0 | - | high |
| gate-44 | `sample_trec` | survived | 0 | - | high |
| gate-45 | `sample_trec` | survived | 0 | - | high |
| gate-46 | `sample_trec` | survived | 0 | - | low |
| gate-47 | `is_valid` | survived | 0 | - | medium |
| gate-48 | `main` | survived | 0 | - | medium |
| gate-49 | `sample_trec` | survived | 0 | - | medium |
| gate-50 | `sample_trec` | survived | 0 | - | medium |
| gate-51 | `Broker.license_expiration` | killed | 141 | test_api.py (1), test_brokers.py (1), test_compliance.py (42), test_matcher.py (58), test_packaging_compat.py (1), test_red_team.py (38) |  |
| gate-52 | `Broker.license_expiration` | killed | 1 | test_api.py (1) |  |
| gate-53 | `load_brokers` | survived | 0 | - | medium |
| gate-54 | `load_brokers` | killed | 1 | test_brokers.py (1) |  |
| gate-55 | `Broker.is_fictional` | survived | 0 | - | low |
| gate-56 | `BROKERS_CSV` | killed | 6 | test_brokers.py (1), test_compliance.py (1), test_matcher.py (1), test_red_team.py (3) |  |
| gate-57 | `DATA_DIR` | killed | 4 | test_brokers.py (1), test_compliance.py (1), test_matcher.py (1), test_red_team.py (1) |  |
| match-41 | `TX_CITY_TO_COUNTY` | survived | 0 | - | medium |
| match-42 | `TX_CITY_TO_COUNTY` | killed | 1 | test_matcher.py (1) |  |
| match-43 | `TX_CITY_TO_COUNTY` | survived | 0 | - | medium |
| match-44 | `_market_point` | survived | 0 | - | low |
| match-45 | `_market_point` | survived | 0 | - | low |
| match-46 | `_score` | survived | 0 | - | low |
| match-47 | `_score` | killed | 1 | test_matcher.py (1) |  |
| match-48 | `Match` | survived | 0 | - | low |
| match-49 | `Match` | killed | 1 | test_matcher.py (1) |  |
| match-50 | `match` | killed | 17 | test_compliance.py (9), test_matcher.py (2), test_red_team.py (6) |  |
| api-21 | `get_matcher` | survived | 0 | - | high |
| api-22 | `match_post` | killed | 15 | test_api.py (2), test_red_team.py (13) |  |
| api-23 | `match_post` | killed | 3 | test_api.py (2), test_red_team.py (1) |  |
| api-24 | `BrokerMatch` | killed | 37 | test_api.py (2), test_red_team.py (35) |  |
| api-25 | `match_post` | killed | 21 | test_api.py (1), test_red_team.py (20) |  |
| api-26 | `match_post` | killed | 2 | test_api.py (2) |  |
| api-27 | `match_post` | killed | 81 | test_api.py (2), test_red_team.py (79) |  |
| api-28 | `get_matcher` | survived | 0 | - | medium |
| api-30 | `match_post` | killed | 1 | test_api.py (1) |  |
