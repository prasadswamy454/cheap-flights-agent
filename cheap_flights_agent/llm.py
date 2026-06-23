from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field

from .locations import get_location_repository
from .models import MultiCitySegment, TripRequest


class TripExtraction(BaseModel):
    origin: str
    destination: str
    trip_type: Literal["round_trip", "one_way", "multi_city"] = "round_trip"
    depart_date: str | None = Field(default=None, description="ISO YYYY-MM-DD date")
    return_date: str | None = Field(default=None, description="ISO YYYY-MM-DD date")
    trip_duration_days: int | None = Field(default=None, ge=1, le=365)
    budget_usd: int | None = Field(default=None, ge=1)
    passengers: int = Field(default=1, ge=1, le=9)
    cabin_class: Literal["economy", "premium_economy", "business", "first"] = "economy"
    max_stops: int | None = Field(default=None, ge=0, le=2)
    include_bags: bool = False
    stopover_location: str | None = None
    stopover_days: int | None = Field(default=None, ge=1, le=30)
    stopover_on_return: bool = True


class FollowUpExtraction(BaseModel):
    action: Literal["answer_existing", "refine_search", "undo"]
    budget_usd: int | None = Field(default=None, ge=1)
    flexible_dates: bool | None = None
    trip_duration_days: int | None = Field(default=None, ge=1, le=365)
    add_location: str | None = None
    remove_stopover: bool = False
    stopover_days: int | None = Field(default=None, ge=1, le=30)
    passengers: int | None = Field(default=None, ge=1, le=9)
    cabin_class: Literal["economy", "premium_economy", "business", "first"] | None = None
    max_stops: int | None = Field(default=None, ge=0, le=2)
    include_bags: bool | None = None


@dataclass(frozen=True)
class LlmFollowUp:
    action: str
    changes: dict[str, object]


class LlmTripInterpreter:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: OpenAI | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-5.5"
        self.client = client or (OpenAI(api_key=self.api_key) if self.api_key else None)

    @property
    def available(self) -> bool:
        return self.client is not None

    def parse_trip(self, text: str) -> TripRequest:
        if not self.client:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        response = self.client.responses.parse(
            model=self.model,
            reasoning={"effort": "low"},
            input=[
                {
                    "role": "system",
                    "content": (
                        "Extract a flight search request. Preserve hard constraints exactly. "
                        "Use location names as written; the application validates them against "
                        "its airport database. If no dates are given, leave both dates null. "
                        "If a duration is given without dates, set trip_duration_days. "
                        "A requested intermediate place is a stopover. Do not invent a budget, "
                        "date, passenger count, cabin, baggage rule, or stop limit."
                    ),
                },
                {"role": "user", "content": text},
            ],
            text_format=TripExtraction,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise ValueError("The trip request could not be interpreted.")
        return _trip_request_from_extraction(parsed)

    def parse_follow_up(self, question: str, request: TripRequest) -> LlmFollowUp:
        if not self.client:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        response = self.client.responses.parse(
            model=self.model,
            reasoning={"effort": "low"},
            input=[
                {
                    "role": "system",
                    "content": (
                        "Classify a follow-up about existing flight results. Use undo for requests "
                        "to restore previous results. Use refine_search when the user changes the "
                        "itinerary, budget, flexibility, duration, passengers, cabin, stops, or "
                        "baggage. Use answer_existing for questions that only compare or explain "
                        "the current fares. Return only fields explicitly changed by the user."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Current trip: {_request_context(request)}\n"
                        f"Follow-up: {question}"
                    ),
                },
            ],
            text_format=FollowUpExtraction,
        )
        parsed = response.output_parsed
        if parsed is None:
            return LlmFollowUp("answer_existing", {})
        changes = {
            key: value
            for key, value in parsed.model_dump(exclude={"action"}, exclude_none=True).items()
            if value is not False
        }
        if parsed.remove_stopover:
            changes["remove_stopover"] = True
        return LlmFollowUp(parsed.action, changes)


_interpreter: LlmTripInterpreter | None = None


def get_llm_interpreter() -> LlmTripInterpreter:
    global _interpreter
    if _interpreter is None:
        _load_dotenv()
        _interpreter = LlmTripInterpreter()
    return _interpreter


def set_llm_interpreter(interpreter: LlmTripInterpreter | None) -> None:
    global _interpreter
    _interpreter = interpreter


def _trip_request_from_extraction(parsed: TripExtraction) -> TripRequest:
    repository = get_location_repository()
    origin = repository.resolve(parsed.origin)
    destination = repository.resolve(parsed.destination)
    if not origin or not destination:
        missing = parsed.origin if not origin else parsed.destination
        raise ValueError(f"I could not identify the location '{missing}'.")

    depart_date = _parse_iso_date(parsed.depart_date)
    return_date = _parse_iso_date(parsed.return_date)
    if depart_date and not return_date and parsed.trip_duration_days and parsed.trip_type != "one_way":
        return_date = depart_date + timedelta(days=parsed.trip_duration_days)

    stopover = repository.resolve(parsed.stopover_location) if parsed.stopover_location else None
    flexible_dates = depart_date is None
    trip_type = parsed.trip_type
    segments: tuple[MultiCitySegment, ...] = ()
    if stopover:
        trip_type = "multi_city"
        if depart_date and return_date:
            stopover_days = parsed.stopover_days or 2
            stopover_date = return_date - timedelta(days=stopover_days)
            if stopover_date <= depart_date:
                raise ValueError("The stopover leaves no time at the main destination.")
            segments = (
                MultiCitySegment(origin.code, destination.code, depart_date),
                MultiCitySegment(destination.code, stopover.code, stopover_date),
                MultiCitySegment(stopover.code, origin.code, return_date),
            )
    elif depart_date and return_date:
        trip_type = "round_trip"
    elif depart_date:
        trip_type = "one_way"

    return TripRequest(
        origin=origin.code,
        destination=destination.code,
        depart_date=depart_date,
        return_date=return_date,
        passengers=parsed.passengers,
        budget_usd=parsed.budget_usd,
        max_stops=parsed.max_stops,
        include_bags=parsed.include_bags,
        cabin_class=parsed.cabin_class,
        trip_type=trip_type,
        multi_city_segments=segments,
        flexible_dates=flexible_dates,
        trip_duration_days=parsed.trip_duration_days or (7 if flexible_dates else None),
        return_stopover=stopover.code if stopover else None,
        stopover_days=(parsed.stopover_days or 2) if stopover else None,
    )


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _request_context(request: TripRequest) -> str:
    return (
        f"origin={request.origin}; destination={request.destination}; "
        f"depart_date={request.depart_date}; return_date={request.return_date}; "
        f"duration_days={request.trip_duration_days}; budget_usd={request.budget_usd}; "
        f"passengers={request.passengers}; cabin={request.cabin_class}; "
        f"max_stops={request.max_stops}; bags={request.include_bags}; "
        f"stopover={request.return_stopover}; stopover_days={request.stopover_days}"
    )


def _load_dotenv() -> None:
    from pathlib import Path

    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
