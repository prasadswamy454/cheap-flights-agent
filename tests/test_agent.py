import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta

from cheap_flights_agent import CheapFlightsAgent, FlightOffer, TripRequest
from cheap_flights_agent.alerts import (
    FareAlert,
    FareAlertChecker,
    trip_request_from_payload,
    trip_request_to_payload,
)
from cheap_flights_agent.agent import AgentResult, parse_trip_request
from cheap_flights_agent.providers import (
    DemoFlightProvider,
    SerpApiFlightProvider,
    _serpapi_location_id,
    _serpapi_travel_class,
)
from cheap_flights_agent.locations import Location, get_location_repository, set_location_repository
from cheap_flights_agent.llm import (
    LlmFollowUp,
    TripExtraction,
    _trip_request_from_extraction,
    set_llm_interpreter,
)
from cheap_flights_agent.web import (
    _answer_follow_up,
    _follow_up_response,
    _location_payload,
    _request_from_payload,
)


class CheapFlightsAgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.locations = {
            location.code: location
            for location in [
                Location("NYC", "New York all airports", "New York", "United States", 40.7128, -74.006, "JFK,LGA,EWR"),
                Location("JFK", "John F. Kennedy International", "New York", "United States", 40.6413, -73.7781, "JFK"),
                Location("LAX", "Los Angeles International", "Los Angeles", "United States", 33.9416, -118.4085, "LAX"),
                Location("PAR", "Paris all airports", "Paris", "France", 48.8566, 2.3522, "CDG,ORY"),
                Location("HYD", "Rajiv Gandhi International", "Hyderabad", "India", 17.2313, 78.4299, "HYD"),
                Location("FAE", "Vágar Airport", "Vágar", "Faroe Islands", 62.0633, -7.2758, "FAE"),
                Location("KEF", "Keflavik International", "Reykjavik", "Iceland", 63.985, -22.6056, "KEF"),
                Location("ZNZ", "Abeid Amani Karume International", "Zanzibar", "Tanzania", -6.222, 39.2249, "ZNZ"),
                Location("ABQ", "Albuquerque International Sunport", "Albuquerque", "United States", 35.04, -106.609, "ABQ"),
                Location("BLR", "Kempegowda International", "Bengaluru", "India", 13.1979, 77.7063, "BLR"),
                Location("BKK", "Suvarnabhumi Airport", "Bangkok", "Thailand", 13.69, 100.7501, "BKK"),
                Location("DEL", "Indira Gandhi International", "New Delhi", "India", 28.5556, 77.0952, "DEL"),
            ]
        }
        aliases = {
            "NEW YORK": "NYC",
            "NEW YORK CITY": "NYC",
            "LOS ANGELES": "LAX",
            "PARIS": "PAR",
            "HYDERABAD": "HYD",
            "FAROE ISLANDS": "FAE",
            "FAROE": "FAE",
            "ICELAND": "KEF",
            "REYKJAVIK": "KEF",
            "ZANZIBAR": "ZNZ",
            "ALBUQUERQUE": "ABQ",
            "BANGALORE": "BLR",
            "THAILAND": "BKK",
            "BANGKOK": "BKK",
            "DELHI": "DEL",
            "NEW DELHI": "DEL",
        }

        class FakeLocationRepository:
            def resolve(self, value):
                normalized = value.upper().strip()
                code = aliases.get(normalized, normalized)
                return cls.locations.get(code)

            def get(self, code):
                return cls.locations.get(code.upper())

            def get_many(self, codes):
                return [
                    cls.locations[code.upper()]
                    for code in dict.fromkeys(codes)
                    if code and code.upper() in cls.locations
                ]

        set_location_repository(FakeLocationRepository())

        class UnavailableInterpreter:
            available = False

        cls.unavailable_interpreter = UnavailableInterpreter()
        set_llm_interpreter(cls.unavailable_interpreter)

    @classmethod
    def tearDownClass(cls) -> None:
        set_location_repository(None)
        set_llm_interpreter(None)

    def test_agent_ranks_matching_flights_by_cheap_value(self) -> None:
        agent = CheapFlightsAgent(DemoFlightProvider())
        result = agent.search(
            TripRequest(
                origin="JFK",
                destination="LAX",
                depart_date=date(2026, 8, 12),
                return_date=date(2026, 8, 18),
                budget_usd=450,
            )
        )

        self.assertTrue(result.ranked_flights)
        self.assertLessEqual(result.ranked_flights[0].offer.price_usd, 450)
        self.assertIn("The strongest value is", result.message)
        self.assertIn("The next best alternatives are", result.message)

    def test_fare_alert_request_round_trip_serialization(self) -> None:
        request = TripRequest(
            origin="NYC",
            destination="DEL",
            depart_date=date(2026, 10, 11),
            return_date=date(2026, 11, 10),
            passengers=2,
            cabin_class="business",
            trip_duration_days=30,
        )

        restored = trip_request_from_payload(trip_request_to_payload(request))

        self.assertEqual(restored, request)

    def test_fare_alert_checker_marks_target_reached(self) -> None:
        now = datetime.now()
        alert = FareAlert(
            id="00000000-0000-0000-0000-000000000001",
            request=TripRequest(
                origin="JFK",
                destination="LAX",
                depart_date=date(2026, 8, 12),
                trip_type="one_way",
            ),
            target_price_usd=400,
            email=None,
            active=True,
            current_price_usd=None,
            lowest_price_usd=None,
            airline=None,
            booking_url=None,
            status="watching",
            last_error=None,
            created_at=now,
            last_checked_at=None,
            triggered_at=None,
        )

        class FakeRepository:
            def update_check(self, alert_id, **values):
                self.values = values
                return alert

        repository = FakeRepository()
        checker = FareAlertChecker(
            repository=repository,
            agent=CheapFlightsAgent(DemoFlightProvider()),
        )
        checker.check(alert)

        self.assertEqual(repository.values["status"], "triggered")
        self.assertEqual(repository.values["current_price_usd"], 319)

    def test_text_parser_understands_common_trip_request(self) -> None:
        agent = CheapFlightsAgent(DemoFlightProvider())
        result = agent.search_text(
            "Find a cheap round trip from NYC to PAR Aug 10 to Aug 20 under 900"
        )

        self.assertEqual(result.request.origin, "NYC")
        self.assertEqual(result.request.destination, "PAR")
        self.assertEqual(result.request.budget_usd, 900)
        self.assertEqual(result.ranked_flights[0].offer.airline, "French Bee")

    def test_text_parser_accepts_city_names(self) -> None:
        request = parse_trip_request(
            "Find a one way flight from New York to Hyderabad on Aug 12 under $900"
        )

        self.assertEqual(request.origin, "NYC")
        self.assertEqual(request.destination, "HYD")
        self.assertEqual(request.depart_date, date(2026, 8, 12))
        self.assertEqual(request.budget_usd, 900)

    def test_text_parser_accepts_delhi_with_trip_duration(self) -> None:
        request = parse_trip_request(
            "Find a cheap round trip from New York to Delhi. "
            "I have 30 days for the whole trip"
        )

        self.assertEqual(request.origin, "NYC")
        self.assertEqual(request.destination, "DEL")
        self.assertEqual(request.trip_duration_days, 30)
        self.assertTrue(request.flexible_dates)

    def test_text_parser_accepts_price_to_be_under_budget(self) -> None:
        request = parse_trip_request(
            "Find flexible fares from New York to Hyderabad. "
            "I want the price to be under $1200."
        )

        self.assertEqual(request.budget_usd, 1200)
        self.assertTrue(request.flexible_dates)

    def test_llm_structured_trip_builds_valid_request(self) -> None:
        request = _trip_request_from_extraction(
            TripExtraction(
                origin="New York",
                destination="Faroe Islands",
                trip_type="round_trip",
                trip_duration_days=10,
                budget_usd=1200,
                stopover_location="Iceland",
                stopover_days=2,
            )
        )

        self.assertEqual(request.origin, "NYC")
        self.assertEqual(request.destination, "FAE")
        self.assertEqual(request.return_stopover, "KEF")
        self.assertEqual(request.budget_usd, 1200)
        self.assertTrue(request.flexible_dates)

    def test_agent_uses_available_llm_interpreter(self) -> None:
        class FakeInterpreter:
            available = True

            def parse_trip(self, text):
                return TripRequest(
                    origin="JFK",
                    destination="LAX",
                    depart_date=date(2026, 8, 12),
                    return_date=date(2026, 8, 18),
                    budget_usd=450,
                )

        set_llm_interpreter(FakeInterpreter())
        try:
            result = CheapFlightsAgent(DemoFlightProvider()).search_text(
                "This deliberately does not match the regex parser."
            )
        finally:
            set_llm_interpreter(self.unavailable_interpreter)

        self.assertEqual(result.request.origin, "JFK")
        self.assertTrue(result.ranked_flights)

    def test_text_parser_accepts_route_without_from(self) -> None:
        request = parse_trip_request("NYC to HYD 2026-08-12")

        self.assertEqual(request.origin, "NYC")
        self.assertEqual(request.destination, "HYD")
        self.assertEqual(request.trip_type, "one_way")

    def test_text_parser_accepts_destination_before_origin(self) -> None:
        request = parse_trip_request("I want to fly to Hyderabad from New York on Aug 12")

        self.assertEqual(request.origin, "NYC")
        self.assertEqual(request.destination, "HYD")

    def test_text_parser_accepts_between_route(self) -> None:
        request = parse_trip_request(
            "Find flights between New York and Hyderabad Aug 12 to Aug 20"
        )

        self.assertEqual(request.origin, "NYC")
        self.assertEqual(request.destination, "HYD")
        self.assertEqual(request.trip_type, "round_trip")

    def test_text_parser_resolves_location_from_database(self) -> None:
        request = parse_trip_request("Find a flight from New York to Zanzibar on Aug 12")

        self.assertEqual(request.origin, "NYC")
        self.assertEqual(request.destination, "ZNZ")

    def test_location_repository_returns_coordinates_and_provider_code(self) -> None:
        location = get_location_repository().resolve("Albuquerque")

        self.assertIsNotNone(location)
        self.assertEqual(location.code, "ABQ")
        self.assertEqual(location.provider_code, "ABQ")
        self.assertAlmostEqual(location.latitude, 35.04, places=1)

    def test_text_parser_builds_flexible_stopover_request(self) -> None:
        request = parse_trip_request(
            "Find a cheap round trip from New York to Faroe Islands "
            "with a stopover in Iceland while coming back. "
            "I have 10 days for the whole trip"
        )

        self.assertTrue(request.flexible_dates)
        self.assertIsNone(request.depart_date)
        self.assertEqual(request.trip_duration_days, 10)
        self.assertEqual(request.return_stopover, "KEF")
        self.assertEqual(request.stopover_days, 2)

    def test_text_parser_builds_return_stopover_itinerary(self) -> None:
        request = parse_trip_request(
            "Find a cheap round trip from New York to Faroe Islands departing Aug 12 "
            "with a 2-day stopover in Iceland while coming back. "
            "I have 10 days for the whole trip"
        )

        self.assertEqual(request.origin, "NYC")
        self.assertEqual(request.destination, "FAE")
        self.assertEqual(request.trip_type, "multi_city")
        self.assertEqual(request.return_date, date(2026, 8, 22))
        self.assertEqual(
            [(segment.origin, segment.destination, segment.depart_date)
             for segment in request.multi_city_segments],
            [
                ("NYC", "FAE", date(2026, 8, 12)),
                ("FAE", "KEF", date(2026, 8, 20)),
                ("KEF", "NYC", date(2026, 8, 22)),
            ],
        )

    def test_web_payload_builds_trip_request(self) -> None:
        request = _request_from_payload(
            {
                "origin": "jfk",
                "destination": "lax",
                "departDate": "2026-08-12",
                "returnDate": "2026-08-18",
                "budgetUsd": "450",
                "passengers": "2",
                "cabinClass": "business",
                "tripType": "round_trip",
                "maxStops": "0",
                "includeBags": True,
            }
        )

        self.assertEqual(request.origin, "JFK")
        self.assertEqual(request.destination, "LAX")
        self.assertEqual(request.depart_date, date(2026, 8, 12))
        self.assertEqual(request.return_date, date(2026, 8, 18))
        self.assertEqual(request.budget_usd, 450)
        self.assertEqual(request.passengers, 2)
        self.assertEqual(request.max_stops, 0)
        self.assertTrue(request.include_bags)
        self.assertEqual(request.cabin_class, "business")
        self.assertEqual(request.trip_type, "round_trip")

    def test_web_payload_builds_multi_city_request(self) -> None:
        request = _request_from_payload(
            {
                "tripType": "multi_city",
                "passengers": "1",
                "cabinClass": "premium_economy",
                "multiCitySegments": [
                    {"origin": "nyc", "destination": "lax", "departDate": "2026-08-12"},
                    {"origin": "lax", "destination": "hyd", "departDate": "2026-08-18"},
                ],
            }
        )

        self.assertEqual(request.origin, "NYC")
        self.assertEqual(request.destination, "HYD")
        self.assertEqual(request.trip_type, "multi_city")
        self.assertEqual(request.cabin_class, "premium_economy")
        self.assertEqual(len(request.multi_city_segments), 2)

    def test_follow_up_answers_from_current_results(self) -> None:
        payload = {
            "question": "Which option is fastest?",
            "request": {"depart_date": "2026-08-12", "return_date": "2026-08-18"},
            "flights": [
                {
                    "airline": "Alpha",
                    "price_usd": 300,
                    "stops": 1,
                    "totalDurationMinutes": 720,
                    "reasons": ["lowest price"],
                },
                {
                    "airline": "Beta",
                    "price_usd": 340,
                    "stops": 0,
                    "totalDurationMinutes": 360,
                    "reasons": ["nonstop"],
                },
            ],
        }

        self.assertIn("Beta", _answer_follow_up(payload))
        self.assertIn("6h", _answer_follow_up(payload))

    def test_follow_up_budget_refinement_launches_new_search(self) -> None:
        class CapturingAgent:
            def __init__(self) -> None:
                self.request = None

            def search(self, request):
                self.request = request
                return AgentResult(request=request, ranked_flights=[], message="No affordable fares.")

        agent = CapturingAgent()
        payload = {
            "question": "That is too expensive. Search again under $1200 with flexible dates.",
            "request": {
                "origin": "NYC",
                "destination": "FAE",
                "depart_date": "2026-09-10",
                "return_date": "2026-09-20",
                "passengers": 1,
                "budget_usd": None,
                "max_stops": None,
                "include_bags": False,
                "cabin_class": "economy",
                "trip_type": "round_trip",
                "multi_city_segments": [],
                "flexible_dates": False,
                "trip_duration_days": 10,
                "return_stopover": None,
                "stopover_days": None,
            },
            "flights": [],
        }

        response = _follow_up_response(payload, agent)

        self.assertTrue(response["refreshed"])
        self.assertEqual(agent.request.budget_usd, 1200)
        self.assertTrue(agent.request.flexible_dates)
        self.assertIsNone(agent.request.depart_date)

    def test_follow_up_uses_llm_change_set(self) -> None:
        class FakeInterpreter:
            available = True

            def parse_follow_up(self, question, request):
                return LlmFollowUp(
                    "refine_search",
                    {
                        "add_location": "Thailand",
                        "stopover_days": 3,
                        "cabin_class": "business",
                    },
                )

        class CapturingAgent:
            def __init__(self) -> None:
                self.request = None

            def search(self, request):
                self.request = request
                return AgentResult(request=request, ranked_flights=[], message="No fares.")

        agent = CapturingAgent()
        set_llm_interpreter(FakeInterpreter())
        try:
            response = _follow_up_response(
                {
                    "question": "Make the trip more interesting.",
                    "request": {
                        "origin": "NYC",
                        "destination": "FAE",
                        "depart_date": "2026-09-10",
                        "return_date": "2026-09-20",
                        "passengers": 1,
                        "budget_usd": 1200,
                        "max_stops": None,
                        "include_bags": False,
                        "cabin_class": "economy",
                        "trip_type": "round_trip",
                        "multi_city_segments": [],
                        "flexible_dates": False,
                        "trip_duration_days": 10,
                        "return_stopover": None,
                        "stopover_days": None,
                    },
                    "flights": [],
                },
                agent,
            )
        finally:
            set_llm_interpreter(self.unavailable_interpreter)

        self.assertTrue(response["refreshed"])
        self.assertEqual(agent.request.return_stopover, "BKK")
        self.assertEqual(agent.request.stopover_days, 3)
        self.assertEqual(agent.request.cabin_class, "business")

    def test_follow_up_adds_country_as_return_stopover(self) -> None:
        class CapturingAgent:
            def __init__(self) -> None:
                self.request = None

            def search(self, request):
                self.request = request
                return AgentResult(request=request, ranked_flights=[], message="No fares yet.")

        agent = CapturingAgent()
        payload = {
            "question": "Add Thailand to the trip and spend 3 days there.",
            "request": {
                "origin": "NYC",
                "destination": "DPS",
                "depart_date": "2026-09-10",
                "return_date": "2026-09-20",
                "passengers": 1,
                "budget_usd": 1200,
                "max_stops": None,
                "include_bags": False,
                "cabin_class": "economy",
                "trip_type": "round_trip",
                "multi_city_segments": [],
                "flexible_dates": False,
                "trip_duration_days": 10,
                "return_stopover": None,
                "stopover_days": None,
            },
            "flights": [],
        }

        response = _follow_up_response(payload, agent)

        self.assertTrue(response["refreshed"])
        self.assertEqual(agent.request.return_stopover, "BKK")
        self.assertEqual(agent.request.stopover_days, 3)
        self.assertEqual(agent.request.trip_duration_days, 10)
        self.assertEqual(agent.request.trip_type, "multi_city")
        self.assertTrue(agent.request.flexible_dates)

    def test_web_location_payload_uses_database_coordinates(self) -> None:
        request = TripRequest(
            origin="NYC",
            destination="ZNZ",
            depart_date=date(2026, 8, 12),
            trip_type="one_way",
        )
        locations = _location_payload(request, [])

        self.assertEqual([location["code"] for location in locations], ["NYC", "ZNZ"])
        self.assertIn("latitude", locations[1])
        self.assertIn("longitude", locations[1])

    def test_serpapi_provider_maps_flight_offer_payload(self) -> None:
        provider = SerpApiFlightProvider("key")
        offer = provider._offer_from_payload(
            {
                "flights": [
                    {
                        "airline": "Iberia",
                        "departure_airport": {
                            "id": "JFK",
                            "time": "2026-08-12 08:00",
                        },
                        "arrival_airport": {
                            "id": "MAD",
                            "time": "2026-08-12 21:00",
                        },
                    },
                    {
                        "airline": "Iberia",
                        "departure_airport": {
                            "id": "MAD",
                            "time": "2026-08-12 23:00",
                        },
                        "arrival_airport": {
                            "id": "PAR",
                            "time": "2026-08-13 01:00",
                        },
                    },
                ],
                "price": 712,
                "extensions": ["Includes checked baggage"],
            },
            "https://www.google.com/travel/flights",
        )

        self.assertEqual(offer.airline, "Iberia")
        self.assertEqual(offer.origin, "JFK")
        self.assertEqual(offer.destination, "PAR")
        self.assertEqual(offer.price_usd, 712)
        self.assertEqual(offer.stops, 1)
        self.assertTrue(offer.bags_included)

    def test_serpapi_location_expands_common_metro_codes(self) -> None:
        self.assertEqual(_serpapi_location_id("NYC"), "JFK,LGA,EWR")
        self.assertEqual(_serpapi_location_id("hyd"), "HYD")
        self.assertEqual(_serpapi_travel_class("business"), "3")

    def test_serpapi_search_query_expands_metro_codes(self) -> None:
        class CapturingSerpApiProvider(SerpApiFlightProvider):
            def __init__(self) -> None:
                super().__init__("key")
                self.query = {}

            def _get_json(self, query):
                self.query = query
                return {"best_flights": [], "other_flights": []}

        provider = CapturingSerpApiProvider()
        list(
            provider.search(
                TripRequest(
                    origin="NYC",
                    destination="HYD",
                    depart_date=date(2026, 8, 12),
                    passengers=1,
                    trip_type="one_way",
                )
            )
        )

        self.assertEqual(provider.query["departure_id"], "JFK,LGA,EWR")
        self.assertEqual(provider.query["arrival_id"], "HYD")
        self.assertEqual(provider.query["type"], "2")
        self.assertEqual(provider.query["travel_class"], "1")

    def test_serpapi_search_query_builds_multi_city_json(self) -> None:
        class CapturingSerpApiProvider(SerpApiFlightProvider):
            def __init__(self) -> None:
                super().__init__("key")
                self.query = {}

            def _get_json(self, query):
                self.query = query
                return {"best_flights": [], "other_flights": []}

        provider = CapturingSerpApiProvider()
        request = _request_from_payload(
            {
                "tripType": "multi_city",
                "cabinClass": "first",
                "multiCitySegments": [
                    {"origin": "NYC", "destination": "LAX", "departDate": "2026-08-12"},
                    {"origin": "LAX", "destination": "HYD", "departDate": "2026-08-18"},
                ],
            }
        )
        list(provider.search(request))

        self.assertEqual(provider.query["type"], "3")
        self.assertEqual(provider.query["travel_class"], "4")
        self.assertIn('"departure_id":"JFK,LGA,EWR"', provider.query["multi_city_json"])
        self.assertIn('"arrival_id":"HYD"', provider.query["multi_city_json"])

    def test_serpapi_resolves_flexible_stopover_dates(self) -> None:
        class FlexibleSerpApiProvider(SerpApiFlightProvider):
            def __init__(self) -> None:
                super().__init__("key")
                self.query = {}

            def _get_json(self, query):
                self.query = query
                return {"start_date": "2026-09-08", "end_date": "2026-09-22"}

        provider = FlexibleSerpApiProvider()
        request = parse_trip_request(
            "Find a cheap round trip from New York to Faroe Islands "
            "with a stopover in Iceland while coming back. "
            "I have 10 days for the whole trip"
        )
        resolved = provider.resolve_request(request)

        self.assertEqual(provider.query["engine"], "google_travel_explore")
        self.assertEqual(provider.query["month"], "0")
        self.assertEqual(provider.query["travel_duration"], "3")
        self.assertEqual(resolved.depart_date, date(2026, 9, 8))
        self.assertEqual(resolved.return_date, date(2026, 9, 18))
        self.assertEqual(
            [(segment.origin, segment.destination, segment.depart_date)
             for segment in resolved.multi_city_segments],
            [
                ("NYC", "FAE", date(2026, 9, 8)),
                ("FAE", "KEF", date(2026, 9, 16)),
                ("KEF", "NYC", date(2026, 9, 18)),
            ],
        )

    def test_agent_never_returns_offer_above_budget(self) -> None:
        class OverBudgetProvider(DemoFlightProvider):
            def search(self, request):
                return [
                    FlightOffer(
                        airline="Too Expensive",
                        origin="JFK",
                        destination="LAX",
                        depart_at=datetime(2026, 8, 12, 8, 0),
                        arrive_at=datetime(2026, 8, 12, 11, 0),
                        price_usd=5000,
                        stops=0,
                        booking_url="https://example.com/expensive",
                    )
                ]

        result = CheapFlightsAgent(OverBudgetProvider()).search(
            TripRequest(
                origin="JFK",
                destination="LAX",
                depart_date=date(2026, 8, 12),
                budget_usd=1200,
                trip_type="one_way",
            )
        )

        self.assertEqual(result.ranked_flights, [])
        self.assertIn("$1200 maximum", result.message)

    def test_flexible_search_retries_until_offer_is_within_budget(self) -> None:
        class RetryingProvider(SerpApiFlightProvider):
            def __init__(self) -> None:
                super().__init__("key")
                self.resolve_calls = 0
                self.search_calls = 0

            def _resolve_flexible_request(self, request, month):
                self.resolve_calls += 1
                depart = date(2026, 8, 1) + timedelta(days=self.resolve_calls * 7)
                return replace(
                    request,
                    depart_date=depart,
                    return_date=depart + timedelta(days=7),
                    flexible_dates=False,
                )

            def search(self, request):
                self.search_calls += 1
                price = 5000 if self.search_calls == 1 else 1100
                return [
                    FlightOffer(
                        airline="Flexible Air",
                        origin="JFK",
                        destination="LAX",
                        depart_at=datetime.combine(request.depart_date, datetime.min.time()),
                        arrive_at=datetime.combine(
                            request.depart_date,
                            datetime.min.time(),
                        ) + timedelta(hours=6),
                        price_usd=price,
                        stops=1,
                        booking_url="https://example.com/flexible",
                    )
                ]

        provider = RetryingProvider()
        request = TripRequest(
            origin="JFK",
            destination="LAX",
            depart_date=None,
            budget_usd=1200,
            flexible_dates=True,
            trip_duration_days=7,
        )
        resolved, offers = provider.search_with_request(request)

        self.assertEqual(provider.search_calls, 2)
        self.assertEqual(offers[0].price_usd, 1100)
        self.assertIsNotNone(resolved.depart_date)


if __name__ == "__main__":
    unittest.main()
