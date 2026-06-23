from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from .models import MultiCitySegment, TripRequest
from .locations import get_location_repository
from .llm import get_llm_interpreter
from .providers import FlightProvider, provider_from_env
from .scoring import RankedFlight, rank_flights


@dataclass(frozen=True)
class AgentResult:
    request: TripRequest
    ranked_flights: list[RankedFlight]
    message: str


class CheapFlightsAgent:
    def __init__(self, provider: Optional[FlightProvider] = None) -> None:
        self.provider = provider or provider_from_env()

    def search(self, request: TripRequest) -> AgentResult:
        resolved_request, offers = self.provider.search_with_request(request)
        if resolved_request.budget_usd is not None:
            offers = [
                offer
                for offer in offers
                if offer.price_usd <= resolved_request.budget_usd
            ]
        ranked = rank_flights(resolved_request, offers)
        return AgentResult(
            request=resolved_request,
            ranked_flights=ranked,
            message=self._summarize(resolved_request, ranked),
        )

    def search_text(self, text: str) -> AgentResult:
        interpreter = get_llm_interpreter()
        if interpreter.available:
            try:
                request = interpreter.parse_trip(text)
            except Exception:
                request = parse_trip_request(text)
        else:
            request = parse_trip_request(text)
        return self.search(request)

    def _summarize(self, request: TripRequest, ranked: list[RankedFlight]) -> str:
        if not ranked:
            budget_note = (
                f" at or below your ${request.budget_usd} maximum"
                if request.budget_usd is not None
                else ""
            )
            flexibility_note = (
                " after checking multiple flexible travel windows"
                if request.trip_duration_days
                else ""
            )
            return (
                f"I could not find live fares from {request.origin} to "
                f"{request.destination}{budget_note}{flexibility_note}. "
                "Try allowing more stops, a wider travel window, or a higher budget."
            )

        best = ranked[0]
        offer = best.offer
        if request.trip_type == "multi_city":
            trip_type = "multi-city"
        else:
            trip_type = "round trip" if request.is_round_trip else "one way"
        itinerary = _describe_itinerary(request)
        opening = (
            f"I found {len(ranked)} live {trip_type} options for {itinerary}. "
            f"The strongest value is {offer.airline} at ${offer.price_usd}, "
            f"with {_stop_description(offer.stops)} and a total flight time of "
            f"{_duration_description(offer.total_duration_minutes)}."
        )

        reason_text = ", ".join(best.reasons[:3])
        recommendation = f"I ranked it first because it offers {reason_text}."
        if len(ranked) > 1:
            alternatives = "; ".join(
                f"{item.offer.airline} at ${item.offer.price_usd} "
                f"with {_stop_description(item.offer.stops)}"
                for item in ranked[1:3]
            )
            recommendation += f" The next best alternatives are {alternatives}."

        return f"{opening}\n\n{recommendation}"


def _describe_itinerary(request: TripRequest) -> str:
    if request.trip_type == "multi_city" and request.multi_city_segments:
        route = " to ".join(
            [request.multi_city_segments[0].origin]
            + [segment.destination for segment in request.multi_city_segments]
        )
    else:
        route = f"{request.origin} to {request.destination}"

    if request.depart_date and request.return_date:
        dates = (
            f" from {request.depart_date.strftime('%B %d, %Y')} "
            f"through {request.return_date.strftime('%B %d, %Y')}"
        )
    elif request.depart_date:
        dates = f" departing {request.depart_date.strftime('%B %d, %Y')}"
    else:
        dates = ""
    return f"{route}{dates}"


def _stop_description(stops: int) -> str:
    if stops == 0:
        return "no stops"
    return f"{stops} stop" if stops == 1 else f"{stops} stops"


def _duration_description(minutes: int) -> str:
    hours, remaining_minutes = divmod(minutes, 60)
    if remaining_minutes:
        return f"{hours} hours {remaining_minutes} minutes"
    return f"{hours} hours"


def parse_trip_request(text: str) -> TripRequest:
    upper = text.upper()
    route = _extract_route(upper)
    if not route:
        raise ValueError(
            "Please include a route such as 'New York to Hyderabad' or 'JFK to LAX'."
        )

    dates = _extract_dates(text)
    trip_days = _extract_trip_days(upper)
    stopover = _extract_return_stopover(upper)
    flexible_dates = not dates
    depart_date = dates[0] if dates else None
    return_date = dates[1] if len(dates) > 1 else None
    if depart_date and return_date is None and trip_days:
        return_date = depart_date + timedelta(days=trip_days)

    budget = extract_budget(upper)

    nonstop = any(word in upper for word in ["NONSTOP", "DIRECT"])
    max_stops = 0 if nonstop else None
    trip_type = "one_way" if re.search(r"\bONE[\s-]?WAY\b", upper) else "round_trip"
    segments: tuple[MultiCitySegment, ...] = ()
    if stopover and depart_date:
        stopover_code, stopover_days = stopover
        if return_date is None:
            raise ValueError(
                "A return stopover needs either a return date or a total trip length, "
                "such as '10 days for the whole trip'."
            )
        stopover_date = return_date - timedelta(days=stopover_days or 0)
        if stopover_date <= depart_date:
            raise ValueError("The stopover and total trip length leave no time at the destination.")
        segments = (
            MultiCitySegment(route[0], route[1], depart_date),
            MultiCitySegment(route[1], stopover_code, stopover_date),
            MultiCitySegment(stopover_code, route[0], return_date),
        )
        trip_type = "multi_city"
    elif flexible_dates:
        trip_type = "multi_city" if stopover else "round_trip"
    elif return_date is None:
        trip_type = "one_way"

    return TripRequest(
        origin=route[0],
        destination=route[1],
        depart_date=depart_date,
        return_date=return_date,
        budget_usd=budget,
        max_stops=max_stops,
        include_bags="BAG" in upper,
        trip_type=trip_type,
        multi_city_segments=segments,
        flexible_dates=flexible_dates,
        trip_duration_days=trip_days or (7 if flexible_dates else None),
        return_stopover=stopover[0] if stopover else None,
        stopover_days=(stopover[1] or 2) if stopover else None,
    )


