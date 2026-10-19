"""What the agent can do. Same shape as your refund agent:
the functions (in finance.py), the menu Claude reads (TOOLS), and the connector (run_tool).

Blast radius: the tools read your data, except three small writes to files on
your computer: record_credit_use, add_card and update_card. The prompt makes the
agent show you a card and get a "yes" before saving it. The agent can't move
money, pay bills or change anything at a bank. There is deliberately no tool for that.

Claude also gets a web search tool (WEB_SEARCH below). It runs on Anthropic's side,
so there's no Python function for it: we only switch it on.
"""

import cards
import finance
import offers

CATEGORY_LIST = cards.CATEGORIES
DATE_HELP = "Optional, YYYY-MM-DD. Leave out to use all data."

WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}

CREDIT_SCHEMA = {
    "type": "object",
    "description": "A benefit to track as used/unused. Leave out amount for perks with no dollar value.",
    "properties": {
        "name": {"type": "string"},
        "amount": {"type": "number", "description": "Dollars per period, if it has a dollar value"},
        "period": {"type": "string", "enum": cards.PERIODS},
    },
    "required": ["name", "period"],
}
KEY_BENEFITS_SCHEMA = {
    "type": "array", "items": {"type": "string"},
    "description": "3-5 short plain-English highlights, e.g. 'Airport lounge access'",
}
EARN_SCHEMA = {
    "type": "object",
    "description": "Points per dollar by category. Must include 'other'. Categories: " + ", ".join(CATEGORY_LIST),
    "additionalProperties": {"type": "number"},
}


def card_ideas():
    """Saved suggestions from the dashboard's New card ideas page."""
    import suggest
    saved = suggest.load_suggestions()
    if not saved:
        return {"note": "No ideas saved yet. They're found on the dashboard's New card ideas page (Check again)."}
    return saved


def list_cards():
    return [{"id": c["id"], "name": c["name"], "owner": c["owner"], "annual_fee": c["annual_fee"],
             "renewal_date": c.get("renewal_date"), "earn": c["earn"],
             "credits": [cr["name"] for cr in c["credits"]], "key_benefits": c.get("key_benefits", []),
             "source": c.get("source"), "foreign_fee_pct": c.get("foreign_fee_pct"),
             "cash_advance_fee": {"pct": c.get("cash_advance_pct"), "min_dollars": c.get("cash_advance_min")},
             "referral_bonus": c.get("referral_bonus"), "referral_link": c.get("referral_link")}
            for c in cards.load_cards()]


