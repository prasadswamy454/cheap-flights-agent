from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


@dataclass(frozen=True)
class MultiCitySegment:
    origin: str
    destination: str
    depart_date: date


@dataclass(frozen=True)
class TripRequest:
    origin: str
    destination: str
    depart_date: Optional[date]
    return_date: Optional[date] = None
    passengers: int = 1
    budget_usd: Optional[int] = None
    max_stops: Optional[int] = None
    include_bags: bool = False
    cabin_class: str = "economy"
    trip_type: str = "round_trip"
    multi_city_segments: tuple[MultiCitySegment, ...] = ()
    flexible_dates: bool = False
    trip_duration_days: Optional[int] = None
    return_stopover: Optional[str] = None
    stopover_days: Optional[int] = None

    @property
    def is_round_trip(self) -> bool:
        return self.trip_type == "round_trip" and self.return_date is not None


@dataclass(frozen=True)
class FlightOffer:
    airline: str
    origin: str
    destination: str
    depart_at: datetime
    arrive_at: datetime
    price_usd: int
    stops: int
    booking_url: str
    return_depart_at: Optional[datetime] = None
    return_arrive_at: Optional[datetime] = None
    bags_included: bool = False

    @property
    def duration_minutes(self) -> int:
        return int((self.arrive_at - self.depart_at).total_seconds() // 60)

    @property
    def total_duration_minutes(self) -> int:
        if self.return_depart_at and self.return_arrive_at:
            outbound = self.duration_minutes
            inbound = int((self.return_arrive_at - self.return_depart_at).total_seconds() // 60)
            return outbound + inbound
        return self.duration_minutes
