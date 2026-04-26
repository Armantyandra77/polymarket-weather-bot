from polymarket_weather_bot.account import PolymarketAccountConfig, PolymarketAccountSync
from polymarket_weather_bot.parser import parse_market_question, range_probability, one_tailed_probability
from polymarket_weather_bot.models import Market, Signal, Trade
from polymarket_weather_bot.strategy import WeatherStrategy
from polymarket_weather_bot.store import Store
from polymarket_weather_bot.dashboard import DashboardState
from polymarket_weather_bot.bot import BotEngine
from polymarket_weather_bot.weather_sources import build_forecast_ensemble
from polymarket_weather_bot.executor import PolymarketLiveExecutor
from polymarket_weather_bot.notifier import TelegramNotifier
from polymarket_weather_bot.telegram_commands import TelegramCommandService
from polymarket_weather_bot.run_bot import _clear_stale_signer_block


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


def test_weather_ensemble_blends_open_meteo_and_nws(monkeypatch):
    def fake_get_json(url, params=None, timeout=30):
        if 'api.weather.gov/points' in url:
            return {'properties': {'forecast': 'https://api.weather.gov/gridpoints/ABC/forecast'}}
        if 'gridpoints/ABC/forecast' in url:
            return {
                'properties': {
                    'periods': [
                        {'startTime': '2030-04-17T06:00:00+00:00', 'temperature': 68, 'temperatureUnit': 'F', 'isDaytime': True},
                        {'startTime': '2030-04-17T18:00:00+00:00', 'temperature': 61, 'temperatureUnit': 'F', 'isDaytime': False},
                    ]
                }
            }
        return {
            'daily': {
                'time': ['2030-04-17'],
                'temperature_2m_mean': [18.0],
                'temperature_2m_max': [21.0],
                'temperature_2m_min': [15.0],
            }
        }

    monkeypatch.setattr('polymarket_weather_bot.weather_sources._get_json', fake_get_json)
    forecast = build_forecast_ensemble(41.0, -87.0, geocoded={'name': 'Chicago', 'country_code': 'US'})
    assert forecast['source'] == 'blend'
    assert len(forecast['sources']) == 2
    stats = forecast['daily']
    assert stats['time'] == ['2030-04-17']
    assert 18.0 < stats['temperature_2m_mean'][0] < 20.5
    assert stats['temperature_2m_max'][0] >= stats['temperature_2m_mean'][0]
    assert stats['temperature_2m_min'][0] <= stats['temperature_2m_mean'][0]
    assert forecast['blend']['source_count'] == 2


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
    monkeypatch.setattr('polymarket_weather_bot.strategy.forecast_city', lambda lat, lon, **kwargs: {
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
    monkeypatch.setattr('polymarket_weather_bot.strategy.forecast_city', lambda lat, lon, **kwargs: {
        'daily': {
            'time': ['2030-04-17'],
            'temperature_2m_mean': [17.5],
            'temperature_2m_max': [20.0],
            'temperature_2m_min': [15.0],
        }
    })

    assert s.analyze_market(allowed_market)['skip'] is False
    assert s.analyze_market(blocked_market)['reason'] == 'blocked_term'


def test_strategy_risk_management_prefers_yes_and_no_and_blocks_duplicate_city(monkeypatch):
    monkeypatch.setenv('BOT_RISK_MIN_CONFIDENCE', '0.92')
    monkeypatch.setenv('BOT_RISK_MAX_POSITION_FRACTION', '0.03')
    monkeypatch.setenv('BOT_RISK_MAX_TOTAL_EXPOSURE_FRACTION', '0.10')
    monkeypatch.setenv('BOT_RISK_MAX_CITY_EXPOSURE_FRACTION', '0.04')
    monkeypatch.setenv('BOT_RISK_MAX_CITY_POSITIONS', '1')
    s = WeatherStrategy(min_volume=1000, max_spread=0.5, edge_threshold=0.10, max_days_out=5000)
    buy_no = Signal(
        market_id='n1',
        question='Will Seoul be below 14°C on 2030-04-17?',
        city='Seoul',
        date='2030-04-17',
        market_prob=0.68,
        model_prob=0.42,
        edge=-0.26,
        action='BUY_NO',
        confidence=0.96,
        rationale='edge=-26.00%',
        generated_at='2030-04-17T00:00:00Z',
    )
    open_positions = [
        {'status': 'open', 'budget': 2.5, 'meta': {'city': 'Seoul', 'date': '2030-04-17'}},
    ]
    assert s.should_enter(buy_no, open_positions=0) is True
    assert s.passes_risk_limits(buy_no, open_positions, bankroll=100.0) is False
    sized = s.recommended_size(buy_no, bankroll=100.0, positions=[])
    assert 1.0 <= sized <= 3.0


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
    assert result['portfolio_value_source'] == 'data-api:/value'
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

        def get_open_orders(self, params=None):
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
    assert result['order_history_count'] == 1
    assert result['order_history'][0]['status'] == 'unknown'


def test_live_account_sync_supports_solana_deposit_balance(monkeypatch):
    monkeypatch.setattr('polymarket_weather_bot.account._get_onchain_usdc_balance', lambda addr: 3.69 if addr.startswith('Anb') else 0.0)

    class FakeClient:
        def get_balance_allowance(self, params=None):
            return {'balance': '3.69', 'allowance': '0.00'}

        def get_open_orders(self, params=None):
            return []

    sync = PolymarketAccountSync(
        PolymarketAccountConfig(
            wallet_address='0x1234567890abcdef1234567890abcdef12345678',
            deposit_address='Anb1TGWNeu7Nb4LXoikpYGsouQkvzosqVxfAXwk1527',
            solana_address='Anb1TGWNeu7Nb4LXoikpYGsouQkvzosqVxfAXwk1527',
            private_key='0xdeadbeef',
        ),
        http_get=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not call public profile endpoints for Solana deposit balance lookups')),
        client_factory=lambda config: FakeClient(),
    )
    result = sync.sync()
    assert result['status'] == 'connected'
    assert result['wallet_balance'] == 3.69
    assert result['equity'] == 3.69
    assert result['portfolio_value_source'] == 'unavailable'
    assert result['positions_count'] == 0
    assert result['open_orders_count'] == 0
    assert result['balance_sources'][0]['kind'] == 'solana'


def test_live_account_sync_refreshes_stale_clob_balance_from_wallet(monkeypatch):
    monkeypatch.setattr('polymarket_weather_bot.account._get_onchain_usdc_balance', lambda addr: 7.25 if addr == '0x1234567890abcdef1234567890abcdef12345678' else 0.0)

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.refreshed = False
            self.signer = type('Signer', (), {'address': lambda self: '0x1234567890abcdef1234567890abcdef12345678'})()

        def get_balance_allowance(self, params=None):
            self.calls.append('get_balance_allowance')
            if self.refreshed:
                return {'balance': '7.25', 'allowance': '7.25'}
            return {'balance': '0', 'allowance': '0'}

        def update_balance_allowance(self, params=None):
            self.calls.append('update_balance_allowance')
            self.refreshed = True
            return {'balance': '7.25', 'allowance': '7.25'}

        def get_open_orders(self, params=None):
            self.calls.append('get_open_orders')
            return []

    client = FakeClient()
    sync = PolymarketAccountSync(
        PolymarketAccountConfig(
            wallet_address='0x1234567890abcdef1234567890abcdef12345678',
            private_key='0xdeadbeef',
        ),
        http_get=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not call public endpoints in this test')),
        client_factory=lambda config: client,
    )
    result = sync.sync()
    assert result['status'] == 'connected'
    assert result['wallet_balance'] == 7.25
    assert result['balance']['balance'] == 7.25
    assert result['balance']['allowance'] == 7.25
    assert result['balance']['refreshed'] is True
    assert result['balance']['source'] == 'clob-collateral'
    assert result['clob_signer_address'] == '0x1234567890abcdef1234567890abcdef12345678'
    assert result['auth_layers']['signer_address'] == '0x1234567890abcdef1234567890abcdef12345678'
    assert client.calls.count('update_balance_allowance') == 1
    assert client.calls.count('get_balance_allowance') == 2
    assert client.calls.count('get_open_orders') == 1


def test_live_account_sync_records_signer_address_without_blocking(monkeypatch):
    monkeypatch.setattr('polymarket_weather_bot.account._get_onchain_usdc_balance', lambda addr: 14.4616 if addr == '0x66025B8D4004CF5a6b288e80fCe16738D819cB25' else 0.0)

    class FakeClient:
        def __init__(self):
            self.signer = type('Signer', (), {'address': lambda self: '0xa035Ba4e9e99A3638468B9b29C2F1788BbBFdE04'})()

        def get_balance_allowance(self, params=None):
            return {'balance': '0', 'allowance': '0'}

        def get_open_orders(self, params=None):
            return []

    sync = PolymarketAccountSync(
        PolymarketAccountConfig(
            wallet_address='0x66025B8D4004CF5a6b288e80fCe16738D819cB25',
            proxy_address='0x66025B8D4004CF5a6b288e80fCe16738D819cB25',
            private_key='0xdeadbeef',
        ),
        http_get=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not call public endpoints in this test')),
        client_factory=lambda config: FakeClient(),
    )
    result = sync.sync()
    assert result['status'] == 'connected'
    assert result['trading_ready'] is True
    assert result['clob_signer_address'] == '0xa035Ba4e9e99A3638468B9b29C2F1788BbBFdE04'
    assert result['auth_layers']['signer_address'] == '0xa035Ba4e9e99A3638468B9b29C2F1788BbBFdE04'
    assert not any('does not match the funded wallet/proxy address' in w for w in result['warnings'])


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
    assert state['forecast_outcomes_count'] == 0
    assert state['calibration_summary']['records_count'] == 0


def test_store_and_dashboard_calibration_summary(tmp_path):
    store = Store(str(tmp_path / 'bot.db'))
    store.save_forecast_outcome({
        'market_id': 'm1',
        'forecast_type': 'numeric',
        'predicted_value': 10.0,
        'actual_value': 12.0,
        'created_at': '2030-04-17T00:00:00Z',
    })
    store.save_forecast_outcome({
        'market_id': 'm2',
        'forecast_type': 'numeric',
        'predicted_value': 14.0,
        'actual_value': 13.0,
        'created_at': '2030-04-17T01:00:00Z',
    })
    store.save_forecast_outcome({
        'market_id': 'm3',
        'forecast_type': 'binary',
        'predicted_probability': 0.8,
        'actual_outcome': 1,
        'created_at': '2030-04-17T02:00:00Z',
    })
    store.save_forecast_outcome({
        'market_id': 'm4',
        'forecast_type': 'binary',
        'predicted_probability': 0.3,
        'actual_outcome': 0,
        'created_at': '2030-04-17T03:00:00Z',
    })

    summary = store.get_forecast_calibration_summary()
    assert summary['records_count'] == 4
    assert summary['numeric_records_count'] == 2
    assert summary['binary_records_count'] == 2
    assert summary['mae'] == 1.5
    assert summary['rmse'] == 1.5811
    assert summary['accuracy'] == 1.0
    assert summary['brier_score'] == 0.065

    state = DashboardState(store).current_state()
    assert state['forecast_outcomes_count'] == 4
    assert state['calibration_summary']['mae'] == 1.5
    assert state['calibration_summary']['accuracy'] == 1.0
    assert len(state['latest_forecast_outcomes']) == 4


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


def test_store_persists_order_snapshots_and_events(tmp_path):
    store = Store(str(tmp_path / 'bot.db'))
    store.save_account_order_snapshot({'created_at': '2030-04-17T00:00:00Z', 'orders': [{'id': 'o1'}], 'source': 'clob-open-orders'})
    store.save_account_order_events([
        {'order_id': 'o1', 'event_type': 'created', 'payload': {'id': 'o1'}, 'created_at': '2030-04-17T00:00:01Z'}
    ])
    snapshots = store.get_account_order_snapshots(10)
    events = store.get_account_order_events(10)
    assert snapshots[0]['source'] == 'clob-open-orders'
    assert snapshots[0]['orders'][0]['id'] == 'o1'
    assert events[0]['event_type'] == 'created'
    assert events[0]['order_id'] == 'o1'


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


def test_live_executor_places_market_order_and_persists_trade(monkeypatch, tmp_path):
    monkeypatch.setenv('BOT_LIVE_ORDER_STYLE', 'market')
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

        def post_order(self, order, order_type=None, post_only=False):
            assert order == {'local': 'order'}
            return {
                'orderID': 'order-123',
                'status': 'filled',
                'avgPrice': '0.11',
                'sizeMatched': '45.4545',
            }

        def get_open_orders(self, params=None):
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


def test_live_executor_refreshes_balance_allowance_from_last_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv('BOT_LIVE_ORDER_STYLE', 'market')
    store = Store(str(tmp_path / 'bot.db'))
    store.save_snapshot({'live_account': {'wallet_balance': 14.4616}})
    config = PolymarketAccountConfig(
        wallet_address='0x1234567890abcdef1234567890abcdef12345678',
        private_key='0xdeadbeef',
    )

    class FakeBook:
        tick_size = '0.01'
        asks = []

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.refreshed = False

        def get_balance_allowance(self, params=None):
            self.calls.append('get_balance_allowance')
            if self.refreshed:
                return {'balance': '14.4616', 'allowance': '14.4616'}
            return {'balance': '0', 'allowance': '0'}

        def update_balance_allowance(self, params=None):
            self.calls.append('update_balance_allowance')
            self.refreshed = True
            return {'balance': '14.4616', 'allowance': '14.4616'}

        def get_order_book(self, token_id):
            return FakeBook()

        def calculate_market_price(self, token_id, side, amount, order_type):
            return 0.10

        def create_market_order(self, order_args):
            return {'local': 'order'}

        def post_order(self, order, order_type=None, post_only=False):
            return {'orderID': 'order-allowance'}

        def get_open_orders(self, params=None):
            return []

    client = FakeClient()
    executor = PolymarketLiveExecutor(
        store=store,
        config=config,
        client_factory=lambda cfg: client,
    )
    market = Market(
        id='m-allowance',
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
        market_id='m-allowance',
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
    pos = executor.open_position(signal, 1.0, market=market)
    assert pos is not None
    assert client.calls.count('update_balance_allowance') == 1
    assert client.calls.count('get_balance_allowance') == 1


def test_live_executor_records_geoblock_and_pauses_on_region_restriction(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_LIVE_ORDER_STYLE', 'limit')
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
            return FakeBook()

        def create_order(self, order_args):
            return {'local': 'order'}

        def post_order(self, order, order_type=None, post_only=False):
            raise RuntimeError('403 Trading restricted in your region')

    executor = PolymarketLiveExecutor(
        store=store,
        config=config,
        client_factory=lambda cfg: FakeClient(),
    )
    market = Market(
        id='m-geo',
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
        market_id='m-geo',
        question=market.question,
        city='Seoul',
        date='2030-04-17',
        market_prob=0.20,
        model_prob=0.72,
        edge=0.52,
        action='BUY_YES',
        confidence=0.96,
        rationale='city=Seoul; edge=+52.00%',
        generated_at='2030-04-17T00:00:00Z',
    )

    pos = executor.open_position(signal, 1.0, market=market)

    assert pos is None
    controls = store.get_controls()
    assert controls['paused'] is True
    assert controls['live_execution_blocked'] is True
    assert 'region' in str(controls['live_execution_block_reason']).lower()
    errors = store.get_errors(5)
    assert errors[0]['stage'] == 'live_order_geoblock'
    assert errors[0]['market_id'] == 'm-geo'


def test_clear_stale_signer_block_resets_persisted_controls(tmp_path):
    store = Store(str(tmp_path / 'bot.db'))
    store.set_control('paused', True)
    store.set_control('live_execution_blocked', True)
    store.set_control('live_execution_block_reason', 'CLOB signer address mismatch')
    store.set_control('live_execution_block_stage', 'signer_mismatch')

    cleared = _clear_stale_signer_block(store, {'status': 'connected', 'trading_ready': True})

    assert cleared is True
    controls = store.get_controls()
    assert controls.get('paused', False) is False
    assert controls.get('live_execution_blocked', False) is False
    assert controls.get('live_execution_block_reason') in ('', None)
    assert controls.get('live_execution_block_stage') in ('', None)


def test_live_executor_does_not_block_on_signer_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv('BOT_LIVE_ORDER_STYLE', 'market')
    store = Store(str(tmp_path / 'bot.db'))
    config = PolymarketAccountConfig(
        wallet_address='0x66025B8D4004CF5a6b288e80fCe16738D819cB25',
        proxy_address='0x66025B8D4004CF5a6b288e80fCe16738D819cB25',
        private_key='0xdeadbeef',
    )

    class FakeClient:
        def __init__(self):
            self.signer = type('Signer', (), {'address': lambda self: '0xa035Ba4e9e99A3638468B9b29C2F1788BbBFdE04'})()

        def get_order_book(self, token_id):
            return type('Book', (), {
                'tick_size': '0.01',
                'asks': [type('Ask', (), {'price': '0.55'})()],
            })()

        def get_balance_allowance(self, params=None):
            return {'balance': '0', 'allowance': '0'}

        def update_balance_allowance(self, params=None):
            return {'balance': '0', 'allowance': '0'}

        def calculate_market_price(self, token_id, side, amount_usd, order_type):
            return 0.55

        def create_market_order(self, args):
            return {'orderID': 'order-1', 'price': 0.55, 'size': 1.8182}

        def post_order(self, order, order_type=None, post_only=None):
            return {'orderID': 'order-1', 'status': 'filled', 'avgPrice': '0.55', 'sizeMatched': '1.8182'}

    executor = PolymarketLiveExecutor(
        store=store,
        config=config,
        client_factory=lambda cfg: FakeClient(),
    )
    market = Market(
        id='m-signer',
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
        market_id='m-signer',
        question=market.question,
        city='Seoul',
        date='2030-04-17',
        market_prob=0.20,
        model_prob=0.72,
        edge=0.52,
        action='BUY_YES',
        confidence=0.96,
        rationale='city=Seoul; edge=+52.00%',
        generated_at='2030-04-17T00:00:00Z',
    )

    pos = executor.open_position(signal, 1.0, market=market)

    assert pos is not None
    controls = store.get_controls()
    assert controls.get('paused', False) is False
    assert controls.get('live_execution_blocked', False) is False
    assert controls.get('live_execution_block_stage') in ('', None)
    errors = store.get_errors(5)
    assert all(err.get('stage') != 'live_order_signer_mismatch' for err in errors)


def test_telegram_command_service_records_history_and_dashboard_state(tmp_path):
    store = Store(str(tmp_path / 'bot.db'))
    store.upsert_markets([
        Market(
            id='m-top',
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
    ])
    store.save_snapshot({
        'mode': 'paper',
        'positions': [],
        'recent_signals': [
            {
                'market_id': 'm-top',
                'action': 'BUY_YES',
                'city': 'Seoul',
                'edge': 0.10,
                'question': 'Will Seoul be between 17°C and 18°C on 2030-04-17?',
            }
        ],
        'controls': {'paused': True},
        'alerts': {'enabled': True, 'chat_id_set': True, 'token_set': True},
        'timestamp': '2030-04-17T00:00:00Z',
    })

    class FakeNotifier:
        def __init__(self):
            self.token = 'fake-token'
            self.chat_id = '123'
            self.sent = []

        def send_message(self, text, chat_id=None):
            self.sent.append({'chat_id': chat_id or self.chat_id, 'text': text})

        def get_updates(self, offset=None, timeout=25):
            return []

    service = TelegramCommandService(store, FakeNotifier(), allowed_chat_id='123')
    assert service.handle_update({'update_id': 1, 'message': {'chat': {'id': '123'}, 'from': {'id': 7, 'username': 'alice'}, 'text': '/top 1'}})
    assert service.handle_update({'update_id': 2, 'message': {'chat': {'id': '123'}, 'from': {'id': 7, 'username': 'alice'}, 'text': '/city Seoul'}})
    assert service.handle_update({'update_id': 3, 'message': {'chat': {'id': '123'}, 'from': {'id': 7, 'username': 'alice'}, 'text': '/diag'}})

    assert len(service.notifier.sent) == 3
    assert 'Top markets' in service.notifier.sent[0]['text']
    assert 'City summary: Seoul' in service.notifier.sent[1]['text']
    assert 'Bot diagnostics' in service.notifier.sent[2]['text']
    assert '/top: 1' in service.notifier.sent[2]['text']
    assert '/city: 1' in service.notifier.sent[2]['text']

    history = store.get_telegram_command_history(10)
    assert [row['command'] for row in history[:3]] == ['diag', 'city', 'top']
    state = DashboardState(store).current_state()
    assert state['telegram_commands_count'] == 3
    assert state['latest_telegram_commands'][0]['command'] == 'diag'
    assert state['telegram_command_usage'][0]['command'] == 'diag'

