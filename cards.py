"""Your cards live in cards.json. This file loads and saves them.

You can edit cards.json by hand, or ask the assistant: "add my new card",
"change the renewal date on the travel card to 2027-03-15".

Each card has:
  name            what you call it
  owner           whose card it is
  account_match   words from the account name in your transactions, so purchases match the card
  annual_fee      in dollars (0 if none)
  renewal_date    YYYY-MM-DD, the date the annual fee is charged (only month and day matter)
  point_value     what one point is worth to you, in cents (cash back = 1.0)
  earn            points per dollar by category; "other" is everything else
  credits         benefits you can use up and should track: a name, how often it resets
                  (monthly, quarterly, semiannual, annual, every_4_years), and an amount in
                  dollars if it has one. Perks with no amount (free night, lounge pass) leave it out.
  key_benefits    a few plain-English highlights, e.g. "No foreign transaction fees"
  source          where the benefits came from, and when they were checked

Never put card numbers or logins here.
"""

import json
import re
from datetime import date
from pathlib import Path

CARDS_FILE = Path(__file__).parent / "cards.json"           # your real cards (private)
EXAMPLE_FILE = Path(__file__).parent / "cards.example.json"  # sample cards (safe to share)
# "flights" and "hotels" are booked directly with the airline or hotel; "travel" is all other travel
# (rideshare, trains, car rentals). Rates that need the bank's travel site go in key_benefits instead.
# A card with no flights/hotels rate uses its "travel" rate for them.
# "amazon" = Amazon and Whole Foods (cards like Prime Visa earn more there).
CATEGORIES = ["flights", "hotels", "travel", "dining", "groceries", "gas", "streaming", "amazon", "other"]
PERIODS = ["monthly", "quarterly", "semiannual", "annual", "every_4_years"]


