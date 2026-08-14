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
tools including a full options surface, and options trading is wired up to
use it directly: get_option_chains -> get_option_instruments (paginated) ->
get_option_quotes for reading a 0DTE chain, and review_option_order ->
place_option_order for submitting a trade, gated on get_accounts showing
agentic_allowed=true and option_level_2/3. Schemas for all five tools were
fetched from a live connection via mcp_probe.py before writing this, not
guessed.

There's no MCP tool for historical price bars, only a live quote
(get_equity_quotes) -- so `get_intraday_bars` builds its own bars in
process (see bar_builder.py) from repeated quote polls rather than falling
back to `robin_stocks` for ready-made candles. That keeps `--live` mode
(with USE_ROBINHOOD_MCP=true, the default) entirely off `robin_stocks`: no
login, no automated calls against it at all -- the ToS risk from
automating an unofficial client applies only to the USE_ROBINHOOD_MCP=false
escape hatch (RobinhoodBroker used directly, see broker.py), not to the
default path. The tradeoff is that the opening range is only as good as
however long this process has been polling -- start it at or before market
open, or the first bars of the day simply won't exist yet.

`login()` logs a warning if it spots any newly-discovered tool with
"option" in its name -- that's exactly how the options surface above was
first noticed, so the same mechanism will flag it if Robinhood adds
something new (e.g. multi-leg-specific tools) this client doesn't use yet.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qs, urlparse

from .bar_builder import BarBuilder
from .broker import Broker
from .config import Config
from .models import Bar, OptionContract, OptionType, OrderResult

logger = logging.getLogger(__name__)

_ELIGIBLE_OPTION_LEVELS = {"option_level_2", "option_level_3"}


class MCPError(RuntimeError):
    pass


def _first_present(data: dict, *keys: str) -> Any | None:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _extract_first_equity_quote(data: Any) -> dict:
    """get_equity_quotes actually nests each symbol's quote at
    data.data.results[].quote (paired with a sibling `close` object for the
    prior session's settled close) -- not the flatter `quotes: [...]` shape
    once assumed here. Handle both, since that assumption was wrong once
    already and Robinhood's MCP schemas have shifted before (see
    get_buying_power's data.buying_power.buying_power nesting)."""
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict):
            results = inner.get("results")
            if isinstance(results, list) and results and isinstance(results[0], dict):
                quote = results[0].get("quote")
                if isinstance(quote, dict):
                    return quote
        quotes = data.get("quotes")
        if isinstance(quotes, list) and quotes and isinstance(quotes[0], dict):
            return quotes[0]
        return data
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    return {}


