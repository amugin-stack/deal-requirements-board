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
