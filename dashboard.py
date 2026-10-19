"""Your cards at a glance.

Run:  python dashboard.py
Opens http://localhost:8765 in your browser. Only works on your own computer.

Two pages:
  Summary    every card in one grid, sorted by renewal date
  Card page  click a card: its benefits, offers, offer screenshots, and editing

It reads and writes the same files as the assistant (agent.py), so both stay in sync.
"""

import base64
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cards
import finance
import offers
import suggest
from lookup import lookup_card

PORT = 8765
HERE = Path(__file__).parent
SCREENSHOTS = HERE / "screenshots"          # private, kept off GitHub
MAX_IMAGE_BYTES = 5 * 1024 * 1024           # Claude's limit per image is about 5 MB
CATEGORY_LABEL = {"flights": "flights", "hotels": "hotels", "travel": "other travel", "groceries": "groceries",
                  "dining": "dining", "gas": "gas", "streaming": "streaming", "amazon": "Amazon & Whole Foods",
                  "other": "everything else"}
# The "Best card for" strip at the top of the summary, in this order
BEST_FOR = [("flights", "Flights"), ("hotels", "Hotels"), ("travel", "Other travel"), ("dining", "Dining"),
            ("groceries", "Groceries"), ("amazon", "Amazon & Whole Foods"), ("gas", "Gas"), ("other", "Everyday shopping")]


def best_by_category(all_cards):
    """For each category, each person's best card. Ties are listed together."""
    out = []
    for category, label in BEST_FOR:
        per_owner = []
        for who in sorted({c["owner"] for c in all_cards}, key=str.lower):
            mine = [c for c in all_cards if c["owner"] == who]
            top = max(finance.cents_per_dollar(c, category) for c in mine)
            winners = [c for c in mine if finance.cents_per_dollar(c, category) == top]
            rate = winners[0]["earn"].get(category) or (winners[0]["earn"].get("travel") if category in ("flights", "hotels") else None) \
                or winners[0]["earn"].get("other", 1)
            per_owner.append({"owner": who, "cards": [{"id": c["id"], "name": c["name"]} for c in winners],
                              "rate": rate, "cents": round(top, 2)})
        out.append({"category": category, "label": label, "best": per_owner})
    return out


# --- what the page shows ---------------------------------------------------------------

def use_for(card, all_cards):
    """What this card is for, highest rate first. 'best' = top card for its owner in that category."""
    mine = [c for c in all_cards if c["owner"] == card["owner"]]
    out = []
    base = card["earn"].get("other", 1)
    bonus = {k: v for k, v in card["earn"].items() if k != "other" and v > base}
    for category, rate in sorted(bonus.items() or [("other", base)], key=lambda kv: -kv[1]):
        best = max(finance.cents_per_dollar(c, category) for c in mine)
        out.append({"label": CATEGORY_LABEL.get(category, category), "rate": rate,
                    "best": finance.cents_per_dollar(card, category) >= best})
    return out


def screenshots_for(card_id):
    if not SCREENSHOTS.exists():
        return []
    return sorted((p.name for p in SCREENSHOTS.glob(f"{card_id}__*")), reverse=True)


def state():
    today = date.today()
    benefits = finance.credits_remaining(today)
    all_cards = cards.load_cards()
    out = []
    for card in all_cards:
        mine = [b for b in benefits if b["card_id"] == card["id"]]
        next_fee = None
        if card.get("annual_fee") and card.get("renewal_date"):
            due = finance.next_fee_date(card["renewal_date"], today)
            next_fee = {"date": due.isoformat(), "days": (due - today).days}
        out.append({**card, "next_fee": next_fee, "benefits": mine,
                    "offers": offers.list_offers(card_id=card["id"]),
                    "use_for": use_for(card, all_cards),
                    "unused_dollars": round(sum(b["left"] for b in mine if b["amount"]), 2),
                    "unused_perks": sum(1 for b in mine if not b["amount"] and not b["used"]),
                    "screenshots": screenshots_for(card["id"]),
                    "lookup": LOOKUPS.get(card["id"])})
    return {"today": today.isoformat(), "cards": out, "periods": cards.PERIODS,
            "has_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "best_for": best_by_category(all_cards) if all_cards else [],
            "ideas": {**IDEAS, "data": suggest.load_suggestions()}}


# --- actions -------------------------------------------------------------------------------

ENV_FILE = HERE / ".env"  # your API key, saved from the dashboard. Private: listed in .gitignore.


def _load_env_file():
    """Use the key saved in .env if the computer doesn't already have one set."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() and not os.environ.get(key.strip()):
                os.environ[key.strip()] = value.strip().strip('"')


_load_env_file()


def save_key(d):
    """Save the API key pasted into the dashboard, on this computer only."""
    key = str(d.get("key", "")).strip()
    if not key.startswith("sk-ant-") or len(key) < 30:
        return {"error": "That doesn't look like an Anthropic API key. It starts with sk-ant-"}
    ENV_FILE.write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")
    os.environ["ANTHROPIC_API_KEY"] = key
    return {"saved": True}


def _client():
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("No API key yet. Paste yours in the box at the top of the summary page.")
    return anthropic.Anthropic()


# Lookups run in the background, so leaving or refreshing the page doesn't lose them.
# card_id -> {"status": "running"} or {"status": "error", "message": ...}. Kept in memory only.
LOOKUPS = {}
_LOOKUP_LOCK = threading.Lock()


def _run_lookup(card_id, name):
    """Look the card up online and save what was found. Runs in its own thread."""
    from agent import MODEL

    try:
        draft = lookup_card(name, _client(), MODEL)
        if "error" in draft:
            problem = draft["error"]
        else:
            saved = apply_lookup({"card_id": card_id, "draft": draft})
            problem = None if saved.get("saved") else ". ".join(saved.get("problems") or ["Couldn't save the details."])
    except Exception as exc:
        problem = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
    print(f"Lookup for {name}: {'saved' if not problem else 'failed: ' + problem}")
    with _LOOKUP_LOCK:
        if problem:
            LOOKUPS[card_id] = {"status": "error", "message": problem}
        else:
            LOOKUPS.pop(card_id, None)


# "New card ideas" search, also in the background. {"status": "running"} or {"status": "error", ...}
IDEAS = {}


def _run_ideas():
    from agent import MODEL

    try:
        found = suggest.find_suggestions([c["name"] for c in cards.load_cards()], _client(), MODEL)
        if "error" in found:
            raise RuntimeError(found["error"])
        suggest.save_suggestions(found)
        IDEAS.clear()
        print("New card ideas: saved")
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
        IDEAS.update({"status": "error", "message": message})
        print("New card ideas failed:", message)


def find_ideas(d):
    _client()  # stop now with a clear message if there's no API key
    with _LOOKUP_LOCK:
        if IDEAS.get("status") == "running":
            return {"started": True}
        IDEAS.clear()
        IDEAS["status"] = "running"
    threading.Thread(target=_run_ideas, daemon=True).start()
    return {"started": True}


# --- chat: the agent loop from agent.py, inside the page ---------------------------------
CHATS = {}  # chat_id -> the conversation sent to Claude. In memory: restarting the dashboard clears it.

STEP_LABELS = {
    "list_cards": "Looked at your card list",
    "credits_remaining": "Checked which credits are unused",
    "upcoming_fees": "Checked upcoming annual fees",
    "spending_summary": "Added up your spending",
    "missed_rewards": "Worked out rewards you missed",
    "list_offers": "Checked your saved offers",
    "card_ideas": "Looked at saved new card ideas",
}


def describe_step(name, args):
    """Plain-English label for one step, shown under the answer."""
    if name == "web_search":
        return f'Searched the web: "{args.get("query", "")}"'
    if name == "best_card_for":
        who = f" for {args['owner']}" if args.get("owner") else ""
        return f"Ranked cards for {args.get('category', '?')}{who}"
    if name == "record_credit_use":
        return f"Marked a benefit used ({args.get('credit_id', '?')})"
    if name == "undo_last_use":
        return f"Undid a benefit use ({args.get('credit_id', '?')})"
    if name == "card_value_review":
        return f"Compared {args.get('card_id', 'a card')}'s value to its fee"
    if name == "add_card":
        return f"Saved a new card: {args.get('name', '?')}"
    if name == "update_card":
        return f"Changed card {args.get('card_id', '?')}: {', '.join(args.get('changes', {}))}"
    if name == "add_offer":
        return f"Saved an offer: {args.get('merchant', '?')}"
    return STEP_LABELS.get(name, name)


def chat(d):
    from agent import run_agent

    text = str(d.get("text", "")).strip()
    if not text:
        return {"error": "Type a question first."}
    client = _client()
    messages = CHATS.setdefault(str(d.get("chat_id", "default")), [])
    messages.append({"role": "user", "content": text})
    steps = []
    reply = run_agent(client, messages, show_tools=False, on_step=lambda n, a: steps.append(describe_step(n, a)))
    return {"reply": reply, "steps": steps}


def lookup(d):
    """Start looking a card up online. Returns right away; the result is saved when it's ready."""
    card = next((c for c in cards.load_cards() if c["id"] == d["card_id"]), None)
    if card is None:
        return {"error": "Unknown card."}
    _client()  # stop now with a clear message if there's no API key
    with _LOOKUP_LOCK:
        if LOOKUPS.get(card["id"], {}).get("status") == "running":
            return {"started": True}
        LOOKUPS[card["id"]] = {"status": "running"}
    print(f"Looking up {card['name']}...")
    threading.Thread(target=_run_lookup, args=(card["id"], card["name"]), daemon=True).start()
    return {"started": True}


