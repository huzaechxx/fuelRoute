import logging
import re

from django.conf import settings
from django.core.cache import cache
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteRequestSerializer
from .services.geocoding import GeocodingError, geocode
from .services.optimizer import OptimizationError, calculate_cost, plan_fuel_stops, stations_on_route
from .services.ors_client import RoutingError, get_route

logger = logging.getLogger(__name__)


def _cache_key(start: str, finish: str) -> str:
    """Stable, normalised cache key for a start/finish pair.

    Lowercases, strips, and collapses whitespace so that 'Tulsa, OK',
    'tulsa,ok', and '  Tulsa,  OK  ' all map to the same key.
    Non-word characters are replaced so the key is safe for all cache
    backends (including memcached which rejects spaces and control chars).
    """
    def normalise(s):
        s = re.sub(r"\s+", "_", s.lower().strip())
        return re.sub(r"[^\w\-]", "_", s)

    return "route__" + "__".join(normalise(t) for t in (start, finish))


class RouteView(APIView):
    """
    POST /api/route/
    {
        "start": "Tulsa, OK",
        "finish": "Chicago, IL"
    }

    Returns the driving route, an ordered list of recommended fuel
    stops (cheapest reachable diesel within the vehicle's max range),
    and the total estimated fuel cost for the trip.

    External API budget for this endpoint: 1 call to OpenRouteService
    for the route (up to 2 more, only if start/finish can't be
    resolved locally — see services/geocoding.py).
    """

    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        start_text = serializer.validated_data["start"]
        finish_text = serializer.validated_data["finish"]

        key = _cache_key(start_text, finish_text)
        cached = cache.get(key)
        if cached is not None:
            return Response(cached)

        # --- Geocoding ---
        try:
            start_lat, start_lon, start_label, _ = geocode(start_text)
            finish_lat, finish_lon, finish_label, _ = geocode(finish_text)
        except GeocodingError as exc:
            return Response({"error": str(exc)}, status=400)

        # --- Routing (1 external API call) ---
        try:
            route = get_route(start_lat, start_lon, finish_lat, finish_lon)
        except RoutingError as exc:
            return Response({"error": str(exc)}, status=502)

        # --- Station matching & optimisation (all local) ---
        on_route = stations_on_route(route["geometry"])

        try:
            stops, warnings = plan_fuel_stops(route["distance_miles"], on_route)
        except OptimizationError as exc:
            logger.warning("Optimisation failed for %s → %s: %s", start_text, finish_text, exc)
            return Response({"error": str(exc)}, status=422)

        total_cost, breakdown = calculate_cost(route["distance_miles"], stops)

        # Short-route case: trip fits in one tank — price at cheapest nearby station.
        if total_cost is None:
            if on_route:
                cheapest_overall = min(on_route, key=lambda s: s["station"].retail_price)
                price = float(cheapest_overall["station"].retail_price)
                gallons = route["distance_miles"] / settings.VEHICLE_MPG
                total_cost = round(gallons * price, 2)
                breakdown = [
                    {
                        "station": cheapest_overall["station"],
                        "mile_marker": round(route["distance_miles"], 1),
                        "leg_miles": round(route["distance_miles"], 1),
                        "price_per_gallon": price,
                        "gallons_purchased": round(gallons, 2),
                        "leg_cost": total_cost,
                        "note": (
                            "trip fits within a single tank; "
                            "priced at cheapest station found near the route"
                        ),
                    }
                ]

        # --- Build response ---
        response_payload = {
            "start": {
                "input": start_text,
                "resolved": start_label,
                "lat": start_lat,
                "lon": start_lon,
            },
            "finish": {
                "input": finish_text,
                "resolved": finish_label,
                "lat": finish_lat,
                "lon": finish_lon,
            },
            "distance_miles": round(route["distance_miles"], 1),
            "duration_hours": round(route["duration_seconds"] / 3600, 2),
            "vehicle": {
                "max_range_miles": settings.VEHICLE_MAX_RANGE_MILES,
                "mpg": settings.VEHICLE_MPG,
            },
            "fuel_stops": [
                {
                    "name": b["station"].name if b.get("station") else finish_label,
                    "city": (
                        b["station"].city
                        if b.get("station")
                        else finish_label.split(",")[0].strip()
                    ),
                    "state": (
                        b["station"].state
                        if b.get("station")
                        else finish_label.split(",")[-1].strip()
                    ),
                    "lat": b["station"].latitude if b.get("station") else finish_lat,
                    "lon": b["station"].longitude if b.get("station") else finish_lon,
                    "mile_marker": b["mile_marker"],
                    "price_per_gallon": b["price_per_gallon"],
                    "gallons_purchased": b["gallons_purchased"],
                    "leg_cost": b["leg_cost"],
                    "reached": not b.get("station"),
                }
                for b in breakdown
            ],
            "total_fuel_cost_usd": total_cost,
            "route_geometry": [[lat, lon] for lat, lon in route["geometry"]],
            "map_provider": "OpenRouteService",
            "warnings": warnings,
        }

        cache.set(key, response_payload, timeout=60 * 60)
        return Response(response_payload)
