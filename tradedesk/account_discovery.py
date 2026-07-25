"""Shared ExchangeAgent account discovery.

Account aliases are discovered generically from variable names only:
<EXCHANGE>_<ACCOUNT>_* -> account.

Values are never returned or logged by this module.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def hermes_env_path() -> Path:
    home = os.getenv("HERMES_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".hermes") / ".env"


def strip_dotenv_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def dotenv_casefold_map() -> dict[str, tuple[str, str]]:
    path = hermes_env_path()
    out: dict[str, tuple[str, str]] = {}
    if not path.exists():
        return out
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        value = strip_dotenv_value(value)
        if key and value.strip():
            out[key.lower()] = (key, value.strip())
    return out


def combined_casefold_env() -> dict[str, tuple[str, str, str]]:
    """Case-insensitive process env + Hermes .env map.

    Values are included for credential resolution callers, but discover_accounts
    only uses variable names and never returns values.
    """
    out: dict[str, tuple[str, str, str]] = {}
    for env_key, env_value in os.environ.items():
        if env_value and env_value.strip():
            out[env_key.lower()] = (env_key, env_value.strip(), "environment")
    for lower_key, (actual_key, value) in dotenv_casefold_map().items():
        if lower_key not in out:
            out[lower_key] = (actual_key, value, "dotenv")
    return out


def discover_accounts(exchange_name: str) -> list[str]:
    """Discover account aliases from <EXCHANGE>_<ACCOUNT>_* variable names.

    Example: PACIFICA_ACCOUNT1_APIKEY -> account1. The suffix after account is
    intentionally ignored; this is naming-convention discovery only.
    """
    exchange = re.sub(r"[^A-Za-z0-9]+", "_", str(exchange_name or "")).strip("_").upper()
    if not exchange:
        return []
    prefix = f"{exchange}_"
    accounts: set[str] = set()
    for actual_key, _value, _source in combined_casefold_env().values():
        upper_key = actual_key.upper()
        if not upper_key.startswith(prefix):
            continue
        remainder = actual_key[len(prefix):]
        if "_" not in remainder:
            continue
        account = remainder.split("_", 1)[0]
        normalized = re.sub(r"[^a-z0-9]+", "_", account.lower()).strip("_")
        if normalized:
            accounts.add(normalized)
    return sorted(accounts)