def apply_lookup(d):
    """Save looked-up details onto the card, flagged for the person to check."""
    draft = d["draft"]
    changes = {k: draft[k] for k in ("annual_fee", "earn", "credits", "key_benefits", "point_value", *cards.FEE_FIELDS)
               if k in draft}
    earn = {k: v for k, v in (changes.get("earn") or {}).items() if k in cards.CATEGORIES}
    earn.setdefault("other", 1)
    changes["earn"] = earn
    changes["credits"] = [c for c in changes.get("credits", []) if c.get("period") in cards.PERIODS and c.get("name")]
    changes["source"] = f"{draft.get('source_url', 'web search')} (checked {date.today().isoformat()})"
    changes["needs_review"] = True
    changes["lookup_notes"] = draft.get("notes") or ""
    return cards.update_card(d["card_id"], changes)


def import_screenshot(d):
    """Save the screenshot under the card, then have Claude read the offers in it. Returns a draft."""
    from agent import MODEL

    if not any(c["id"] == d["card_id"] for c in cards.load_cards()):
        return {"error": "Unknown card."}
    raw = base64.b64decode(d["data"])
    if len(raw) > MAX_IMAGE_BYTES:
        return {"error": "That screenshot is over 5 MB. Crop it or take a smaller one."}
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(d.get("media_type"))
    if ext is None:
        return {"error": "Please upload a PNG, JPG, WEBP or GIF image."}
    SCREENSHOTS.mkdir(exist_ok=True)
    stamp, n = date.today().isoformat(), 1
    while (SCREENSHOTS / f"{d['card_id']}__{stamp}_{n}{ext}").exists():
        n += 1
    path = SCREENSHOTS / f"{d['card_id']}__{stamp}_{n}{ext}"
    path.write_bytes(raw)
    found = offers.read_screenshot(str(path), _client(), MODEL)
    return {"screenshot": path.name, "offers": found}


def save_offers(d):
    saved = 0
    for o in d["offers"]:
        result = offers.add_offer(d["card_id"], o["merchant"], o["deal"], o.get("expires") or None,
                                  screenshot=d.get("screenshot"))
        saved += "saved" in result
    return {"saved": saved}


def remove_card(d):
    result = cards.remove_card(d["card_id"])
    if result.get("saved"):
        offers.remove_offers_for_card(d["card_id"])
    return result


ACTIONS = {
    "add_card": lambda d: cards.add_card(d["name"].strip(), d["owner"].strip(), renewal_date=d.get("renewal_date") or None),
    "update_card": lambda d: cards.update_card(d["card_id"], d["changes"]),
    "remove_card": remove_card,
    "lookup": lookup,
    "review_ok": lambda d: cards.update_card(d["card_id"], {"needs_review": False}),
    "find_ideas": find_ideas,
    "save_key": save_key,
    "chat": chat,
    "chat_reset": lambda d: {"ok": CHATS.pop(str(d.get("chat_id", "")), None) is not None},
    "add_benefit": lambda d: cards.add_benefit(d["card_id"], d["name"], d["period"], d.get("amount")),
    "edit_benefit": lambda d: cards.edit_benefit(d["card_id"], d["benefit_id"], d["changes"]),
    "remove_benefit": lambda d: cards.remove_benefit(d["card_id"], d["benefit_id"]),
    "use": lambda d: finance.record_credit_use(d["benefit_id"], d.get("amount")),
    "undo": lambda d: finance.undo_last_use(d["benefit_id"]),
    "add_offer": lambda d: offers.add_offer(d["card_id"], d["merchant"], d["deal"], d.get("expires") or None),
    "edit_offer": lambda d: offers.edit_offer(d["offer_id"], d["changes"]),
    "import_screenshot": import_screenshot,
    "save_offers": save_offers,
    "offer_used": lambda d: offers.set_offer_status(d["offer_id"], "used"),
    "offer_remove": lambda d: offers.remove_offer(d["offer_id"]),
}


# Share mode (python dashboard.py --share): other devices can connect, so every request needs the password.
SHARE_PASSWORD = None


def _password_ok(header):
    """Check the browser's login (HTTP Basic auth). Any username works; only the password matters."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    return hmac.compare_digest(given.encode(), SHARE_PASSWORD.encode())


class Handler(BaseHTTPRequestHandler):
    def _locked(self):
        """In share mode, ask for the password and stop if it's missing or wrong."""
        if SHARE_PASSWORD is None or _password_ok(self.headers.get("Authorization")):
            return False
        if self.client_address[0] in ("127.0.0.1", "::1"):  # the laptop itself never needs the password
            return False
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Card dashboard"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _send(self, body, content_type="application/json", status=200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self._locked():
            return
        if self.path == "/api/state":
            return self._send(json.dumps(state(), default=str))
        if self.path in ("/", "/index.html"):
            return self._send(PAGE, "text/html; charset=utf-8")
        match = re.fullmatch(r"/screenshots/([A-Za-z0-9_\-]+\.(?:png|jpg|jpeg|webp|gif))", self.path)
        if match and (SCREENSHOTS / match.group(1)).exists():
            path = SCREENSHOTS / match.group(1)
            return self._send(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/png")
        self._send(json.dumps({"error": "Not found"}), status=404)

    def do_POST(self):
        if self._locked():
            return
        name = self.path.removeprefix("/api/")
        if name == "save_key" and self.client_address[0] not in ("127.0.0.1", "::1"):
            return self._send(json.dumps({"error": "Add the API key from the laptop itself, not a phone."}), status=403)
        if name not in ACTIONS:
            return self._send(json.dumps({"error": "Unknown action"}), status=404)
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
            result = ACTIONS[name](payload)
        except Exception as exc:
            result = {"error": str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"}
        self._send(json.dumps(result, default=str))

    def log_message(self, *args):
        pass  # keep the terminal quiet


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Our Cards</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#EEF1F4; --surface:#FFFFFF; --ink:#1B2430; --muted:#5B6675; --line:#E1E5EB;
  --p0:#1F6F78; --p1:#6E3F8F; --p2:#9A4A1F; --p3:#2B5DA8; --p4:#4A6B2A; --p5:#A3355E; --warn:#B7791F; --warn-bg:#FBF1DE; --ok:#2F7A4A;
  --danger:#B42318; --meter:#E6EAEF; --on-accent:#FFFFFF; --hover:#F6F8FA;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#141A21; --surface:#1C242D; --ink:#E7ECF1; --muted:#9AA6B4; --line:#2B3541;
    --p0:#4FB3BD; --p1:#B48BD6; --p2:#E59A6B; --p3:#7FA8E8; --p4:#9CC77A; --p5:#E68AAE; --warn:#E3A947; --warn-bg:#3A2E1A; --ok:#6CC28C;
    --danger:#F97066; --meter:#2A3440; --on-accent:#0F151B; --hover:#222C36; }
}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 "Public Sans",system-ui,sans-serif;
  font-variant-numeric:tabular-nums}
main{max-width:1320px;margin:0 auto;padding:36px 16px 80px}
h1{font-size:28px;line-height:1.2;margin:0;font-weight:700}
h2{font-size:17px;margin:0;font-weight:600}
a{color:inherit}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--p0);outline-offset:2px}
.top{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;justify-content:space-between}
.lede{color:var(--muted);margin:8px 0 24px;max-width:72ch}
.lede strong{color:var(--ink);font-weight:600}
.filter{display:flex;background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:2px}
.filter button{border:0;background:none;padding:5px 14px;border-radius:999px;font-size:14px;color:var(--muted);cursor:pointer}
.filter button[aria-pressed="true"]{background:var(--ink);color:var(--surface)}
.panel{background:var(--surface);border-radius:12px;padding:6px 0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--who);margin-right:8px;vertical-align:1px}
.who,.muted{color:var(--muted)}
.soon{color:var(--warn);font-weight:600}

.attention{margin:0 0 20px;padding:12px 18px;border-radius:12px;background:var(--warn-bg)}
.attention h2{font-size:15px;margin-bottom:4px}
.attention ul{margin:0;padding-left:18px;font-size:14px}
.attention li{margin:2px 0}