def load_cards():
    if not CARDS_FILE.exists():  # first run: start from the sample cards
        CARDS_FILE.write_text(EXAMPLE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    return json.loads(CARDS_FILE.read_text(encoding="utf-8"))


def save_cards(cards):
    CARDS_FILE.write_text(json.dumps(cards, indent=2), encoding="utf-8")


def _make_id(name, existing):
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "card"
    card_id, n = base, 2
    while card_id in existing:
        card_id, n = f"{base}_{n}", n + 1
    return card_id


# Fees for using the card abroad and for cash advances. None means "not known yet".
#   foreign_fee_pct     % added to purchases in other currencies (0 = no fee)
#   cash_advance_pct    % of each cash advance, with cash_advance_min dollars at least
FEE_FIELDS = ["foreign_fee_pct", "cash_advance_pct", "cash_advance_min"]


def _check(card):
    """Return a list of problems, empty if the card looks valid."""
    problems = []
    if not str(card.get("name", "")).strip():
        problems.append("name is required")
    if not str(card.get("owner", "")).strip():
        problems.append("owner is required")
    if card.get("renewal_date"):
        try:
            date.fromisoformat(card["renewal_date"])
        except ValueError:
            problems.append("renewal_date must be YYYY-MM-DD")
    for field in FEE_FIELDS:
        value = card.get(field)
        if value is not None and (not isinstance(value, (int, float)) or value < 0):
            problems.append(f"{field} must be a number of 0 or more, or empty if unknown")
    link = card.get("referral_link")
    if link and not str(link).startswith(("http://", "https://")):
        problems.append("referral_link must be a web address starting with https://")
    for category in card.get("earn", {}):
        if category not in CATEGORIES:
            problems.append(f"earn category '{category}' must be one of {CATEGORIES}")
    if "other" not in card.get("earn", {}):
        problems.append("earn must include 'other' (the rate on everything else)")
    for credit in card.get("credits", []):
        if not str(credit.get("name", "")).strip():
            problems.append("every benefit needs a name")
        if credit.get("amount") is not None and float(credit["amount"]) < 0:
            problems.append("benefit amounts can't be negative")
        if credit.get("period") not in PERIODS:
            problems.append(f"credit '{credit.get('name')}' period must be one of {PERIODS}")
    return problems


def add_card(name, owner, annual_fee=0, earn=None, point_value=1.0, renewal_date=None,
             account_match=None, credits=None, key_benefits=None, source=None,
             foreign_fee_pct=None, cash_advance_pct=None, cash_advance_min=None,
             referral_bonus=None, referral_link=None):
    cards = load_cards()
    ids = {c["id"] for c in cards}
    card = {
        "id": _make_id(name, ids), "name": name, "owner": owner,
        "account_match": account_match or name, "annual_fee": float(annual_fee),
        "renewal_date": renewal_date, "point_value": float(point_value),
        "earn": earn or {"other": 1}, "credits": [], "key_benefits": key_benefits or [],
        "source": source or "Benefits not looked up yet",
        "foreign_fee_pct": foreign_fee_pct, "cash_advance_pct": cash_advance_pct,
        "cash_advance_min": cash_advance_min,
        "referral_bonus": referral_bonus, "referral_link": referral_link,
    }
    for credit in credits or []:
        credit_ids = ids | {cr["id"] for c in cards for cr in c["credits"]} | {cr["id"] for cr in card["credits"]}
        card["credits"].append({"id": _make_id(f"{name} {credit['name']}", credit_ids), **credit})
    problems = _check(card)
    if problems:
        return {"saved": False, "problems": problems}
    cards.append(card)
    save_cards(cards)
    return {"saved": True, "card": card}


def update_card(card_id, changes):
    """Change some fields on one card, for example {"renewal_date": "2027-03-15"}."""
    cards = load_cards()
    card = next((c for c in cards if c["id"] == card_id), None)
    if card is None:
        return {"saved": False, "problems": [f"Unknown card. Valid ids: {[c['id'] for c in cards]}"]}
    updated = {**card, **{k: v for k, v in changes.items() if k != "id"}}
    for credit in updated.get("credits", []):
        credit.setdefault("id", _make_id(f"{updated['name']} {credit.get('name', 'credit')}",
                                         {cr["id"] for c in cards for cr in c["credits"] if "id" in cr}))
    problems = _check(updated)
    if problems:
        return {"saved": False, "problems": problems}
    card.clear()
    card.update(updated)
    save_cards(cards)
    return {"saved": True, "card": card}


def add_benefit(card_id, name, period, amount=None):
    """Add one trackable benefit to a card."""
    cards = load_cards()
    card = next((c for c in cards if c["id"] == card_id), None)
    if card is None:
        return {"saved": False, "problems": ["Unknown card."]}
    taken = {cr["id"] for c in cards for cr in c["credits"]}
    benefit = {"id": _make_id(f"{card['name']} {name}", taken), "name": name, "period": period}
    if amount not in (None, ""):
        benefit["amount"] = float(amount)
    return update_card(card_id, {"credits": card["credits"] + [benefit]})


def remove_benefit(card_id, benefit_id):
    cards = load_cards()
    card = next((c for c in cards if c["id"] == card_id), None)
    if card is None:
        return {"saved": False, "problems": ["Unknown card."]}
    return update_card(card_id, {"credits": [b for b in card["credits"] if b["id"] != benefit_id]})


def remove_card(card_id):
    """Delete a card you closed. Its saved offers are deleted too."""
    all_cards = load_cards()
    kept = [c for c in all_cards if c["id"] != card_id]
    if len(kept) == len(all_cards):
        return {"saved": False, "problems": ["Unknown card."]}
    save_cards(kept)
    return {"saved": True, "removed": card_id}


def edit_benefit(card_id, benefit_id, changes):
    """Fix a benefit's name, amount or how often it resets."""
    all_cards = load_cards()
    card = next((c for c in all_cards if c["id"] == card_id), None)
    if card is None:
        return {"saved": False, "problems": ["Unknown card."]}
    credits = []
    for b in card["credits"]:
        if b["id"] == benefit_id:
            b = {**b, **{k: v for k, v in changes.items() if k in ("name", "amount", "period")}}
            if b.get("amount") in ("", None):
                b.pop("amount", None)
            else:
                b["amount"] = float(b["amount"])
        credits.append(b)
    return update_card(card_id, {"credits": credits})
