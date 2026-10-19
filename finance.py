"""The math. Everything here has one right answer, so it's plain code, not AI.

The agent never adds up numbers itself: it calls these functions through tools,
so totals and recommendations come from your data, not the model's guesses.
"""

import csv
import json
from datetime import date, timedelta
from pathlib import Path

from cards import load_cards

HERE = Path(__file__).parent
CREDITS_FILE = HERE / "credits_used.json"

# Which file to read: your real data (made by simplefin.py) if it exists, otherwise the sample.
# Using a Rocket Money export instead? Save it as transactions.csv in this folder.
REAL_FILE = HERE / "transactions.csv"
TRANSACTIONS_FILE = REAL_FILE if REAL_FILE.exists() else HERE / "sample_transactions.csv"

# Are purchases positive numbers in the file? True for simplefin.py's file and the sample.
# For a Rocket Money export, open it and check one purchase; if it's negative, set this to False.
PURCHASES_ARE_POSITIVE = True

# Rocket Money categories -> reward categories the cards use.
CATEGORY_MAP = {
    "amazon": "amazon",
    "whole foods": "amazon",
    "groceries": "groceries",
    "dining": "dining",
    "restaurant": "dining",
    "food & drink": "dining",
    "airline": "flights",
    "flight": "flights",
    "hotel": "hotels",
    "lodging": "hotels",
    "travel": "travel",
    "gas": "gas",
    "auto & transport": "gas",
    "streaming": "streaming",
    "entertainment": "streaming",
}
SKIP_CATEGORIES = ("payment", "transfer", "income", "credit card", "refund")


def reward_category(rocket_money_category):
    text = (rocket_money_category or "").lower()
    if any(word in text for word in SKIP_CATEGORIES):
        return None
    for key, category in CATEGORY_MAP.items():
        if key in text:
            return category
    return "other"


def card_for_account(account_name, cards=None):
    for card in cards or load_cards():
        if card["account_match"].lower() in (account_name or "").lower():
            return card
    return None


def cents_per_dollar(card, category):
    """What a card earns on $1 in this category, in cents of value."""
    earn = card["earn"]
    fallback = earn.get("travel", earn.get("other", 1)) if category in ("flights", "hotels") else earn.get("other", 1)
    rate = earn.get(category, fallback)
    return rate * card["point_value"]


