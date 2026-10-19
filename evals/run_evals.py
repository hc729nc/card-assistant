"""Level 2 evals: ask the real assistant real questions and grade what it did and said.

Run:
    python -m evals.run_evals            # every case once
    python -m evals.run_evals --k 3      # every case 3 times (pass^3)
    python -m evals.run_evals --only R1,X1
    python -m evals.run_evals --no-judge # skip the AI grader (cheaper)

Each case runs on a fresh copy of the SAMPLE cards in a temporary folder,
so your real cards and offers are never touched.

Three kinds of checks:
  1. Tools: did it call the tools it should, and avoid the ones it shouldn't?
  2. Answer text: does the answer contain (or avoid) specific words?
  3. State: after the conversation, is the data what it should be?
Plus an optional AI judge that grades the answer against a one-line rubric.
"""

import argparse
import json
import shutil
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import anthropic

import agent
import cards
import finance
import offers

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))


def fresh_sandbox():
    """Point every data file at a new temporary copy of the sample data."""
    folder = Path(tempfile.mkdtemp(prefix="card_eval_"))
    shutil.copy(ROOT / "cards.example.json", folder / "cards.json")
    cards.CARDS_FILE = folder / "cards.json"
    finance.CREDITS_FILE = folder / "credits_used.json"
    finance.TRANSACTIONS_FILE = ROOT / "sample_transactions.csv"
    offers.OFFERS_FILE = folder / "offers.json"
    return folder


def run_case(client, case):
    folder = fresh_sandbox()
    calls = []
    original = agent.run_tool

    def recording_run_tool(name, args):
        calls.append(name)
        return original(name, args)

    agent.run_tool = recording_run_tool
    try:
        answer = agent.run_agent(client, [{"role": "user", "content": case["question"]}], show_tools=False)
    finally:
        agent.run_tool = original

    failures = []
    for tool in case.get("must_call", []):
        if tool not in calls:
            failures.append(f"didn't call {tool}")
    for tool in case.get("must_not_call", []):
        if tool in calls:
            failures.append(f"called {tool}")
    if case.get("must_include_any") and not any(w.lower() in answer.lower() for w in case["must_include_any"]):
        failures.append(f"answer missing any of {case['must_include_any']}")
    for word in case.get("must_not_include", []):
        if word.lower() in answer.lower():
            failures.append(f"answer mentions '{word}'")

    state = case.get("state", {})
    if "card_count" in state and len(cards.load_cards()) != state["card_count"]:
        failures.append(f"card count changed to {len(cards.load_cards())}")
    if "benefit_used" in state:
        want = state["benefit_used"]
        item = next((c for c in finance.credits_remaining() if c["id"] == want["id"]), None)
        if item is None or item["used"] != want["used"]:
            failures.append(f"{want['id']} used = {item and item['used']}, expected {want['used']}")

    shutil.rmtree(folder, ignore_errors=True)
    return {"answer": answer, "calls": calls, "failures": failures}


JUDGE_TOOL = {
    "name": "verdict",
    "description": "Record whether the answer meets the rubric.",
    "input_schema": {
        "type": "object",
        "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["passed", "reason"],
    },
}


def judge(client, case, result):
    prompt = (f"Grade a credit-card assistant's answer. Be strict.\n\nQuestion: {case['question']}\n"
              f"Tools it called: {result['calls']}\nAnswer:\n{result['answer']}\n\nRubric: {case['rubric']}")
    response = client.messages.create(
        model=agent.MODEL, max_tokens=400, tools=[JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "verdict"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next(b for b in response.content if b.type == "tool_use").input


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=1, help="runs per case")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args()

    cases = [c for c in CASES if not args.only or c["id"] in args.only.split(",")]
    client = anthropic.Anthropic()
    rows, by_case = [], defaultdict(list)

    for case in cases:
        for trial in range(1, args.k + 1):
            try:
                result = run_case(client, case)
                if not args.no_judge and case.get("rubric"):
                    verdict = judge(client, case, result)
                    if not verdict["passed"]:
                        result["failures"].append(f"judge: {verdict['reason']}")
            except Exception as exc:
                result = {"answer": "", "calls": [], "failures": [f"error: {exc!r}"]}
            passed = not result["failures"]
            by_case[case["id"]].append(passed)
            rows.append((case, trial, passed, result))
            print(f"{'PASS' if passed else 'FAIL'}  {case['id']:<3} {case['question'][:60]}")
            for f in result["failures"]:
                print(f"        - {f}")

    pass1 = sum(r[2] for r in rows) / len(rows)
    passk = sum(all(v) for v in by_case.values()) / len(by_case)
    summary = f"\npass^1: {pass1:.0%} of runs passed.  pass^{args.k}: {passk:.0%} of cases passed every run."
    print(summary)

    out = ROOT / "evals" / "results"
    out.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    lines = ["# Eval results", summary.strip(), ""]
    for case, trial, passed, result in rows:
        lines += [f"## {case['id']} (trial {trial}): {'PASS' if passed else 'FAIL'}",
                  f"**Q:** {case['question']}", f"**Tools:** {', '.join(result['calls']) or 'none'}",
                  f"**A:** {result['answer']}", ""]
        lines += [f"- {f}" for f in result["failures"]] + [""]
    (out / f"run_{stamp}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Full results: evals/results/run_{stamp}.md")


if __name__ == "__main__":
    main()
