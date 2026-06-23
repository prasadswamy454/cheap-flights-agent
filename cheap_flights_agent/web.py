from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .agent import CheapFlightsAgent, extract_budget, parse_trip_request, resolve_location
from .models import FlightOffer, MultiCitySegment, TripRequest
from .locations import get_location_repository
from .llm import get_llm_interpreter
from .providers import ProviderConfigurationError, ProviderSearchError, provider_from_env
from .scoring import RankedFlight


ASSET_DIR = Path(__file__).with_name("web_assets")


class FlightsWebHandler(SimpleHTTPRequestHandler):
    agent: CheapFlightsAgent | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ASSET_DIR), **kwargs)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/search", "/api/follow-up"}:
            self.send_error(404, "Endpoint not found")
            return

        try:
            payload = self._read_json()
            if path == "/api/follow-up":
                self._send_json(_follow_up_response(payload, self._agent()))
                return

            agent = self._agent()
            result = agent.search_text(payload["text"]) if payload.get("text") else agent.search(
                _request_from_payload(payload)
            )
            flights = [_ranked_to_payload(item) for item in result.ranked_flights]
            self._send_json(
                {
                    "request": _to_jsonable(result.request),
                    "message": result.message,
                    "flights": flights,
                    "locations": _location_payload(result.request, flights),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=400)
        except ProviderConfigurationError as exc:
            self._send_json({"error": str(exc)}, status=503)
        except ProviderSearchError as exc:
            self._send_json({"error": str(exc)}, status=502)
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=503)

    def _agent(self) -> CheapFlightsAgent:
        if self.__class__.agent is None:
            self.__class__.agent = CheapFlightsAgent(provider_from_env(require_live=True))
        return self.__class__.agent

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Search payload must be a JSON object.")
        return payload

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(host: str = "0.0.0.0", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), FlightsWebHandler)
    print(f"Cheap Flights UI running at http://{host}:{port}")
    server.serve_forever()


def _request_from_payload(payload: dict[str, Any]) -> TripRequest:
    trip_type = str(payload.get("tripType") or "round_trip")
    if trip_type == "multi_city":
        segments = _multi_city_segments_from_payload(payload)
        if len(segments) < 2:
            raise ValueError("Multi-city searches need at least two flight segments.")
        first_segment = segments[0]
        last_segment = segments[-1]
        origin = first_segment.origin
        destination = last_segment.destination
        depart_date = first_segment.depart_date
        return_date = None
    else:
        origin = _required_text(payload, "origin").upper()
        destination = _required_text(payload, "destination").upper()
        depart_date = _date_from_payload(payload, "departDate")
        return_date = (
            _optional_date_from_payload(payload, "returnDate")
            if trip_type == "round_trip"
            else None
        )
        segments = ()

    passengers = int(payload.get("passengers") or 1)
    budget = payload.get("budgetUsd")
    max_stops = payload.get("maxStops")

    if passengers < 1:
        raise ValueError("Passengers must be at least 1.")

    return TripRequest(
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        return_date=return_date,
        passengers=passengers,
        budget_usd=int(budget) if budget not in (None, "") else None,
        max_stops=int(max_stops) if max_stops not in (None, "") else None,
        include_bags=bool(payload.get("includeBags")),
        cabin_class=str(payload.get("cabinClass") or "economy"),
        trip_type=trip_type,
        multi_city_segments=segments,
    )