def load_purchases(path=None):
    """Read the export and return purchases only (no payments, refunds or transfers)."""
    purchases = []
    cards = load_cards()
    with open(path or TRANSACTIONS_FILE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            category = reward_category(row.get("Category"))
            if category is None:
                continue
            amount = float(str(row.get("Amount", "0")).replace("$", "").replace(",", ""))
            if not PURCHASES_ARE_POSITIVE:
                amount = -amount
            if amount <= 0:
                continue
            card = card_for_account(row.get("Account Name"), cards)
            purchases.append({
                "date": row.get("Date"),
                "merchant": row.get("Name") or row.get("Description"),
                "category": category,
                "amount": round(amount, 2),
                "card_id": card["id"] if card else None,
                "card_name": card["name"] if card else row.get("Account Name"),
                "owner": card["owner"] if card else "unknown",
            })
    return purchases


def _in_period(purchase, start, end):
    return (start is None or purchase["date"] >= start) and (end is None or purchase["date"] <= end)


# --- the calculations the tools use -------------------------------------------------

def best_card_for(category, owner=None):
    """Rank cards for a category. If owner is given, only that person's cards."""
    cards = [c for c in load_cards() if owner is None or c["owner"].lower() == owner.lower()]
    ranked = sorted(cards, key=lambda c: cents_per_dollar(c, category), reverse=True)
    return [{"card": c["name"], "owner": c["owner"],
             "value_per_100_dollars": round(cents_per_dollar(c, category), 2)} for c in ranked]


def spending_summary(start=None, end=None, group_by="category"):
    """Total spending grouped by category, card or owner."""
    key = {"category": "category", "card": "card_name", "owner": "owner"}[group_by]
    totals = {}
    for p in load_purchases():
        if _in_period(p, start, end):
            totals[p[key]] = round(totals.get(p[key], 0) + p["amount"], 2)
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


def missed_rewards(start=None, end=None, top=5):
    """Value lost by not using the best household card for each purchase."""
    missed_total, examples = 0.0, []
    cards = load_cards()
    for p in load_purchases():
        if not _in_period(p, start, end) or p["card_id"] is None:
            continue
        used = next(c for c in cards if c["id"] == p["card_id"])
        best = max(cards, key=lambda c: cents_per_dollar(c, p["category"]))
        lost = p["amount"] * (cents_per_dollar(best, p["category"]) - cents_per_dollar(used, p["category"])) / 100
        if lost > 0.005:
            missed_total += lost
            examples.append({"date": p["date"], "merchant": p["merchant"], "amount": p["amount"],
                             "used": used["name"], "better": best["name"], "missed_dollars": round(lost, 2)})
    examples.sort(key=lambda e: e["missed_dollars"], reverse=True)
    return {"missed_dollars": round(missed_total, 2), "biggest_examples": examples[:top]}


def _load_credit_log():
    if CREDITS_FILE.exists():
        return json.loads(CREDITS_FILE.read_text())
    return {}


def _period_start(period, today):
    """When the current period began. Annual = calendar year (some cards use card-member year)."""
    if period == "monthly":
        return today.replace(day=1)
    if period == "quarterly":
        return today.replace(month=3 * ((today.month - 1) // 3) + 1, day=1)
    if period == "semiannual":
        return today.replace(month=1 if today.month <= 6 else 7, day=1)
    if period == "every_4_years":
        return today.replace(year=today.year - 4)
    return today.replace(month=1, day=1)


def _period_end(period, start):
    """Last day before the benefit resets."""
    months = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}.get(period)
    if months is None:
        return None
    m = start.month - 1 + months
    nxt = start.replace(year=start.year + m // 12, month=m % 12 + 1, day=1)
    return nxt - timedelta(days=1)


def credits_remaining(today=None):
    """Every benefit and whether it's been used this period.

    Dollar credits show used and left. Perks without a dollar amount (lounge pass,
    free night) show used: true/false.
    """
    today = today or date.today()
    log = _load_credit_log()
    result = []
    for card in load_cards():
        for credit in card["credits"]:
            start = _period_start(credit["period"], today)
            uses = [u for u in log.get(credit["id"], []) if u["date"] >= start.isoformat()]
            end = _period_end(credit["period"], start)
            item = {"card": card["name"], "card_id": card["id"], "owner": card["owner"],
                    "credit": credit["name"], "id": credit["id"], "resets": credit["period"],
                    "period_ends": end.isoformat() if end else None,
                    "days_left_in_period": (end - today).days if end else None}
            if credit.get("amount"):
                used = round(sum(u["amount"] for u in uses), 2)
                item.update({"amount": credit["amount"], "used": used,
                             "left": round(max(credit["amount"] - used, 0), 2),
                             "fully_used": used >= credit["amount"]})
            else:
                item.update({"amount": None, "used": bool(uses), "left": None, "fully_used": bool(uses)})
            result.append(item)
    return result


def record_credit_use(credit_id, amount=None, on_date=None):
    """Write down that a benefit was used. For a dollar credit with no amount given, uses what's left."""
    status = next((c for c in credits_remaining() if c["id"] == credit_id), None)
    if status is None:
        valid = sorted(c["id"] for c in credits_remaining())
        return {"error": f"Unknown benefit. Valid ids: {valid}"}
    if amount is None:
        amount = status["left"] if status["amount"] else 0
    log = _load_credit_log()
    log.setdefault(credit_id, []).append({"date": on_date or date.today().isoformat(), "amount": float(amount)})
    CREDITS_FILE.write_text(json.dumps(log, indent=2))
    return {"recorded": True, "credit_id": credit_id, "amount": float(amount)}


def undo_last_use(credit_id):
    """Remove the most recent use of a benefit (for fixing a mistaken click)."""
    log = _load_credit_log()
    if not log.get(credit_id):
        return {"error": "Nothing to undo for that benefit."}
    removed = log[credit_id].pop()
    CREDITS_FILE.write_text(json.dumps(log, indent=2))
    return {"undone": True, "removed": removed}


def next_fee_date(renewal_date, today):
    """Next time the annual fee is charged, from the renewal date's month and day."""
    r = date.fromisoformat(renewal_date)
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, r.month, r.day)
        except ValueError:  # Feb 29 in a non-leap year
            candidate = date(year, r.month, 28)
        if candidate >= today:
            return candidate


def upcoming_fees(today=None, days_ahead=90):
    """Annual fees charged in the next N days, soonest first."""
    today = today or date.today()
    upcoming = []
    for card in load_cards():
        if card["annual_fee"] and card.get("renewal_date"):
            due = next_fee_date(card["renewal_date"], today)
            days = (due - today).days
            if days <= days_ahead:
                upcoming.append({"card": card["name"], "owner": card["owner"], "fee": card["annual_fee"],
                                 "fee_date": due.isoformat(), "days_away": days})
    upcoming.sort(key=lambda u: u["days_away"])
    missing = [c["name"] for c in load_cards() if c["annual_fee"] and not c.get("renewal_date")]
    return {"upcoming": upcoming, "fee_cards_missing_renewal_date": missing}


def card_value_review(card_id, today=None):
    """Is this card worth its fee? Rewards earned vs a no-fee 2% card, plus credits used, minus the fee."""
    cards = load_cards()
    card = next((c for c in cards if c["id"] == card_id), None)
    if card is None:
        return {"error": f"Unknown card. Valid ids: {[c['id'] for c in cards]}"}
    today = today or date.today()
    year_start = today.replace(month=1, day=1).isoformat()
    spend = [p for p in load_purchases() if p["card_id"] == card_id and p["date"] >= year_start]
    rewards = sum(p["amount"] * cents_per_dollar(card, p["category"]) / 100 for p in spend)
    vs_2pct_card = rewards - sum(p["amount"] for p in spend) * 0.02
    log = _load_credit_log()
    credits_used = sum(u["amount"] for c in card["credits"] if c.get("amount")
                       for u in log.get(c["id"], []) if u["date"] >= year_start)
    return {"card": card["name"], "annual_fee": card["annual_fee"],
            "spend_this_year": round(sum(p["amount"] for p in spend), 2),
            "rewards_value": round(rewards, 2),
            "extra_vs_no_fee_2pct_card": round(vs_2pct_card, 2),
            "credits_used_this_year": round(credits_used, 2),
            "net_value": round(vs_2pct_card + credits_used - card["annual_fee"], 2),
            "note": "Data covers only the transactions in your export file."}
