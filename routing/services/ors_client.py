"""
Thin client around OpenRouteService's Directions API
(https://openrouteservice.org/dev/#/api-docs/v2/directions) - the one
and only routing/map API call this app needs per request.

Free tier: https://openrouteservice.org/dev/#/signup
"""
import requests
from django.conf import settings

ORS_DIRECTIONS_URL = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"

METERS_PER_MILE = 1609.344


class RoutingError(Exception):
    pass


def get_route(start_lat, start_lon, finish_lat, finish_lon):
    """Call ORS once and return a normalized route dict:

    {
        "distance_miles": float,
        "duration_seconds": float,
        "geometry": [[lat, lon], ...]   # ordered polyline points
    }
    """
    if not settings.ORS_API_KEY:
        raise RoutingError(
            "ORS_API_KEY is not configured. Sign up for a free key at "
            "https://openrouteservice.org/dev/#/signup and set it in your .env file."
        )

    body = {
        # ORS wants [lon, lat]
        "coordinates": [[start_lon, start_lat], [finish_lon, finish_lat]],
        "units": "mi",
    }
    headers = {
        "Authorization": settings.ORS_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(ORS_DIRECTIONS_URL, json=body, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise RoutingError(f"Network error contacting OpenRouteService: {exc}") from exc

    if resp.status_code != 200:
        raise RoutingError(f"OpenRouteService error ({resp.status_code}): {resp.text[:500]}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise RoutingError(
            f"OpenRouteService returned a non-JSON response ({resp.status_code}): "
            f"{resp.text[:200]}"
        ) from exc

    features = data.get("features")
    if not features:
        raise RoutingError("OpenRouteService returned no route for these coordinates.")

    feature = features[0]
    summary = feature["properties"]["summary"]
    coords = feature["geometry"]["coordinates"]  # [[lon, lat], ...]

    return {
        "distance_miles": summary["distance"],  # already miles, units='mi'
        "duration_seconds": summary["duration"],
        "geometry": [[lat, lon] for lon, lat in coords],
    }
