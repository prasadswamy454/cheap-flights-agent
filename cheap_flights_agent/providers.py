from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import FlightOffer, MultiCitySegment, TripRequest
from .locations import get_location_repository


class FlightProvider(ABC):
    def search_with_request(
        self,
        request: TripRequest,
    ) -> tuple[TripRequest, list[FlightOffer]]:
        resolved = self.resolve_request(request)
        return resolved, list(self.search(resolved))

    def resolve_request(self, request: TripRequest) -> TripRequest:
        """Resolve flexible constraints into a concrete request."""

        return request

    @abstractmethod
    def search(self, request: TripRequest) -> Iterable[FlightOffer]:
        """Return flight offers for a trip request."""


class ProviderConfigurationError(RuntimeError):
    """Raised when a live provider cannot be created from local configuration."""


class ProviderSearchError(RuntimeError):
    """Raised when a live provider search fails."""


def provider_from_env(require_live: bool = False) -> FlightProvider:
    """Create the live provider when credentials exist, otherwise demo provider."""

    _load_dotenv()
    serpapi_key = os.getenv("SERPAPI_API_KEY")
    if serpapi_key:
        from .usage import ApiUsageManager

        return SerpApiFlightProvider(
            api_key=serpapi_key,
            api_usage=ApiUsageManager.from_env(),
        )
    if require_live:
        raise ProviderConfigurationError(
            "Set SERPAPI_API_KEY to search live Google Flights results."
        )
    return DemoFlightProvider()


