from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from polymarket_weather_bot.bootstrap_auth import _parse_env_file, _render_env_file, bootstrap_auth


@dataclass
class FakeCreds:
    api_key: str
    api_secret: str
    api_passphrase: str


def test_render_env_file_updates_existing_and_appends_missing():
    lines = [
        '# comment',
        'BOT_POLYMARKET_API_KEY=old-key',
        'BOT_KEEP=value',
    ]
    rendered = _render_env_file(
        lines,
        {
            'BOT_POLYMARKET_API_KEY': 'new key',
            'BOT_POLYMARKET_API_SECRET': 'secret-value',
        },
    )
    assert rendered[0] == '# comment'
    assert 'BOT_KEEP=value' in rendered
    assert any(line == "BOT_POLYMARKET_API_KEY='new key'" for line in rendered)
    assert any(line == "BOT_POLYMARKET_API_SECRET=secret-value" for line in rendered)


def test_bootstrap_auth_derives_and_writes_env_file(tmp_path, monkeypatch):
    env_path = tmp_path / 'polymarket-weather-bot'
    env_path.write_text(
        'BOT_POLYMARKET_PRIVATE_KEY=0xabc\n'
        'BOT_POLYMARKET_CLOB_HOST=https://clob.polymarket.com\n'
        'BOT_KEEP=1\n',
        encoding='utf-8',
    )

    monkeypatch.setattr(
        'polymarket_weather_bot.bootstrap_auth._derive_api_creds',
        lambda config: FakeCreds('k-123', 's-456', 'p-789'),
    )
    calls = []
    monkeypatch.setattr(
        'polymarket_weather_bot.bootstrap_auth.subprocess.run',
        lambda cmd, check: calls.append((cmd, check)),
    )

    result = bootstrap_auth(env_path, restart_service=True, service_name='polymarket-weather-bot.service')

    assert result.updated is True
    assert result.derived is True
    assert result.used_existing_creds is False
    assert result.service_restarted is True
    assert calls == [(['systemctl', 'restart', 'polymarket-weather-bot.service'], True)]

    parsed = _parse_env_file(env_path)
    assert parsed['BOT_POLYMARKET_API_KEY'] == 'k-123'
    assert parsed['BOT_POLYMARKET_API_SECRET'] == 's-456'
    assert parsed['BOT_POLYMARKET_API_PASSPHRASE'] == 'p-789'
    assert parsed['BOT_KEEP'] == '1'
    assert parsed['BOT_POLYMARKET_CLOB_HOST'] == 'https://clob.polymarket.com'
    assert parsed['BOT_POLYMARKET_CHAIN_ID'] == '137'
    assert parsed['BOT_POLYMARKET_SIGNATURE_TYPE'] == '0'


def test_bootstrap_auth_reuses_existing_creds_when_present(tmp_path, monkeypatch):
    env_path = tmp_path / 'polymarket-weather-bot'
    env_path.write_text(
        'BOT_POLYMARKET_PRIVATE_KEY=0xabc\n'
        'BOT_POLYMARKET_API_KEY=existing\n'
        'BOT_POLYMARKET_API_SECRET=existing-secret\n'
        'BOT_POLYMARKET_API_PASSPHRASE=existing-pass\n',
        encoding='utf-8',
    )

    monkeypatch.setattr(
        'polymarket_weather_bot.bootstrap_auth._derive_api_creds',
        lambda config: FakeCreds('should-not', 'run', 'here'),
    )

    result = bootstrap_auth(env_path)
    assert result.updated is False
    assert result.derived is False
    assert result.used_existing_creds is True
    parsed = _parse_env_file(env_path)
    assert parsed['BOT_POLYMARKET_API_KEY'] == 'existing'
