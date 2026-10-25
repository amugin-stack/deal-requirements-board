# Review loop log

Each round: two read-only reviewer subagents (eval_method, code_quality) in parallel, then one adversarial
verifier subagent per candidate with its own `python -I` script in the session scratchpad. Verifier scripts
are embedded verbatim in `findings.json`.

## Round 1

- reviewers: eval_method (9 candidates, plus the README numbers audit), code_quality (8 candidates)
- candidates: 17 — EM-1..EM-9, CQ-1..CQ-8
- confirmed: 13 — EM-1, EM-2, EM-3, EM-5, EM-6, EM-7, EM-8, CQ-2, CQ-3, CQ-4, CQ-5, CQ-7, CQ-8
- rejected: 4 — EM-4 (already disclosed, not misleading), EM-9 (documented derived metric, not double-counted), CQ-1 (duplicate of EM-5), CQ-6 (unreachable: argparse limits --split)
- severity changes by verifiers: EM-1 high->medium, EM-2/EM-3 medium->low, CQ-5 medium->low; several claims narrowed (see verifier_note)
- README audit: all 27 numeric cells match results/*.json; prose mismatches: keep rule (EM-2), "500 tests" vs 837
- new_confirmed: 13
