"""The agent loop. Almost the same as your refund agent.

Run:  python agent.py
"""

import json
from datetime import date
from pathlib import Path

import anthropic

from tools import TOOLS, WEB_SEARCH, run_tool

MODEL = "claude-sonnet-5-5"
RULES = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")
MAX_MESSAGES = 40  # keep the conversation from growing forever (older turns are dropped)


def system_prompt():
    """The rules plus today's date (built fresh, so a long-running dashboard knows the date)."""
    return RULES + f"\nToday's date is {date.today().isoformat()}."


def trim_history(messages):
    """Drop the oldest turns once the chat is long. Always start on a question you typed,
    so a tool result is never left without the request that asked for it."""
    while len(messages) > MAX_MESSAGES:
        messages.pop(0)
        while messages and not (messages[0]["role"] == "user" and isinstance(messages[0]["content"], str)):
            messages.pop(0)


def run_agent(client, messages, show_tools=True, on_step=None):
    """Get the agent's reply, running any tools it asks for along the way.
    on_step(name, input) is called for every tool and web search, so a page can show the steps."""
    trim_history(messages)
    for _ in range(12):  # safety limit
        response = client.messages.create(
            model=MODEL, max_tokens=3000, system=system_prompt(), tools=TOOLS + [WEB_SEARCH], messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        for block in response.content:
            if block.type == "server_tool_use":  # a web search Anthropic ran for us
                if show_tools:
                    print(f"   [web search] {block.input.get('query')}")
                if on_step:
                    on_step("web_search", block.input)

        # A long web search can pause; send it straight back to let it continue.
        if response.stop_reason == "pause_turn":
            continue
        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = run_tool(block.name, block.input)
                if show_tools:
                    print(f"   [tool] {block.name} {block.input}")
                if on_step:
                    on_step(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(result, default=str)})
        messages.append({"role": "user", "content": results})
    return "Sorry, that took too many steps. Try asking a narrower question."


if __name__ == "__main__":
    import finance

    client = anthropic.Anthropic()
    messages = []
    print(f"Card assistant ready (using {finance.TRANSACTIONS_FILE.name}). Type 'quit' to stop.")
    print("Try: Which card for groceries?  What credits do we have left?  What did we miss last month?\n")
    while True:
        text = input("You: ").strip()
        if text.lower() in {"quit", "exit"}:
            break
        if text:
            messages.append({"role": "user", "content": text})
            print("\nAssistant:", run_agent(client, messages), "\n")
