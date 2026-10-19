"""Find new cards worth considering: current sign-up bonuses, cards that earn more than
yours in a category (groceries, gas, flights...), and checking account bonuses.

Used by the dashboard's "New card ideas" page. Results are saved to suggestions.json
with the date they were checked, because offers change every few weeks.

This is a workflow, not an agent: one Claude call with web search, then a fixed form.
"""

import json
from datetime import date
from pathlib import Path

import cards

HERE = Path(__file__).parent
SUGGESTIONS_FILE = HERE / "suggestions.json"

WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}

RECORD_TOOL = {
    "name": "record_suggestions",
    "description": "Record the card suggestions you found, after searching.",
    "input_schema": {
        "type": "object",
        "properties": {
            "signup": {
                "type": "array",
                "description": "8-12 personal cards with strong current welcome offers.",
                "items": {
                    "type": "object",
                    "properties": {
                        "card": {"type": "string"},
                        "bonus": {"type": "string", "description": "e.g. '75,000 points' or '$250 cash back'"},
                        "min_spend": {"type": "number", "description": "Dollars to spend to earn it"},
                        "months": {"type": "number", "description": "Months allowed to reach the spend"},
                        "annual_fee": {"type": "number"},
                        "ends": {"type": "string", "description": "YYYY-MM-DD the offer ends, only if the source states it"},
                        "note": {"type": "string", "description": "Eligibility rules or catches, one short sentence"},
                        "source_url": {"type": "string"},
                    },
                    "required": ["card", "bonus", "annual_fee", "source_url"],
                },
            },
            "category": {
                "type": "array",
                "description": "For each category, the 1-3 best cards for it.",
                "items": {
                    "type": "object",
                    "properties": {
                        "card": {"type": "string"},
                        "category": {"type": "string", "enum": cards.CATEGORIES},
                        "rate": {"type": "number", "description": "Points or % per dollar in this category"},
                        "point_value": {"type": "number", "description": "Cents per point; 1.0 for cash back"},
                        "annual_fee": {"type": "number"},
                        "cap": {"type": "string", "description": "Spending cap, e.g. 'up to $6,000/year'"},
                        "source_url": {"type": "string"},
                    },
                    "required": ["card", "category", "rate", "point_value", "annual_fee", "source_url"],
                },
            },
            "bank": {
                "type": "array",
                "description": "8-12 current checking account sign-up bonuses from US banks.",
                "items": {
                    "type": "object",
                    "properties": {
                        "bank": {"type": "string"},
                        "account": {"type": "string"},
                        "bonus": {"type": "string", "description": "e.g. '$400' or 'up to $600'"},
                        "requirement": {"type": "string", "description": "What you must do, e.g. '$1,000 direct deposits in 90 days'"},
                        "fee": {"type": "string", "description": "Monthly fee and how to avoid it, if known"},
                        "ends": {"type": "string", "description": "YYYY-MM-DD the offer ends, only if stated"},
                        "source_url": {"type": "string"},
                    },
                    "required": ["bank", "bonus", "requirement", "source_url"],
                },
            },
        },
        "required": ["signup", "category", "bank"],
    },
}

INSTRUCTIONS = """Find US credit cards worth considering for a household that already has these cards:
{owned}

Search the web (prefer pages updated this month) and then call record_suggestions once.
1. signup: personal cards they DON'T have with the best current welcome offers. Give the
   offer end date only when a source states it ("for applications through ..."), else leave it out.
   In note, mention known eligibility rules (e.g. Chase 5/24, Amex once-per-lifetime or family rules).
2. category: for each of {categories} ("amazon" = Amazon and Whole Foods), the 1-3 best cards for that category that they don't have.
   "flights"/"hotels" mean booked directly, not through a bank's travel site. Use these point values
   so cards compare fairly: Amex/Chase/Citi points 1.6-2.0, airline miles about 1.2, hotel points
   0.5-0.8, cash back 1.0.
3. bank: the best current checking account bonuses (not savings or CDs), with requirements,
   monthly fee and end date if stated. Skip ones that need more than $25,000 deposited.
Today is {today}."""


def find_suggestions(owned_names, client, model):
    """Search for suggestions and return them. Doesn't save."""
    messages = [{"role": "user", "content": INSTRUCTIONS.format(
        owned="\n".join(f"- {n}" for n in sorted(set(owned_names))) or "- none",
        categories=", ".join(cards.CATEGORIES), today=date.today().isoformat())}]
    for _ in range(10):
        response = client.messages.create(
            model=model, max_tokens=6000, tools=[WEB_SEARCH, RECORD_TOOL], messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_suggestions":
                found = dict(block.input)
                found["category"] = [c for c in found.get("category", []) if c.get("category") in cards.CATEGORIES]
                return found
        if response.stop_reason == "pause_turn":
            continue
        messages.append({"role": "user", "content": "Please call record_suggestions now."})
    return {"error": "Couldn't put the suggestions together. Try again in a minute."}


def save_suggestions(found):
    data = {"checked": date.today().isoformat(), "signup": found.get("signup", []),
            "category": found.get("category", []), "bank": found.get("bank", [])}
    SUGGESTIONS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def load_suggestions():
    if not SUGGESTIONS_FILE.exists():
        return None
    return json.loads(SUGGESTIONS_FILE.read_text(encoding="utf-8"))
