from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path
from urllib.request import urlopen

import psycopg

from .locations import get_database_url, normalize_location_name


AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
COUNTRIES_URL = "https://davidmegginson.github.io/ourairports-data/countries.csv"
SEED_DIR = Path(__file__).with_name("location_seed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import airport locations into PostgreSQL.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--airports-csv", type=Path)
    parser.add_argument("--countries-csv", type=Path)
    args = parser.parse_args()

    database_url = get_database_url(args.database_url)
    airports = _read_csv(args.airports_csv, AIRPORTS_URL)
    countries = _read_csv(args.countries_csv, COUNTRIES_URL)
    import_locations(database_url, airports, countries)
    print("Imported locations into PostgreSQL")


def import_locations(
    database_url: str,
    airports: list[dict[str, str]],
    countries: list[dict[str, str]],
) -> None:
    country_names = {row["code"]: row["name"] for row in countries}
    location_rows: dict[str, tuple] = {}
    alias_rows: dict[tuple[str, str], int] = {}

    for airport in airports:
        code = airport.get("iata_code", "").strip().upper()
        if not code:
            continue
        municipality = airport.get("municipality", "").strip()
        country_name = country_names.get(airport.get("iso_country", ""), "")
        airport_type = airport.get("type", "")
        rank = _airport_rank(airport_type, airport.get("scheduled_service") == "yes")
        location_rows[code] = (
                code,
                airport["name"],
                normalize_location_name(airport["name"]),
                municipality,
                normalize_location_name(municipality),
                airport.get("iso_country", ""),
                country_name,
                normalize_location_name(country_name),
                float(airport["latitude_deg"]),
                float(airport["longitude_deg"]),
                airport_type,
                code,
                rank,
            )
        _add_alias(alias_rows, code, code, 0)
        _add_alias(alias_rows, airport["name"], code, rank + 20)
        if municipality:
            _add_alias(alias_rows, municipality, code, rank + 10)
            if municipality.lower().startswith("new ") and len(municipality) > 4:
                _add_alias(alias_rows, municipality[4:], code, rank + 11)
        if country_name:
            _add_alias(alias_rows, country_name, code, rank + 40)
        for keyword in airport.get("keywords", "").split(","):
            if keyword.strip():
                _add_alias(alias_rows, keyword, code, rank + 30)

    _append_seed_locations(location_rows, alias_rows)
    _append_seed_aliases(alias_rows)

    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS locations (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                name_normalized TEXT NOT NULL,
                municipality TEXT,
                municipality_normalized TEXT,
                country_code TEXT,
                country_name TEXT,
                country_normalized TEXT,
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                type TEXT NOT NULL,
                provider_code TEXT NOT NULL,
                rank INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS location_aliases (
                alias_normalized TEXT NOT NULL,
                location_code TEXT NOT NULL REFERENCES locations(code) ON DELETE CASCADE,
                priority INTEGER NOT NULL DEFAULT 100,
                PRIMARY KEY (alias_normalized, location_code)
            )
            """
        )
        connection.execute("TRUNCATE location_aliases, locations")
        with connection.cursor() as cursor:
            with cursor.copy(
                """
                COPY locations (
                    code, name, name_normalized, municipality, municipality_normalized,
                    country_code, country_name, country_normalized, latitude, longitude,
                    type, provider_code, rank
                ) FROM STDIN
                """
            ) as copy:
                for row in location_rows.values():
                    copy.write_row(row)
            with cursor.copy(
                "COPY location_aliases (alias_normalized, location_code, priority) FROM STDIN"
            ) as copy:
                for (alias, code), priority in alias_rows.items():
                    copy.write_row((alias, code, priority))
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_locations_name ON locations(name_normalized)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_locations_municipality "
            "ON locations(municipality_normalized)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_locations_country ON locations(country_normalized)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_aliases_alias "
            "ON location_aliases(alias_normalized, priority)"
        )


def _read_csv(path: Path | None, url: str) -> list[dict[str, str]]:
    if path:
        with path.open(encoding="utf-8-sig", newline="") as source:
            return list(csv.DictReader(source))
    with urlopen(url, timeout=60) as response:
        text = response.read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _airport_rank(airport_type: str, scheduled: bool) -> int:
    type_rank = {
        "large_airport": 0,
        "medium_airport": 20,
        "small_airport": 40,
        "seaplane_base": 60,
        "heliport": 80,
    }.get(airport_type, 100)
    return type_rank + (0 if scheduled else 15)


def _add_alias(
    aliases: dict[tuple[str, str], int],
    alias: str,
    location_code: str,
    priority: int,
) -> None:
    normalized = normalize_location_name(alias)
    if not normalized:
        return
    key = (normalized, location_code)
    aliases[key] = min(priority, aliases.get(key, priority))


def _append_seed_locations(
    locations: dict[str, tuple],
    aliases: dict[tuple[str, str], int],
) -> None:
    with (SEED_DIR / "metro_locations.csv").open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            locations[row["code"]] = (
                    row["code"],
                    row["name"],
                    normalize_location_name(row["name"]),
                    row["municipality"],
                    normalize_location_name(row["municipality"]),
                    row["country_code"],
                    row["country_name"],
                    normalize_location_name(row["country_name"]),
                    float(row["latitude"]),
                    float(row["longitude"]),
                    "metro",
                    row["provider_code"],
                    -10,
                )
            _add_alias(aliases, row["code"], row["code"], 0)
            _add_alias(aliases, row["name"], row["code"], 0)
            _add_alias(aliases, row["municipality"], row["code"], 0)


def _append_seed_aliases(aliases: dict[tuple[str, str], int]) -> None:
    with (SEED_DIR / "aliases.csv").open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            _add_alias(aliases, row["alias"], row["location_code"], int(row["priority"]))


if __name__ == "__main__":
    main()
