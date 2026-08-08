"""Client for Robinhood's official Agentic Trading MCP server
(https://agent.robinhood.com/mcp/trading), used for --live mode when
`USE_ROBINHOOD_MCP=true` (the default).

This module was originally written and unit-tested without network access to
agent.robinhood.com (the dev sandbox it was built in blocks that egress), so
the first real run against the live server is what actually exercises the
OAuth handshake and connection lifecycle. That first run got through OAuth
and tool discovery successfully, then hit a real bug on close(): spawning a
fresh asyncio Task per call (via run_coroutine_threadsafe) broke anyio's
requirement that a cancel scope be exited by the same task that entered it,
raising "Attempted to exit cancel scope in a different task than it was
entered in". Fixed by running the whole connection lifetime -- connect,
every tool call, and close -- inside one persistent task (`_MCPSession`),
with work fed in through a queue. The exact tool names/argument shapes
returned by get_portfolio/get_equity_quotes/etc. are still best-effort,
though -- see README.md's "Troubleshooting the MCP connection" section, and
treat any `MCPError` you hit as useful information to report back.

As of a live connection in August 2026, Robinhood's MCP server exposes 54
tools including a full options surface: get_option_chains, get_option_quotes,
place_option_order, review_option_order, cancel_option_order,
get_option_positions, get_option_orders, and more. This client still only
relies on MCP for account/underlying-price data, though -- everything
options-related (0DTE chain lookup, option quotes, order placement) still
goes through the robin_stocks-based RobinhoodBroker for now, pending
mapping the real request/response shapes of those option tools (see
mcp_probe.py) and wiring them in deliberately rather than guessing.
`login()` logs a warning if it spots any newly-discovered tool with
"option" in its name, which is exactly how the options surface above was
first noticed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qs, urlparse

from .broker import Broker, RobinhoodBroker
from .config import Config
from .models import Bar, OptionContract, OrderResult

logger = logging.getLogger(__name__)


class MCPError(RuntimeError):
    pass


def _first_present(data: dict, *keys: str) -> Any | None:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


_SHUTDOWN = object()


class _MCPSession:
    """Owns the MCP connection's entire async lifetime as a single asyncio
    Task in a background thread.

    `anyio` (used internally by the `mcp` SDK's streamable HTTP transport)
    ties its cancel scopes to whichever *task* entered them, and requires
    that same task to exit them. A naive bridge that runs each call via
    `asyncio.run_coroutine_threadsafe` spawns a fresh Task per call --
    connecting in one task and closing in another -- which trips exactly
    that check with "Attempted to exit cancel scope in a different task
    than it was entered in". Everything here (connect, every tool call,
    and close) instead runs inside one persistent coroutine, with work fed
    in through a queue so the sync rest of this project can still call it
    like a normal blocking client.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._queue: asyncio.Queue | None = None
        self._main_future: "asyncio.Future | None" = None
        self._ready = threading.Event()
        self._connect_error: BaseException | None = None
        self.tool_names: set[str] = set()
        self.tools: list = []  # full mcp.types.Tool objects: name/description/input_schema/output_schema

    def connect(self) -> None:
        self._main_future = asyncio.run_coroutine_threadsafe(self._main(), self._loop)
        self._ready.wait()
        if self._connect_error is not None:
            raise self._connect_error

    async def _main(self) -> None:
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        from mcp.shared.auth import OAuthClientMetadata

        self._queue = asyncio.Queue()
        try:
            client_metadata = OAuthClientMetadata(
                client_name="IWM 0DTE Agent",
                redirect_uris=[f"http://127.0.0.1:{self._config.robinhood_mcp_oauth_port}/callback"],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )
            oauth = OAuthClientProvider(
                server_url=self._config.robinhood_mcp_url,
                client_metadata=client_metadata,
                storage=FileTokenStorage(self._config.robinhood_mcp_token_cache_path),
                redirect_handler=_browser_redirect_handler,
                callback_handler=lambda: _await_oauth_callback(self._config.robinhood_mcp_oauth_port),
            )

            async with create_mcp_http_client(auth=oauth) as http_client:
                async with streamable_http_client(
                    self._config.robinhood_mcp_url, http_client=http_client
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        tools_result = await session.list_tools()
                        self.tools = list(tools_result.tools)
                        self.tool_names = {t.name for t in self.tools}
                        self._ready.set()

                        while True:
                            item = await self._queue.get()
                            if item is _SHUTDOWN:
                                return
                            name, arguments, result_holder, done_event = item
                            try:
                                result_holder["result"] = await session.call_tool(name, arguments)
                            except BaseException as exc:  # noqa: BLE001 -- surfaced to the calling thread
                                result_holder["error"] = exc
                            finally:
                                done_event.set()
        except BaseException as exc:  # noqa: BLE001 -- surfaced to the calling thread via connect()
            self._connect_error = exc
            self._ready.set()

    def call_tool(self, name: str, arguments: dict, timeout: float = 30.0):
        if self._queue is None:
            raise MCPError("Not connected to the MCP server -- call connect()/login() first")
        result_holder: dict = {}
        done_event = threading.Event()
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, (name, arguments, result_holder, done_event)
        )
        if not done_event.wait(timeout=timeout):
            raise MCPError(f"Timed out waiting for MCP tool {name!r} to respond")
        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder["result"]

    def close(self, timeout: float = 30.0) -> None:
        if self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, _SHUTDOWN)
        if self._main_future is not None:
            try:
                self._main_future.result(timeout=timeout)
            except Exception:
                logger.exception("Error while closing the MCP session")
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