.grid .row{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,.7fr) minmax(0,.9fr) minmax(0,.7fr) minmax(0,1.4fr) minmax(0,2fr) minmax(0,.7fr) minmax(0,.9fr) minmax(0,1fr) minmax(0,1fr);gap:12px;align-items:center;padding:12px 20px}
.grid .head{font-size:13px;color:var(--muted);padding-top:10px;padding-bottom:6px}
.grid .item{border-top:1px solid var(--line);cursor:pointer;text-decoration:none;color:inherit}
.grid .item:hover{background:var(--hover)}
.grid .name{font-weight:600}
.num{text-align:right}
.uses{font-size:14px}
.fees{font-size:14px}
.keybox{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--ink);border-radius:12px;padding:14px 16px;margin:0 0 18px}
.bestwrap{margin:0 0 20px}
.bestwrap h2{font-size:16px;margin:0}
.sec-top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:0 0 10px;flex-wrap:wrap}
.ideas-grid .row{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1.5fr) minmax(0,1.1fr) minmax(0,.7fr) minmax(0,1.3fr) minmax(0,.5fr);gap:12px;padding:10px 0;align-items:start;font-size:14px}
.ideas-grid .head{font-size:13px;color:var(--muted)}
.bank-grid .row{grid-template-columns:minmax(0,1.3fr) minmax(0,.8fr) minmax(0,2fr) minmax(0,1.4fr) minmax(0,1.3fr) minmax(0,.5fr)}
.ideas-grid .idea{border-top:1px solid var(--line)}
.ideas-grid .lbl{display:none}
.idea-note{font-size:13px;margin-top:2px}
.idea-pick{font-size:14px;padding:3px 0}
.good{color:var(--ok, #2F7A4A);font-weight:600}
.bestfor{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.bf{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 14px;min-width:0}
.bf h3{font-size:13px;color:var(--muted);font-weight:600;margin:0 0 6px;text-transform:uppercase;letter-spacing:.04em}
.bf-row{display:flex;gap:8px;align-items:flex-start;font-size:14px;padding:3px 0}
.bf-row .dot{margin:6px 0 0;flex:none}
.bf-row a{color:inherit;font-weight:600;text-decoration:none}
.bf-row a:hover{text-decoration:underline}
.bf-rate{font-size:12.5px}
.bf-note{font-size:13px;margin:8px 0 0}
.fees .lbl{display:none}
.perks{font-size:13.5px;color:var(--muted);line-height:1.45}
.uses b{font-weight:600}
.uses .best b{color:var(--who)}
.add-row{border-top:1px solid var(--line);padding:12px 20px}

.back{display:inline-block;margin-bottom:18px;font-size:14px;color:var(--muted);text-decoration:none}
.back:hover{color:var(--ink)}
.detail{background:var(--surface);border-radius:12px;padding:26px 28px}
.detail-head{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px 24px;align-items:flex-start}
.detail-head h1{font-size:24px}
.fee{font-size:22px;font-weight:600;text-align:right}
.fee small{display:block;font-size:13px;font-weight:400}
.facts{margin:10px 0 0;font-size:15px}
.facts p{margin:4px 0}
section{margin-top:28px}
.sec-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px;
  padding-bottom:8px;border-bottom:1px solid var(--line)}
.sec-head p{margin:0;font-size:13px;color:var(--muted)}
.line{display:grid;grid-template-columns:1fr auto;gap:4px 16px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line)}
.line:last-child{border-bottom:0}
.what{font-weight:500}
.state{font-size:14px;color:var(--muted)}
.line.flag .state{color:var(--warn);font-weight:600}
.meter{height:5px;border-radius:999px;background:var(--meter);margin-top:6px;max-width:260px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--who)}
.done{color:var(--ok);font-weight:600}
.acts{display:flex;gap:4px;align-items:center;justify-content:flex-end;flex-wrap:wrap}
.btn{border:1px solid var(--line);background:var(--surface);border-radius:7px;padding:5px 12px;font-size:14px;cursor:pointer}
.btn:hover{background:var(--hover)}
.btn.primary{background:var(--who,var(--ink));border-color:var(--who,var(--ink));color:var(--on-accent)}
.btn.quiet{border-color:transparent;background:none;color:var(--muted);text-decoration:underline;padding:5px 6px}
.btn.danger{border-color:var(--danger);color:var(--danger);background:none}
.btn.danger.solid{background:var(--danger);color:#fff}
.offer{display:grid;grid-template-columns:1fr auto;gap:4px 16px;align-items:center;
  border:1.5px dashed var(--line);border-radius:10px;padding:10px 14px;margin-top:10px}
.offer.ending{border-color:var(--warn);background:var(--warn-bg)}
.offer .state a{color:var(--muted)}
.empty{color:var(--muted);font-size:14px;margin:12px 0 0}
.form{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px}
.form input,.form select{border:1px solid var(--line);border-radius:7px;padding:6px 10px;background:var(--surface);font-size:14px}
.form label{display:flex;gap:6px;align-items:center;font-size:14px;color:var(--muted)}
.review{margin-top:12px;padding:14px 16px;border-radius:10px;background:var(--bg)}
.review h3{font-size:15px;margin:0 0 8px}
.review label{display:flex;gap:10px;align-items:flex-start;font-size:14px;padding:4px 0}
.review p{margin:4px 0;font-size:14px}
.thumbs{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px;font-size:14px}
details{margin-top:28px;border-top:1px solid var(--line);padding-top:14px}
summary{cursor:pointer;color:var(--muted);font-size:14px}
details h3{font-size:14px;margin:20px 0 0;font-weight:600}
.edit-benefit{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}
.edit-benefit input,.edit-benefit select{border:1px solid var(--line);border-radius:7px;padding:5px 8px;background:var(--surface);font-size:14px}
.danger-zone{margin-top:24px;padding-top:14px;border-top:1px solid var(--line)}
.chat-fab{position:fixed;right:20px;bottom:20px;z-index:20;border:0;border-radius:999px;padding:12px 18px;font:inherit;font-weight:600;
  background:var(--ink);color:var(--surface);box-shadow:0 6px 20px rgba(0,0,0,.18);cursor:pointer}
.chat{position:fixed;right:16px;bottom:16px;top:16px;width:min(420px,calc(100vw - 32px));z-index:30;display:flex;flex-direction:column;
  background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.2);overflow:hidden}
.chat[hidden]{display:none!important}
.chat header{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;padding:14px 14px 10px;border-bottom:1px solid var(--line)}
.chat-log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:92%;font-size:14.5px;line-height:1.5;overflow-wrap:anywhere}
.msg .txt{white-space:pre-wrap}
.chat header>div:last-child{display:flex;gap:2px;flex:none}
.msg.you{align-self:flex-end;background:var(--ink);color:var(--surface);padding:8px 12px;border-radius:12px 12px 4px 12px}
.msg.bot{align-self:flex-start}
.steps{margin:6px 0 0;padding:0;list-style:none;font-size:12.5px;color:var(--muted);white-space:normal}
.steps li::before{content:"✓ ";color:var(--ok)}
.quick{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.starter{display:flex;flex-direction:column;gap:6px;align-items:flex-start}
.chat-form{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--line)}
.chat-form input{flex:1;min-width:0;font:inherit;font-size:14.5px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
.thinking{color:var(--muted);font-size:14px}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--ink);color:var(--surface);
  padding:8px 16px;border-radius:8px;font-size:14px;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.show{opacity:1}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
@media (max-width:720px){
  .grid .head{display:none}
  .grid .row{grid-template-columns:1fr auto;gap:2px 12px}
  .grid .item .c-who,.grid .item .c-uses,.grid .item .c-perks,.grid .item .c-unused{grid-column:1/-1}
  .grid .item .c-fx,.grid .item .c-cash,.grid .item .c-ref{grid-column:1/-1}
  .bestfor{grid-template-columns:1fr 1fr}
  .ideas-grid .head{display:none}
  .ideas-grid .row{grid-template-columns:1fr}
  .ideas-grid .lbl{display:inline;color:var(--muted)}
  .fees .lbl{display:inline;color:var(--muted)}
  .num{text-align:left}
  .detail{padding:20px 18px}
  .fee{text-align:left}
}
@media (prefers-reduced-motion:reduce){.toast{transition:none}}
</style>
</head>
<body>
<main id="app">Loading…</main>
<div class="toast" id="toast" role="status"></div>
<button class="chat-fab" id="chatOpen" aria-controls="chat" aria-expanded="false">Ask the assistant</button>
<aside class="chat" id="chat" hidden aria-label="Assistant">
  <header><div><strong>Assistant</strong><div class="muted" style="font-size:12.5px">Answers from your cards. Each question costs a few cents.</div></div>
    <div><button class="btn quiet" id="chatNew">New chat</button><button class="btn quiet" id="chatClose" aria-label="Close">✕</button></div></header>
  <div class="chat-log" id="chatLog" aria-live="polite"></div>
  <form id="chatForm" class="chat-form"><input id="chatInput" placeholder="Ask about your cards…" autocomplete="off" aria-label="Your question">
    <button class="btn primary">Send</button></form>