def _current_trade_price(quote: dict) -> Any | None:
    """Prefer whichever of last_trade_price / last_non_reg_trade_price has
    the more recent venue timestamp, per the MCP server's own guidance for
    this endpoint -- both are ISO 8601 UTC timestamps in the same format, so
    a plain string comparison orders them correctly. Falls back to older/
    flatter field names in case a different MCP schema is ever seen."""
    candidates = [
        (quote.get("last_trade_price"), quote.get("venue_last_trade_time")),
        (quote.get("last_non_reg_trade_price"), quote.get("venue_last_non_reg_trade_time")),
    ]
    candidates = [(price, ts) for price, ts in candidates if price not in (None, "") and ts]
    if candidates:
        candidates.sort(key=lambda pt: pt[1])
        return candidates[-1][0]
    return _first_present(quote, "last_trade_price", "lastTradePrice", "price", "mark_price")


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
    def __init__(self, config: Config):
        self._config = config
        self._mcp_session: _MCPSession | None = None
        self._account_number: str | None = None
        self._instrument_cache: dict[str, dict] = {}
        # One BarBuilder per symbol -- tracking multiple tickers means each
        # needs its own independent accumulated bar series; a single shared
        # instance would mix different symbols' quotes into one series.
        self._bar_builders: dict[str, BarBuilder] = {}

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
        used_option_tools = {
            "get_option_chains", "get_option_instruments", "get_option_quotes",
            "review_option_order", "place_option_order",
        }
        option_like = sorted(n for n in tool_names if "option" in n.lower() and n not in used_option_tools)
        if option_like:
            logger.warning(
                "Detected option-related MCP tools not yet wired up: %s. (Chain lookup, "
                "quotes, and order placement already use the MCP path -- these are "
                "additional ones, e.g. positions/orders history/exercise, not used yet.)",
                option_like,
            )

    def login(self) -> None:
        self.connect()

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
        # get_accounts explicitly documents that it does NOT return reliable
        # buying power -- get_portfolio is the authoritative source, and it
        # requires account_number and nests the figure at
        # data.buying_power.buying_power (an object, not a flat field).
        account_number = self._resolve_account_number()
        data = self._call_tool_sync("get_portfolio", {"account_number": account_number})
        buying_power = (data.get("data") or {}).get("buying_power") or {}
        value = buying_power.get("buying_power")
        if value is None:
            raise MCPError(f"Could not find buying power in get_portfolio response: {data}")
        return float(value)

    def get_underlying_price(self, symbol: str) -> float:
        tool = self._pick_tool("get_equity_quotes")
        data = self._call_tool_sync(tool, {"symbols": [symbol]})
        quote = _extract_first_equity_quote(data)
        price = _current_trade_price(quote)
        if price is None:
            raise MCPError(f"Could not find a price for {symbol} in {tool} response: {data}")
        return float(price)

    # --- Broker interface: no MCP historical-bars tool exists, so this
    # samples the live quote each call and accumulates its own bars ---

    def get_intraday_bars(self, symbol: str, since: dt.datetime) -> list[Bar]:
        price = self.get_underlying_price(symbol)
        # 1-minute buckets to match the strategy's 1-minute entry granularity
        # (see paper_broker.py's matching yfinance interval).
        builder = self._bar_builders.setdefault(symbol, BarBuilder(bucket_minutes=1))
        builder.add_quote(price, dt.datetime.now())
        return builder.bars_since(since)

    # --- Broker interface: options, MCP-backed ---

    def _resolve_account_number(self) -> str:
        if self._account_number is not None:
            return self._account_number
        data = self._call_tool_sync("get_accounts", {})
        accounts = (data.get("data") or {}).get("accounts") or []
        eligible = [
            a for a in accounts
            if a and a.get("agentic_allowed") is True
            and a.get("option_level") in _ELIGIBLE_OPTION_LEVELS
            and a.get("state") == "active"
            and not a.get("deactivated")
            and not a.get("permanently_deactivated")
        ]
        if not eligible:
            raise MCPError(
                "No Robinhood account is agentic_allowed with option_level_2/3 approval. "
                "Enable agentic trading and options level 2+ on an account, then retry."
            )
        chosen = next((a for a in eligible if a.get("is_default")), eligible[0])
        if len(eligible) > 1:
            logger.warning(
                "Multiple eligible accounts found, using account ending %s",
                str(chosen.get("account_number", ""))[-4:],
            )
        self._account_number = chosen["account_number"]
        return self._account_number

    @staticmethod
    def _extract_cursor(next_url: str | None) -> str | None:
        if not next_url:
            return None
        values = parse_qs(urlparse(next_url).query).get("cursor")
        return values[0] if values else None

    def _fetch_option_instruments(self, chain_id: str, expiration_date: str) -> list[dict]:
        # Deliberately NOT filtering by tradability="tradable" server-side --
        # get_0dte_chain() below needs to see non-tradable instruments too
        # (e.g. "position_closing_only") to tell an account-level trading
        # restriction apart from "no options exist for this date" in its
        # diagnostic logging, rather than both looking identically empty.
        instruments: list[dict] = []
        cursor: str | None = None
        for _ in range(20):  # pagination safety cap
            args = {
                "chain_id": chain_id, "expiration_dates": expiration_date,
                "state": "active",
            }
            if cursor:
                args["cursor"] = cursor
            data = self._call_tool_sync("get_option_instruments", args)
            page = (data.get("data") or {}).get("instruments") or []
            instruments.extend(i for i in page if i is not None)
            cursor = self._extract_cursor((data.get("data") or {}).get("next"))
            if not cursor:
                break
        return instruments

    def _fetch_option_quotes(self, instrument_ids: list[str]) -> dict[str, dict]:
        if not instrument_ids:
            return {}
        data = self._call_tool_sync("get_option_quotes", {"instrument_ids": instrument_ids})
        results = (data.get("data") or {}).get("results") or []
        quotes: dict[str, dict] = {}
        for entry in results:
            quote = (entry or {}).get("quote")
            if quote and quote.get("instrument_id"):
                quotes[quote["instrument_id"]] = quote
        return quotes

    def _build_contract(self, instrument: dict, quote: dict) -> OptionContract:
        bid = float(quote.get("bid_price") or 0)
        ask = float(quote.get("ask_price") or 0)
        mark = quote.get("mark_price")
        mid = float(mark) if mark else round((bid + ask) / 2, 2)
        self._instrument_cache[instrument["id"]] = instrument
        return OptionContract(
            symbol=instrument["chain_symbol"],
            strike=float(instrument["strike_price"]),
            option_type=OptionType(instrument["type"]),
            expiration=instrument["expiration_date"],
            bid=bid, ask=ask, mid=mid,
            contract_id=instrument["id"],
        )

    def get_0dte_chain(self, symbol: str) -> Sequence[OptionContract]:
        today = dt.date.today().isoformat()
        chains_data = self._call_tool_sync("get_option_chains", {"underlying_symbol": symbol})
        chains = (chains_data.get("data") or {}).get("chains") or []
        chain = next(
            (c for c in chains if c and today in (c.get("expiration_dates") or []) and c.get("can_open_position")),
            None,
        )
        if chain is None:
            logger.warning("No %s option chain with a %s expiration found (0DTE not available today?)", symbol, today)
            return []

        instruments = self._fetch_option_instruments(chain["id"], today)
        if not instruments:
            return []

        tradable = [i for i in instruments if i.get("tradability") == "tradable"]
        if not tradable:
            # Distinguishes "Robinhood has no 0DTE instruments for this
            # symbol today" (instruments == []) from "instruments exist but
            # none are openable" -- the latter is almost always an
            # account-level restriction (PDT, options approval level,
            # Agentic Trading permissions), not a data/parsing problem, so
            # it needs a human to check the account, not a code fix.
            statuses = sorted({i.get("tradability", "unknown") for i in instruments})
            logger.warning(
                "%s has %d active 0DTE instrument(s) for %s, but none have tradability=tradable "
                "(status seen: %s) -- likely an account-level restriction (PDT, options approval "
                "level, Agentic Trading permissions), not a data issue. Check your Robinhood account.",
                symbol, len(instruments), today, ", ".join(statuses),
            )
            return []

        quotes = self._fetch_option_quotes([i["id"] for i in tradable if i.get("id")])
        contracts = []
        for instrument in tradable:
            quote = quotes.get(instrument.get("id"))
            if quote is None:
                continue
            contracts.append(self._build_contract(instrument, quote))
        return contracts

    def get_option_quote(self, contract_id: str) -> OptionContract:
        instrument = self._instrument_cache.get(contract_id)
        if instrument is None:
            data = self._call_tool_sync("get_option_instruments", {"ids": contract_id})
            found = [i for i in ((data.get("data") or {}).get("instruments") or []) if i]
            if not found:
                raise MCPError(f"Option instrument {contract_id} not found via get_option_instruments")
            instrument = found[0]
        quote = self._fetch_option_quotes([contract_id]).get(contract_id)
        if quote is None:
            raise MCPError(f"No quote returned for option instrument {contract_id}")
        return self._build_contract(instrument, quote)

    def submit_order(
        self, contract: OptionContract, quantity: int, limit_price: float, side: str,
    ) -> OrderResult:
        try:
            account_number = self._resolve_account_number()
        except MCPError as exc:
            return OrderResult(submitted=False, broker_order_id=None, detail=str(exc))

        # This agent only ever buys to open and sells to close (never writes
        # naked/short options), so side alone determines position_effect.
        position_effect = "open" if side == "buy" else "close"
        legs = [{"option_id": contract.contract_id, "side": side, "position_effect": position_effect}]
        price_str = f"{limit_price:.2f}"

        review_args = {
            "account_number": account_number, "legs": legs, "quantity": str(quantity),
            "type": "limit", "price": price_str, "time_in_force": "gfd",
            "chain_symbol": contract.symbol, "underlying_type": "equity",
        }
        try:
            review = self._call_tool_sync("review_option_order", review_args)
        except MCPError as exc:
            return OrderResult(submitted=False, broker_order_id=None, detail=f"review_option_order failed: {exc}")

        order_checks = (review.get("data") or {}).get("order_checks") or {}
        if order_checks:
            detail = f"Robinhood flagged this order: {order_checks.get('alertType')} {order_checks.get('details')}"
            logger.warning("Declining to place order -- %s", detail)
            return OrderResult(submitted=False, broker_order_id=None, detail=detail)

        place_args = {
            "account_number": account_number, "legs": legs, "quantity": str(quantity),
            "type": "limit", "price": price_str, "time_in_force": "gfd",
            "ref_id": str(uuid.uuid4()),
        }
        try:
            placed = self._call_tool_sync("place_option_order", place_args)
        except MCPError as exc:
            return OrderResult(submitted=False, broker_order_id=None, detail=f"place_option_order failed: {exc}")

        order = (placed.get("data") or {}).get("order")
        if not order:
            return OrderResult(submitted=False, broker_order_id=None, detail=f"place_option_order returned no order: {placed}")
        return OrderResult(submitted=True, broker_order_id=order.get("id"), detail=f"state={order.get('state')}")
