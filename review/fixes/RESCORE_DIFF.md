# Rescore before/after

Rescore of `results/claude_20261024T234359Z.json` (the final Claude run) into scratch `--results-dir`s, from `main`'s code (before) and from this branch (after). Produced by a leaf-by-leaf diff of every `datasets.*.scores` block. The only intended scoring change is eval-quality/EM-61 (+EM-24): hallucination denominators are the posts where a guess was possible. Every count, accuracy, accuracy CI and failure list is identical.

```text
## holdout: 84 leaves before, 90 after, 13 changed; changed leaf names: ['hallucination_ci95', 'hallucination_rate', 'n_null']
  fields.city.hallucination_ci95: [0.0, 0.2425007574992425] -> [0.0, 0.7934567085261071]
  fields.city.n_null: <absent> -> 1
  fields.deal_value_usd.hallucination_ci95: [0.0, 0.2425007574992425] -> [0.0, 0.5615060804490177]
  fields.deal_value_usd.n_null: <absent> -> 3
  fields.property_type.hallucination_ci95: [0.014864709450773478, 0.35388592179859524] -> [0.20654329147389294, 1.0]
  fields.property_type.hallucination_rate: 0.08333333333333333 -> 1.0
  fields.property_type.n_null: <absent> -> 1
  fields.state.hallucination_ci95: [0.0, 0.2425007574992425] -> [0.0, 0.7934567085261071]
  fields.state.n_null: <absent> -> 1
  fields.timeline.hallucination_ci95: [0.0, 0.2425007574992425] -> [0.0, 0.48990002040399916]
  fields.timeline.n_null: <absent> -> 4
  fields.transaction_type.hallucination_ci95: [0.0, 0.2425007574992425] -> [0.0, 0.7934567085261071]
  fields.transaction_type.n_null: <absent> -> 1
## posts: 84 leaves before, 90 after, 14 changed; changed leaf names: ['hallucination_ci95', 'hallucination_rate', 'n_null']
  fields.city.hallucination_ci95: [0.0, 0.11697359446555587] -> [0.0, 0.43449149475208104]
  fields.city.n_null: <absent> -> 5
  fields.deal_value_usd.hallucination_ci95: [0.0, 0.11697359446555587] -> [0.0, 0.43449149475208104]
  fields.deal_value_usd.n_null: <absent> -> 5
  fields.property_type.hallucination_ci95: [0.006113056614032261, 0.1717589106470025] -> [0.20654329147389294, 1.0]
  fields.property_type.hallucination_rate: 0.034482758620689655 -> 1.0
  fields.property_type.n_null: <absent> -> 1
  fields.state.hallucination_ci95: [0.019120864097994433, 0.21964947595851925] -> [0.09676933255921683, 0.7000116786584712]
  fields.state.hallucination_rate: 0.06896551724137931 -> 0.3333333333333333
  fields.state.n_null: <absent> -> 6
  fields.timeline.hallucination_ci95: [0.0, 0.11697359446555587] -> [0.0, 0.22810184305529166]
  fields.timeline.n_null: <absent> -> 13
  fields.transaction_type.hallucination_ci95: [0.0, 0.11697359446555587] -> [0.0, 0.7934567085261071]
  fields.transaction_type.n_null: <absent> -> 1
top-level keys added: ['request_sha', 'scoring_version', 'source_scoring'] removed: []
```

## Every saved run (after the ADV-1 fix)

All 21 saved `claude_*`/`regex_*` runs (none touches holdout_v3), re-scored with `main`'s code and with this branch, compared leaf by leaf over every `scores` block, ignoring only the EM-61/EM-24 keys (`hallucination_rate`, `hallucination_ci95`, `n_null`, `posts_with_any_*`). `raw_check` is which output check the branch applied: `strict` for runs of the current prompt `ac1a145b`, `legacy` (main's behaviour) for older prompts.

```text
claude_20261024T231514Z (prompt de789943, raw_check legacy): IDENTICAL
claude_20261024T232452Z (prompt de789943, raw_check legacy): IDENTICAL
claude_20261024T232525Z (prompt de789943, raw_check legacy): IDENTICAL
claude_20261024T232557Z (prompt de789943, raw_check legacy): IDENTICAL
claude_20261024T232649Z (prompt de789943, raw_check legacy): IDENTICAL
claude_20261024T232722Z (prompt de789943, raw_check legacy): IDENTICAL
claude_20261024T232921Z (prompt a86a1ad8, raw_check legacy): IDENTICAL
claude_20261024T233007Z (prompt a86a1ad8, raw_check legacy): IDENTICAL
claude_20261024T233059Z (prompt a86a1ad8, raw_check legacy): IDENTICAL
claude_20261024T233307Z (prompt b0950301, raw_check legacy): IDENTICAL
claude_20261024T233351Z (prompt b0950301, raw_check legacy): IDENTICAL
claude_20261024T233433Z (prompt b0950301, raw_check legacy): IDENTICAL
claude_20261024T233711Z (prompt b4f9c9ab, raw_check legacy): IDENTICAL
claude_20261024T233757Z (prompt b4f9c9ab, raw_check legacy): IDENTICAL
claude_20261024T233842Z (prompt b4f9c9ab, raw_check legacy): IDENTICAL
claude_20261024T234059Z (prompt ac1a145b, raw_check strict): IDENTICAL
claude_20261024T234146Z (prompt ac1a145b, raw_check strict): IDENTICAL
claude_20261024T234229Z (prompt ac1a145b, raw_check strict): IDENTICAL
claude_20261024T234359Z (prompt ac1a145b, raw_check strict): IDENTICAL
regex_20261024T224548Z (prompt None, raw_check legacy): IDENTICAL
regex_20261024T224817Z (prompt None, raw_check legacy): IDENTICAL
runs with unintended differences: 0
```