</aside>
<input type="file" id="file" accept="image/png,image/jpeg,image/webp,image/gif" class="sr" aria-hidden="true" tabindex="-1">
<script>
let data = null, owner = "all";
const offerDrafts = {}, reading = {};
let pollTimer = null;
let showAddCard = false, showOfferForm = false, uploadFor = null, editingOffer = null, confirmRemove = false, keepDetailsOpen = false;
const PERIOD = {monthly: "monthly", quarterly: "quarterly", semiannual: "twice a year", annual: "yearly", every_4_years: "every 4 years"};
const KNOWN = ["flights", "hotels", "travel", "groceries", "dining", "gas", "streaming", "amazon", "other"];
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = n => "$" + Number(n).toLocaleString(undefined, Number(n) % 1 ? {minimumFractionDigits: 2, maximumFractionDigits: 2} : {maximumFractionDigits: 0});
const fmt = iso => new Date(iso + "T12:00").toLocaleDateString(undefined, {month: "short", day: "numeric", year: "numeric"});
const short = iso => new Date(iso + "T12:00").toLocaleDateString(undefined, {month: "short", day: "numeric"});
const owners = () => [...new Set(data.cards.map(c => c.owner))];
const color = o => `var(--p${Math.max(0, owners().indexOf(o)) % 6})`;
const ownerOptions = () => `<datalist id="owners">${owners().map(o => `<option value="${esc(o)}">`).join("")}</datalist>`;
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function toast(m){ const t = $("#toast"); t.textContent = m; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2400); }
async function post(action, body){
  const r = await fetch("/api/" + action, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  return r.json();
}
async function act(action, body){
  const out = await post(action, body);
  if (out.error || out.saved === false){ toast((out.problems || [out.error]).join(". ")); return null; }
  await load(); return out;
}
async function load(){
  data = await (await fetch("/api/state")).json(); render();
  // while a lookup is running, check back every few seconds so its result shows up on its own
  clearTimeout(pollTimer);
  if (data.cards.some(c => c.lookup && c.lookup.status === "running") || data.ideas?.status === "running") pollTimer = setTimeout(load, 3000);
}
const route = () => { const m = location.hash.match(/^#\/card\/(.+)$/); return m ? decodeURIComponent(m[1]) : null; };
window.addEventListener("hashchange", () => { showOfferForm = false; editingOffer = null; confirmRemove = false; keepDetailsOpen = false; render(); window.scrollTo(0, 0); });

/* ---------- summary page ---------- */
function attentionItems(cards){
  const items = [];
  for (const c of cards){
    if (c.next_fee && c.next_fee.days <= 30) items.push([c.next_fee.days, `<strong>${esc(c.name)}</strong> renews in ${plural(c.next_fee.days, "day")}: ${money(c.annual_fee)} fee`]);
    for (const b of c.benefits) if (!b.fully_used && b.days_left_in_period !== null && b.days_left_in_period <= 7)
      items.push([b.days_left_in_period, `<strong>${esc(b.credit)}</strong> on ${esc(c.name)} unused, resets in ${plural(b.days_left_in_period, "day")}`]);
    for (const o of c.offers) if (o.days_left !== null && o.days_left <= 7)
      items.push([o.days_left, `<strong>${esc(o.merchant)}</strong> offer on ${esc(c.name)} ends in ${plural(o.days_left, "day")}`]);
  }
  return items.sort((a, b) => a[0] - b[0]).map(i => i[1]);
}

function usesHtml(c){
  if (!c.use_for.length) return `<span class="muted">Not looked up yet</span>`;
  return c.use_for.slice(0, 3).map(u => `<span class="${u.best ? "best" : ""}"><b>${u.rate}x</b> ${esc(u.label)}</span>`).join(", ");
}

function summary(){
  if (owner !== "all" && !owners().includes(owner)) owner = "all";
  const cards = data.cards.filter(c => owner === "all" || c.owner === owner)
    .sort((a, b) => (a.next_fee?.days ?? 9999) - (b.next_fee?.days ?? 9999) || (b.annual_fee - a.annual_fee) || a.name.localeCompare(b.name));
  const fees = cards.reduce((s, c) => s + (c.annual_fee || 0), 0);
  const unused = cards.reduce((s, c) => s + c.unused_dollars, 0);
  const noDate = cards.filter(c => c.annual_fee && !c.renewal_date).length;
  const att = attentionItems(cards);
  const rows = cards.map(c => {
    const renew = !c.annual_fee ? `<span class="muted">No fee</span>`
      : c.next_fee ? `${short(c.next_fee.date)} <span class="${c.next_fee.days <= 30 ? "soon" : "muted"}">· ${c.next_fee.days}d</span>`
      : `<span class="muted">Add date</span>`;
    const left = c.unused_dollars || c.unused_perks
      ? `${c.unused_dollars ? money(c.unused_dollars) : ""}${c.unused_dollars && c.unused_perks ? " + " : ""}${c.unused_perks ? plural(c.unused_perks, "perk") : ""}`
      : `<span class="muted">None</span>`;
    return `<a class="row item" href="#/card/${encodeURIComponent(c.id)}" style="--who:${color(c.owner)}">
      <span class="name"><span class="dot"></span>${esc(c.name)}${c.lookup && c.lookup.status === "running" ? ` <span class="muted" style="font-weight:400;font-size:13px">· looking up…</span>`
        : c.needs_review ? ` <span class="soon" style="font-size:13px">· check details</span>` : ""}</span>
      <span class="who c-who">${esc(c.owner)}</span>
      <span class="c-renew">${renew}</span>
      <span class="num">${c.annual_fee ? money(c.annual_fee) : "$0"}</span>
      <span class="uses c-uses">${usesHtml(c)}</span>
      <span class="perks c-perks">${perksHtml(c)}</span>
      <span class="fees c-fx"><span class="lbl">Foreign fee </span>${fxHtml(c)}</span>
      <span class="fees c-cash"><span class="lbl">Cash advance </span>${cashHtml(c)}</span>
      <span class="fees c-ref"><span class="lbl">Referral bonus </span>${refHtml(c)}</span>
      <span class="num c-unused">${left}</span></a>`;
  }).join("");
  const addRow = showAddCard ? `<form class="form" id="addCard" style="margin:0">
      <input name="name" placeholder="Card name, e.g. Chase Sapphire Preferred" required style="flex:1;min-width:240px">
      <input name="owner" list="owners" placeholder="Whose card? e.g. Alex" required aria-label="Whose card" style="width:170px">${ownerOptions()}
      <input name="renewal_date" type="date" title="Renewal date (optional)" aria-label="Renewal date, optional">
      <button class="btn primary">Add and look up</button>
      <button class="btn quiet" type="button" data-cancel-add>Cancel</button></form>
      <p class="empty" style="margin-top:8px">The fee and benefits are looked up online for you to check. The renewal date is optional.</p>`
    : `<button class="btn" data-show-add>Add a card</button>`;
  return `<div class="top"><h1>Our cards</h1>
      <div class="filter" role="group" aria-label="Show cards for" ${owners().length > 1 ? "" : "hidden"}>
        ${(owners().length > 1 ? ["all", ...owners()] : []).map(o => `<button data-owner="${esc(o)}" aria-pressed="${owner === o}">${o === "all" ? "Everyone" : esc(o)}</button>`).join("")}
      </div></div>
    <p class="lede"><strong>${plural(cards.length, "card")}</strong>, <strong>${money(fees)}</strong> in annual fees a year,
      and <strong>${money(unused)}</strong> in credits still unused this period.${noDate ? ` ${plural(noDate, "card")} still need${noDate === 1 ? "s" : ""} a renewal date.` : ""}</p>
    ${data.has_key ? "" : `<form class="keybox" id="keyForm"><div><strong>Turn on the AI features</strong>
        <p class="muted" style="margin:2px 0 8px;font-size:14px">Looking up cards, reading screenshots, new card ideas and the chat need an
        <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">Anthropic API key</a> (pay-as-you-go, a few cents per use).
        It's saved only on this computer and never uploaded.</p>
        <div class="form" style="margin:0"><input name="key" type="password" placeholder="sk-ant-…" autocomplete="off" required style="flex:1;min-width:220px" aria-label="Anthropic API key">
        <button class="btn primary">Save key</button></div></div></form>`}
    ${att.length ? `<div class="attention"><h2>Needs attention</h2><ul>${att.map(a => `<li>${a}</li>`).join("")}</ul></div>` : ""}
    ${bestForHtml()}
    <div class="panel grid">
      <div class="row head"><span>Card</span><span>Whose</span><span>Renews</span><span class="num">Annual fee</span><span>Use it for</span><span>Key benefits</span><span>Foreign fee</span><span>Cash advance</span><span>Referral bonus</span><span class="num">Unused now</span></div>
      ${rows || `<p class="empty" style="padding:0 20px">No cards yet.</p>`}
      <div class="add-row">${addRow}</div>
    </div>`;
}

const unknown = `<span class="muted" title="Not looked up yet. Add it under Edit details, or look the card up again.">—</span>`;
function fxHtml(c){
  if (c.foreign_fee_pct == null) return unknown;
  return c.foreign_fee_pct ? `<span class="soon">${c.foreign_fee_pct}%</span>` : "None";
}
function cashHtml(c){
  if (c.cash_advance_pct == null && c.cash_advance_min == null) return unknown;
  const parts = [c.cash_advance_pct != null ? `${c.cash_advance_pct}%` : "", c.cash_advance_min ? `${money(c.cash_advance_min)} min` : ""];
  return parts.filter(Boolean).join(", ");
}

function refHtml(c){
  if (!c.referral_bonus) return `<span class="muted" title="Add it under Edit details. It's in your bank app, under Refer a friend.">—</span>`;
  return esc(c.referral_bonus);
}

function bestForHtml(){
  if (!data.best_for || !data.best_for.length) return "";
  const tiles = data.best_for.map(cat => {
    const rows = cat.best.filter(b => owner === "all" || b.owner === owner).map(b => {
      const names = b.cards.map(x => `<a href="#/card/${encodeURIComponent(x.id)}">${esc(x.name)}</a>`).join(" or ");
      return `<div class="bf-row" style="--who:${color(b.owner)}"><span class="dot"></span><div>${owners().length > 1 && owner === "all" ? `<span class="muted">${esc(b.owner)}:</span> ` : ""}${names}
        <div class="muted bf-rate">${b.rate}x · about ${b.cents}¢ per $1</div></div></div>`;
    }).join("");
    return `<div class="bf"><h3>${esc(cat.label)}</h3>${rows}</div>`;
  }).join("");
  return `<section class="bestwrap"><div class="sec-top"><h2>Best card for</h2><a class="btn" href="#/ideas">New card ideas →</a></div><div class="bestfor">${tiles}</div>
    <p class="muted bf-note">Based on each card's earn rate times what a point is worth (set per card). Booking through a bank's travel site can earn more.</p></section>`;
}

function perksHtml(c){
  // the card's highlights if it has them, otherwise the benefits it tracks
  const items = (c.key_benefits || []).length ? c.key_benefits : c.benefits.map(b => b.credit);
  if (!items.length) return `<span class="muted">—</span>`;
  // keep the grid readable: two short highlights, the full list is on the card's page
  const clip = t => t.length > 48 ? t.slice(0, 45).replace(/[\s,;:(]+\S*$/, "") + "…" : t;
  const shown = items.slice(0, 2).map(t => esc(clip(t))).join(" · ");
  return items.length > 2 ? `${shown} <span>+${items.length - 2} more</span>` : shown;
}

/* ---------- card page ---------- */
function benefitLine(b){
  const flag = !b.fully_used && b.days_left_in_period !== null && b.days_left_in_period <= 7;
  const when = b.period_ends ? `resets ${short(b.period_ends)}` : "";
  let state;
  const tail = when ? ` · ${when}` : "";
  if (b.amount) state = b.fully_used ? `<span class="done">Used</span>${tail}` : `${money(b.left)} of ${money(b.amount)} left${tail}`;
  else state = b.used ? `<span class="done">Used</span>${tail}` : `Not used yet${tail}`;
  if (flag) state = `Unused, resets in ${plural(b.days_left_in_period, "day")}`;
  const meter = b.amount ? `<div class="meter"><i style="width:${Math.min(100, b.used / b.amount * 100)}%"></i></div>` : "";
  const acts = [];
  if (!b.fully_used) acts.push(`<button class="btn primary" data-use="${b.id}">Mark used</button>`);
  if (b.amount && !b.fully_used) acts.push(`<button class="btn quiet" data-partial="${b.id}">Part</button>`);
  if (b.used) acts.push(`<button class="btn quiet" data-undo="${b.id}">Undo</button>`);
  return `<div class="line ${flag ? "flag" : ""}"><div><div class="what">${esc(b.credit)} <span class="muted" style="font-weight:400;font-size:14px">· ${PERIOD[b.resets]}</span></div>
    <div class="state">${state}</div>${meter}</div><div class="acts">${acts.join("")}</div></div>`;
}

function offerLine(o){
  if (editingOffer === o.id) return `<form class="form offer" data-edit-offer="${o.id}" style="display:flex">
      <input name="merchant" value="${esc(o.merchant)}" required aria-label="Store">
      <input name="deal" value="${esc(o.deal)}" required aria-label="Deal" style="min-width:200px">
      <input name="expires" type="date" value="${o.expires || ""}" aria-label="Ends">
      <button class="btn primary">Save</button><button class="btn quiet" type="button" data-cancel-offer-edit>Cancel</button></form>`;
  const ending = o.days_left !== null && o.days_left <= 7;
  const until = o.expires ? (ending ? `<span class="soon">ends in ${plural(o.days_left, "day")}</span>` : `until ${fmt(o.expires)}`) : "no end date";
  const from = o.screenshot ? ` · from <a href="/screenshots/${encodeURIComponent(o.screenshot)}" target="_blank" rel="noopener">screenshot</a>` : " · added by hand";
  return `<div class="offer ${ending ? "ending" : ""}"><div><div class="what"><strong>${esc(o.merchant)}</strong>: ${esc(o.deal)}</div>
    <div class="state">${until}${from}</div></div>
    <div class="acts"><button class="btn" data-offer-used="${o.id}">Used it</button>
      <button class="btn quiet" data-offer-edit="${o.id}">Edit</button><button class="btn quiet" data-offer-remove="${o.id}">Remove</button></div></div>`;
}

function lookupBlock(c){
  const lk = c.lookup;
  if (lk && lk.status === "running")
    return `<div class="review"><p><strong>Looking up ${esc(c.name)} online…</strong></p>
      <p class="muted">Usually 30–60 seconds. You can leave this page: it keeps going and saves on its own.</p></div>`;
  if (lk && lk.status === "error")
    return `<div class="review"><p><strong>The lookup didn't work.</strong> ${esc(lk.message)}</p>
      <div class="acts" style="justify-content:flex-start;margin-top:8px"><button class="btn primary" data-lookup="${c.id}">Try again</button></div></div>`;
  if (c.needs_review){
    const url = (c.source || "").split(" (checked")[0];
    return `<div class="review"><h3>Filled in from online: please check</h3>
      <p>The fee and benefits below were looked up for you. Make sure they match your card.</p>
      ${c.lookup_notes ? `<p class="muted">${esc(c.lookup_notes)}</p>` : ""}
      ${url.startsWith("http") ? `<p class="muted">Source: <a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a></p>` : ""}
      <div class="acts" style="justify-content:flex-start;margin-top:8px"><button class="btn primary" data-review-ok="${c.id}">Looks right</button>
      <button class="btn quiet" data-open-edit>Fix something</button></div></div>`;
  }
  if ((c.source || "").startsWith("Benefits not looked up"))
    return `<div class="review"><p>This card's fee and benefits haven't been looked up yet.</p>
      <div class="acts" style="justify-content:flex-start;margin-top:8px"><button class="btn primary" data-lookup="${c.id}">Look up fee and benefits</button></div></div>`;
  return "";
}

function offerReview(c){
  if (reading[c.id]) return `<p class="empty">Reading your screenshot… about 15 seconds.</p>`;
  const r = offerDrafts[c.id];
  if (!r) return "";
  if (!r.offers.length) return `<div class="review"><p>No offers found in that screenshot.</p>
    <button class="btn quiet" data-discard-offers="${c.id}">Close</button></div>`;
  return `<div class="review"><h3>${plural(r.offers.length, "offer")} found in your screenshot</h3>
    <p class="muted">Compare with your screenshot, untick any that are wrong, then save. You can edit any offer afterward.
      <a href="/screenshots/${encodeURIComponent(r.screenshot)}" target="_blank" rel="noopener">Open screenshot</a></p>
    ${r.offers.map((o, i) => `<label><input type="checkbox" data-pick="${i}" checked>
      <span><strong>${esc(o.merchant)}</strong>: ${esc(o.deal)}${o.expires ? ` <span class="muted">· until ${fmt(o.expires)}</span>` : ""}</span></label>`).join("")}
    <div class="acts" style="justify-content:flex-start;margin-top:8px"><button class="btn primary" data-save-offers="${c.id}">Save checked offers</button>
    <button class="btn quiet" data-discard-offers="${c.id}">Discard</button></div></div>`;
}

function editBenefits(c){
  if (!c.credits.length) return "";
  return `<h3>Benefits</h3>${c.credits.map(b => `<form class="edit-benefit" data-edit-benefit="${b.id}">
      <input name="name" value="${esc(b.name)}" required aria-label="Benefit name" style="flex:1;min-width:180px">
      <input name="amount" type="number" min="0" step="0.01" value="${b.amount ?? ""}" placeholder="$ (none)" aria-label="Amount" style="width:110px">
      <select name="period" aria-label="Resets">${data.periods.map(p => `<option value="${p}" ${b.period === p ? "selected" : ""}>${PERIOD[p]}</option>`).join("")}</select>
      <button class="btn">Save</button><button class="btn quiet" type="button" data-remove-benefit="${b.id}">Remove</button></form>`).join("")}`;
}

function cardPage(c){
  const fee = c.annual_fee ? `${money(c.annual_fee)}<small class="${c.next_fee && c.next_fee.days <= 30 ? "soon" : "muted"}">${
    c.next_fee ? `renews ${fmt(c.next_fee.date)} · ${plural(c.next_fee.days, "day")}` : "renewal date not set"}</small>` : `No annual fee<small class="muted">&nbsp;</small>`;
  const uses = c.use_for.length ? c.use_for.map(u => `${u.rate}x ${u.label}${u.best ? ` (best for ${esc(c.owner)})` : ""}`).join(", ") : "";
  const offersLive = c.offers.filter(o => o.status === "active");
  const offerForm = showOfferForm ? `<form class="form" id="offerForm">
      <input name="merchant" placeholder="Store" required><input name="deal" placeholder="Deal, e.g. $20 back on $100" required style="min-width:220px">
      <input name="expires" type="date" title="Offer ends" aria-label="Offer ends"><button class="btn primary">Save offer</button>
      <button class="btn quiet" type="button" data-hide-offer>Cancel</button></form>` : "";
  const remove = confirmRemove
    ? `<p>Remove <strong>${esc(c.name)}</strong> and its saved offers? Use this if you closed the card. If you downgraded it, change its name above and look it up again instead.</p>
       <div class="acts" style="justify-content:flex-start"><button class="btn danger solid" data-remove-card="${c.id}">Yes, remove card</button>
       <button class="btn quiet" data-cancel-remove>Cancel</button></div>`
    : `<button class="btn danger" data-ask-remove>Remove this card</button>`;
  return `<a class="back" href="#/">← All cards</a>
  <article class="detail" style="--who:${color(c.owner)}">
    <div class="detail-head"><div><h1>${esc(c.name)}</h1><div class="who"><span class="dot"></span>${esc(c.owner)}</div></div>
      <div class="fee">${fee}</div></div>
    <div class="facts">${uses ? `<p><strong>Use it for:</strong> ${uses}</p>` : ""}
      <p><strong>Foreign transaction fee:</strong> ${fxHtml(c)} &nbsp;·&nbsp; <strong>Cash advance fee:</strong> ${cashHtml(c)}</p>
      <p><strong>Referral bonus:</strong> ${refHtml(c)}${c.referral_link ? ` &nbsp;·&nbsp; <a href="${esc(c.referral_link)}" target="_blank" rel="noopener">referral link</a> <button class="btn quiet" data-copy="${esc(c.referral_link)}">Copy link</button>` : ""}</p>
      ${(c.key_benefits || []).length ? `<p class="muted">${c.key_benefits.map(esc).join(" · ")}</p>` : ""}</div>
    ${lookupBlock(c)}

    <section><div class="sec-head"><h2>Card benefits</h2><p>Built into the card, reset every period</p></div>
      ${c.benefits.length ? c.benefits.map(benefitLine).join("") : `<p class="empty">No benefits to track yet.</p>`}</section>

    <section><div class="sec-head"><h2>Limited-time offers</h2>
      <div class="acts"><button class="btn primary" data-upload="${c.id}">Upload screenshot</button><button class="btn" data-show-offer>Add by hand</button></div></div>
      ${offerReview(c)}${offerForm}
      ${offersLive.length ? offersLive.map(offerLine).join("") : (offerForm || offerDrafts[c.id] || reading[c.id] ? "" : `<p class="empty">No offers saved. Screenshot the offers page in your bank app and upload it here.</p>`)}
      ${c.screenshots.length ? `<div class="thumbs"><span class="muted">Screenshots:</span>${c.screenshots.map(s => {
        const d = (s.match(/__(\d{4}-\d{2}-\d{2})/) || [])[1];
        return `<a href="/screenshots/${encodeURIComponent(s)}" target="_blank" rel="noopener">${d ? short(d) : esc(s)}</a>`;}).join("")}</div>` : ""}
    </section>

    <details id="editDetails" ${keepDetailsOpen ? "open" : ""}><summary>Edit details, fix mistakes, or remove this card</summary>
      <h3>Card</h3>
      <form class="form" id="editCard">
        <label>Name <input name="name" value="${esc(c.name)}" required style="min-width:220px"></label>
        <label>Whose <input name="owner" list="owners" value="${esc(c.owner)}" required style="width:150px">${ownerOptions()}</label>
        <label>Annual fee <input name="annual_fee" type="number" min="0" value="${c.annual_fee}" style="width:100px"></label>
        <label>Renews <input name="renewal_date" type="date" value="${c.renewal_date || ""}"></label>
        <label>Foreign fee % <input name="foreign_fee_pct" type="number" min="0" step="0.1" value="${c.foreign_fee_pct ?? ""}" placeholder="?" style="width:80px"></label>
        <label>Cash advance % <input name="cash_advance_pct" type="number" min="0" step="0.1" value="${c.cash_advance_pct ?? ""}" placeholder="?" style="width:80px"></label>
        <label>min $ <input name="cash_advance_min" type="number" min="0" step="1" value="${c.cash_advance_min ?? ""}" placeholder="?" style="width:80px"></label>
        <label>Referral bonus <input name="referral_bonus" value="${esc(c.referral_bonus || "")}" placeholder="e.g. 20,000 points" style="width:170px"></label>
        <label>Referral link <input name="referral_link" type="url" value="${esc(c.referral_link || "")}" placeholder="https://…" style="width:220px"></label>
        <button class="btn">Save card</button></form>
      ${editBenefits(c)}
      <form class="form" id="addBenefit">
        <input name="name" placeholder="Add a benefit, e.g. Uber credit" required>
        <input name="amount" type="number" min="0" step="0.01" placeholder="$ (optional)" style="width:120px">
        <select name="period" aria-label="Resets">${data.periods.map(p => `<option value="${p}">${PERIOD[p]}</option>`).join("")}</select>
        <button class="btn">Add benefit</button></form>
      <p class="empty">Benefits from: ${esc(c.source || "not recorded")} <button class="btn quiet" data-lookup="${c.id}">Look up again</button></p>
      <div class="danger-zone">${remove}</div>
    </details>
  </article>`;
}

function render(){
  if (location.hash === "#/ideas"){ $("#app").innerHTML = ideasPage(); document.title = "New card ideas · Our Cards"; return; }
  const id = route();
  const card = id && data.cards.find(c => c.id === id);
  if (id && !card){ location.hash = "#/"; return; }
  $("#app").innerHTML = card ? cardPage(card) : summary();
  document.title = card ? `${card.name} · Our Cards` : "Our Cards";
}

/* ---------- new card ideas page ---------- */
// "Do we already have this card?" A suggestion counts as owned only if every distinctive word in its
// name is in one of your card names: "Chase Sapphire Preferred" = your "Sapphire Preferred",
// but "Hilton Honors Amex Aspire" is not your "Hilton Honors American Express" (extra word: aspire).
const GENERIC = new Set(["the", "card", "credit", "from", "by", "chase", "citi", "capital", "one", "bank", "of", "and",
  "visa", "mastercard", "signature", "infinite", "world", "rewards", "mileage", "plus", "honors", "anywhere", "sapphire", "skymiles"]);
// Words that make it a different card in the same family (Amex Platinum vs Amex Gold, Venture vs Venture X)
const TIER = new Set(["gold", "green", "platinum", "reserve", "aspire", "surpass", "business", "premier", "preferred",
  "elite", "x", "select", "executive", "cash", "unlimited", "flex", "boundless", "bold", "brilliant", "bevy", "explorer",
  "quest", "club", "infinite", "premium"]);
const words = t => new Set(String(t || "").toLowerCase().replace(/american express/g, "amex").split(/[^a-z0-9]+/)
  .filter(w => w && !GENERIC.has(w)));
function alreadyHave(name){
  const want = words(name);
  return want.size > 0 && data.cards.some(c => {
    const mine = words(c.name);
    if ([...want].every(w => mine.has(w))) return true;              // "Venture Rewards" = your "Venture"
    return mine.size > 1 && [...mine].every(w => want.has(w))        // your words all appear in it...
      && ![...want].some(w => !mine.has(w) && TIER.has(w));          // ...and it isn't a different tier
  });
}
const daysUntil = d => Math.round((new Date(d + "T12:00:00") - new Date(data.today + "T12:00:00")) / 86400000);

function ideasPage(){
  const st = data.ideas || {}, d = st.data;
  const running = st.status === "running";
  const head = `<a class="back" href="#/">← All cards</a>
    <div class="top"><h1>New card ideas</h1>
      <button class="btn ${d ? "" : "primary"}" data-find-ideas ${running ? "disabled" : ""}>${running ? "Searching…" : d ? "Check again" : "Find ideas"}</button></div>
    <p class="lede">${d ? `Checked ${fmt(d.checked)}. Offers change every few weeks, so check again before applying.` : "Nothing checked yet."}
      ${running ? " Searching the web now, about a minute. You can leave this page." : ""}
      ${st.status === "error" ? ` <span class="soon">The search didn't work: ${esc(st.message)}</span>` : ""}</p>
    <p class="muted" style="font-size:14px;max-width:72ch">Information, not financial advice. Each application is a hard credit check, and bank rules
      (Chase's 5/24, Amex's one-bonus-per-card and card-family rules) can make you ineligible for a bonus.</p>`;
  if (!d) return head;

  const signup = (d.signup || []).filter(o => !alreadyHave(o.card) && !(o.ends && daysUntil(o.ends) < 0))
    .sort((a, b) => (a.ends ? daysUntil(a.ends) : 9999) - (b.ends ? daysUntil(b.ends) : 9999));
  const signupRows = signup.map(o => {
    const left = o.ends ? daysUntil(o.ends) : null;
    const ends = o.ends ? `${fmt(o.ends)} <span class="${left <= 21 ? "soon" : "muted"}">· ${plural(left, "day")} left</span>` : `<span class="muted">No end date announced</span>`;
    const spend = o.min_spend ? `${money(o.min_spend)}${o.months ? ` in ${plural(o.months, "month")}` : ""}` : "—";
    return `<div class="row idea"><span><strong>${esc(o.card)}</strong>${o.note ? `<div class="muted idea-note">${esc(o.note)}</div>` : ""}</span>
      <span><span class="lbl">Bonus </span>${esc(o.bonus)}</span><span><span class="lbl">Spend </span>${spend}</span>
      <span><span class="lbl">Annual fee </span>${o.annual_fee ? money(o.annual_fee) : "$0"}</span><span><span class="lbl">Offer ends </span>${ends}</span>
      <span>${o.source_url ? `<a href="${esc(o.source_url)}" target="_blank" rel="noopener">Source</a>` : ""}</span></div>`;
  }).join("");

  const who = owners().filter(o => owner === "all" || o === owner);
  const catBlocks = (data.best_for || []).map(cat => {
    const mineBest = cat.best.filter(b => who.includes(b.owner));
    const ideas = (d.category || []).filter(x => x.category === cat.category && !alreadyHave(x.card))
      .map(x => ({...x, cents: Math.round(x.rate * x.point_value * 100) / 100}))
      .sort((a, b) => b.cents - a.cents);
    const lowest = mineBest.length ? Math.min(...mineBest.map(b => b.cents)) : 0;
    const better = ideas.filter(x => x.cents > lowest + 0.25);
    const now = mineBest.map(b => `<span style="--who:${color(b.owner)}"><span class="dot"></span>${who.length > 1 ? esc(b.owner) + ": " : ""}${b.cards.map(x => esc(x.name)).join(" or ")}, about ${b.cents}¢</span>`).join(" &nbsp; ");
    const list = better.length ? better.map(x => {
      const gains = mineBest.filter(b => x.cents > b.cents + 0.25).map(b => `+${(x.cents - b.cents).toFixed(1)}¢ for ${esc(b.owner)}`).join(", ");
      return `<div class="idea-pick"><strong>${esc(x.card)}</strong>: ${x.rate}x${x.cap ? ` (${esc(x.cap)})` : ""}, about ${x.cents}¢ per $1 ·
        ${x.annual_fee ? money(x.annual_fee) + "/yr" : "no annual fee"} <span class="good">${gains}</span>
        ${x.source_url ? ` · <a href="${esc(x.source_url)}" target="_blank" rel="noopener">source</a>` : ""}</div>`;
    }).join("") : `<div class="muted">You're already well covered here.</div>`;
    return `<div class="line" style="display:block"><h3>${esc(cat.label)}</h3><p class="muted" style="margin:0 0 6px;font-size:14px">Best now: ${now}</p>${list}</div>`;
  }).join("");

  return head + `
    <section class="panel" style="padding:20px"><div class="sec-head"><h2>Sign-up bonuses</h2><p>Cards you don't have, ending soonest first</p></div>
      ${signup.length ? `<div class="ideas-grid"><div class="row head"><span>Card</span><span>Bonus</span><span>Spend to earn it</span><span>Annual fee</span><span>Offer ends</span><span></span></div>${signupRows}</div>` : `<p class="empty">No offers saved.</p>`}</section>
    <section class="panel" style="padding:20px;margin-top:16px"><div class="sec-head"><h2>Earn more on…</h2><p>Cards that beat your current best, in cents of value per $1</p></div>${catBlocks}</section>
    ${bankSection(d)}`;
}

function bankSection(d){
  const banks = (d.bank || []).filter(b => !(b.ends && daysUntil(b.ends) < 0))
    .sort((a, b) => (a.ends ? daysUntil(a.ends) : 9999) - (b.ends ? daysUntil(b.ends) : 9999));
  if (!banks.length) return "";
  const rows = banks.map(b => {
    const left = b.ends ? daysUntil(b.ends) : null;
    const ends = b.ends ? `${fmt(b.ends)} <span class="${left <= 21 ? "soon" : "muted"}">· ${plural(left, "day")} left</span>` : `<span class="muted">Ongoing</span>`;
    return `<div class="row idea"><span><strong>${esc(b.bank)}</strong>${b.account ? `<div class="muted idea-note">${esc(b.account)}</div>` : ""}</span>
      <span><span class="lbl">Bonus </span><strong>${esc(b.bonus)}</strong></span><span><span class="lbl">To get it </span>${esc(b.requirement)}</span>
      <span><span class="lbl">Monthly fee </span>${b.fee ? esc(b.fee) : `<span class="muted">—</span>`}</span><span><span class="lbl">Offer ends </span>${ends}</span>
      <span>${b.source_url ? `<a href="${esc(b.source_url)}" target="_blank" rel="noopener">Source</a>` : ""}</span></div>`;
  }).join("");
  return `<section class="panel" style="padding:20px;margin-top:16px"><div class="sec-head"><h2>Checking account bonuses</h2><p>Ending soonest first</p></div>
    <p class="muted" style="font-size:14px;margin:0 0 8px">Usually only for new customers of that bank. Bonuses count as taxable income, and you may need to keep the account open 3–6 months.</p>
    <div class="ideas-grid bank-grid"><div class="row head"><span>Bank</span><span>Bonus</span><span>To get it</span><span>Monthly fee</span><span>Offer ends</span><span></span></div>${rows}</div></section>`;
}

/* ---------- actions ---------- */
async function runLookup(id){
  const out = await post("lookup", {card_id: id});
  if (out.error) toast(out.error);
  await load();
}

$("#file").addEventListener("change", async e => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file || !uploadFor) return;
  const id = uploadFor; reading[id] = true; render();
  const dataUrl = await new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(file); });
  const out = await post("import_screenshot", {card_id: id, media_type: file.type, data: dataUrl.split(",")[1]});
  delete reading[id];
  if (out.error) toast(out.error); else offerDrafts[id] = out;
  await load();
});

