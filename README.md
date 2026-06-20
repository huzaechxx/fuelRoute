# Fuel Route API

Django + DRF API that, given a start and finish location in the USA, returns:

- the driving route (map polyline) between them,
- an ordered list of recommended fuel stops along the way (cheapest
  diesel reachable within the vehicle's range),
- the total estimated fuel cost for the whole trip.

Vehicle assumptions: **500 mile max range**, **10 mpg**.

## How it works

### 1. Fuel price data is pre-geocoded at load time, not at request time

The provided CSV (`data/fuel-prices-for-be-assessment.csv`) has ~8,150
stations but no coordinates - only city/state. Geocoding every station
through a paid/rate-limited API on every request would be slow and
would blow the "don't hit the map API much" requirement.

Instead, `python manage.py load_stations` joins the CSV against a free,
static US-cities lat/lon reference table (`data/us_cities.csv`, from
the public [US-Cities-Database](https://github.com/kelvins/US-Cities-Database)
dataset) **once**, offline, and stores `latitude`/`longitude` directly
on each `FuelStation` row. ~97% of rows matched; the rest (mostly
Canadian stations and a few typo'd city names) are skipped and logged.

### 2. Exactly one call to the routing API per request (usually)

- `start`/`finish` are first resolved to coordinates **locally**
  against the same city reference table (`routing/services/geocoding.py`)
  - zero network calls if you pass `"City, ST"`.
- If a location can't be resolved locally (street address, unusual
  spelling, etc.) it falls back to OpenRouteService's free geocoder -
  one extra call per unresolved location.
- The route itself is fetched with **one call** to OpenRouteService's
  Directions API (`routing/services/ors_client.py`), which returns the
  full route geometry + distance in a single request.
- So: best case 1 external API call, worst case 3 (if both start and
  finish need online geocoding).
- Identical requests are cached in-memory for an hour, so repeat calls
  make **zero** external calls.

### 3. Picking fuel stops (`routing/services/optimizer.py`)

1. The route polyline is down-sampled and every fuel station within a
   ~5 mile corridor of the route is found (via a DB bounding-box query
   + haversine distance, all local - no API calls), and tagged with
   its mile-marker position along the route.
2. Starting from a full tank, the planner greedily looks ahead up to
   500 miles and picks the **cheapest** reachable station, refuels
   there, and repeats until the destination is within range. This is
   the standard cost-greedy heuristic for "gas stations along a fixed
   highway route" and matches the brief's "optimal mostly means cost
   effective."
3. Cost is computed leg by leg: gallons used on each leg ÷ 10 mpg ×
   the price paid at the stop that supplied that leg's fuel. The first
   leg (before any stop) is priced at the first stop's rate, since
   that's the price you'd have paid filling up before departure - this
   assumption is the one debatable judgment call in the project and is
   called out here and in the Loom.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and add a free OpenRouteService key:
# https://openrouteservice.org/dev/#/signup

python manage.py migrate
python manage.py load_stations   # one-time: loads & geocodes the CSV
python manage.py runserver
```

## API

`POST /api/route/`

```json
{
  "start": "Tulsa, OK",
  "finish": "Chicago, IL"
}
```

`start`/`finish` work best as `"City, ST"` (zero extra API calls); a
full street address also works (falls back to online geocoding).

Response (truncated):

```json
{
  "start": {"input": "Tulsa, OK", "resolved": "Tulsa, OK", "lat": 36.15, "lon": -95.99},
  "finish": {"input": "Chicago, IL", "resolved": "Chicago, IL", "lat": 41.88, "lon": -87.63},
  "distance_miles": 598.2,
  "duration_hours": 8.89,
  "vehicle": {"max_range_miles": 500, "mpg": 10},
  "fuel_stops": [
    {
      "name": "RAPID ROBERTS #122",
      "city": "Joplin",
      "state": "MO",
      "lat": 37.097,
      "lon": -94.505,
      "mile_marker": 103.6,
      "price_per_gallon": 2.899,
      "gallons_purchased": 10.36,
      "leg_cost": 30.03
    }
  ],
  "total_fuel_cost_usd": 173.41,
  "route_geometry": [[36.154, -95.993], ["..."]],
  "map_provider": "OpenRouteService",
  "warnings": []
}
```

`route_geometry` is the full ordered `[lat, lon]` polyline returned by
OpenRouteService - drop it straight into Leaflet/Mapbox/Google Maps JS
to draw the route and stop markers.

## Tests / sanity check

```bash
python manage.py test
```

A quick manual smoke test (no live ORS key needed) is also included in
`routing/tests.py`, which mocks the ORS call and exercises the full
view + optimizer against the real loaded station data.

## Notes / things I'd do with more time

- The 5-mile "corridor" around the route and the greedy stop-selection
  are reasonable defaults for a take-home, but a production version
  would want configurable corridor width and a proper shortest-path /
  DP optimization (minimize total cost subject to the range
  constraint, rather than greedy-cheapest-next) for routes with dense
  station coverage where greedy can be a few dollars off optimal.
- Add request-level rate limiting / API auth.
- Swap LocMemCache for Redis in production so the route cache survives
  process restarts and is shared across workers.
