from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from .account import PolymarketAccountConfig
from .clob_sdk import create_or_derive_api_creds

DEFAULT_ENV_FILE = Path('/etc/default/polymarket-weather-bot')
DEFAULT_SERVICE_NAME = 'polymarket-weather-bot.service'
TARGET_ENV_KEYS = (
    'BOT_POLYMARKET_CLOB_HOST',
    'BOT_POLYMARKET_CHAIN_ID',
    'BOT_POLYMARKET_SIGNATURE_TYPE',
    'BOT_POLYMARKET_FUNDER_ADDRESS',
    'BOT_POLYMARKET_API_KEY',
    'BOT_POLYMARKET_API_SECRET',
    'BOT_POLYMARKET_API_PASSPHRASE',
)


@dataclass(frozen=True)
class BootstrapResult:
    env_file: str
    updated: bool
    derived: bool
    used_existing_creds: bool
    private_key_present: bool
    saved_keys: List[str]
    values: Dict[str, str]
    service_restarted: bool = False
    service_name: str | None = None
    timestamp: str | None = None


def _mask(value: str | None, keep: int = 6) -> str:
    if not value:
        return '—'
    clean = value.strip()
    if len(clean) <= keep + 4:
        return f'{clean[:keep]}…'
    return f'{clean[:keep]}…{clean[-4:]}'


