from __future__ import annotations

import argparse
from datetime import datetime

from .agent import CheapFlightsAgent
from .models import TripRequest


def main() -> None:
    parser = argparse.ArgumentParser(description="Find and explain cheap flight options.")
    parser.add_argument("request", nargs="?", help="Free-form trip request.")
    parser.add_argument("--from", dest="origin", help="Origin airport or city code.")
    parser.add_argument("--to", dest="destination", help="Destination airport or city code.")
    parser.add_argument("--depart", help="Departure date in YYYY-MM-DD format.")
    parser.add_argument("--return", dest="return_date", help="Return date in YYYY-MM-DD format.")
    parser.add_argument("--budget", type=int, help="Maximum fare in USD.")
    parser.add_argument("--passengers", type=int, default=1, help="Number of passengers.")
    parser.add_argument("--max-stops", type=int, help="Maximum stops.")
    parser.add_argument("--bags", action="store_true", help="Require included bags.")
    parser.add_argument("--limit", type=int, default=3, help="Number of options to print.")
    args = parser.parse_args()

    agent = CheapFlightsAgent()
    result = agent.search_text(args.request) if args.request else agent.search(_request_from_args(args))

    print(result.message)
    print()
    for index, ranked in enumerate(result.ranked_flights[: args.limit], start=1):
        offer = ranked.offer
        depart = offer.depart_at.strftime("%Y-%m-%d %H:%M")
        arrive = offer.arrive_at.strftime("%Y-%m-%d %H:%M")
        stops = "nonstop" if offer.stops == 0 else f"{offer.stops} stop"
        bags = "bags included" if offer.bags_included else "bags extra"
        reasons = "; ".join(ranked.reasons)
        print(f"{index}. {offer.airline}: ${offer.price_usd} | {stops} | {bags}")
        print(f"   Outbound: {depart} -> {arrive}")
        if offer.return_depart_at and offer.return_arrive_at:
            ret_depart = offer.return_depart_at.strftime("%Y-%m-%d %H:%M")
            ret_arrive = offer.return_arrive_at.strftime("%Y-%m-%d %H:%M")
            print(f"   Return:   {ret_depart} -> {ret_arrive}")
        print(f"   Reasons:  {reasons}")
        print(f"   Link:     {offer.booking_url}")


def _request_from_args(args: argparse.Namespace) -> TripRequest:
    missing = [
        name
        for name, value in {
            "--from": args.origin,
            "--to": args.destination,
            "--depart": args.depart,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing required fields: {', '.join(missing)}")

    return TripRequest(
        origin=args.origin.upper(),
        destination=args.destination.upper(),
        depart_date=datetime.strptime(args.depart, "%Y-%m-%d").date(),
        return_date=datetime.strptime(args.return_date, "%Y-%m-%d").date()
        if args.return_date
        else None,
        passengers=args.passengers,
        budget_usd=args.budget,
        max_stops=args.max_stops,
        include_bags=args.bags,
    )


if __name__ == "__main__":
    main()
