"""Level 1 evals: test the math and the guardrails in plain code. Free, no AI, runs in a second.

Run:  python -m pytest

These run on the sample cards in a temporary folder, so your real data is never touched.
"""

import shutil
from datetime import date
from pathlib import Path

import pytest

import cards
import finance
import offers

HERE = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Point every data file at a temporary copy of the sample data."""
    shutil.copy(HERE / "cards.example.json", tmp_path / "cards.json")
    monkeypatch.setattr(cards, "CARDS_FILE", tmp_path / "cards.json")
    monkeypatch.setattr(finance, "CREDITS_FILE", tmp_path / "credits_used.json")
    monkeypatch.setattr(finance, "TRANSACTIONS_FILE", HERE / "sample_transactions.csv")
    monkeypatch.setattr(offers, "OFFERS_FILE", tmp_path / "offers.json")


def test_best_card_for_dining_is_the_dining_card():
    assert finance.best_card_for("dining")[0]["card"] == "Sample Dining Card"


def test_best_card_only_uses_the_payers_cards():
    ranked = finance.best_card_for("gas", owner="Sam")
    assert {r["owner"] for r in ranked} == {"Sam"}
    assert ranked[0]["card"] == "Sample Grocery Card"


def test_next_fee_date_rolls_into_next_year():
    assert finance.next_fee_date("2025-03-01", date(2026, 10, 17)) == date(2027, 3, 1)
    assert finance.next_fee_date("2024-02-29", date(2027, 1, 1)) == date(2027, 2, 28)


def test_card_with_fee_but_no_date_is_flagged():
    cards.add_card("No Date Card", "Alex", 95)
    assert "No Date Card" in finance.upcoming_fees()["fee_cards_missing_renewal_date"]


def test_dollar_credit_tracks_amount_left():
    finance.record_credit_use("dining_credit", 4)
    item = next(c for c in finance.credits_remaining() if c["id"] == "dining_credit")
    assert (item["used"], item["left"], item["fully_used"]) == (4, 6, False)


def test_mark_used_without_amount_uses_the_rest():
    finance.record_credit_use("rideshare_credit")
    item = next(c for c in finance.credits_remaining() if c["id"] == "rideshare_credit")
    assert item["fully_used"]


def test_perk_used_and_undo():
    finance.record_credit_use("free_night")
    assert next(c for c in finance.credits_remaining() if c["id"] == "free_night")["used"] is True
    finance.undo_last_use("free_night")
    assert next(c for c in finance.credits_remaining() if c["id"] == "free_night")["used"] is False


def test_monthly_credit_resets_next_month():
    finance.record_credit_use("dining_credit", 10, on_date="2026-09-15")
    october = next(c for c in finance.credits_remaining(date(2026, 10, 1)) if c["id"] == "dining_credit")
    assert october["left"] == 10


def test_card_needs_a_name_and_owner():
    assert cards.add_card("", "Alex")["saved"] is False
    assert cards.add_card("Some Card", "")["saved"] is False


def test_bad_renewal_date_is_rejected():
    assert cards.add_card("Some Card", "Sam", renewal_date="March 5")["saved"] is False


def test_payments_are_not_counted_as_spending():
    assert all(p["amount"] > 0 for p in finance.load_purchases())
    assert not any("Payment" in p["merchant"] for p in finance.load_purchases())


def test_expired_offers_are_hidden():
    offers.add_offer("travel", "Old Shop", "10% back", "2020-01-01")
    offers.add_offer("travel", "New Shop", "10% back", "2099-01-01")
    assert [o["merchant"] for o in offers.list_offers()] == ["New Shop"]


def test_fees_are_optional_but_must_be_valid():
    assert cards.add_card("No Fee Info", "Alex")["card"]["foreign_fee_pct"] is None
    assert cards.add_card("Bad Fee", "Alex", foreign_fee_pct=-3)["saved"] is False
    saved = cards.add_card("Abroad Card", "Sam", foreign_fee_pct=0, cash_advance_pct=5, cash_advance_min=10)
    assert saved["saved"] and saved["card"]["cash_advance_min"] == 10


def test_flights_and_hotels_fall_back_to_travel():
    card = {"earn": {"travel": 3, "other": 1}, "point_value": 1.0}
    assert finance.cents_per_dollar(card, "flights") == 3
    assert finance.cents_per_dollar({**card, "earn": {"flights": 5, "travel": 1, "other": 1}}, "flights") == 5
    assert finance.cents_per_dollar(card, "groceries") == 1


def test_referral_link_must_be_a_web_address():
    assert cards.add_card("Ref Card", "Alex", referral_link="not a link")["saved"] is False
    assert cards.add_card("Ref Card", "Alex", referral_bonus="20,000 points",
                          referral_link="https://example.com/r/abc")["saved"] is True