document.addEventListener("click", async e => {
  const t = e.target.closest("button"); if (!t) return;
  const ds = t.dataset, id = route();
  if (ds.owner){ owner = ds.owner; render(); }
  else if ("showAdd" in ds){ showAddCard = true; render(); $("#addCard input").focus(); }
  else if ("cancelAdd" in ds){ showAddCard = false; render(); }
  else if (ds.use){ if (await act("use", {benefit_id: ds.use})) toast("Marked used"); }
  else if (ds.undo){ if (await act("undo", {benefit_id: ds.undo})) toast("Undone"); }
  else if (ds.partial){ const amt = prompt("How many dollars did you use?");
    if (amt && !isNaN(amt) && await act("use", {benefit_id: ds.partial, amount: Number(amt)})) toast(`Logged ${money(amt)}`); }
  else if (ds.lookup){ runLookup(ds.lookup); window.scrollTo(0, 0); }
  else if (ds.copy){ try { await navigator.clipboard.writeText(ds.copy); toast("Link copied"); } catch { toast("Couldn't copy: select the link instead"); } }
  else if ("findIdeas" in ds){ const out = await post("find_ideas", {}); if (out.error) toast(out.error); await load(); }
  else if (ds.reviewOk){ if (await act("review_ok", {card_id: ds.reviewOk})) toast("Thanks, marked as checked"); }
  else if ("openEdit" in ds){ keepDetailsOpen = true; render(); $("#editDetails").scrollIntoView({behavior: "smooth"}); }
  else if (ds.upload){ uploadFor = ds.upload; $("#file").click(); }
  else if (ds.saveOffers){
    const r = offerDrafts[ds.saveOffers];
    const picked = [...document.querySelectorAll("[data-pick]")].filter(x => x.checked).map(x => r.offers[Number(x.dataset.pick)]);
    const out = await act("save_offers", {card_id: ds.saveOffers, screenshot: r.screenshot, offers: picked});
    if (out){ delete offerDrafts[ds.saveOffers]; render(); toast(`Saved ${plural(out.saved, "offer")}`); }
  }
  else if (ds.discardOffers){ delete offerDrafts[ds.discardOffers]; render(); }
  else if ("showOffer" in ds){ showOfferForm = true; render(); $("#offerForm input").focus(); }
  else if ("hideOffer" in ds){ showOfferForm = false; render(); }
  else if (ds.offerEdit){ editingOffer = ds.offerEdit; render(); }
  else if ("cancelOfferEdit" in ds){ editingOffer = null; render(); }
  else if (ds.offerUsed){ if (await act("offer_used", {offer_id: ds.offerUsed})) toast("Offer marked used"); }
  else if (ds.offerRemove){ if (await act("offer_remove", {offer_id: ds.offerRemove})) toast("Offer removed"); }
  else if (ds.removeBenefit){ keepDetailsOpen = true; if (await act("remove_benefit", {card_id: id, benefit_id: ds.removeBenefit})) toast("Benefit removed"); }
  else if ("askRemove" in ds){ confirmRemove = true; keepDetailsOpen = true; render(); }
  else if ("cancelRemove" in ds){ confirmRemove = false; render(); }
  else if (ds.removeCard){
    const name = data.cards.find(c => c.id === ds.removeCard)?.name;
    if (await act("remove_card", {card_id: ds.removeCard})){ confirmRemove = false; location.hash = "#/"; toast(`Removed ${name}`); }
  }
});

