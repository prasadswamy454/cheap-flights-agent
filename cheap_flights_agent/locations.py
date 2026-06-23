from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import psycopg
from psycopg.rows import dict_row


@dataclass(frozen=True)
class Location:
    code: str
    name: str
    municipality: str
    country: str
    latitude: float
    longitude: float
    provider_code: str


class LocationLookup(Protocol):
    def resolve(self, value: str) -> Location | None: ...

    def get(self, code: str) -> Location | None: ...

    def get_many(self, codes: Iterable[str]) -> list[Location]: ...


class LocationRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = get_database_url(database_url)

    def resolve(self, value: str) -> Location | None:
        query = normalize_location_name(value)
        if not query:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT l.*
                FROM location_aliases AS a
                JOIN locations AS l ON l.code = a.location_code
                WHERE a.alias_normalized = %s
                ORDER BY a.priority ASC, l.rank ASC
                LIMIT 1
                """,
                (query,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT *
                    FROM locations
                    WHERE code = %s
                       OR name_normalized = %s
                       OR municipality_normalized = %s
                       OR country_normalized = %s
                    ORDER BY
                        CASE
                            WHEN code = %s THEN 0
                            WHEN municipality_normalized = %s THEN 1
                            WHEN name_normalized = %s THEN 2
                            ELSE 3
                        END,
                        rank ASC
                    LIMIT 1
                    """,
                    (query, query, query, query, query, query, query),
                ).fetchone()
        return _location_from_row(row) if row else None

    def get(self, code: str) -> Location | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM locations WHERE code = %s LIMIT 1",
                (code.strip().upper(),),
            ).fetchone()
        return _location_from_row(row) if row else None

    def get_many(self, codes: Iterable[str]) -> list[Location]:
        unique_codes = list(dict.fromkeys(code.strip().upper() for code in codes if code))
        if not unique_codes:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM locations WHERE code = ANY(%s)",
                (unique_codes,),
            ).fetchall()
        by_code = {row["code"]: _location_from_row(row) for row in rows}
        return [by_code[code] for code in unique_codes if code in by_code]

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(
                self.database_url,
                row_factory=dict_row,
                connect_timeout=3,
            )
        except psycopg.Error as exc:
            raise RuntimeError(
                "PostgreSQL location database is unavailable. Set DATABASE_URL and run "
                "'python -m cheap_flights_agent.import_locations'."
            ) from exc


_repository: LocationLookup | None = None


def get_location_repository() -> LocationLookup:
    global _repository
    if _repository is None:
        _repository = LocationRepository()
    return _repository


def set_location_repository(repository: LocationLookup | None) -> None:
    global _repository
    _repository = repository


def normalize_location_name(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()
    if normalized.startswith("THE "):
        normalized = normalized[4:]
    return normalized


def get_database_url(explicit_url: str | None = None) -> str:
    if explicit_url:
        return explicit_url
    _load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required.")
    return database_url


def _load_dotenv() -> None:
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _location_from_row(row: dict) -> Location:
    return Location(
        code=row["code"],
        name=row["name"],
        municipality=row["municipality"] or "",
        country=row["country_name"] or "",
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        provider_code=row["provider_code"] or row["code"],
    )
