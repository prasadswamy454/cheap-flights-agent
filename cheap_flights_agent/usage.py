from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from .locations import get_database_url


class ApiUsageLimitError(RuntimeError):
    """Raised before a provider call would exceed a configured quota."""


@dataclass(frozen=True)
class ApiUsageStatus:
    daily_used: int
    daily_limit: int
    monthly_used: int
    monthly_limit: int
    cache_entries: int


class ApiUsageManager:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        cache_ttl_minutes: int = 360,
        daily_limit: int = 75,
        monthly_limit: int = 900,
    ) -> None:
        self.database_url = get_database_url(database_url)
        self.cache_ttl_minutes = max(0, cache_ttl_minutes)
        self.daily_limit = max(0, daily_limit)
        self.monthly_limit = max(0, monthly_limit)
        self.ensure_schema()

    @classmethod
    def from_env(cls) -> ApiUsageManager:
        return cls(
            cache_ttl_minutes=_env_int("API_CACHE_TTL_MINUTES", 360),
            daily_limit=_env_int("API_DAILY_LIMIT", 75),
            monthly_limit=_env_int("API_MONTHLY_LIMIT", 900),
        )

    def get_json(
        self,
        query: dict[str, str],
        fetch: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        cache_key = _cache_key(query)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        self._reserve_call(str(query.get("engine") or "unknown"))
        payload = fetch()
        if not payload.get("error"):
            self._store_cached(cache_key, str(query.get("engine") or "unknown"), payload)
        return payload

    def status(self) -> ApiUsageStatus:
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE called_at >= date_trunc('day', NOW())) AS daily_used,
                    COUNT(*) FILTER (WHERE called_at >= date_trunc('month', NOW())) AS monthly_used
                FROM api_usage_events
                """
            ).fetchone()
            cache_row = connection.execute(
                "SELECT COUNT(*) AS cache_entries FROM api_response_cache WHERE expires_at > NOW()"
            ).fetchone()
        return ApiUsageStatus(
            daily_used=int(row["daily_used"]),
            daily_limit=self.daily_limit,
            monthly_used=int(row["monthly_used"]),
            monthly_limit=self.monthly_limit,
            cache_entries=int(cache_row["cache_entries"]),
        )

    def ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS api_response_cache (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    response_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS api_usage_events (
                    id BIGSERIAL PRIMARY KEY,
                    provider TEXT NOT NULL,
                    called_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_usage_events_called_at "
                "ON api_usage_events(called_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_response_cache_expires_at "
                "ON api_response_cache(expires_at)"
            )
            connection.execute(
                "DELETE FROM api_response_cache WHERE expires_at <= NOW()"
            )
            connection.execute(
                "DELETE FROM api_usage_events "
                "WHERE called_at < NOW() - INTERVAL '13 months'"
            )

    def _get_cached(self, cache_key: str) -> dict[str, Any] | None:
        if self.cache_ttl_minutes == 0:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT response_json
                FROM api_response_cache
                WHERE cache_key = %s AND expires_at > NOW()
                """,
                (cache_key,),
            ).fetchone()
        return dict(row["response_json"]) if row else None

    def _store_cached(
        self,
        cache_key: str,
        provider: str,
        payload: dict[str, Any],
    ) -> None:
        if self.cache_ttl_minutes == 0:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO api_response_cache (
                    cache_key, provider, response_json, expires_at
                ) VALUES (%s, %s, %s::jsonb, NOW() + (%s * INTERVAL '1 minute'))
                ON CONFLICT (cache_key) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    response_json = EXCLUDED.response_json,
                    created_at = NOW(),
                    expires_at = EXCLUDED.expires_at
                """,
                (cache_key, provider, json.dumps(payload), self.cache_ttl_minutes),
            )

    def _reserve_call(self, provider: str) -> None:
        with self._connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (734221,))
            row = connection.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE called_at >= date_trunc('day', NOW())) AS daily_used,
                    COUNT(*) FILTER (WHERE called_at >= date_trunc('month', NOW())) AS monthly_used
                FROM api_usage_events
                """
            ).fetchone()
            daily_used = int(row["daily_used"])
            monthly_used = int(row["monthly_used"])
            enforce_usage_limits(
                daily_used,
                self.daily_limit,
                monthly_used,
                self.monthly_limit,
            )
            connection.execute(
                "INSERT INTO api_usage_events (provider) VALUES (%s)",
                (provider,),
            )

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self.database_url, row_factory=dict_row)
        except psycopg.Error as exc:
            raise RuntimeError(
                "API cache and usage controls could not connect to PostgreSQL."
            ) from exc


def usage_status_payload(status: ApiUsageStatus) -> dict[str, int]:
    return {
        "dailyUsed": status.daily_used,
        "dailyLimit": status.daily_limit,
        "monthlyUsed": status.monthly_used,
        "monthlyLimit": status.monthly_limit,
        "cacheEntries": status.cache_entries,
    }


def enforce_usage_limits(
    daily_used: int,
    daily_limit: int,
    monthly_used: int,
    monthly_limit: int,
) -> None:
    if daily_limit and daily_used >= daily_limit:
        raise ApiUsageLimitError(
            f"Daily live-flight API limit reached ({daily_limit}). "
            "Cached searches still work; the quota resets at midnight UTC."
        )
    if monthly_limit and monthly_used >= monthly_limit:
        raise ApiUsageLimitError(
            f"Monthly live-flight API limit reached ({monthly_limit}). "
            "Cached searches still work; the quota resets next month."
        )


def _cache_key(query: dict[str, str]) -> str:
    safe_query = {key: value for key, value in query.items() if key != "api_key"}
    serialized = json.dumps(safe_query, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default