document.addEventListener("toggle", e => { if (e.target.id === "editDetails") keepDetailsOpen = e.target.open; }, true);

document.addEventListener("submit", async e => {
  e.preventDefault(); const f = e.target, v = Object.fromEntries(new FormData(f)), id = route();
  if (f.id === "keyForm"){
    if (await act("save_key", {key: v.key})) toast("Key saved. AI features are on.");
  } else if (f.id === "addCard"){
    const out = await act("add_card", v);
    if (out){ showAddCard = false; location.hash = "#/card/" + encodeURIComponent(out.card.id); runLookup(out.card.id); }
  } else if (f.id === "editCard"){
    keepDetailsOpen = true;
    if (await act("update_card", {card_id: id, changes: {name: v.name.trim(), owner: v.owner.trim(), annual_fee: Number(v.annual_fee || 0), renewal_date: v.renewal_date || null,
      foreign_fee_pct: v.foreign_fee_pct === "" ? null : Number(v.foreign_fee_pct),
      cash_advance_pct: v.cash_advance_pct === "" ? null : Number(v.cash_advance_pct),
      cash_advance_min: v.cash_advance_min === "" ? null : Number(v.cash_advance_min),
      referral_bonus: v.referral_bonus.trim() || null, referral_link: v.referral_link.trim() || null}})) toast("Card saved");
  } else if (f.dataset.editBenefit){
    keepDetailsOpen = true;
    if (await act("edit_benefit", {card_id: id, benefit_id: f.dataset.editBenefit, changes: {name: v.name, amount: v.amount, period: v.period}})) toast("Benefit saved");
  } else if (f.id === "addBenefit"){
    keepDetailsOpen = true;
    if (await act("add_benefit", {card_id: id, name: v.name, period: v.period, amount: v.amount || null})) toast("Benefit added");
  } else if (f.id === "offerForm"){
    showOfferForm = false;
    if (await act("add_offer", {card_id: id, merchant: v.merchant, deal: v.deal, expires: v.expires})) toast("Offer saved"); else render();
  } else if (f.dataset.editOffer){
    const oid = f.dataset.editOffer; editingOffer = null;
    if (await act("edit_offer", {offer_id: oid, changes: {merchant: v.merchant, deal: v.deal, expires: v.expires}})) toast("Offer saved"); else render();
  }
});

