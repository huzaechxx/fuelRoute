"""Small geometry helpers: haversine distance and route-polyline utilities."""
import math

EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def downsample_polyline(points, max_points=600):
    """Evenly thin a polyline down to ~max_points for cheaper distance checks."""
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    sampled = [points[int(i * step)] for i in range(max_points)]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def cumulative_distances(points):
    """Given ordered [(lat, lon), ...], return list of cumulative miles at each point."""
    cum = [0.0]
    for i in range(1, len(points)):
        lat1, lon1 = points[i - 1]
        lat2, lon2 = points[i]
        cum.append(cum[-1] + haversine_miles(lat1, lon1, lat2, lon2))
    return cum


def bounding_box(points, buffer_miles=8):
    """Lat/lon bounding box around a polyline, padded by buffer_miles.

    Longitude padding is computed from the route's mean latitude so the box
    is accurate at high latitudes (e.g. a Seattle→Minneapolis route where a
    degree of longitude is only ~45 miles, not ~69).
    """
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]

    lat_pad = buffer_miles / 69.0

    avg_lat = (min(lats) + max(lats)) / 2.0
    miles_per_deg_lon = 69.0 * math.cos(math.radians(avg_lat))
    # Guard against polar edge cases where cos → 0 (irrelevant for US routes).
    lon_pad = buffer_miles / max(miles_per_deg_lon, 1.0)

    return (
        min(lats) - lat_pad,
        max(lats) + lat_pad,
        min(lons) - lon_pad,
        max(lons) + lon_pad,
    )


def _nearest_point_on_segment(lat, lon, lat1, lon1, lat2, lon2, cum1, cum2):
    """Return (distance_miles, mile_marker) for the closest point on segment A→B.

    Uses a planar projection (treating lat/lon as Cartesian) which introduces
    < 0.1% error for the short segments (~1 mile) produced by a 600-point
    polyline sample — acceptable for this use-case.
    """
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    seg_len_sq = dlat ** 2 + dlon ** 2

    if seg_len_sq == 0.0:
        # Degenerate segment (both points identical): treat as a point.
        return haversine_miles(lat, lon, lat1, lon1), cum1

    # Project station onto the segment line; clamp to [0, 1].
    t = ((lat - lat1) * dlat + (lon - lon1) * dlon) / seg_len_sq
    t = max(0.0, min(1.0, t))

    nearest_lat = lat1 + t * dlat
    nearest_lon = lon1 + t * dlon
    dist = haversine_miles(lat, lon, nearest_lat, nearest_lon)
    mile = cum1 + t * (cum2 - cum1)
    return dist, mile


def nearest_point_on_route(lat, lon, sampled_points, sampled_cum):
    """Return (distance_to_route_miles, mile_marker_along_route) for a station.

    Finds the nearest point on any *segment* of the polyline rather than just
    the nearest sampled vertex, giving accurate mile-markers even when the
    station sits between two sparse sample points on a long straight road.
    """
    if not sampled_points:
        return float("inf"), 0.0

    # Single-point degenerate polyline.
    if len(sampled_points) == 1:
        return haversine_miles(lat, lon, sampled_points[0][0], sampled_points[0][1]), 0.0

    best_dist = float("inf")
    best_mile = 0.0

    for i in range(len(sampled_points) - 1):
        lat1, lon1 = sampled_points[i]
        lat2, lon2 = sampled_points[i + 1]
        d, mile = _nearest_point_on_segment(
            lat, lon, lat1, lon1, lat2, lon2, sampled_cum[i], sampled_cum[i + 1]
        )
        if d < best_dist:
            best_dist = d
            best_mile = mile

    return best_dist, best_mile