TOOLS = [
    {
        "name": "list_cards",
        "description": "List every card in the household: owner, annual fee, earning rates, credits, foreign transaction fee and cash advance fee.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "best_card_for",
        "description": "Rank cards by value for a purchase category. Use for 'which card should I use for X'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": CATEGORY_LIST},
                "owner": {"type": "string", "description": "Optional: only this person's cards."},
            },
            "required": ["category"],
        },
    },
    {
        "name": "spending_summary",
        "description": "Total spending grouped by category, card, or owner, for a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": DATE_HELP},
                "end": {"type": "string", "description": DATE_HELP},
                "group_by": {"type": "string", "enum": ["category", "card", "owner"]},
            },
        },
    },
    {
        "name": "missed_rewards",
        "description": "How much reward value was lost by not using the best card, with the biggest examples.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": DATE_HELP},
                "end": {"type": "string", "description": DATE_HELP},
            },
        },
    },
    {
        "name": "credits_remaining",
        "description": "Every trackable benefit: whether it's used this period, how much is left, and when it resets.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "record_credit_use",
        "description": "Mark a benefit as used. Only when the user says they used it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "credit_id": {"type": "string", "description": "The benefit's id from credits_remaining."},
                "amount": {"type": "number", "description": "Dollars used. Leave out to use the whole remaining amount."},
                "on_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today."},
            },
            "required": ["credit_id"],
        },
    },
    {
        "name": "undo_last_use",
        "description": "Undo the most recent 'used' mark on a benefit, if the user says it was a mistake.",
        "input_schema": {
            "type": "object",
            "properties": {"credit_id": {"type": "string"}},
            "required": ["credit_id"],
        },
    },
    {
        "name": "upcoming_fees",
        "description": "Annual fees charged in the next N days, with the date and days away.",
        "input_schema": {
            "type": "object",
            "properties": {"days_ahead": {"type": "integer", "description": "Default 90."}},
        },
    },
    {
        "name": "list_offers",
        "description": ("Card-linked offers (like Amex Offers) the user saved. Filter by card or merchant. "
                        "Use to answer 'any offers at X?' before recommending a card for a purchase there."),
        "input_schema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}, "merchant": {"type": "string"}},
        },
    },
    {
        "name": "add_offer",
        "description": "Save a card-linked offer the user tells you about. Confirm the details first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "merchant": {"type": "string"},
                "deal": {"type": "string", "description": "e.g. 'Spend $100, get $20 back'"},
                "expires": {"type": "string", "description": "YYYY-MM-DD, if known"},
            },
            "required": ["card_id", "merchant", "deal"],
        },
    },
    {
        "name": "add_card",
        "description": ("Save a new card. ONLY after showing the user the full details and getting a clear yes. "
                        "Look up benefits with web_search first if the user didn't give them."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "owner": {"type": "string"},
                "annual_fee": {"type": "number"},
                "renewal_date": {"type": "string", "description": "YYYY-MM-DD when the annual fee is charged"},
                "point_value": {"type": "number", "description": "Cents per point. 1.0 for cash back."},
                "earn": EARN_SCHEMA,
                "credits": {"type": "array", "items": CREDIT_SCHEMA},
                "key_benefits": KEY_BENEFITS_SCHEMA,
"foreign_fee_pct": {"type": "number", "description": "Foreign transaction fee in %, 0 if none"},
                "cash_advance_pct": {"type": "number", "description": "Cash advance fee in % of the amount"},
                "cash_advance_min": {"type": "number", "description": "Minimum cash advance fee in dollars"},
                "referral_bonus": {"type": "string", "description": "What the owner gets for referring a friend, e.g. '20,000 points'"},
                "referral_link": {"type": "string", "description": "The owner's referral link"},
                "account_match": {"type": "string", "description": "Words from this card's account name in transactions"},
                "source": {"type": "string", "description": "Where benefits came from (URL or 'user') and today's date"},
            },
            "required": ["name", "owner"],
        },
    },
    {
        "name": "update_card",
        "description": ("Change fields on an existing card, e.g. its renewal_date or a credit. "
                        "Show the user the change and get a yes first. 'credits' replaces the whole list."),
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "changes": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"}, "owner": {"type": "string"},
                        "annual_fee": {"type": "number"}, "renewal_date": {"type": "string"},
                        "point_value": {"type": "number"}, "earn": EARN_SCHEMA,
                        "credits": {"type": "array", "items": CREDIT_SCHEMA},
                        "key_benefits": KEY_BENEFITS_SCHEMA,
                        "account_match": {"type": "string"}, "source": {"type": "string"},
                        "foreign_fee_pct": {"type": "number"}, "cash_advance_pct": {"type": "number"},
                        "cash_advance_min": {"type": "number"},
                        "referral_bonus": {"type": "string"}, "referral_link": {"type": "string"},
                    },
                },
            },
            "required": ["card_id", "changes"],
        },
    },
    {
        "name": "card_ideas",
        "description": ("New cards worth considering, saved from a web search: current sign-up bonuses (with end dates), "
                        "cards that earn more in a category, and checking account bonuses. Includes the date checked. "
                        "Compare against list_cards so you only suggest cards they don't have."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "card_value_review",
        "description": "Is a card worth its annual fee this year? Compares rewards to a no-fee 2% card, adds credits used, subtracts the fee.",
        "input_schema": {
            "type": "object",
            "properties": {"card_id": {"type": "string", "description": "The card's id from list_cards."}},
            "required": ["card_id"],
        },
    },
]

FUNCTIONS = {
    "list_cards": list_cards,
    "best_card_for": finance.best_card_for,
    "spending_summary": finance.spending_summary,
    "missed_rewards": finance.missed_rewards,
    "credits_remaining": finance.credits_remaining,
    "record_credit_use": finance.record_credit_use,
    "undo_last_use": finance.undo_last_use,
    "upcoming_fees": finance.upcoming_fees,
    "card_value_review": finance.card_value_review,
    "list_offers": offers.list_offers,
    "add_offer": offers.add_offer,
    "card_ideas": card_ideas,
    "add_card": cards.add_card,
    "update_card": cards.update_card,
}


def run_tool(name, args):
    try:
        return FUNCTIONS[name](**args)
    except Exception as exc:  # tell Claude what went wrong instead of crashing
        return {"error": f"{type(exc).__name__}: {exc}"}
