"""Card-linked offers (like Amex Offers or Chase Offers): "$20 back on $100 at Whole Foods".

These are different for every person and change every month, and banks don't share them
with outside apps. So they come in two ways:
  1. Type them in (dashboard or assistant).
  2. Screenshot the offers page in your bank's app and let Claude read it:
        python offers.py import offers_screenshot.png --card gold
     It shows what it found and saves only after you say yes.

Offers are saved in offers.json (private, kept off GitHub).
"""

import argparse
import base64
import json
import mimetypes
import re
from datetime import date
from pathlib import Path

import cards

OFFERS_FILE = Path(__file__).parent / "offers.json"


def _load():
    return json.loads(OFFERS_FILE.read_text(encoding="utf-8")) if OFFERS_FILE.exists() else []


def _save(offers):
    OFFERS_FILE.write_text(json.dumps(offers, indent=2), encoding="utf-8")


def add_offer(card_id, merchant, deal, expires=None, screenshot=None):
    if not any(c["id"] == card_id for c in cards.load_cards()):
        return {"error": "Unknown card."}
    if not merchant.strip() or not deal.strip():
        return {"error": "Offers need a merchant and a deal."}
    if expires:
        try:
            date.fromisoformat(expires)
        except ValueError:
            return {"error": "expires must be YYYY-MM-DD"}
    offers = _load()
    offer_id = re.sub(r"[^a-z0-9]+", "_", f"{card_id} {merchant}".lower()).strip("_")
    base, n = offer_id, 2
    while any(o["id"] == offer_id for o in offers):
        offer_id, n = f"{base}_{n}", n + 1
    offer = {"id": offer_id, "card_id": card_id, "merchant": merchant.strip(), "deal": deal.strip(),
             "expires": expires, "status": "active", "added": date.today().isoformat(),
             "screenshot": screenshot}
    offers.append(offer)
    _save(offers)
    return {"saved": True, "offer": offer}


def list_offers(card_id=None, merchant=None, include_inactive=False, today=None):
    """Offers, soonest-ending first. Expired ones are marked automatically."""
    today = today or date.today()
    names = {c["id"]: (c["name"], c["owner"]) for c in cards.load_cards()}
    result = []
    for o in _load():
        o = dict(o)
        if o["status"] == "active" and o.get("expires") and o["expires"] < today.isoformat():
            o["status"] = "expired"
        if card_id and o["card_id"] != card_id:
            continue
        if merchant and merchant.lower() not in o["merchant"].lower():
            continue
        if o["status"] != "active" and not include_inactive:
            continue
        o["days_left"] = (date.fromisoformat(o["expires"]) - today).days if o.get("expires") else None
        o["card"], o["owner"] = names.get(o["card_id"], (o["card_id"], "?"))
        result.append(o)
    return sorted(result, key=lambda o: (o["days_left"] is None, o["days_left"] or 0))


def set_offer_status(offer_id, status):
    offers = _load()
    for o in offers:
        if o["id"] == offer_id:
            o["status"] = status
            _save(offers)
            return {"saved": True, "offer": o}
    return {"error": "Unknown offer."}


def remove_offer(offer_id):
    offers = _load()
    kept = [o for o in offers if o["id"] != offer_id]
    if len(kept) == len(offers):
        return {"error": "Unknown offer."}
    _save(kept)
    return {"removed": True}


# --- reading offers from a screenshot -----------------------------------------------

READ_TOOL = {
    "name": "save_offers",
    "description": "Record every offer visible in the screenshot.",
    "input_schema": {
        "type": "object",
        "properties": {
            "offers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "merchant": {"type": "string"},
                        "deal": {"type": "string", "description": "The offer in short words, e.g. 'Spend $50, get $10 back'"},
                        "expires": {"type": "string", "description": "YYYY-MM-DD if shown, otherwise empty"},
                    },
                    "required": ["merchant", "deal"],
                },
            }
        },
        "required": ["offers"],
    },
}


def read_screenshot(image_path, client, model):
    """Ask Claude to list the offers in a screenshot. Returns a draft list; nothing is saved."""
    media_type = mimetypes.guess_type(image_path)[0] or "image/png"
    image = base64.b64encode(Path(image_path).read_bytes()).decode()
    response = client.messages.create(
        model=model, max_tokens=4000,
        tools=[READ_TOOL], tool_choice={"type": "tool", "name": "save_offers"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image}},
            {"type": "text", "text": f"This is a credit card app's offers page. List every offer you can read. "
                                     f"Today is {date.today().isoformat()}; write expiry dates as YYYY-MM-DD. "
                                     f"Only include what's actually shown."},
        ]}],
    )
    block = next(b for b in response.content if b.type == "tool_use")
    return block.input["offers"]


if __name__ == "__main__":
    import anthropic

    from agent import MODEL

    parser = argparse.ArgumentParser(description="Import offers from a screenshot.")
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("image")
    imp.add_argument("--card", required=True, help="Card id, e.g. gold (see cards.json)")
    args = parser.parse_args()

    if not any(c["id"] == args.card for c in cards.load_cards()):
        raise SystemExit(f"Unknown card. Card ids: {[c['id'] for c in cards.load_cards()]}")
    found = read_screenshot(args.image, anthropic.Anthropic(), MODEL)
    if not found:
        raise SystemExit("No offers found in that screenshot.")
    print(f"Found {len(found)} offers:")
    for i, o in enumerate(found, 1):
        print(f"  {i}. {o['merchant']}: {o['deal']}" + (f" (until {o['expires']})" if o.get("expires") else ""))
    if input("Save these? (y/n) ").strip().lower() == "y":
        for o in found:
            result = add_offer(args.card, o["merchant"], o["deal"], o.get("expires") or None)
            if "error" in result:
                print(f"  Skipped {o['merchant']}: {result['error']}")
        print("Saved. Open the dashboard to see them.")


def edit_offer(offer_id, changes):
    """Fix an offer's store, deal or end date."""
    if changes.get("expires"):
        try:
            date.fromisoformat(changes["expires"])
        except ValueError:
            return {"error": "expires must be YYYY-MM-DD"}
    all_offers = _load()
    for o in all_offers:
        if o["id"] == offer_id:
            o.update({k: (v or None) if k == "expires" else v for k, v in changes.items()
                      if k in ("merchant", "deal", "expires") and (k == "expires" or str(v).strip())})
            _save(all_offers)
            return {"saved": True, "offer": o}
    return {"error": "Unknown offer."}


def remove_offers_for_card(card_id):
    _save([o for o in _load() if o["card_id"] != card_id])
