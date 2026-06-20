"""
Resolve a free-text location ("start"/"finish" from the request) into
(lat, lon) coordinates while making as few external API calls as
possible.

Strategy:
  1. Try to parse the input as "City, ST" and look it up against the
     same static us_cities reference table used to place fuel stations
     on the map. This is a local DB/dict lookup - zero network calls.
  2. If that fails (e.g. the user passed a street address, landmark,
     or a city we don't have in the reference set), fall back to
     OpenRouteService's free geocoding endpoint. This costs at most
     one extra HTTP call per unresolved location.

In the common case (city-level start/finish) this means the ONLY
external API call made by the whole request is the single directions
call in services/ors_client.py.

City ambiguity note: when a (city, state) pair appears more than once
in the reference CSV, the first occurrence is used. This is documented
here because a caller has no way to know which of several "Springfield"
coordinates was chosen.
"""
import csv
import re
from functools import lru_cache
from pathlib import Path

import requests
from django.conf import settings

CITIES_CSV = Path(settings.BASE_DIR) / "data" / "us_cities.csv"

US_STATE_ABBREVS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}


class GeocodingError(Exception):
    pass


@lru_cache(maxsize=1)
def _city_lookup():
    lookup = {}
    with CITIES_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["CITY"].strip().upper(), row["STATE_CODE"].strip().upper())
            lookup.setdefault(key, (float(row["LATITUDE"]), float(row["LONGITUDE"])))
    return lookup


def _try_parse_city_state(text):
    """'Tulsa, OK' / 'Tulsa OK' / 'tulsa, oklahoma' -> (city, state_abbrev) or None."""
    text = text.strip()
    m = re.match(r"^(?P<city>[A-Za-z .'-]+)[,\s]+(?P<state>[A-Za-z]{2})$", text)
    if not m:
        return None
    city = m.group("city").strip().upper()
    state = m.group("state").strip().upper()
    if state not in US_STATE_ABBREVS:
        return None
    return city, state


def geocode(location_text):
    """Return (lat, lon, resolved_label, source) for a location string.

    source is 'local' (no network call) or 'ors' (one network call).
    """
    parsed = _try_parse_city_state(location_text)
    if parsed:
        coords = _city_lookup().get(parsed)
        if coords:
            lat, lon = coords
            label = f"{parsed[0].title()}, {parsed[1]}"
            return lat, lon, label, "local"

    # Fallback: ask OpenRouteService's geocoder (Pelias). Costs 1 API call.
    if not settings.ORS_API_KEY:
        raise GeocodingError(
            f"Could not resolve '{location_text}' from the local city list, and no "
            f"ORS_API_KEY is configured to fall back to online geocoding. Try the "
            f"format 'City, ST' (e.g. 'Tulsa, OK')."
        )

    try:
        resp = requests.get(
            "https://api.openrouteservice.org/geocode/search",
            params={
                "api_key": settings.ORS_API_KEY,
                "text": location_text,
                "boundary.country": "US",
                "size": 1,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        raise GeocodingError(
            f"Network error while geocoding '{location_text}': {exc}"
        ) from exc

    if resp.status_code != 200:
        raise GeocodingError(
            f"Geocoding failed for '{location_text}' ({resp.status_code}): {resp.text[:500]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise GeocodingError(
            f"Geocoding service returned a non-JSON response for '{location_text}': "
            f"{resp.text[:200]}"
        ) from exc

    features = data.get("features") or []
    if not features:
        raise GeocodingError(f"No geocoding match found for '{location_text}'.")

    lon, lat = features[0]["geometry"]["coordinates"]
    label = features[0]["properties"].get("label", location_text)
    return lat, lon, label, "ors"
