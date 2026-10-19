"""Sort merchants into reward categories (flights, hotels, travel, groceries, dining, gas, streaming, other).

SimpleFIN sends merchant names but no categories, so:
  1. Simple word rules handle the obvious ones (free, instant, always the same answer).
  2. Claude sorts whatever the rules miss, all at once.
  3. Every answer is saved in merchant_categories.json, so each merchant is sorted only once.
     You can open that file and correct any mistake by hand.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
CACHE_FILE = HERE / "merchant_categories.json"
CATEGORIES = ["flights", "hotels", "travel", "amazon", "groceries", "dining", "gas", "streaming", "other"]

RULES = {
    "amazon": ["amazon", "amzn", "whole foods", "wholefds"],
    "groceries": ["trader joe", "safeway", "kroger", "ralphs", "vons", "costco", "sprouts", "h mart", "aldi"],
    "dining": ["restaurant", "cafe", "coffee", "starbucks", "sweetgreen", "chipotle", "doordash", "uber eats", "grubhub", "pizza", "bistro", "sushi", "bar "],
    "flights": ["airline", "airways", "united air", "delta air", "american air", "southwest", "jetblue", "alaska air"],
    "hotels": ["marriott", "hilton", "hyatt", "ihg", "holiday inn", "hotel"],
    "travel": ["airbnb", "expedia", "uber", "lyft", "amtrak", "hertz", "enterprise rent"],
    "gas": ["shell", "chevron", "exxon", "mobil", "arco", "76 ", "bp "],
    "streaming": ["netflix", "spotify", "hulu", "disney", "youtube", "hbo", "max.com", "apple.com/bill"],
}


def _load_cache():
    return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}


def by_rules(merchant):
    text = f" {merchant.lower()} "
    for category, words in RULES.items():
        if any(word in text for word in words):
            return category
    return None


SORT_TOOL = {
    "name": "save_categories",
    "description": "Save the reward category for each merchant.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "merchant": {"type": "string"},
                        "category": {"type": "string", "enum": CATEGORIES},
                    },
                    "required": ["merchant", "category"],
                },
            }
        },
        "required": ["results"],
    },
}


def by_claude(merchants, client, model):
    """Ask Claude to sort a batch of merchant names. Returns {merchant: category}."""
    if not merchants:
        return {}
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        tools=[SORT_TOOL],
        tool_choice={"type": "tool", "name": "save_categories"},
        messages=[{"role": "user", "content":
                   "Sort each merchant from a credit card statement into one category: "
                   + ", ".join(CATEGORIES) + ". Use 'other' if unsure.\n\n" + "\n".join(merchants)}],
    )
    block = next(b for b in response.content if b.type == "tool_use")
    return {r["merchant"]: r["category"] for r in block.input["results"] if r["category"] in CATEGORIES}


def categorize_all(merchants, client=None, model=None):
    """Return {merchant: category} for every merchant, using the saved answers when possible."""
    cache = _load_cache()
    unknown = []
    for m in set(merchants):
        if m in cache:
            continue
        rule = by_rules(m)
        if rule:
            cache[m] = rule
        else:
            unknown.append(m)
    if unknown and client is not None:
        for start in range(0, len(unknown), 100):  # 100 at a time
            cache.update(by_claude(unknown[start:start + 100], client, model))
    for m in unknown:
        cache.setdefault(m, "other")
    CACHE_FILE.write_text(json.dumps(dict(sorted(cache.items())), indent=2))
    return cache
