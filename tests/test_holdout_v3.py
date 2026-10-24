"""Checks for data/holdout_v3.jsonl, the fresh double-labeled holdout.

Skeleton only: the data file does not exist yet. Once labels are final this file
will check:

- every line is JSON with keys id, source, split, tags, text, label, label_note,
  adjudication;
- ids are unique and follow v3-01 .. v3-25;
- source is "synthetic-holdout-v3" and split is "holdout_v3";
- each label has exactly the seven ExtractedFields label keys and parses through
  schema.ExtractedFields, so a typo'd enum or a bad state code fails here, not
  silently in the eval;
- non-deal posts have every other field null (nothing to score or follow up);
- tags are a non-empty list of strings;
- adjudication is "agreed" or "lead-resolved".
"""