def _parse_env_file(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        env[key] = value
    return env


def _format_env_value(value: str) -> str:
    return shlex.quote(value)


def _render_env_file(existing_lines: List[str], updates: Mapping[str, str]) -> List[str]:
    last_update_idx: Dict[str, int] = {}
    for idx, raw_line in enumerate(existing_lines):
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key = line.split('=', 1)[0].strip()
        if key in updates:
            last_update_idx[key] = idx

    seen: set[str] = set()
    output: List[str] = []
    for idx, raw_line in enumerate(existing_lines):
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            output.append(raw_line)
            continue
        key = line.split('=', 1)[0].strip()
        if key in updates:
            if last_update_idx.get(key) != idx:
                continue
            output.append(f'{key}={_format_env_value(str(updates[key]))}')
            seen.add(key)
        else:
            output.append(raw_line)

    missing = [key for key in updates if key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append('')
        output.append('# Derived / refreshed by bootstrap_polymarket_auth.py')
        for key in missing:
            output.append(f'{key}={_format_env_value(str(updates[key]))}')
    return output


def _derive_api_creds(config: PolymarketAccountConfig):
    try:
        from .clob_sdk import ClobClient
    except Exception as exc:  # pragma: no cover - import failure path
        raise RuntimeError(f'polymarket clob sdk unavailable: {exc}') from exc

    client = ClobClient(
        config.clob_host,
        key=config.private_key,
        chain_id=config.chain_id,
        signature_type=config.resolved_signature_type,
        funder=config.resolved_funder_address,
    )
    creds = create_or_derive_api_creds(client)
    client.set_api_creds(creds)
    return creds


def bootstrap_auth(
    env_file: str | Path = DEFAULT_ENV_FILE,
    *,
    force: bool = False,
    restart_service: bool = False,
    service_name: str = DEFAULT_SERVICE_NAME,
) -> BootstrapResult:
    env_path = Path(env_file).expanduser()
    existing_env = _parse_env_file(env_path)
    config = PolymarketAccountConfig.from_mapping(existing_env)

    if not config.private_key:
        raise RuntimeError('BOT_POLYMARKET_PRIVATE_KEY is missing; cannot derive Polymarket CLOB credentials.')

    existing_creds = bool(config.api_key and config.api_secret and config.api_passphrase)
    if existing_creds and not force:
        result = BootstrapResult(
            env_file=str(env_path),
            updated=False,
            derived=False,
            used_existing_creds=True,
            private_key_present=True,
            saved_keys=[],
            values={
                'BOT_POLYMARKET_API_KEY': _mask(config.api_key),
                'BOT_POLYMARKET_API_SECRET': _mask(config.api_secret),
                'BOT_POLYMARKET_API_PASSPHRASE': _mask(config.api_passphrase),
                'BOT_POLYMARKET_CLOB_HOST': config.clob_host,
                'BOT_POLYMARKET_CHAIN_ID': str(config.chain_id),
                'BOT_POLYMARKET_SIGNATURE_TYPE': str(config.resolved_signature_type),
                'BOT_POLYMARKET_FUNDER_ADDRESS': config.resolved_funder_address or '—',
            },
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        if restart_service:
            subprocess.run(['systemctl', 'restart', service_name], check=True)
            result = BootstrapResult(**{**asdict(result), 'service_restarted': True, 'service_name': service_name})
        return result

    creds = _derive_api_creds(config)
    updates = {
        'BOT_POLYMARKET_CLOB_HOST': config.clob_host,
        'BOT_POLYMARKET_CHAIN_ID': str(config.chain_id),
        'BOT_POLYMARKET_SIGNATURE_TYPE': str(config.resolved_signature_type),
        'BOT_POLYMARKET_FUNDER_ADDRESS': config.resolved_funder_address or '',
        'BOT_POLYMARKET_API_KEY': str(getattr(creds, 'api_key', '') or ''),
        'BOT_POLYMARKET_API_SECRET': str(getattr(creds, 'api_secret', '') or ''),
        'BOT_POLYMARKET_API_PASSPHRASE': str(getattr(creds, 'api_passphrase', '') or ''),
    }

    existing_lines = env_path.read_text(encoding='utf-8').splitlines() if env_path.exists() else []
    rendered = _render_env_file(existing_lines, updates)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text('\n'.join(rendered).rstrip('\n') + '\n', encoding='utf-8')

    result = BootstrapResult(
        env_file=str(env_path),
        updated=True,
        derived=True,
        used_existing_creds=False,
        private_key_present=True,
        saved_keys=list(updates.keys()),
        values={
            'BOT_POLYMARKET_API_KEY': _mask(updates['BOT_POLYMARKET_API_KEY']),
            'BOT_POLYMARKET_API_SECRET': _mask(updates['BOT_POLYMARKET_API_SECRET']),
            'BOT_POLYMARKET_API_PASSPHRASE': _mask(updates['BOT_POLYMARKET_API_PASSPHRASE']),
            'BOT_POLYMARKET_CLOB_HOST': updates['BOT_POLYMARKET_CLOB_HOST'],
            'BOT_POLYMARKET_CHAIN_ID': updates['BOT_POLYMARKET_CHAIN_ID'],
            'BOT_POLYMARKET_SIGNATURE_TYPE': updates['BOT_POLYMARKET_SIGNATURE_TYPE'],
            'BOT_POLYMARKET_FUNDER_ADDRESS': updates['BOT_POLYMARKET_FUNDER_ADDRESS'] or '—',
        },
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    if restart_service:
        subprocess.run(['systemctl', 'restart', service_name], check=True)
        result = BootstrapResult(**{**asdict(result), 'service_restarted': True, 'service_name': service_name})
    return result


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description='Derive and persist Polymarket CLOB API credentials from BOT_POLYMARKET_PRIVATE_KEY.')
    parser.add_argument('--env-file', default=str(DEFAULT_ENV_FILE), help='Path to the system env file (default: /etc/default/polymarket-weather-bot)')
    parser.add_argument('--force', action='store_true', help='Re-derive and overwrite API creds even if existing creds are present')
    parser.add_argument('--restart', action='store_true', help='Restart the systemd service after writing the env file')
    parser.add_argument('--service', default=DEFAULT_SERVICE_NAME, help='systemd service name to restart when --restart is used')
    parser.add_argument('--json', action='store_true', help='Print the result as JSON instead of a human summary')
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = bootstrap_auth(args.env_file, force=args.force, restart_service=args.restart, service_name=args.service)

    if args.json:
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
    else:
        print(f'env_file: {result.env_file}')
        print(f'updated: {result.updated}')
        print(f'derived: {result.derived}')
        print(f'used_existing_creds: {result.used_existing_creds}')
        print(f'private_key_present: {result.private_key_present}')
        print(f'service_restarted: {result.service_restarted}')
        print('saved:')
        for key in result.saved_keys:
            print(f'  - {key}')
        print('values:')
        for key, value in result.values.items():
            print(f'  - {key}: {value}')
        print(f'timestamp: {result.timestamp}')
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
