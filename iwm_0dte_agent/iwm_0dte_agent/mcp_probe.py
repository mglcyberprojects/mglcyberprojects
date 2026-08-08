"""Ad-hoc tool for inspecting Robinhood's MCP server directly -- used to
discover real argument/response shapes before wiring a tool into
MCPBroker properly, instead of guessing.

Usage:
    python -m iwm_0dte_agent.mcp_probe describe <tool_name>
    python -m iwm_0dte_agent.mcp_probe call <tool_name> ['<json arguments>']

`describe` only reads the tool's declared JSON schema from the server's own
list_tools() response -- it never actually invokes the tool, so it's always
safe to run on anything, including order-placement tools.

`call` actually invokes the tool and prints the raw response. Safe for
read-only tools (get_option_chains, get_option_quotes, get_option_positions,
get_option_orders, ...). Do NOT use `call` on place_option_order,
review_option_order, cancel_option_order, or exercise_option without
understanding exactly what you're sending first -- those can have real
side effects on your account. `describe` them instead.

Examples:
    python -m iwm_0dte_agent.mcp_probe describe get_option_chains
    python -m iwm_0dte_agent.mcp_probe call get_option_chains '{"symbol": "IWM"}'
    python -m iwm_0dte_agent.mcp_probe call get_equity_quotes '{"symbols": ["IWM"]}'
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import CONFIG
from .mcp_broker import MCPBroker, MCPError

_MUTATING_TOOLS = {
    "place_equity_order", "place_option_order",
    "cancel_equity_order", "cancel_option_order",
    "review_equity_order", "review_option_order",
    "exercise_option", "cancel_option_exercise",
}


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def describe(tool_name: str) -> None:
    broker = MCPBroker(CONFIG)
    try:
        broker.connect()
        info = broker.describe_tool(tool_name)
        if info is None:
            print(
                f"\n{tool_name!r} was not found among the discovered tools. "
                f"Run `python -m iwm_0dte_agent --list-mcp-tools` to see what's available."
            )
            return
        print(f"\n=== {tool_name} ===")
        print(f"description: {info['description']}")
        print("\ninput_schema:")
        print(json.dumps(_jsonable(info["input_schema"]), indent=2, default=str))
        print("\noutput_schema:")
        print(json.dumps(_jsonable(info["output_schema"]), indent=2, default=str))
    finally:
        broker.close()


def call(tool_name: str, arguments: dict) -> None:
    if tool_name in _MUTATING_TOOLS:
        print(
            f"\n*** {tool_name} can place/modify/cancel a real order or exercise a real "
            f"option position. Re-run with the exact arguments you intend, or use "
            f"`describe {tool_name}` first to see its schema without side effects. ***\n"
        )
        answer = input("Type the tool name again to confirm you want to actually call it: ").strip()
        if answer != tool_name:
            print("Aborting.")
            return

    broker = MCPBroker(CONFIG)
    try:
        broker.connect()
        print(f"\nCalling {tool_name} with arguments: {json.dumps(arguments)}\n")
        try:
            result = broker._call_tool_sync(tool_name, arguments)
            print("RESPONSE:")
            print(json.dumps(_jsonable(result), indent=2, default=str))
        except MCPError as exc:
            print("MCPError (often still useful -- can reveal required/invalid params):")
            print(str(exc))
    finally:
        broker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    describe_parser = sub.add_parser("describe", help="Print a tool's input/output JSON schema (never invokes it)")
    describe_parser.add_argument("tool_name")

    call_parser = sub.add_parser("call", help="Actually invoke a tool and print the raw response")
    call_parser.add_argument("tool_name")
    call_parser.add_argument("arguments", nargs="?", default="{}", help='JSON object, e.g. \'{"symbol": "IWM"}\'')

    args = parser.parse_args()

    if args.command == "describe":
        describe(args.tool_name)
        return

    try:
        arguments = json.loads(args.arguments)
    except json.JSONDecodeError as exc:
        print(f"Could not parse arguments as JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    call(args.tool_name, arguments)


if __name__ == "__main__":
    main()
