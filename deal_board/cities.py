"""City keys: when two spellings of a city count as the same place.

Two rules, one strictly coarser than the other:
- city_key is the eval's rule (scoring.normalize for "city"): drop '.', treat ',' as a space,
  lowercase, collapse whitespace. It decides whether an extracted city is scored correct.
- market_key is the matcher's rule. It starts from city_key and only merges further
  (Unicode compatibility forms, case folding, a trailing state code, Ft/St/Mt), so any city
  the eval scores correct also earns the matcher's market or county point. If the matcher
  were stricter, the eval would credit cities the product can't use.

Neither rule tolerates spelling mistakes: a wrong city must not look like a local fit.
"""

import unicodedata

# Leading abbreviations written out, so "Ft. Lauderdale" meets a "Fort Lauderdale" market.
_ABBREVIATIONS = {"ft": "fort", "st": "saint", "mt": "mount"}


def city_key(text: str) -> str:
    """The eval's city equality: "St. Louis," == "st  louis". Must stay identical to scoring.normalize."""
    return " ".join(text.replace(".", "").replace(",", " ").lower().split())


def market_key(text: str, state: str | None) -> str:
    """The matcher's city equality, a coarsening of city_key.

    A trailing token equal to the deal's state code is dropped ("Austin, TX" in a TX deal is
    Austin), but a different code is kept, so a market written "Hoboken, NJ" never matches a
    NY deal's "Hoboken": a city in another state is not a local fit. A lone token is never
    dropped or expanded, so "St" or "TX" on its own doesn't become empty or a different word.
    """
    # Computed from city_key's output, so equal city keys always give equal market keys.
    folded = unicodedata.normalize("NFKC", city_key(text)).casefold()
    tokens = city_key(folded).split()
    if state is not None and len(tokens) > 1 and tokens[-1] == state.casefold():
        tokens.pop()
    if len(tokens) > 1:
        tokens[0] = _ABBREVIATIONS.get(tokens[0], tokens[0])
    return " ".join(tokens)