class SerpApiFlightProvider(FlightProvider):
    """Live Google Flights results via SerpApi."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://serpapi.com/search.json",
        timeout_seconds: int = 20,
        api_usage: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.api_usage = api_usage

    def search_with_request(
        self,
        request: TripRequest,
    ) -> tuple[TripRequest, list[FlightOffer]]:
        if not request.flexible_dates:
            return super().search_with_request(request)

        first_resolved: TripRequest | None = None
        for month in _flexible_months():
            resolved = self._resolve_flexible_request(request, month)
            if resolved is None:
                continue
            if first_resolved is None:
                first_resolved = resolved
            offers = list(self.search(resolved))
            affordable = _within_budget(offers, request.budget_usd)
            if affordable:
                return resolved, affordable

        if first_resolved is None:
            raise ProviderSearchError(
                "No flexible-date fare window was found in the next six months."
            )
        return first_resolved, []

    def resolve_request(self, request: TripRequest) -> TripRequest:
        if not request.flexible_dates:
            return request
        resolved = self._resolve_flexible_request(request, 0)
        if resolved is None:
            raise ProviderSearchError(
                "No flexible-date fare window was found in the next six months."
            )
        return resolved

    def _resolve_flexible_request(
        self,
        request: TripRequest,
        month: int,
    ) -> TripRequest | None:

        duration_days = request.trip_duration_days or 7
        query = {
            "engine": "google_travel_explore",
            "api_key": self.api_key,
            "currency": "USD",
            "hl": "en",
            "gl": "us",
            "departure_id": _serpapi_location_id(request.origin),
            "arrival_id": _serpapi_location_id(request.destination),
            "type": "1",
            "month": str(month),
            "travel_duration": _serpapi_travel_duration(duration_days),
            "travel_class": _serpapi_travel_class(request.cabin_class),
            "adults": str(request.passengers),
            "travel_mode": "1",
        }
        if request.max_stops is not None:
            query["stops"] = str(min(request.max_stops + 1, 3))
        if request.budget_usd:
            query["max_price"] = str(request.budget_usd)
        if request.include_bags:
            query["bags"] = str(request.passengers)

        payload = self._get_json(query)
        if payload.get("error"):
            raise ProviderSearchError(f"Flexible-date search failed: {payload['error']}")
        start_date = _explore_start_date(payload, request.destination)
        if start_date is None:
            return None

        end_date = start_date + timedelta(days=duration_days)
        segments: tuple[MultiCitySegment, ...] = ()
        trip_type = "round_trip"
        if request.return_stopover:
            stopover_days = request.stopover_days or 2
            stopover_date = end_date - timedelta(days=stopover_days)
            segments = (
                MultiCitySegment(request.origin, request.destination, start_date),
                MultiCitySegment(request.destination, request.return_stopover, stopover_date),
                MultiCitySegment(request.return_stopover, request.origin, end_date),
            )
            trip_type = "multi_city"

        return replace(
            request,
            depart_date=start_date,
            return_date=end_date,
            trip_type=trip_type,
            multi_city_segments=segments,
            flexible_dates=False,
        )

    def search(self, request: TripRequest) -> Iterable[FlightOffer]:
        query = {
            "engine": "google_flights",
            "api_key": self.api_key,
            "currency": "USD",
            "hl": "en",
            "gl": "us",
            "adults": str(request.passengers),
            "type": _serpapi_trip_type(request),
            "travel_class": _serpapi_travel_class(request.cabin_class),
            "sort_by": "2",
        }
        if request.trip_type == "multi_city":
            query["multi_city_json"] = json.dumps(
                [
                    {
                        "departure_id": _serpapi_location_id(segment.origin),
                        "arrival_id": _serpapi_location_id(segment.destination),
                        "date": segment.depart_date.isoformat(),
                    }
                    for segment in request.multi_city_segments
                ],
                separators=(",", ":"),
            )
        else:
            if request.depart_date is None:
                raise ProviderSearchError("A departure date is required for an exact fare search.")
            query["departure_id"] = _serpapi_location_id(request.origin)
            query["arrival_id"] = _serpapi_location_id(request.destination)
            query["outbound_date"] = request.depart_date.isoformat()
        if request.trip_type == "round_trip" and request.return_date:
            query["return_date"] = request.return_date.isoformat()
        if request.max_stops is not None:
            query["stops"] = str(min(request.max_stops + 1, 3))
        if request.budget_usd:
            query["max_price"] = str(request.budget_usd)
        if request.include_bags:
            query["bags"] = str(request.passengers)

        payload = self._get_json(query)
        if payload.get("error"):
            raise ProviderSearchError(f"SerpApi search failed: {payload['error']}")

        google_flights_url = payload.get("search_metadata", {}).get(
            "google_flights_url", "https://www.google.com/travel/flights"
        )
        offers = [
            *payload.get("best_flights", []),
            *payload.get("other_flights", []),
        ]
        mapped_offers = [
            self._offer_from_payload(item, google_flights_url)
            for item in offers
            if isinstance(item, dict) and item.get("flights") and item.get("price")
        ]
        if request.max_stops is not None:
            mapped_offers = [offer for offer in mapped_offers if offer.stops <= request.max_stops]
        mapped_offers = _within_budget(mapped_offers, request.budget_usd)
        return mapped_offers

    def _get_json(self, query: dict[str, str]) -> dict:
        if self.api_usage is not None:
            return self.api_usage.get_json(query, lambda: self._fetch_json(query))
        return self._fetch_json(query)

    def _fetch_json(self, query: dict[str, str]) -> dict:
        request = Request(f"{self.base_url}?{urlencode(query)}", method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise ProviderSearchError(f"SerpApi search failed: {message}") from exc
        except (URLError, TimeoutError) as exc:
            raise ProviderSearchError(f"SerpApi search failed: {exc}") from exc

    def _offer_from_payload(self, payload: dict, google_flights_url: str) -> FlightOffer:
        segments = payload["flights"]
        first_segment = segments[0]
        last_segment = segments[-1]
        airline_names = [segment.get("airline") for segment in segments if segment.get("airline")]
        airline = airline_names[0] if len(set(airline_names)) == 1 else "Multiple airlines"
        extensions = [item.lower() for item in payload.get("extensions", [])]
        bags_included = any("bag" in item and "fee" not in item for item in extensions)

        return FlightOffer(
            airline=airline,
            origin=first_segment["departure_airport"]["id"],
            destination=last_segment["arrival_airport"]["id"],
            depart_at=_parse_serpapi_datetime(first_segment["departure_airport"]["time"]),
            arrive_at=_parse_serpapi_datetime(last_segment["arrival_airport"]["time"]),
            price_usd=int(payload["price"]),
            stops=max(0, len(segments) - 1),
            bags_included=bags_included,
            booking_url=google_flights_url,
        )


class DemoFlightProvider(FlightProvider):
    """Offline sample fares for local development and tests."""

    def __init__(self) -> None:
        self._offers = [
            FlightOffer(
                airline="JetBlue",
                origin="JFK",
                destination="LAX",
                depart_at=datetime(2026, 8, 12, 8, 15),
                arrive_at=datetime(2026, 8, 12, 11, 35),
                return_depart_at=datetime(2026, 8, 18, 14, 10),
                return_arrive_at=datetime(2026, 8, 18, 22, 35),
                price_usd=386,
                stops=0,
                bags_included=False,
                booking_url="https://example.com/book/jfk-lax-jetblue",
            ),
            FlightOffer(
                airline="Delta",
                origin="JFK",
                destination="LAX",
                depart_at=datetime(2026, 8, 12, 6, 0),
                arrive_at=datetime(2026, 8, 12, 9, 30),
                return_depart_at=datetime(2026, 8, 18, 16, 25),
                return_arrive_at=datetime(2026, 8, 19, 0, 45),
                price_usd=421,
                stops=0,
                bags_included=True,
                booking_url="https://example.com/book/jfk-lax-delta",
            ),
            FlightOffer(
                airline="United",
                origin="JFK",
                destination="LAX",
                depart_at=datetime(2026, 8, 11, 19, 20),
                arrive_at=datetime(2026, 8, 11, 23, 5),
                return_depart_at=datetime(2026, 8, 19, 7, 0),
                return_arrive_at=datetime(2026, 8, 19, 15, 35),
                price_usd=319,
                stops=1,
                bags_included=False,
                booking_url="https://example.com/book/jfk-lax-united-flex",
            ),
            FlightOffer(
                airline="Alaska",
                origin="SFO",
                destination="SEA",
                depart_at=datetime(2026, 7, 8, 9, 40),
                arrive_at=datetime(2026, 7, 8, 11, 55),
                price_usd=96,
                stops=0,
                bags_included=False,
                booking_url="https://example.com/book/sfo-sea-alaska",
            ),
            FlightOffer(
                airline="Delta",
                origin="SFO",
                destination="SEA",
                depart_at=datetime(2026, 7, 8, 16, 5),
                arrive_at=datetime(2026, 7, 8, 18, 25),
                price_usd=118,
                stops=0,
                bags_included=True,
                booking_url="https://example.com/book/sfo-sea-delta",
            ),
            FlightOffer(
                airline="French Bee",
                origin="NYC",
                destination="PAR",
                depart_at=datetime(2026, 8, 10, 22, 30),
                arrive_at=datetime(2026, 8, 11, 11, 50),
                return_depart_at=datetime(2026, 8, 20, 18, 45),
                return_arrive_at=datetime(2026, 8, 20, 21, 10),
                price_usd=712,
                stops=0,
                bags_included=False,
                booking_url="https://example.com/book/nyc-par-frenchbee",
            ),
            FlightOffer(
                airline="Air France",
                origin="NYC",
                destination="PAR",
                depart_at=datetime(2026, 8, 10, 17, 15),
                arrive_at=datetime(2026, 8, 11, 6, 45),
                return_depart_at=datetime(2026, 8, 20, 13, 20),
                return_arrive_at=datetime(2026, 8, 20, 15, 55),
                price_usd=884,
                stops=0,
                bags_included=True,
                booking_url="https://example.com/book/nyc-par-airfrance",
            ),
        ]

    def search(self, request: TripRequest) -> Iterable[FlightOffer]:
        matches: List[FlightOffer] = []
        for offer in self._offers:
            if offer.origin.upper() != request.origin.upper():
                continue
            if offer.destination.upper() != request.destination.upper():
                continue
            if request.max_stops is not None and offer.stops > request.max_stops:
                continue
            if request.include_bags and not offer.bags_included:
                continue
            if request.return_date and not offer.return_depart_at:
                continue
            matches.append(offer)
        return matches


def _parse_serpapi_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


def _serpapi_location_id(code: str) -> str:
    normalized = code.strip().upper()
    location = get_location_repository().get(normalized)
    return location.provider_code if location else normalized


def _serpapi_trip_type(request: TripRequest) -> str:
    if request.trip_type == "multi_city":
        return "3"
    if request.trip_type == "one_way":
        return "2"
    return "1"


def _serpapi_travel_class(cabin_class: str) -> str:
    cabin_classes = {
        "economy": "1",
        "premium_economy": "2",
        "business": "3",
        "first": "4",
    }
    return cabin_classes.get(cabin_class, "1")


def _serpapi_travel_duration(days: int) -> str:
    if days <= 4:
        return "1"
    if days <= 8:
        return "2"
    return "3"


def _within_budget(
    offers: Iterable[FlightOffer],
    budget_usd: int | None,
) -> list[FlightOffer]:
    offers = list(offers)
    if budget_usd is None:
        return offers
    return [offer for offer in offers if offer.price_usd <= budget_usd]


def _flexible_months() -> list[int]:
    today = date.today()
    months = [0]
    for offset in range(6):
        month = ((today.month - 1 + offset) % 12) + 1
        if month not in months:
            months.append(month)
    return months


def _explore_start_date(payload: dict, destination: str) -> date | None:
    if payload.get("start_date"):
        return datetime.strptime(payload["start_date"], "%Y-%m-%d").date()

    normalized_destination = destination.upper()
    candidates = payload.get("destinations", [])
    for candidate in candidates:
        airport = candidate.get("destination_airport", {})
        if airport.get("code", "").upper() == normalized_destination and candidate.get("start_date"):
            return datetime.strptime(candidate["start_date"], "%Y-%m-%d").date()
    return None


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
