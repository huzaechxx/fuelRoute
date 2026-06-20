"""
Core fuel-stop optimizer.

Given a route polyline + cumulative mileage, and the set of fuel
stations near that route (each tagged with its mile-marker position
along the route), greedily choose fuel stops so that:

  - the vehicle never runs out of fuel (never exceeds VEHICLE_MAX_RANGE
    miles since its last fill, including the very first leg from the
    full starting tank),
  - at each fill-up decision point we pick the cheapest reachable
    station within the remaining range, which is the standard
    cost-greedy heuristic for this kind of "gas station on a highway"
    problem and is what the assignment calls for ("optimal mostly
    means cost effective").

Assumption (documented): the vehicle starts with a full tank. The fuel
used on the very first leg (before the first stop) is priced at the
rate of the first station actually chosen, since that's the price you'd
have paid to fill up before departing. Every subsequent leg's fuel is
priced at the stop where it was purchased.

Tie-breaking: when two stations share the cheapest price, the one
earlier on the route is preferred (it appears first in the
mile_marker-sorted list, and Python's min() is stable).

Cost rounding: individual leg costs in the breakdown are rounded to 2
decimal places for display. The total is computed from unrounded values
so the column sum and the total are always internally consistent (they
may still differ from the sum of the displayed rounded leg values by
a few cents — the same behaviour you see on any itemised receipt).
"""
from django.conf import settings

from .geometry import (
    bounding_box,
    cumulative_distances,
    downsample_polyline,
    nearest_point_on_route,
)
from stations.models import FuelStation

ROUTE_CORRIDOR_MILES = 5  # how far off the route a station can be and still count


class OptimizationError(Exception):
    pass


def _candidate_stations_near_route(points):
    min_lat, max_lat, min_lon, max_lon = bounding_box(points, buffer_miles=ROUTE_CORRIDOR_MILES + 3)
    return FuelStation.objects.filter(
        latitude__gte=min_lat,
        latitude__lte=max_lat,
        longitude__gte=min_lon,
        longitude__lte=max_lon,
        latitude__isnull=False,
        longitude__isnull=False,
    )


def stations_on_route(geometry):
    """Return a list of dicts for stations within ROUTE_CORRIDOR_MILES of the
    route, each with a `mile_marker` (distance along the route) attached,
    sorted by mile_marker. Pure local computation — no API calls.
    """
    sampled = downsample_polyline(geometry, max_points=600)
    sampled_cum = cumulative_distances(sampled)

    candidates = _candidate_stations_near_route(geometry)

    on_route = []
    for station in candidates:
        dist, mile = nearest_point_on_route(
            station.latitude, station.longitude, sampled, sampled_cum
        )
        if dist <= ROUTE_CORRIDOR_MILES:
            on_route.append(
                {
                    "station": station,
                    "distance_from_route_miles": round(dist, 2),
                    "mile_marker": mile,
                }
            )

    on_route.sort(key=lambda x: x["mile_marker"])
    return on_route


def plan_fuel_stops(total_distance_miles, on_route_stations):
    """Greedy cheapest-reachable-station fuel planner.

    Returns (stops, warnings) where stops is an ordered list of dicts:
        {station, mile_marker, ...}

    Raises OptimizationError if the vehicle cannot complete the route
    because no fuel station exists within range of a required stop point.
    This is a hard failure — the caller should surface it as an error
    rather than returning a partial/misleading result.
    """
    max_range = settings.VEHICLE_MAX_RANGE_MILES
    mpg = settings.VEHICLE_MPG
    warnings = []

    if total_distance_miles <= max_range:
        return [], warnings

    stops = []
    current_mile = 0.0
    remaining = list(on_route_stations)  # already sorted by mile_marker

    while total_distance_miles - current_mile > max_range:
        reachable = [
            s for s in remaining
            if current_mile < s["mile_marker"] <= current_mile + max_range
        ]
        if not reachable:
            raise OptimizationError(
                f"No fuel station found within {max_range} miles of mile marker "
                f"{current_mile:.1f} (searched up to mile {current_mile + max_range:.1f} "
                f"within the {ROUTE_CORRIDOR_MILES}-mile route corridor). "
                f"The vehicle cannot complete this route without leaving the search corridor."
            )

        cheapest = min(reachable, key=lambda s: s["station"].retail_price)
        stops.append(cheapest)

        current_mile = cheapest["mile_marker"]
        remaining = [s for s in remaining if s["mile_marker"] > current_mile]

    return stops, warnings


def calculate_cost(total_distance_miles, stops):
    """Price every leg of the trip using the stop that supplied its fuel.

    Returns (total_cost, breakdown).

    total_cost is None when there are no stops (trip fits in one tank) —
    the caller is responsible for pricing that case (e.g. at the cheapest
    nearby station's rate).

    The total is computed from unrounded intermediate values so it is
    always internally consistent, even though individual leg_cost values
    in the breakdown are rounded to 2 decimal places for display.
    """
    mpg = settings.VEHICLE_MPG

    if not stops:
        return None, []

    breakdown = []
    raw_costs = []
    leg_start = 0.0

    for stop in stops:
        leg_miles = stop["mile_marker"] - leg_start
        price = float(stop["station"].retail_price)
        gallons = leg_miles / mpg
        cost = gallons * price
        raw_costs.append(cost)
        breakdown.append(
            {
                "station": stop["station"],
                "mile_marker": round(stop["mile_marker"], 1),
                "leg_miles": round(leg_miles, 1),
                "price_per_gallon": price,
                "gallons_purchased": round(gallons, 2),
                "leg_cost": round(cost, 2),
            }
        )
        leg_start = stop["mile_marker"]

    # Final leg from the last stop to the destination, priced at the last stop's rate.
    final_leg_miles = total_distance_miles - leg_start
    final_price = float(stops[-1]["station"].retail_price)
    final_gallons = final_leg_miles / mpg
    final_cost = final_gallons * final_price
    raw_costs.append(final_cost)
    breakdown.append(
        {
            "station": None,
            "mile_marker": round(total_distance_miles, 1),
            "leg_miles": round(final_leg_miles, 1),
            "price_per_gallon": final_price,
            "gallons_purchased": round(final_gallons, 2),
            "leg_cost": round(final_cost, 2),
            "note": "final leg to destination, fueled at last stop's price",
        }
    )

    total_cost = round(sum(raw_costs), 2)
    return total_cost, breakdown
