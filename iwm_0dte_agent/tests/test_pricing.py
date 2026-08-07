from iwm_0dte_agent.models import OptionType
from iwm_0dte_agent.pricing import bs_price


def test_call_price_increases_with_moneyness():
    otm = bs_price(spot=200, strike=205, years_to_expiry=0.02, option_type=OptionType.CALL)
    atm = bs_price(spot=200, strike=200, years_to_expiry=0.02, option_type=OptionType.CALL)
    itm = bs_price(spot=200, strike=195, years_to_expiry=0.02, option_type=OptionType.CALL)
    assert otm < atm < itm


def test_put_price_increases_as_spot_falls_below_strike():
    otm = bs_price(spot=200, strike=195, years_to_expiry=0.02, option_type=OptionType.PUT)
    atm = bs_price(spot=200, strike=200, years_to_expiry=0.02, option_type=OptionType.PUT)
    itm = bs_price(spot=200, strike=205, years_to_expiry=0.02, option_type=OptionType.PUT)
    assert otm < atm < itm


def test_price_never_below_floor():
    price = bs_price(spot=100, strike=500, years_to_expiry=1e-8, option_type=OptionType.CALL)
    assert price >= 0.01