def _multi_city_segments_from_payload(payload: dict[str, Any]) -> tuple[MultiCitySegment, ...]:
    raw_segments = payload.get("multiCitySegments") or []
    if not isinstance(raw_segments, list):
        raise ValueError("multiCitySegments must be a list.")
    segments: list[MultiCitySegment] = []
    for index, item in enumerate(raw_segments, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Segment {index} must be an object.")
        segments.append(
            MultiCitySegment(
                origin=_required_text(item, "origin").upper(),
                destination=_required_text(item, "destination").upper(),
                depart_date=_date_from_payload(item, "departDate"),
            )
        )
    return tuple(segments)


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required.")
    return value


def _date_from_payload(payload: dict[str, Any], key: str) -> date:
    value = _required_text(payload, key)
    return datetime.strptime(value, "%Y-%m-%d").date()


def _optional_date_from_payload(payload: dict[str, Any], key: str) -> date | None:
    value = str(payload.get(key) or "").strip()
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _ranked_to_payload(ranked: RankedFlight) -> dict[str, Any]:
    offer = ranked.offer
    payload = _to_jsonable(offer)
    payload["score"] = ranked.score
    payload["reasons"] = ranked.reasons
    payload["durationMinutes"] = offer.duration_minutes
    payload["totalDurationMinutes"] = offer.total_duration_minutes
    return payload


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _answer_follow_up(payload: dict[str, Any]) -> str:
    question = _required_text(payload, "question").lower()
    flights = payload.get("flights") or []
    request = payload.get("request") or {}
    if not isinstance(flights, list) or not flights:
        raise ValueError("Run a flight search before asking a follow-up question.")

    valid_flights = [flight for flight in flights if isinstance(flight, dict)]
    if not valid_flights:
        raise ValueError("The current flight results are unavailable.")

    cheapest = min(valid_flights, key=lambda flight: int(flight.get("price_usd", 10**9)))
    fastest = min(
        valid_flights,
        key=lambda flight: int(flight.get("totalDurationMinutes", 10**9)),
    )

    if any(word in question for word in ["cheapest", "lowest", "least expensive", "best price"]):
        return (
            f"The cheapest option is {cheapest.get('airline', 'the first option')} at "
            f"${cheapest.get('price_usd')}, with {_follow_up_stops(cheapest)}."
        )
    if any(word in question for word in ["fastest", "shortest", "quickest", "duration"]):
        minutes = int(fastest.get("totalDurationMinutes", 0))
        return (
            f"The fastest option is {fastest.get('airline', 'this flight')} at "
            f"{_follow_up_duration(minutes)}. It costs ${fastest.get('price_usd')} "
            f"and has {_follow_up_stops(fastest)}."
        )
    if any(word in question for word in ["nonstop", "non-stop", "direct"]):
        nonstop = [flight for flight in valid_flights if int(flight.get("stops", 0)) == 0]
        if not nonstop:
            return "None of the current results are nonstop. Every displayed option includes at least one stop."
        options = ", ".join(
            f"{flight.get('airline')} at ${flight.get('price_usd')}" for flight in nonstop[:3]
        )
        return f"I found {len(nonstop)} nonstop option{'s' if len(nonstop) != 1 else ''}: {options}."
    if any(word in question for word in ["bag", "baggage", "luggage"]):
        included = [flight for flight in valid_flights if flight.get("bags_included")]
        if not included:
            return "None of these fares clearly include baggage in the provider data, so check the fare terms on Google Flights before booking."
        options = ", ".join(
            f"{flight.get('airline')} at ${flight.get('price_usd')}" for flight in included[:3]
        )
        return f"Baggage is shown as included on {len(included)} option{'s' if len(included) != 1 else ''}: {options}."
    if any(word in question for word in ["when", "date", "dates", "travel window"]):
        depart = request.get("depart_date")
        returning = request.get("return_date")
        if depart and returning:
            return f"The selected travel window is {depart} through {returning}."
        if depart:
            return f"The selected departure date is {depart}."
    if any(word in question for word in ["airline", "carriers", "who flies"]):
        airlines = list(dict.fromkeys(str(flight.get("airline")) for flight in valid_flights))
        return f"The current results include {', '.join(airlines)}."
    if any(word in question for word in ["compare", "top", "alternatives", "other options"]):
        options = "; ".join(
            f"{flight.get('airline')} at ${flight.get('price_usd')} with {_follow_up_stops(flight)}"
            for flight in valid_flights[:3]
        )
        return f"The leading options are {options}."
    if any(word in question for word in ["why", "rank", "recommended", "first"]):
        reasons = cheapest.get("reasons") or []
        reason_text = ", ".join(str(reason) for reason in reasons[:3])
        return (
            f"I favored {cheapest.get('airline')} at ${cheapest.get('price_usd')} "
            f"because it offers {reason_text or 'the strongest overall price and itinerary balance'}."
        )

    return (
        f"The best current fare is {cheapest.get('airline')} at ${cheapest.get('price_usd')}, "
        f"while the fastest is {fastest.get('airline')} at "
        f"{_follow_up_duration(int(fastest.get('totalDurationMinutes', 0)))}. "
        "You can ask about price, duration, stops, baggage, airlines, dates, or comparisons."
    )


def _follow_up_response(
    payload: dict[str, Any],
    agent: CheapFlightsAgent,
) -> dict[str, Any]:
    question = _required_text(payload, "question")
    request_payload = payload.get("request")
    if not isinstance(request_payload, dict):
        raise ValueError("Run a flight search before refining the results.")

    request = _trip_request_from_context(request_payload)
    llm_follow_up = None
    interpreter = get_llm_interpreter()
    if interpreter.available:
        try:
            llm_follow_up = interpreter.parse_follow_up(question, request)
        except Exception:
            llm_follow_up = None

    if llm_follow_up and llm_follow_up.action == "undo":
        return {"answer": "Restoring the previous results.", "refreshed": False, "undo": True}

    budget = (
        int(llm_follow_up.changes["budget_usd"])
        if llm_follow_up and "budget_usd" in llm_follow_up.changes
        else extract_budget(question)
    )
    added_location_name = (
        str(llm_follow_up.changes["add_location"])
        if llm_follow_up and llm_follow_up.changes.get("add_location")
        else None
    )
    added_location = (
        resolve_location(added_location_name)
        if added_location_name
        else _extract_added_location(question)
    )
    wants_flexible_dates = (
        bool(llm_follow_up.changes.get("flexible_dates"))
        if llm_follow_up and "flexible_dates" in llm_follow_up.changes
        else any(
        phrase in question.lower()
        for phrase in [
            "flexible",
            "other dates",
            "different dates",
            "cheaper dates",
            "another date",
            "check again",
            "search again",
            "try again",
        ]
        )
    )
    wants_refinement = bool(
        llm_follow_up and llm_follow_up.action == "refine_search"
    ) or budget is not None or wants_flexible_dates or added_location is not None
    if wants_refinement:
        changes: dict[str, Any] = {}
        if budget is not None:
            changes["budget_usd"] = budget
        if wants_flexible_dates:
            changes.update(
                {
                    "depart_date": None,
                    "return_date": None,
                    "flexible_dates": True,
                    "multi_city_segments": (),
                }
            )
        if added_location is not None:
            llm_stopover_days = (
                int(llm_follow_up.changes["stopover_days"])
                if llm_follow_up and llm_follow_up.changes.get("stopover_days")
                else None
            )
            stopover_days = llm_stopover_days or _extract_stopover_days(question) or request.stopover_days or 2
            changes.update(
                {
                    "depart_date": None,
                    "return_date": None,
                    "flexible_dates": True,
                    "trip_type": "multi_city",
                    "multi_city_segments": (),
                    "return_stopover": added_location,
                    "stopover_days": stopover_days,
                }
            )
        if llm_follow_up:
            _apply_llm_follow_up_changes(changes, llm_follow_up.changes, request)
        refined = replace(request, **changes)
        result = agent.search(refined)
        flights = [_ranked_to_payload(item) for item in result.ranked_flights]
        return {
            "answer": result.message,
            "refreshed": True,
            "request": _to_jsonable(result.request),
            "message": result.message,
            "flights": flights,
            "locations": _location_payload(result.request, flights),
        }

    return {"answer": _answer_follow_up(payload), "refreshed": False, "undo": False}


def _apply_llm_follow_up_changes(
    changes: dict[str, Any],
    llm_changes: dict[str, object],
    request: TripRequest,
) -> None:
    for field in [
        "trip_duration_days",
        "passengers",
        "cabin_class",
        "max_stops",
        "include_bags",
    ]:
        if field in llm_changes:
            changes[field] = llm_changes[field]

    if llm_changes.get("remove_stopover"):
        changes.update(
            {
                "return_stopover": None,
                "stopover_days": None,
                "multi_city_segments": (),
                "trip_type": "round_trip",
                "depart_date": None,
                "return_date": None,
                "flexible_dates": True,
            }
        )
    if "trip_duration_days" in llm_changes and request.depart_date:
        changes.update(
            {
                "depart_date": None,
                "return_date": None,
                "multi_city_segments": (),
                "flexible_dates": True,
            }
        )


def _extract_added_location(question: str) -> str | None:
    patterns = [
        r"\b(?:ADD|INCLUDE|VISIT)\s+(.+?)(?=\s+(?:TO|IN|FOR|WITH|ON)\b|[.!?]|$)",
        r"\b(?:STOP|STOPOVER)\s+IN\s+(.+?)(?=\s+(?:FOR|WITH|ON)\b|[.!?]|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, question, re.IGNORECASE)
        if not match:
            continue
        location = resolve_location(match.group(1))
        if location:
            return location
    return None


def _extract_stopover_days(question: str) -> int | None:
    patterns = [
        r"\b(?:SPEND|STAY)\s+(\d{1,2})[\s-]*(?:DAY|NIGHT)S?\b",
        r"\b(\d{1,2})[\s-]*(?:DAY|NIGHT)S?\s+(?:STAY|STOPOVER)\b",
        r"\bSTOPOVER\s+(?:FOR\s+)?(\d{1,2})[\s-]*(?:DAY|NIGHT)S?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _trip_request_from_context(payload: dict[str, Any]) -> TripRequest:
    raw_segments = payload.get("multi_city_segments") or []
    segments = tuple(
        MultiCitySegment(
            origin=str(segment["origin"]),
            destination=str(segment["destination"]),
            depart_date=datetime.strptime(segment["depart_date"], "%Y-%m-%d").date(),
        )
        for segment in raw_segments
    )
    depart_date = _context_date(payload.get("depart_date"))
    return TripRequest(
        origin=_required_text(payload, "origin"),
        destination=_required_text(payload, "destination"),
        depart_date=depart_date,
        return_date=_context_date(payload.get("return_date")),
        passengers=int(payload.get("passengers") or 1),
        budget_usd=int(payload["budget_usd"]) if payload.get("budget_usd") is not None else None,
        max_stops=int(payload["max_stops"]) if payload.get("max_stops") is not None else None,
        include_bags=bool(payload.get("include_bags")),
        cabin_class=str(payload.get("cabin_class") or "economy"),
        trip_type=str(payload.get("trip_type") or "round_trip"),
        multi_city_segments=segments,
        flexible_dates=bool(payload.get("flexible_dates")),
        trip_duration_days=int(payload["trip_duration_days"])
        if payload.get("trip_duration_days") is not None
        else None,
        return_stopover=str(payload["return_stopover"])
        if payload.get("return_stopover")
        else None,
        stopover_days=int(payload["stopover_days"])
        if payload.get("stopover_days") is not None
        else None,
    )


def _context_date(value: Any) -> date | None:
    if not value:
        return None
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _follow_up_stops(flight: dict[str, Any]) -> str:
    stops = int(flight.get("stops", 0))
    if stops == 0:
        return "no stops"
    return f"{stops} stop" if stops == 1 else f"{stops} stops"


def _follow_up_duration(minutes: int) -> str:
    hours, remaining = divmod(minutes, 60)
    return f"{hours}h {remaining}m" if remaining else f"{hours}h"


def _location_payload(request: TripRequest, flights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    codes = [request.origin, request.destination]
    for segment in request.multi_city_segments:
        codes.extend([segment.origin, segment.destination])
    for flight in flights:
        codes.extend([str(flight.get("origin") or ""), str(flight.get("destination") or "")])

    return [
        {
            "code": location.code,
            "name": location.name,
            "municipality": location.municipality,
            "country": location.country,
            "latitude": location.latitude,
            "longitude": location.longitude,
        }
        for location in get_location_repository().get_many(codes)
    ]


if __name__ == "__main__":
    run()
