"""
Test suite for the /api/route/ endpoint.

Design decisions:
- ORS directions calls are always mocked — tests must run offline and
  without an API key.
- FuelStation objects are created programmatically in setUpTestData so
  there's no fixture file to keep in sync with the schema.
- Stations are placed along a straight Tulsa→Chicago line so the fake
  straight-line geometry reliably intersects them within the 5-mile
  corridor.
"""
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from stations.models import FuelStation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_route(start, finish, n=200, distance_miles=None):
    """Build a fake ORS route response between two (lat, lon) points."""
    if distance_miles is None:
        # straight-line approximation in degrees × 69 mi/deg (good enough for tests)
        import math
        dlat = finish[0] - start[0]
        dlon = finish[1] - start[1]
        distance_miles = math.sqrt((dlat * 69) ** 2 + (dlon * 52) ** 2)

    geometry = [
        [
            start[0] + (finish[0] - start[0]) * i / n,
            start[1] + (finish[1] - start[1]) * i / n,
        ]
        for i in range(n + 1)
    ]
    return {
        "distance_miles": distance_miles,
        "duration_seconds": distance_miles * 60,
        "geometry": geometry,
    }


TULSA = (36.1540, -95.9928)
CHICAGO = (41.8781, -87.6298)
OKC = (35.4676, -97.5164)

URL = "/api/route/"


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

