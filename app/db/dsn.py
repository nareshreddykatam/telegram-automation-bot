"""DSN helpers shared by the PostgreSQL backend and configuration code.

Deliberately free of third-party imports so it can be tested (and imported by
the SQLite path) without asyncpg installed.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query-string options that asyncpg rejects but hosted poolers append.
IGNORED_DSN_PARAMS = frozenset({"pgbouncer", "connection_limit", "pool_mode"})

_SSLMODE = re.compile(r"(?:^|[?&])sslmode=", re.IGNORECASE)


def normalize_dsn(dsn: str) -> str:
    """Drop pooler-specific query options asyncpg cannot parse."""

    stripped = dsn.strip()
    parts = urlsplit(stripped)
    if not parts.query:
        return stripped
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if key.lower() not in IGNORED_DSN_PARAMS]
    if len(kept) == len(pairs):
        return stripped
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def describe_dsn(dsn: str) -> str:
    """Return a log-safe label: scheme, host, port, database. Never credentials."""

    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "postgres"
    database = (parts.path or "").lstrip("/") or "postgres"
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    scheme = parts.scheme or "postgresql"
    return f"{scheme}://{host}{port}/{database}"


def dsn_has_sslmode(dsn: str) -> bool:
    return bool(_SSLMODE.search(dsn))


__all__ = ["IGNORED_DSN_PARAMS", "describe_dsn", "dsn_has_sslmode", "normalize_dsn"]
