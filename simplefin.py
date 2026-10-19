"""Pull your real transactions from SimpleFIN Bridge into transactions.csv.

One-time setup (after linking your banks at https://beta-bridge.simplefin.org):
    python simplefin.py setup PASTE_YOUR_SETUP_TOKEN_HERE

Then, whenever you want fresh data:
    python simplefin.py sync            # last 90 days
    python simplefin.py sync --days 30

The setup step saves a private access link in a file called .simplefin_access.
Treat it like a password: it's listed in .gitignore so it never goes to GitHub.
"""

import argparse
import base64
import csv
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).parent
ACCESS_FILE = HERE / ".simplefin_access"
OUTPUT_FILE = HERE / "transactions.csv"


def claim(setup_token):
    """Trade the one-time setup token for a long-term access link."""
    claim_url = base64.b64decode(setup_token.strip()).decode()
    request = urllib.request.Request(claim_url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(request) as response:
            access_url = response.read().decode().strip()
    except urllib.error.HTTPError as err:
        if err.code == 403:
            raise SystemExit("That token was already used or doesn't exist. Make a new one in SimpleFIN Bridge, "
                             "and if you didn't use it yourself, disable the old one there.")
        raise
    ACCESS_FILE.write_text(access_url)
    print("Connected. Access link saved to .simplefin_access (keep it private).")


def fetch_accounts(days):
    """Download accounts and transactions from the last N days."""
    if not ACCESS_FILE.exists():
        raise SystemExit("Run setup first: python simplefin.py setup YOUR_TOKEN")
    parts = urlsplit(ACCESS_FILE.read_text().strip())
    # The access link has a username and password inside it; send them as a login header.
    login = base64.b64encode(f"{parts.username}:{parts.password}".encode()).decode()
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    start = int(time.time()) - days * 86400
    url = urlunsplit((parts.scheme, host, parts.path.rstrip("/") + "/accounts", f"start-date={start}", ""))
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {login}"})
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        if err.code == 402:
            raise SystemExit("SimpleFIN says payment is required. Check your subscription.")
        if err.code == 403:
            raise SystemExit("SimpleFIN refused the login. Run setup again with a new token.")
        raise


def sync(days):
    import anthropic

    from agent import MODEL
    from categorize import categorize_all

    data = fetch_accounts(days)
    for problem in data.get("errlist", []) or data.get("errors", []):
        print("SimpleFIN note:", problem)

    banks = {c.get("conn_id"): c.get("name", "") for c in data.get("connections", [])}
    rows = []
    for account in data.get("accounts", []):
        bank = banks.get(account.get("conn_id"), "")
        account_name = f"{bank} {account['name']}".strip()
        for t in account.get("transactions", []):
            if not t.get("posted"):
                continue  # pending
            amount = float(t["amount"])
            rows.append({
                "Date": datetime.fromtimestamp(t["posted"]).date().isoformat(),
                "Account Name": account_name,
                "Name": t.get("description", "").strip(),
                "Amount": round(-amount, 2),  # SimpleFIN: spending is negative. We store purchases as positive.
            })

    purchases = [r["Name"] for r in rows if r["Amount"] > 0]
    categories = categorize_all(purchases, anthropic.Anthropic(), MODEL)
    for r in rows:
        r["Category"] = categories.get(r["Name"], "other") if r["Amount"] > 0 else "payment or refund"

    rows.sort(key=lambda r: r["Date"])
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Date", "Account Name", "Name", "Category", "Amount"])
        writer.writeheader()
        writer.writerows(rows)
    accounts = sorted({r["Account Name"] for r in rows})
    print(f"Saved {len(rows)} transactions from {len(accounts)} accounts to transactions.csv:")
    for name in accounts:
        print("  -", name)
    print("Copy each account name above into the matching card's account_match in cards.py.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    setup_cmd = sub.add_parser("setup")
    setup_cmd.add_argument("token")
    sync_cmd = sub.add_parser("sync")
    sync_cmd.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    if args.command == "setup":
        claim(args.token)
    else:
        sync(args.days)
