import datetime as dt

from iwm_0dte_agent.config import Config
from iwm_0dte_agent.risk import RiskManager


def make_config(**overrides) -> Config:
    defaults = dict(
        risk_pct_per_trade=0.01,
        stop_loss_pct=0.50,
        profit_target_pct=1.00,
        max_trades_per_day=2,
        max_daily_loss_pct=0.03,
        max_contracts_per_trade=5,
        entry_cutoff=dt.time(14, 30),
        hard_exit=dt.time(15, 45),
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_position_size_respects_risk_budget():
    risk = RiskManager(config=make_config())
    # $25,000 buying power, 1% risk = $250 budget. Premium $1.00/contract,
    # stop at 50% -> max loss per contract = $50. Expect 5 contracts (capped).
    assert risk.position_size(buying_power=25_000, premium_per_contract=1.00) == 5


def test_position_size_capped_by_max_contracts():
    risk = RiskManager(config=make_config(max_contracts_per_trade=2))
    assert risk.position_size(buying_power=25_000, premium_per_contract=1.00) == 2


def test_position_size_zero_premium():
    risk = RiskManager(config=make_config())
    assert risk.position_size(buying_power=25_000, premium_per_contract=0.0) == 0


def test_cheap_otm_position_size_buys_one_when_affordable():
    risk = RiskManager(config=make_config())
    # $50 buying power, $0.35 premium -> $35/contract, fits.
    assert risk.cheap_otm_position_size(buying_power=50, premium_per_contract=0.35) == 1


def test_cheap_otm_position_size_zero_when_too_expensive():
    risk = RiskManager(config=make_config())
    # $50 buying power, $0.65 premium -> $65/contract, doesn't fit.
    assert risk.cheap_otm_position_size(buying_power=50, premium_per_contract=0.65) == 0


def test_cheap_otm_position_size_exactly_at_budget_fits():
    risk = RiskManager(config=make_config())
    assert risk.cheap_otm_position_size(buying_power=50, premium_per_contract=0.50) == 1


def test_cheap_otm_position_size_zero_premium():
    risk = RiskManager(config=make_config())
    assert risk.cheap_otm_position_size(buying_power=50, premium_per_contract=0.0) == 0


def test_quantity_choices_returns_all_affordable_candidates():
    risk = RiskManager(config=make_config())
    # $25,000 buying power, $2.00 premium -> $200/contract. 1/3/5 all fit.
    assert risk.quantity_choices(buying_power=25_000, premium_per_contract=2.00) == [1, 3, 5]


def test_quantity_choices_filters_by_affordability():
    risk = RiskManager(config=make_config())
    # $500 buying power, $2.00 premium -> $200/contract. Only 1 fits (3x = $600).
    assert risk.quantity_choices(buying_power=500, premium_per_contract=2.00) == [1]


def test_quantity_choices_boundary_exactly_affordable():
    risk = RiskManager(config=make_config())
    # $600 buying power, $2.00 premium -> $200/contract. 3x = $600 exactly fits, 5x = $1000 doesn't.
    assert risk.quantity_choices(buying_power=600, premium_per_contract=2.00) == [1, 3]


def test_quantity_choices_capped_by_max_contracts_per_trade():
    risk = RiskManager(config=make_config(max_contracts_per_trade=3))
    # Affordable up to 5, but capped at max_contracts_per_trade=3.
    assert risk.quantity_choices(buying_power=25_000, premium_per_contract=2.00) == [1, 3]


def test_quantity_choices_empty_when_nothing_affordable():
    risk = RiskManager(config=make_config())
    assert risk.quantity_choices(buying_power=50, premium_per_contract=2.00) == []


def test_quantity_choices_zero_premium():
    risk = RiskManager(config=make_config())
    assert risk.quantity_choices(buying_power=25_000, premium_per_contract=0.0) == []


def test_max_trades_per_day_blocks_further_entries():
    risk = RiskManager(config=make_config(max_trades_per_day=1))
    can_open, _ = risk.can_open_new_trade(buying_power=25_000, now=dt.time(10, 0))
    assert can_open is True
    risk.record_trade_opened()
    can_open, why = risk.can_open_new_trade(buying_power=25_000, now=dt.time(10, 30))
    assert can_open is False
    assert "max trades" in why


def test_entry_cutoff_blocks_new_trades():
    risk = RiskManager(config=make_config())
    can_open, why = risk.can_open_new_trade(buying_power=25_000, now=dt.time(15, 0))
    assert can_open is False
    assert "cutoff" in why


def test_daily_loss_limit_blocks_new_trades():
    risk = RiskManager(config=make_config(max_daily_loss_pct=0.02))
    risk.record_trade_closed(pnl=-600)  # -2.4% of 25k
    can_open, why = risk.can_open_new_trade(buying_power=25_000, now=dt.time(10, 0))
    assert can_open is False
    assert "loss limit" in why


def test_is_hard_exit_time():
    risk = RiskManager(config=make_config())
    assert risk.is_hard_exit_time(dt.time(15, 44)) is False
    assert risk.is_hard_exit_time(dt.time(15, 45)) is True
    assert risk.is_hard_exit_time(dt.time(16, 0)) is True
