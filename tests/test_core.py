from polymarket_weather_bot.account import PolymarketAccountConfig, PolymarketAccountSync
from polymarket_weather_bot.parser import parse_market_question, range_probability, one_tailed_probability
from polymarket_weather_bot.models import Market, Signal, Trade
from polymarket_weather_bot.strategy import WeatherStrategy
from polymarket_weather_bot.store import Store
from polymarket_weather_bot.dashboard import DashboardState
from polymarket_weather_bot.bot import BotEngine
from polymarket_weather_bot.executor import PolymarketLiveExecutor


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
    s = WeatherStrategy(min_volume=1000, max_spread=0.5, edge_threshold=0.01, max_days_out=5000)
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


def test_strategy_honors_city_and_term_filters(monkeypatch):
    monkeypatch.setenv('BOT_ALLOWED_CITIES', 'Seoul,Tokyo')
    monkeypatch.setenv('BOT_BLOCKED_TERMS', 'humidity,rain')
    s = WeatherStrategy(min_volume=1000, max_spread=0.5, edge_threshold=0.01, max_days_out=5000)

    allowed_market = Market(
        id='2',
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
    blocked_market = Market(
        id='3',
        question='Will Seoul humidity be above 70% on 2030-04-17?',
        slug='seoul-humidity',
        condition_id='0xdef',
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

    assert s.analyze_market(allowed_market)['skip'] is False
    assert s.analyze_market(blocked_market)['reason'] == 'blocked_term'


def test_live_account_config_reads_session_hint(monkeypatch):
    monkeypatch.delenv('BOT_POLYMARKET_WALLET_ADDRESS', raising=False)
    monkeypatch.delenv('BOT_POLYMARKET_PROXY_ADDRESS', raising=False)
    monkeypatch.setenv('BOT_POLYMARKET_SESSION_HINT', '{"proxyAddress":"0x1234567890abcdef1234567890abcdef12345678","authenticationType":"magic"}')
    config = PolymarketAccountConfig.from_env()
    assert config.proxy_address == '0x1234567890abcdef1234567890abcdef12345678'
    assert config.wallet_address == '0x1234567890abcdef1234567890abcdef12345678'
    assert config.authentication_type == 'magic'


def test_live_account_sync_uses_session_proxy_address(monkeypatch):
    monkeypatch.setattr('polymarket_weather_bot.account._get_onchain_usdc_balance', lambda addr: 3.69)

    def fake_http_get(url, params=None, timeout=25):
        if 'public-profile' in url:
            assert params == {'address': '0x1234567890abcdef1234567890abcdef12345678'}
            return {
                'name': 'King Proxy',
                'pseudonym': 'king-session',
                'xUsername': 'kingsession',
                'proxyWallet': '0xProxySession',
                'verifiedBadge': False,
            }
        if '/positions' in url:
            assert params == {'user': '0x1234567890abcdef1234567890abcdef12345678', 'limit': 100}
            return [
                {
                    'title': 'Will Toronto be below 12°C on 2026-04-24?',
                    'slug': 'toronto-weather',
                    'conditionId': '0xabc',
                    'outcome': 'YES',
                    'size': '4.5',
                    'avgPrice': '0.0625',
                    'currentValue': '0.2',
                    'cashPnl': '0.05',
                    'percentPnl': '33.3',
                    'curPrice': '0.08',
                }
            ]
        if '/value' in url:
            assert params == {'user': '0x1234567890abcdef1234567890abcdef12345678'}
            return {'value': '0.2'}
        raise AssertionError(f'unexpected url {url}')

    sync = PolymarketAccountSync(
        PolymarketAccountConfig(
            proxy_address='0x1234567890abcdef1234567890abcdef12345678',
            authentication_type='magic',
        ),
        http_get=fake_http_get,
    )
    result = sync.sync()
    assert result['status'] == 'read_only'
    assert result['proxy_address'] == '0x1234567890abcdef1234567890abcdef12345678'
    assert result['authentication_type'] == 'magic'
    assert result['profile']['name'] == 'King Proxy'
    assert result['positions_count'] == 1
    assert result['portfolio_value'] == 0.2
    assert result['wallet_balance'] == 3.69
    assert result['equity'] == 3.89
    assert result['open_orders_count'] == 0


def test_live_account_sync_normalizes_profile_positions_and_balance():
    def fake_http_get(url, params=None, timeout=25):
        if 'public-profile' in url:
            return {
                'name': 'King Wallet',
                'pseudonym': 'king-alpha',
                'xUsername': 'kingalpha',
                'proxyWallet': '0xProxy',
                'verifiedBadge': True,
            }
        if '/positions' in url:
            return [
                {
                    'title': 'Will Toronto be below 12°C on 2026-04-24?',
                    'slug': 'toronto-weather',
                    'conditionId': '0xabc',
                    'outcome': 'YES',
                    'size': '4.5',
                    'avgPrice': '0.0625',
                    'currentValue': '0.2',
                    'cashPnl': '0.05',
                    'percentPnl': '33.3',
                    'curPrice': '0.08',
                }
            ]
        if '/value' in url:
            return {'value': '0.2'}
        raise AssertionError(f'unexpected url {url}')

    class FakeClient:
        def get_balance_allowance(self, params=None):
            return {'balance': '12.34', 'allowance': '20.00'}

        def get_orders(self, params=None, next_cursor='MA=='):
            return [{'id': 'order-1'}]

    sync = PolymarketAccountSync(
        PolymarketAccountConfig(wallet_address='0x1234567890abcdef1234567890abcdef12345678', private_key='0xdeadbeef'),
        http_get=fake_http_get,
        client_factory=lambda config: FakeClient(),
    )
    result = sync.sync()
    assert result['status'] == 'connected'
    assert result['profile']['name'] == 'King Wallet'
    assert result['positions_count'] == 1
    assert result['positions'][0]['outcome'] == 'YES'
    assert result['portfolio_value'] == 0.2
    assert result['balance']['balance'] == 12.34
    assert result['open_orders_count'] == 1
    assert result['trading_ready'] is True


def test_live_account_sync_supports_solana_deposit_balance(monkeypatch):
    monkeypatch.setattr('polymarket_weather_bot.account._get_onchain_usdc_balance', lambda addr: 3.69)

    class FakeClient:
        def get_balance_allowance(self, params=None):
            return {'balance': '3.69', 'allowance': '0.00'}

        def get_orders(self, params=None, next_cursor='MA=='):
            return []

    sync = PolymarketAccountSync(
        PolymarketAccountConfig(
            wallet_address='0x1234567890abcdef1234567890abcdef12345678',
            deposit_address='Anb1TGWNeu7Nb4LXoikpYGsouQkvzosqVxfAXwk1527',
            private_key='0xdeadbeef',
        ),
        http_get=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not call public profile endpoints for Solana deposit balance lookups')),
        client_factory=lambda config: FakeClient(),
    )
    result = sync.sync()
    assert result['status'] == 'connected'
    assert result['wallet_balance'] == 3.69
    assert result['equity'] == 3.69
    assert result['positions_count'] == 0
    assert result['open_orders_count'] == 0


def test_dashboard_state_and_controls(tmp_path):
    store = Store(str(tmp_path / 'bot.db'))
    store.set_control('paused', True)
    store.set_control('force_scan', False)
    state = DashboardState(store).current_state()
    assert state['health']['paused'] is True
    assert state['controls']['paused'] is True
    assert state['alerts']['enabled'] in (True, False)
    assert 'freshness_seconds' in state
    assert state['market_scans_count'] == 0
    assert state['forecast_snapshots_count'] == 0
    assert state['signal_outcomes_count'] == 0


def test_bot_engine_records_market_scan_forecast_and_signal_outcome(monkeypatch, tmp_path):
    store = Store(str(tmp_path / 'bot.db'))
    strategy = WeatherStrategy(min_volume=1000, max_spread=0.5, edge_threshold=0.01)
    sync = PolymarketAccountSync(
        PolymarketAccountConfig(
            wallet_address='0x1234567890abcdef1234567890abcdef12345678',
            private_key='0xdeadbeef',
        ),
        http_get=lambda *args, **kwargs: {'value': '0'},
        client_factory=lambda config: object(),
    )
    engine = BotEngine(store, strategy, mode='paper', account_sync=sync)
    market = Market(
        id='m-scan',
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

    def fake_analyze_market(market_obj):
        signal = Signal(
            market_id=market_obj.id,
            question=market_obj.question,
            city='Seoul',
            date='2030-04-17',
            market_prob=0.20,
            model_prob=0.70,
            edge=0.50,
            action='BUY_YES',
            confidence=0.9,
            rationale='edge=+50.00%',
            generated_at='2030-04-17T00:00:00Z',
        )
        return {
            'skip': False,
            'signal': signal,
            'meta': {'kind': 'range'},
            'forecast': {'city': 'Seoul', 'date': '2030-04-17', 'mean': 17.5, 'high': 18.5, 'low': 16.5, 'sigma': 1.0},
        }

    monkeypatch.setattr(strategy, 'analyze_market', fake_analyze_market)
    monkeypatch.setattr(strategy, 'should_enter', lambda signal, open_positions: False)
    snapshot = engine.scan_and_trade([market])
    assert snapshot['market_scans_count'] == 1
    assert snapshot['forecast_snapshots_count'] == 1
    assert snapshot['signal_outcomes_count'] == 1
    assert store.get_market_scans(10)[0]['analysis']['skip'] is False
    assert store.get_forecast_snapshots(10)[0]['forecast']['city'] == 'Seoul'
    assert store.get_signal_outcomes(10)[0]['signal']['action'] == 'BUY_YES'


def test_bot_engine_switches_to_live_executor(monkeypatch, tmp_path):
    monkeypatch.setenv('BOT_LIVE_ORDER_STYLE', 'market')
    store = Store(str(tmp_path / 'bot.db'))
    strategy = WeatherStrategy(min_volume=1000, max_spread=0.5, edge_threshold=0.01)
    sync = PolymarketAccountSync(
        PolymarketAccountConfig(
            wallet_address='0x1234567890abcdef1234567890abcdef12345678',
            private_key='0xdeadbeef',
        ),
        http_get=lambda *args, **kwargs: {'value': '0'},
        client_factory=lambda config: object(),
    )
    engine = BotEngine(store, strategy, mode='live', account_sync=sync)
    assert isinstance(engine.executor, PolymarketLiveExecutor)


def test_live_executor_places_market_order_and_persists_trade(tmp_path):
    store = Store(str(tmp_path / 'bot.db'))
    config = PolymarketAccountConfig(
        wallet_address='0x1234567890abcdef1234567890abcdef12345678',
        private_key='0xdeadbeef',
    )

    class FakeBookLevel:
        def __init__(self, price):
            self.price = price

    class FakeBook:
        tick_size = '0.01'
        asks = [FakeBookLevel('0.12')]

    class FakeClient:
        def get_order_book(self, token_id):
            assert token_id == 'yes-token'
            return FakeBook()

        def calculate_market_price(self, token_id, side, amount, order_type):
            assert token_id == 'yes-token'
            assert side == 'BUY'
            return 0.12

        def create_market_order(self, order_args):
            assert order_args.token_id == 'yes-token'
            assert order_args.side == 'BUY'
            assert order_args.amount == 5.0
            return {'local': 'order'}

        def post_order(self, order, orderType=None, post_only=False):
            assert order == {'local': 'order'}
            return {
                'orderID': 'order-123',
                'status': 'filled',
                'avgPrice': '0.11',
                'sizeMatched': '45.4545',
            }

        def get_orders(self, params=None, next_cursor='MA=='):
            return []

    executor = PolymarketLiveExecutor(
        store=store,
        config=config,
        client_factory=lambda cfg: FakeClient(),
    )
    market = Market(
        id='m1',
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
        clob_yes_token='yes-token',
        clob_no_token='no-token',
    )
    signal = Signal(
        market_id='m1',
        question=market.question,
        city='Seoul',
        date='2030-04-17',
        market_prob=0.20,
        model_prob=0.72,
        edge=0.52,
        action='BUY_YES',
        confidence=0.9,
        rationale='city=Seoul; edge=+52.00%',
        generated_at='2030-04-17T00:00:00Z',
    )
    pos = executor.open_position(signal, 5.0, market=market)
    assert pos is not None
    assert pos.source == 'live'
    assert pos.order_id == 'order-123'
    assert pos.status == 'open'
    trades = store.get_trades(10)
    assert trades[0]['mode'] == 'live'
    assert trades[0]['status'] == 'filled'
    assert trades[0]['order_id'] == 'order-123'

