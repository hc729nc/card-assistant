"""Look up a card's public details online: annual fee, earning rates, credits and perks.

Used by the dashboard's "Look up details" button. It only returns a draft;
nothing is saved until you click Save on the dashboard.

The renewal date is NOT looked up: it's the anniversary of when you opened the
card, so only you (or your statement) know it.
"""

from datetime import date

import cards

WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}

RECORD_TOOL = {
    "name": "record_card_details",
    "description": "Record the card's current public details, after searching.",
    "input_schema": {
        "type": "object",
        "properties": {
            "official_name": {"type": "string"},
            "annual_fee": {"type": "number"},
            "point_value": {"type": "number", "description": "Typical cents per point. 1.0 for cash back."},
            "earn": {
                "type": "object",
                "description": "Points per dollar. Keys only from: " + ", ".join(cards.CATEGORIES) + ". Must include 'other'.",
                "additionalProperties": {"type": "number"},
            },
            "credits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "amount": {"type": "number", "description": "Dollars per period; leave out for perks with no dollar value"},
                        "period": {"type": "string", "enum": cards.PERIODS},
                    },
                    "required": ["name", "period"],
                },
            },
            "key_benefits": {"type": "array", "items": {"type": "string"}, "description": "3-5 short highlights"},
            "foreign_fee_pct": {"type": "number", "description": "Foreign transaction fee in %. 0 if the card has none."},
            "cash_advance_pct": {"type": "number", "description": "Cash advance fee as % of the amount, e.g. 5"},
            "cash_advance_min": {"type": "number", "description": "Minimum cash advance fee in dollars, e.g. 10"},
            "notes": {"type": "string", "description": "Caps, rotating categories, or anything uncertain"},
            "source_url": {"type": "string"},
        },
        "required": ["annual_fee", "earn", "credits", "key_benefits", "source_url"],
    },
}

INSTRUCTIONS = """Look up the current public details of this US credit card: "{name}".
Search the web, preferring the card issuer's official page. Then call record_card_details once.
- earn: points per dollar, mapped only to these categories: {categories}. Always include "other".
  "flights" = flights booked directly with any airline; "hotels" = hotels booked directly;
  "travel" = all other travel (rideshare, trains, car rentals). Only use rates anyone gets without
  booking through the bank's travel site; put travel-site and brand-only rates (e.g. "6x on JetBlue",
  "7x at Hilton") in key_benefits instead. Groceries means in-store supermarkets. "amazon" = Amazon and Whole Foods purchases.
- credits: every benefit worth tracking as used or unused: statement credits (with dollar amount)
  and perks like free night certificates, lounge passes or Global Entry credit (no amount).
- Also find the foreign transaction fee and the cash advance fee (usually "5% or $10, whichever
  is greater"), ideally from the issuer's rates and fees table. Leave them out if you can't find them.
- If the card name is ambiguous or you can't find it, still call the tool with your best match
  and explain in notes.
Today is {today}."""


def lookup_card(name, client, model):
    """Return a draft of the card's details. Nothing is saved."""
    messages = [{"role": "user", "content": INSTRUCTIONS.format(
        name=name, categories=", ".join(cards.CATEGORIES), today=date.today().isoformat())}]
    for _ in range(8):
        response = client.messages.create(
            model=model, max_tokens=3000, tools=[WEB_SEARCH, RECORD_TOOL], messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_card_details":
                draft = dict(block.input)
                draft.setdefault("earn", {"other": 1})
                draft["earn"] = {k: v for k, v in draft["earn"].items() if k in cards.CATEGORIES}
                draft["earn"].setdefault("other", 1)
                return draft
        if response.stop_reason == "pause_turn":
            continue  # a long search paused; let it carry on
        messages.append({"role": "user", "content": "Please call record_card_details now."})
    return {"error": "Couldn't find details for that card. Try its full name, e.g. 'Chase Sapphire Preferred'."}
