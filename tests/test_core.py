from polymarket_weather_bot.parser import parse_market_question, range_probability, one_tailed_probability
from polymarket_weather_bot.models import Market, Signal, Trade
from polymarket_weather_bot.strategy import WeatherStrategy
from polymarket_weather_bot.store import Store
from polymarket_weather_bot.dashboard import DashboardState


def test_parse_temperature_range_city():
    q = 'Will Seoul be between 17°C and 18°C on 2030-04-17?'
    meta = parse_market_question(q)
    assert meta['kind'] == 'range'
    assert meta['low'] == 17.0
    assert meta['high'] == 18.0
    assert 'Seoul' in meta['city']


def test_probability_helpers():
    p = range_probability(17, 18, mean=17.4, sigma=1.0)
    assert 0.0 < p < 1.0
    assert one_tailed_probability('above', 18, mean=17, sigma=1.0) > 0.1


def test_strategy_builds_signal_for_weather_market(monkeypatch):
    s = WeatherStrategy(min_volume=1000, max_spread=0.5, edge_threshold=0.01)
    market = Market(
        id='1',
        question='Will Seoul be between 17°C and 18°C on 2030-04-17?',
        slug='seoul-weather',
        condition_id='0xabc',
        yes_price=0.20,
        no_price=0.80,
        volume=10000,
        liquidity=5000,
        active=True,
        closed=False,
        end_date='2030-04-17T00:00:00Z',
    )

    monkeypatch.setattr('polymarket_weather_bot.strategy.geocode_city', lambda city: {'latitude': 1.0, 'longitude': 2.0})
    monkeypatch.setattr('polymarket_weather_bot.strategy.forecast_city', lambda lat, lon: {
        'daily': {
            'time': ['2030-04-17'],
            'temperature_2m_mean': [17.5],
            'temperature_2m_max': [20.0],
            'temperature_2m_min': [15.0],
        }
    })
    res = s.analyze_market(market)
    assert res['skip'] is False
    signal = res['signal']
    assert signal.market_id == '1'
    assert signal.action in ('BUY_YES', 'BUY_NO', 'HOLD')


def test_dashboard_state_and_controls(tmp_path):
    store = Store(str(tmp_path / 'bot.db'))
    store.set_control('paused', True)
    store.set_control('force_scan', False)
    state = DashboardState(store).current_state()
    assert state['health']['paused'] is True
    assert state['controls']['paused'] is True
    assert state['alerts']['enabled'] in (True, False)
    assert 'freshness_seconds' in state