class FileTokenStorage:
    """Persists OAuth tokens + dynamic client registration to a local JSON
    file so you only have to authorize in the browser once. Keep this file
    out of git -- it's in .gitignore -- it's equivalent to a login session.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except json.JSONDecodeError:
            return {}

    def _write(self, data: dict) -> None:
        self._path.write_text(json.dumps(data))
        try:
            self._path.chmod(0o600)
        except OSError:
            pass  # best-effort on platforms without POSIX permissions

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        self._write(data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)


def _make_callback_server(port: int) -> tuple[HTTPServer, dict]:
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib handler naming)
            params = parse_qs(urlparse(self.path).query)
            result["code"] = params.get("code", [None])[0]
            result["state"] = params.get("state", [None])[0]
            result["error"] = params.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body>Robinhood authorization received. You can close this tab.</body></html>"
            )

        def log_message(self, format: str, *args) -> None:  # silence default stderr access log
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    return server, result


async def _await_oauth_callback(port: int):
    from mcp.shared.auth import AuthorizationCodeResult

    server, result = _make_callback_server(port)
    try:
        await asyncio.to_thread(server.handle_request)
    finally:
        server.server_close()
    if result.get("error"):
        raise MCPError(f"Robinhood OAuth authorization failed: {result['error']}")
    if not result.get("code"):
        raise MCPError(f"OAuth callback did not include an authorization code: {result}")
    return AuthorizationCodeResult(code=result["code"], state=result.get("state"), iss=None)


async def _browser_redirect_handler(url: str) -> None:
    print(f"\nOpen this URL to authorize the agent with Robinhood (opening it now if possible):\n{url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        logger.debug("Could not auto-open a browser; use the URL above manually.")


class MCPBroker(Broker):
    def __init__(self, config: Config, fallback: Broker | None = None):
        self._config = config
        self._fallback = fallback or RobinhoodBroker(config)
        self._mcp_session: _MCPSession | None = None

    def list_discovered_tools(self) -> set[str]:
        return set(self._mcp_session.tool_names) if self._mcp_session else set()

    def describe_tool(self, name: str) -> dict | None:
        """Returns {description, input_schema, output_schema} for a tool as
        declared by the server itself -- no guessing, no need to actually
        invoke the tool. None if the name isn't among the discovered tools.
        """
        if self._mcp_session is None:
            return None
        for tool in self._mcp_session.tools:
            if tool.name == name:
                return {
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "output_schema": tool.output_schema,
                }
        return None

    def connect(self) -> None:
        """MCP-only handshake (account/quote data). Does not touch robin_stocks."""
        self._mcp_session = _MCPSession(self._config)
        self._mcp_session.connect()
        tool_names = self._mcp_session.tool_names
        logger.info(
            "Connected to Robinhood MCP (%s), discovered %d tools: %s",
            self._config.robinhood_mcp_url, len(tool_names), sorted(tool_names),
        )
        option_like = sorted(n for n in tool_names if "option" in n.lower())
        if option_like:
            logger.warning(
                "Detected possibly-new option-related MCP tools not yet used by this "
                "client: %s. Options trading still goes through robin_stocks -- if these "
                "are real, ask to have MCPBroker wired up to use them.", option_like,
            )

    def login(self) -> None:
        self.connect()
        self._fallback.login()  # options order flow still needs robin_stocks directly

    def close(self) -> None:
        if self._mcp_session is not None:
            self._mcp_session.close()
            self._mcp_session = None

    def _pick_tool(self, *candidates: str) -> str:
        tool_names = self._mcp_session.tool_names if self._mcp_session else set()
        for name in candidates:
            if name in tool_names:
                return name
        raise MCPError(
            f"None of the expected MCP tools {candidates} were found on the server "
            f"(discovered: {sorted(tool_names)}). Robinhood may have renamed them "
            f"-- run `python -m iwm_0dte_agent --list-mcp-tools` to see the current list."
        )

    def _call_tool_sync(self, name: str, arguments: dict) -> dict:
        if self._mcp_session is None:
            raise MCPError("Not connected to the MCP server -- call connect()/login() first")
        result = self._mcp_session.call_tool(name, arguments)
        if result.is_error:
            raise MCPError(f"MCP tool {name} returned an error: {result.content}")
        if result.structured_content is not None:
            return result.structured_content
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue
        raise MCPError(f"MCP tool {name} returned no parseable content: {result.content}")

    # --- Broker interface: MCP-backed ---

    def get_buying_power(self) -> float:
        tool = self._pick_tool("get_portfolio", "get_accounts")
        data = self._call_tool_sync(tool, {})
        value = _first_present(data, "buying_power", "buyingPower", "cash_available_for_withdrawal")
        if value is None:
            accounts = data.get("accounts") if isinstance(data, dict) else None
            if isinstance(accounts, list) and accounts:
                value = _first_present(accounts[0], "buying_power", "buyingPower")
        if value is None:
            raise MCPError(f"Could not find buying power in {tool} response: {data}")
        return float(value)

    def get_underlying_price(self, symbol: str) -> float:
        tool = self._pick_tool("get_equity_quotes")
        data = self._call_tool_sync(tool, {"symbols": [symbol]})
        quotes = data.get("quotes") if isinstance(data, dict) else None
        if quotes is None:
            quotes = data if isinstance(data, list) else [data]
        quote = quotes[0] if quotes else {}
        price = _first_present(quote, "last_trade_price", "lastTradePrice", "price", "mark_price")
        if price is None:
            raise MCPError(f"Could not find a price for {symbol} in {tool} response: {data}")
        return float(price)

    # --- Broker interface: not yet on MCP, delegate to robin_stocks ---

    def get_intraday_bars(self, symbol: str, since: dt.datetime) -> list[Bar]:
        return self._fallback.get_intraday_bars(symbol, since)

    def get_0dte_chain(self, symbol: str) -> Sequence[OptionContract]:
        return self._fallback.get_0dte_chain(symbol)

    def get_option_quote(self, contract_id: str) -> OptionContract:
        return self._fallback.get_option_quote(contract_id)

    def submit_order(
        self, contract: OptionContract, quantity: int, limit_price: float, side: str,
    ) -> OrderResult:
        return self._fallback.submit_order(contract, quantity, limit_price, side)
