"""Ad-hoc tool for inspecting Robinhood's MCP server directly -- used to
discover real argument/response shapes before wiring a tool into
MCPBroker properly, instead of guessing.

Usage:
    python -m iwm_0dte_agent.mcp_probe describe <tool_name> [<tool_name> ...] [--out FILE]
    python -m iwm_0dte_agent.mcp_probe call <tool_name> ['<json arguments>']
    python -m iwm_0dte_agent.mcp_probe call <tool_name> -   (reads JSON arguments from stdin)

`describe` only reads each tool's declared JSON schema from the server's own
list_tools() response -- it never actually invokes the tool, so it's always
safe to run on anything, including order-placement tools. Takes one
connection and any number of tool names, so you can dump several schemas
in one run; `--out FILE` writes them to a file instead of the terminal,
which is easier to share back accurately than a screenshot for anything
this detail-sensitive.

`call` actually invokes the tool and prints the raw response. Safe for
read-only tools (get_option_chains, get_option_quotes, get_option_positions,
get_option_orders, ...). Do NOT use `call` on place_option_order,
review_option_order, cancel_option_order, or exercise_option without
understanding exactly what you're sending first -- those can have real
side effects on your account. `describe` them instead.

Examples:
    python -m iwm_0dte_agent.mcp_probe describe get_option_chains
    python -m iwm_0dte_agent.mcp_probe describe get_option_instruments get_option_quotes place_option_order review_option_order --out option_schemas.txt
    python -m iwm_0dte_agent.mcp_probe call get_option_chains '{"underlying_symbol": "IWM"}'

Windows/PowerShell note: quoting a JSON object containing double quotes
directly on the command line is notoriously unreliable in PowerShell (it
can silently strip the quotes, or split on spaces inside the JSON even
with `--%`). Pass `-` as the arguments and pipe the JSON in instead -- this
sidesteps PowerShell's native-executable argument quoting entirely:
    '{"underlying_symbol": "IWM"}' | python -m iwm_0dte_agent.mcp_probe call get_option_chains -
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


def _describe_one(broker: MCPBroker, tool_name: str, out) -> None:
    info = broker.describe_tool(tool_name)
    if info is None:
        print(
            f"\n{tool_name!r} was not found among the discovered tools. "
            f"Run `python -m iwm_0dte_agent --list-mcp-tools` to see what's available.",
            file=out,
        )
        return
    print(f"\n=== {tool_name} ===", file=out)
    print(f"description: {info['description']}", file=out)
    print("\ninput_schema:", file=out)
    print(json.dumps(_jsonable(info["input_schema"]), indent=2, default=str), file=out)
    print("\noutput_schema:", file=out)
    print(json.dumps(_jsonable(info["output_schema"]), indent=2, default=str), file=out)


def describe(tool_names: list[str], out_path: str | None = None) -> None:
    broker = MCPBroker(CONFIG)
    try:
        broker.connect()
        if out_path:
            with open(out_path, "w", encoding="utf-8") as f:
                for tool_name in tool_names:
                    _describe_one(broker, tool_name, f)
            print(f"Wrote {len(tool_names)} tool schema(s) to {out_path}")
        else:
            for tool_name in tool_names:
                _describe_one(broker, tool_name, sys.stdout)
    finally:
        broker.close()


def _read_arguments(raw: str) -> str:
    """Resolve the raw `arguments` CLI value -- "-" means read JSON from
    stdin instead of the literal string, for the Windows/PowerShell
    quoting workaround described in the module docstring."""
    return sys.stdin.read() if raw == "-" else raw


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

    describe_parser = sub.add_parser("describe", help="Print one or more tools' input/output JSON schema (never invokes them)")
    describe_parser.add_argument("tool_name", nargs="+", help="One or more tool names")
    describe_parser.add_argument("--out", default=None, help="Write to this file instead of stdout")

    call_parser = sub.add_parser("call", help="Actually invoke a tool and print the raw response")
    call_parser.add_argument("tool_name")
    call_parser.add_argument(
        "arguments", nargs="?", default="{}",
        help='JSON object, e.g. \'{"underlying_symbol": "IWM"}\'. Pass "-" to read the '
             'JSON from stdin instead (recommended on Windows/PowerShell -- see the '
             "module docstring's Windows note for why quoting JSON directly is unreliable there).",
    )

    args = parser.parse_args()

    if args.command == "describe":
        describe(args.tool_name, out_path=args.out)
        return

    raw_arguments = _read_arguments(args.arguments)
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        print(f"Could not parse arguments as JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    call(args.tool_name, arguments)


if __name__ == "__main__":
    main()