def extract_budget(text: str) -> int | None:
    matches = re.findall(
        r"(?:UNDER|BELOW|LESS THAN|NO MORE THAN|MAX(?:IMUM)?(?: PRICE)?(?: OF)?|"
        r"BUDGET(?: OF| IS| TO BE)?|PRICE TO BE UNDER)\s+\$?(\d{2,5})",
        text.upper(),
    )
    return int(matches[-1]) if matches else None


def _extract_route(text: str) -> tuple[str, str] | None:
    route_end = (
        r"(?=[.!?]|\s+(?:ON|DEPARTING|LEAVING|RETURNING|ROUND\s+TRIP|ONE[\s-]?WAY|"
        r"UNDER|BUDGET|WITH|NONSTOP|DIRECT)\b|\s+\d{4}-\d{2}-\d{2}\b|"
        r"\s+(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s+\d|\s*$)"
    )
    route_patterns = [
        (rf"\bFROM\s+(.+?)\s+TO\s+(.+?){route_end}", False),
        (rf"\bBETWEEN\s+(.+?)\s+AND\s+(.+?){route_end}", False),
        (rf"\bLEAVING\s+(.+?)\s+FOR\s+(.+?){route_end}", False),
        (
            rf"\b(?:FLY|FLYING|TRAVEL|TRAVELING|GO|GOING|FLIGHTS?|TICKETS?)"
            rf"\s+TO\s+(.+?)\s+FROM\s+(.+?){route_end}",
            True,
        ),
        (rf"\bTO\s+(.+?)\s+FROM\s+(.+?){route_end}", True),
        (rf"^\s*(.+?)\s+TO\s+(.+?){route_end}", False),
    ]
    for pattern, reverse in route_patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        first = resolve_location(match.group(1))
        second = resolve_location(match.group(2))
        origin, destination = (second, first) if reverse else (first, second)
        if origin and destination:
            return origin, destination
    return None


def resolve_location(value: str) -> str | None:
    location = get_location_repository().resolve(value)
    return location.code if location else None


def _extract_trip_days(text: str) -> int | None:
    patterns = [
        r"\b(\d{1,3})\s+DAYS?\s+(?:FOR\s+)?(?:THE\s+)?WHOLE\s+TRIP\b",
        r"\b(?:TOTAL\s+TRIP|TRIP)\s+(?:OF\s+)?(\d{1,3})\s+DAYS?\b",
        r"\bFOR\s+(\d{1,3})\s+DAYS?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def _extract_return_stopover(text: str) -> tuple[str, int | None] | None:
    pattern = (
        r"\b(?:WITH\s+)?(?:A\s+)?(?:(\d{1,2})[\s-]*(?:DAY|NIGHT)S?\s+)?"
        r"STOPOVER\s+IN\s+(.+?)(?=\s+(?:WHILE|WHEN|ON|COMING|RETURNING|"
        r"FOR|UNDER|BUDGET)\b|\s*$)"
    )
    match = re.search(pattern, text)
    if not match:
        return None
    location = resolve_location(match.group(2))
    if not location:
        raise ValueError(f"I could not identify the stopover location '{match.group(2).title()}'.")
    is_return = any(
        phrase in text
        for phrase in ["COMING BACK", "ON RETURN", "ON THE RETURN", "RETURNING", "WHILE COMING BACK"]
    )
    if not is_return:
        return None
    return location, int(match.group(1)) if match.group(1) else None


def _extract_dates(text: str) -> list[date]:
    iso_dates = [
        datetime.strptime(match, "%Y-%m-%d").date()
        for match in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    ]
    if iso_dates:
        return iso_dates

    month_pattern = (
        r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s+"
        r"(\d{1,2})\b"
    )
    month_numbers = {
        "JAN": 1,
        "FEB": 2,
        "MAR": 3,
        "APR": 4,
        "MAY": 5,
        "JUN": 6,
        "JUL": 7,
        "AUG": 8,
        "SEP": 9,
        "OCT": 10,
        "NOV": 11,
        "DEC": 12,
    }
    dates: list[date] = []
    for month, day in re.findall(month_pattern, text.upper()):
        dates.append(date(2026, month_numbers[month[:3]], int(day)))
    return dates