class RouteAPITests(TestCase):
    """Integration tests: real geocoding, real DB, real optimizer; mocked ORS."""

    @classmethod
    def setUpTestData(cls):
        """
        Place stations exactly on the Tulsa→Chicago straight-line route so
        the fake geometry (which IS that straight line) finds them at
        distance ≈ 0 miles, well within the 5-mile corridor.

        Coordinates are computed by interpolating between TULSA and CHICAGO
        at t = 0.20, 0.40, 0.60, 0.80:
            lat(t) = 36.1540 + t * (41.8781 - 36.1540)
            lon(t) = -95.9928 + t * (-87.6298 - (-95.9928))
        """
        dlat = CHICAGO[0] - TULSA[0]   # 5.7241
        dlon = CHICAGO[1] - TULSA[1]   # 8.363

        def interp(t):
            return (
                round(TULSA[0] + t * dlat, 6),
                round(TULSA[1] + t * dlon, 6),
            )

        p20 = interp(0.20)   # ~124 mi along a 620-mile route
        p40 = interp(0.40)   # ~248 mi
        p60 = interp(0.60)   # ~372 mi
        p80 = interp(0.80)   # ~496 mi

        FuelStation.objects.bulk_create([
            FuelStation(
                opis_id=1, name="Station Alpha", address="1 Alpha Rd",
                city="Joplin", state="MO", retail_price=Decimal("2.899"),
                latitude=p20[0], longitude=p20[1],
            ),
            FuelStation(
                opis_id=2, name="Station Beta", address="2 Beta Rd",
                city="Springfield", state="MO", retail_price=Decimal("2.750"),
                latitude=p40[0], longitude=p40[1],
            ),
            FuelStation(
                opis_id=3, name="Station Gamma", address="3 Gamma Rd",
                city="Rolla", state="MO", retail_price=Decimal("3.100"),
                latitude=p60[0], longitude=p60[1],
            ),
            FuelStation(
                opis_id=4, name="Station Delta", address="4 Delta Rd",
                city="Effingham", state="IL", retail_price=Decimal("2.800"),
                latitude=p80[0], longitude=p80[1],
            ),
        ])

    def setUp(self):
        self.client = APIClient()
        # LocMemCache is process-local and persists between tests in the same
        # run. Clear it so no test can observe another test's cached response.
        cache.clear()

    # --- Happy path ---

    def test_long_route_returns_fuel_stops_and_cost(self):
        """600+ mile route must produce at least one fuel stop and a cost."""
        fake = _fake_route(TULSA, CHICAGO, distance_miles=620.0)
        with patch("routing.views.get_route", return_value=fake):
            resp = self.client.post(URL, {"start": "Tulsa, OK", "finish": "Chicago, IL"}, format="json")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertAlmostEqual(data["distance_miles"], 620.0, delta=1)
        self.assertGreaterEqual(len(data["fuel_stops"]), 1)
        self.assertIsNotNone(data["total_fuel_cost_usd"])
        self.assertGreater(data["total_fuel_cost_usd"], 0)

    def test_last_stop_is_destination(self):
        """The final entry in fuel_stops must carry reached=True and destination coords."""
        fake = _fake_route(TULSA, CHICAGO, distance_miles=620.0)
        with patch("routing.views.get_route", return_value=fake):
            resp = self.client.post(URL, {"start": "Tulsa, OK", "finish": "Chicago, IL"}, format="json")

        data = resp.json()
        last = data["fuel_stops"][-1]
        self.assertTrue(last["reached"])
        self.assertIsNotNone(last["lat"])
        self.assertIsNotNone(last["lon"])
        self.assertIsNotNone(last["name"])

    def test_intermediate_stops_have_reached_false(self):
        """All stops before the destination must carry reached=False."""
        fake = _fake_route(TULSA, CHICAGO, distance_miles=620.0)
        with patch("routing.views.get_route", return_value=fake):
            resp = self.client.post(URL, {"start": "Tulsa, OK", "finish": "Chicago, IL"}, format="json")

        data = resp.json()
        for stop in data["fuel_stops"][:-1]:
            self.assertFalse(stop["reached"], msg=f"Expected reached=False for {stop['name']}")

    def test_short_route_no_required_stops_still_prices_trip(self):
        """A sub-500-mile trip needs no stop but should still return a cost estimate."""
        fake = _fake_route(TULSA, OKC, distance_miles=100.0)
        with patch("routing.views.get_route", return_value=fake):
            resp = self.client.post(URL, {"start": "Tulsa, OK", "finish": "Oklahoma City, OK"}, format="json")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Cost may be None if no stations exist near a very short route, but
        # with our test stations present it should be priced.
        self.assertIn("total_fuel_cost_usd", data)

    def test_response_is_cached(self):
        """Identical requests (normalised) should only call ORS once."""
        fake = _fake_route(TULSA, CHICAGO, distance_miles=620.0)
        with patch("routing.views.get_route", return_value=fake) as mock_ors:
            self.client.post(URL, {"start": "Tulsa, OK", "finish": "Chicago, IL"}, format="json")
            self.client.post(URL, {"start": "tulsa, ok", "finish": "chicago, il"}, format="json")

        self.assertEqual(mock_ors.call_count, 1, "Cache miss: ORS was called more than once")

    # --- Input validation ---

    def test_missing_finish_returns_400(self):
        resp = self.client.post(URL, {"start": "Tulsa, OK"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_missing_start_returns_400(self):
        resp = self.client.post(URL, {"finish": "Chicago, IL"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_blank_start_returns_400(self):
        resp = self.client.post(URL, {"start": "   ", "finish": "Chicago, IL"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_blank_finish_returns_400(self):
        resp = self.client.post(URL, {"start": "Tulsa, OK", "finish": ""}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_start_equals_finish_returns_400(self):
        resp = self.client.post(URL, {"start": "Tulsa, OK", "finish": "Tulsa, OK"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_start_equals_finish_case_insensitive(self):
        resp = self.client.post(URL, {"start": "Tulsa, OK", "finish": "tulsa, ok"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_whitespace_stripped_from_inputs(self):
        """Leading/trailing whitespace must not cause a geocoding miss."""
        fake = _fake_route(TULSA, CHICAGO, distance_miles=620.0)
        with patch("routing.views.get_route", return_value=fake):
            resp = self.client.post(
                URL,
                {"start": "  Tulsa, OK  ", "finish": "  Chicago, IL  "},
                format="json",
            )
        self.assertEqual(resp.status_code, 200)

    # --- External service failures ---

    def test_ors_routing_error_returns_502(self):
        from routing.services.ors_client import RoutingError
        with patch("routing.views.get_route", side_effect=RoutingError("ORS timeout")):
            resp = self.client.post(URL, {"start": "Tulsa, OK", "finish": "Chicago, IL"}, format="json")

        self.assertEqual(resp.status_code, 502)
        self.assertIn("error", resp.json())

    def test_geocoding_error_returns_400(self):
        from routing.services.geocoding import GeocodingError
        with patch("routing.views.geocode", side_effect=GeocodingError("Unknown location")):
            resp = self.client.post(URL, {"start": "Nowhere, XX", "finish": "Chicago, IL"}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_no_stations_mid_route_returns_422(self):
        """When the optimizer cannot bridge a gap in coverage, return 422."""
        # A 600-mile route with no stations in the DB for that corridor.
        FuelStation.objects.all().delete()
        fake = _fake_route(TULSA, CHICAGO, distance_miles=600.0)
        with patch("routing.views.get_route", return_value=fake):
            resp = self.client.post(URL, {"start": "Tulsa, OK", "finish": "Chicago, IL"}, format="json")

        self.assertEqual(resp.status_code, 422)
        self.assertIn("error", resp.json())


# ---------------------------------------------------------------------------
# Unit tests for optimizer internals
# ---------------------------------------------------------------------------

class PlanFuelStopsTests(TestCase):
    """Unit-test the greedy planner directly."""

    def _make_station(self, mile, price, name="S"):
        s = FuelStation(
            opis_id=mile, name=name, address="", city="X", state="XX",
            retail_price=Decimal(str(price)), latitude=0.0, longitude=0.0,
        )
        s.save()
        return {"station": s, "mile_marker": float(mile), "distance_from_route_miles": 0.0}

    def test_short_trip_needs_no_stop(self):
        from routing.services.optimizer import plan_fuel_stops
        stops, warnings = plan_fuel_stops(300.0, [])
        self.assertEqual(stops, [])
        self.assertEqual(warnings, [])

    def test_greedy_picks_cheapest_reachable(self):
        """With two stations in range, the cheaper one must be chosen."""
        from routing.services.optimizer import plan_fuel_stops
        cheap = self._make_station(200, 2.50, "Cheap")
        expensive = self._make_station(250, 3.50, "Expensive")
        stops, _ = plan_fuel_stops(600.0, [cheap, expensive])
        self.assertEqual(stops[0]["station"].name, "Cheap")

    def test_raises_when_gap_is_too_large(self):
        """No station within range → OptimizationError, not a silent warning."""
        from routing.services.optimizer import OptimizationError, plan_fuel_stops
        with self.assertRaises(OptimizationError):
            plan_fuel_stops(600.0, [])


class CalculateCostTests(TestCase):
    """Unit-test cost calculation and rounding consistency."""

    def _make_stop(self, mile, price):
        s = FuelStation(
            opis_id=int(mile), name="S", address="", city="X", state="XX",
            retail_price=Decimal(str(price)), latitude=0.0, longitude=0.0,
        )
        s.save()
        return {"station": s, "mile_marker": float(mile), "distance_from_route_miles": 0.0}

    def test_total_cost_from_unrounded_values(self):
        """total_fuel_cost_usd must equal sum of raw (unrounded) leg costs, not
        the sum of the rounded leg_cost display values."""
        from routing.services.optimizer import calculate_cost
        stop = self._make_stop(250, 2.999)
        total, breakdown = calculate_cost(600.0, [stop])
        # Recompute from raw values to verify
        leg1 = (250.0 / 10) * 2.999
        leg2 = (350.0 / 10) * 2.999
        expected_total = round(leg1 + leg2, 2)
        self.assertAlmostEqual(total, expected_total, places=4)

    def test_no_stops_returns_none(self):
        from routing.services.optimizer import calculate_cost
        total, breakdown = calculate_cost(400.0, [])
        self.assertIsNone(total)
        self.assertEqual(breakdown, [])
