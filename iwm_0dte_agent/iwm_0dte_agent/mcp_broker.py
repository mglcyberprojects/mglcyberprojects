"""Client for Robinhood's official Agentic Trading MCP server
(https://agent.robinhood.com/mcp/trading), used for --live mode when
`USE_ROBINHOOD_MCP=true` (the default).

*** UNVERIFIED AGAINST THE LIVE SERVER ***
This module is written against the `mcp` Python SDK's actual client API
(confirmed by inspecting the installed `mcp==2.0.0` package: `OAuthClientProvider`,
`streamable_http_client`, `ClientSession`, `create_mcp_http_client`) and against
Robinhood's publicly described OAuth + MCP integration. It has NOT been
exercised against the real `agent.robinhood.com` endpoint -- this development
sandbox's network egress to robinhood.com is blocked, so the OAuth handshake
and the exact tool names/argument shapes below are best-effort, not confirmed.
Expect to debug the first real connection. See README.md's "Troubleshooting
the MCP connection" section, and treat any `MCPError` you hit as useful
information to report back rather than a sign the whole approach is broken.

As of mid-2026, Robinhood's MCP server exposes read/account/equity-order
tools (get_accounts, get_portfolio, get_equity_quotes, place_equity_order,
etc.) but no confirmed options tools. This client is deliberately narrow: it
only relies on MCP for account/underlying-price data. Everything
options-related (0DTE chain lookup, option quotes, order placement) still
goes through the robin_stocks-based RobinhoodBroker. `login()` logs a
warning if it spots any newly-discovered tool with "option" in its name, so
a future run makes it obvious when Robinhood ships that surface.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import threading
import webbrowser
from contextlib import AsyncExitStack
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


class _AsyncBridge:
    """Runs a persistent asyncio event loop in a background thread so the
    rest of this (synchronous) project can call into the async `mcp` SDK
    without being rewritten around asyncio itself.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro, timeout: float | None = None):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
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
        self._bridge = _AsyncBridge()
        self._exit_stack: AsyncExitStack | None = None
        self._session = None
        self._tool_names: set[str] = set()

    def list_discovered_tools(self) -> set[str]:
        return set(self._tool_names)

    def connect(self) -> None:
        """MCP-only handshake (account/quote data). Does not touch robin_stocks."""
        self._bridge.run(self._connect())
        logger.info(
            "Connected to Robinhood MCP (%s), discovered %d tools: %s",
            self._config.robinhood_mcp_url, len(self._tool_names), sorted(self._tool_names),
        )
        option_like = sorted(n for n in self._tool_names if "option" in n.lower())
        if option_like:
            logger.warning(
                "Detected possibly-new option-related MCP tools not yet used by this "
                "client: %s. Options trading still goes through robin_stocks -- if these "
                "are real, ask to have MCPBroker wired up to use them.", option_like,
            )

    def login(self) -> None:
        self.connect()
        self._fallback.login()  # options order flow still needs robin_stocks directly

    async def _connect(self) -> None:
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        from mcp.shared.auth import OAuthClientMetadata

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

        self._exit_stack = AsyncExitStack()
        http_client = await self._exit_stack.enter_async_context(create_mcp_http_client(auth=oauth))
        read_stream, write_stream = await self._exit_stack.enter_async_context(
            streamable_http_client(self._config.robinhood_mcp_url, http_client=http_client)
        )
        session = await self._exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        tools_result = await session.list_tools()

        self._session = session
        self._tool_names = {t.name for t in tools_result.tools}

    def close(self) -> None:
        if self._exit_stack is not None:
            self._bridge.run(self._exit_stack.aclose())
            self._exit_stack = None
        self._bridge.close()

    def _pick_tool(self, *candidates: str) -> str:
        for name in candidates:
            if name in self._tool_names:
                return name
        raise MCPError(
            f"None of the expected MCP tools {candidates} were found on the server "
            f"(discovered: {sorted(self._tool_names)}). Robinhood may have renamed them "
            f"-- run `python -m iwm_0dte_agent --list-mcp-tools` to see the current list."
        )

    def _call_tool_sync(self, name: str, arguments: dict) -> dict:
        return self._bridge.run(self._call_tool(name, arguments))

    async def _call_tool(self, name: str, arguments: dict) -> dict:
        if self._session is None:
            raise MCPError("Not connected to the MCP server -- call connect()/login() first")
        result = await self._session.call_tool(name, arguments)
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
