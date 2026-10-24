# Loop Lessons

One entry per iteration, newest last. Format:

```
## Iteration N: <hypothesis>
- Change: <one line>, prompt <old> → <new>
- Dev mean (3 runs): <before> → <after>; per-field deltas: <...>
- Decision: kept | reverted (commit <sha>)
- Why: <what the raw outputs showed>
- Next: <how the backlog changed>
```

## Iteration 1: API-enforced enums remove the s17 schema error
- Change: tool `strict: true`, schema transformed in `claude.py` (additionalProperties false, all properties required, defaults/minimum dropped), prompt de789943 → a86a1ad8
- Dev mean (3 runs): 0.8848 → 0.9264; per-field deltas: is_deal_request +0.027, state +0.088, city +0.035, property_type +0.035, transaction_type 0.000, deal_value_usd +0.053, timeline +0.053
- Decision: kept (commit cd36d51)
- Why: s17's `within_90_days` became valid output in all 3 runs, so 0 errors (one errored post had cost every field). The s20 non-deal state fill (TX on a lender request) also disappeared in all 3 runs, and s11's IL inference showed up in only 1 of 3. Confirmed by one probe call first: strict mode works with Haiku 5.5 and a forced tool_choice.
- Next: s22 is now the biggest single failure (is_deal_request false + city + property_type missed, all 3 runs) and it blocks the 100% is_deal_request bar. Timeline (s04, s25 every run; s17, s13 once) is weakest at 0.86. Sell vs buy now covers s07 + s23.

## Iteration 2: a request for a professional to handle specific properties is a deal (s22)
- Change: one system-prompt paragraph: property-manager-style requests for specific properties are deals with transaction_type null; lenders, attorneys, and other non-brokerage services stay non-deal. Prompt a86a1ad8 → b0950301
- Dev mean (3 runs): 0.9264 → 0.9298; per-field deltas: is_deal_request +0.042 (now 1.0 every run), state −0.035, city −0.035, property_type +0.053, transaction_type 0.000, deal_value_usd 0.000, timeline 0.000
- Decision: kept (commit 3fa37ce)
- Why: s22 flipped to is_deal_request true with property_type single_family in all 3 runs, and s20 (lender) stayed non-deal with 0 fills. s22 city is still null every run: the raw notes say "ambiguous which Springfield", so the model nulls an ambiguous city the same way it nulls the state. The state and city drops are both 2/3 of one post: s11 IL inference went from 1/3 to 3/3 runs, and s02 Portland was nulled in 2/3 runs (its notes also mention ambiguity). Probably noise on top of an existing tendency, since the new rule doesn't touch either one, but it's the same pattern of nulling an ambiguous city.
- Next: check_success dev now fails only on mean (0.930 vs 0.932), which is about one more correct field per run. Ambiguous-city rule (s22 ×3, s02 ×2) is the most general cheap fix. The label convention keeps the city and nulls only the state.
