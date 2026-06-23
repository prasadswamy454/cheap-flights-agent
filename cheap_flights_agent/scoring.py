from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable, List

from .models import FlightOffer, TripRequest


@dataclass(frozen=True)
class RankedFlight:
    offer: FlightOffer
    score: float
    reasons: List[str]


def rank_flights(request: TripRequest, offers: Iterable[FlightOffer]) -> List[RankedFlight]:
    offers = list(offers)
    if not offers:
        return []

    median_price = median(offer.price_usd for offer in offers)
    ranked = [_rank_one(request, offer, median_price) for offer in offers]
    return sorted(ranked, key=lambda item: (item.score, item.offer.price_usd, item.offer.stops))


def _rank_one(request: TripRequest, offer: FlightOffer, median_price: float) -> RankedFlight:
    score = float(offer.price_usd)
    reasons: List[str] = []

    if offer.price_usd < median_price:
        reasons.append(f"${int(median_price - offer.price_usd)} below the route median")
    if request.budget_usd is not None:
        if offer.price_usd <= request.budget_usd:
            reasons.append(f"within your ${request.budget_usd} budget")
        else:
            score += (offer.price_usd - request.budget_usd) * 1.5
            reasons.append(f"${offer.price_usd - request.budget_usd} over budget")

    score += offer.stops * 55
    if offer.stops == 0:
        reasons.append("nonstop")
    else:
        reasons.append(f"{offer.stops} stop")

    duration_hours = offer.total_duration_minutes / 60
    score += max(0, duration_hours - 6) * 8
    if duration_hours <= 7:
        reasons.append("short travel time")

    if offer.bags_included:
        score -= 20
        reasons.append("bags included")
    elif request.include_bags:
        score += 35

    departure_hour = offer.depart_at.hour
    if 6 <= departure_hour <= 20:
        score -= 10
        reasons.append("reasonable departure time")

    return RankedFlight(offer=offer, score=round(score, 2), reasons=reasons)
