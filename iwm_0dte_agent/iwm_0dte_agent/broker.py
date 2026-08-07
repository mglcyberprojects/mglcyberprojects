"""Broker interface plus the live Robinhood implementation.

Robinhood has no official public trading API. This module talks to it via
``robin_stocks``, an unofficial, reverse-engineered client. Using it to
automate trading is against Robinhood's Terms of Service and can lead to
account restrictions -- that risk sits entirely with whoever runs this in
``--live`` mode. Nothing in this module places an order on its own; the
agent loop only calls ``submit_order`` after an explicit human confirmation.
"""

from __future__ import annotations

import abc
import datetime as dt
import logging
from typing import Sequence

from .config import Config
from .models import Bar, OptionContract, OptionType, OrderResult

logger = logging.getLogger(__name__)


class BrokerError(RuntimeError):
    pass


class Broker(abc.ABC):
    """Everything the agent needs from a broker, live or simulated."""

    @abc.abstractmethod
    def login(self) -> None: ...

    @abc.abstractmethod
    def get_buying_power(self) -> float: ...

    @abc.abstractmethod
    def get_underlying_price(self, symbol: str) -> float: ...

    @abc.abstractmethod
    def get_intraday_bars(self, symbol: str, since: dt.datetime) -> list[Bar]: ...

    @abc.abstractmethod
    def get_0dte_chain(self, symbol: str) -> Sequence[OptionContract]: ...

    @abc.abstractmethod
    def get_option_quote(self, contract_id: str) -> OptionContract: ...

    @abc.abstractmethod
    def submit_order(
        self,
        contract: OptionContract,
        quantity: int,
        limit_price: float,
        side: str,
    ) -> OrderResult: ...


class RobinhoodBroker(Broker):
    """Live broker backed by robin_stocks. Only used with --live."""

    def __init__(self, config: Config):
        self._config = config
        self._rh = None  # imported lazily so --dry-run never needs the dependency

    def _client(self):
        if self._rh is None:
            try:
                import robin_stocks.robinhood as rh
            except ImportError as exc:
                raise BrokerError(
                    "robin_stocks is not installed. Run `pip install robin_stocks` "
                    "or use --dry-run instead of --live."
                ) from exc
            self._rh = rh
        return self._rh

    def login(self) -> None:
        rh = self._client()
        if not self._config.rh_username or not self._config.rh_password:
            raise BrokerError(
                "ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD are not set. "
                "Populate .env before running in --live mode."
            )
        logger.info("Logging in to Robinhood as %s", self._config.rh_username)
        rh.authentication.login(
            username=self._config.rh_username,
            password=self._config.rh_password,
            mfa_code=self._totp() if self._config.rh_totp_secret else None,
            store_session=True,
        )

    def _totp(self) -> str:
        import pyotp

        return pyotp.TOTP(self._config.rh_totp_secret).now()

    def get_buying_power(self) -> float:
        rh = self._client()
        profile = rh.profiles.load_account_profile()
        return float(profile["buying_power"])

    def get_underlying_price(self, symbol: str) -> float:
        rh = self._client()
        quote = rh.stocks.get_latest_price(symbol)
        return float(quote[0])

    def get_intraday_bars(self, symbol: str, since: dt.datetime) -> list[Bar]:
        rh = self._client()
        historicals = rh.stocks.get_stock_historicals(
            symbol, interval="5minute", span="day", bounds="regular"
        )
        bars: list[Bar] = []
        for h in historicals or []:
            ts = dt.datetime.fromisoformat(h["begins_at"].replace("Z", "+00:00"))
            if ts < since:
                continue
            bars.append(
                Bar(
                    timestamp=ts,
                    open=float(h["open_price"]),
                    high=float(h["high_price"]),
                    low=float(h["low_price"]),
                    close=float(h["close_price"]),
                    volume=float(h["volume"]),
                )
            )
        return bars

    def get_0dte_chain(self, symbol: str) -> Sequence[OptionContract]:
        rh = self._client()
        today = dt.date.today().isoformat()
        contracts: list[OptionContract] = []
        for option_type in (OptionType.CALL, OptionType.PUT):
            found = rh.options.find_tradable_options(
                symbol, expirationDate=today, optionType=option_type.value
            )
            for item in found or []:
                market = rh.options.get_option_market_data_by_id(item["id"])
                md = market[0] if market else {}
                bid = float(md.get("bid_price") or 0.0)
                ask = float(md.get("ask_price") or 0.0)
                contracts.append(
                    OptionContract(
                        symbol=symbol,
                        strike=float(item["strike_price"]),
                        option_type=option_type,
                        expiration=today,
                        bid=bid,
                        ask=ask,
                        mid=round((bid + ask) / 2, 2) if (bid or ask) else 0.0,
                        contract_id=item["id"],
                    )
                )
        return contracts

    def get_option_quote(self, contract_id: str) -> OptionContract:
        rh = self._client()
        info = rh.options.get_option_instrument_data_by_id(contract_id)
        market = rh.options.get_option_market_data_by_id(contract_id)
        md = market[0] if market else {}
        bid = float(md.get("bid_price") or 0.0)
        ask = float(md.get("ask_price") or 0.0)
        return OptionContract(
            symbol=info["chain_symbol"],
            strike=float(info["strike_price"]),
            option_type=OptionType(info["type"]),
            expiration=info["expiration_date"],
            bid=bid,
            ask=ask,
            mid=round((bid + ask) / 2, 2) if (bid or ask) else 0.0,
            contract_id=contract_id,
        )

    def submit_order(
        self,
        contract: OptionContract,
        quantity: int,
        limit_price: float,
        side: str,
    ) -> OrderResult:
        rh = self._client()
        logger.warning(
            "Submitting LIVE order: %s %d x %s %s %.2f @ limit %.2f",
            side, quantity, contract.symbol, contract.option_type.value,
            contract.strike, limit_price,
        )
        if side == "buy":
            result = rh.orders.order_buy_option_limit(
                positionEffect="open",
                creditOrDebit="debit",
                price=limit_price,
                symbol=contract.symbol,
                quantity=quantity,
                expirationDate=contract.expiration,
                strike=contract.strike,
                optionType=contract.option_type.value,
                timeInForce="gfd",
            )
        else:
            result = rh.orders.order_sell_option_limit(
                positionEffect="close",
                creditOrDebit="credit",
                price=limit_price,
                symbol=contract.symbol,
                quantity=quantity,
                expirationDate=contract.expiration,
                strike=contract.strike,
                optionType=contract.option_type.value,
                timeInForce="gfd",
            )
        if not result or "id" not in result:
            return OrderResult(submitted=False, broker_order_id=None, detail=str(result))
        return OrderResult(submitted=True, broker_order_id=result["id"], detail="submitted")