/* ---------- chat ---------- */
const chatId = "c" + Math.random().toString(36).slice(2);
let chatLog = [], chatBusy = false, chatStarted = 0;
const STARTERS = ["Which card should I use for groceries?", "What credits are about to expire this month?",
  "Is the Amex Platinum worth its annual fee?", "Should we get a new card?"];

function renderChat(){
  const log = $("#chatLog");
  if (!chatLog.length && !chatBusy){
    log.innerHTML = `<div class="starter"><p class="muted" style="margin:0 0 4px;font-size:14px">Try asking:</p>${STARTERS.map(q =>
      `<button class="btn" data-ask="${esc(q)}">${esc(q)}</button>`).join("")}</div>`;
    return;
  }
  log.innerHTML = chatLog.map((m, i) => m.role === "you" ? `<div class="msg you"><span class="txt">${esc(m.text)}</span></div>`
    : `<div class="msg bot"><span class="txt">${esc(m.text)}</span>${m.steps && m.steps.length ? `<ul class="steps" aria-label="What it did">${m.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}
       ${i === chatLog.length - 1 && !chatBusy && /\?\s*$/.test(m.text) ? `<div class="quick"><button class="btn primary" data-ask="Yes">Yes</button><button class="btn" data-ask="No">No</button></div>` : ""}</div>`).join("")
    + (chatBusy ? `<div class="thinking" id="thinking">Thinking…</div>` : "");
  log.scrollTop = log.scrollHeight;
}

async function ask(text){
  if (chatBusy || !text.trim()) return;
  chatLog.push({role: "you", text}); chatBusy = true; chatStarted = Date.now(); renderChat();
  const tick = setInterval(() => { const t = $("#thinking"); if (t) t.textContent = `Thinking… ${Math.round((Date.now() - chatStarted) / 1000)}s`; }, 1000);
  try {
    const out = await post("chat", {chat_id: chatId, text});
    chatLog.push(out.error ? {role: "bot", text: "Sorry, something went wrong: " + out.error} : {role: "bot", text: out.reply, steps: out.steps});
  } catch (e) {
    chatLog.push({role: "bot", text: "Couldn't reach the dashboard. Is it still running in the terminal?"});
  }
  clearInterval(tick); chatBusy = false; renderChat();
  await load();  // the assistant may have changed your cards
}

function openChat(open){
  $("#chat").hidden = !open; $("#chatOpen").hidden = open; $("#chatOpen").setAttribute("aria-expanded", open);
  if (open){ renderChat(); $("#chatInput").focus(); }
}
$("#chatOpen").addEventListener("click", () => openChat(true));
$("#chatClose").addEventListener("click", () => openChat(false));
$("#chatNew").addEventListener("click", async () => { if (chatBusy) return; await post("chat_reset", {chat_id: chatId}); chatLog = []; renderChat(); });
$("#chatForm").addEventListener("submit", e => { e.preventDefault(); const v = $("#chatInput").value; $("#chatInput").value = ""; ask(v); });
$("#chatLog").addEventListener("click", e => { const b = e.target.closest("[data-ask]"); if (b) ask(b.dataset.ask); });

load();
</script>
</body>
</html>
"""


def _this_computers_addresses():
    """The addresses other devices can use to reach this computer (Wi-Fi, and Tailscale's 100.x.x.x)."""
    found = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no data is sent; this just finds the Wi-Fi address
            found.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        found.update(a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("127."))


if __name__ == "__main__":
    import sys

    share = "--share" in sys.argv
    if share:
        # Your phone and Jun's can connect. Set your own password with the DASHBOARD_PASSWORD
        # environment variable, or a new random one is made each time.
        SHARE_PASSWORD = os.environ.get("DASHBOARD_PASSWORD") or secrets.token_urlsafe(6)
    host = "0.0.0.0" if share else "127.0.0.1"  # 127.0.0.1 = this computer only
    server = ThreadingHTTPServer((host, PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Dashboard running at {url}  (press Ctrl+C to stop)")
    if share:
        print("\nShare mode is ON. On your phone, open one of these:")
        for address in _this_computers_addresses():
            hint = "  (Tailscale: works anywhere)" if address.startswith("100.") else "  (same Wi-Fi only)"
            print(f"   http://{address}:{PORT}{hint}")
        print(f"   Password: {SHARE_PASSWORD}   (any username)")
        if not os.environ.get("DASHBOARD_PASSWORD"):
            print("   To keep the same password: setx DASHBOARD_PASSWORD \"pick-one\" and restart the terminal.")
        print("Your laptop must stay on and awake, with this window open.\n")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
