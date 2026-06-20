"""
Load the fuel-price CSV into the FuelStation table, attaching a
latitude/longitude to each row by joining on (city, state) against a
static, free US-cities reference dataset (data/us_cities.csv, sourced
from https://github.com/kelvins/US-Cities-Database).

Why this approach instead of calling a geocoding API per station:
  - There are ~8,150 stations / ~3,900 unique (city, state) pairs.
  - The assignment explicitly wants the map/route API hit as little as
    possible, and that constraint is really about *runtime* API calls,
    not setup. So we resolve coordinates once, offline, at load time,
    from a free static dataset, and store them in the DB.
  - At request time the routing API therefore makes ZERO geocoding
    calls for stations and only 1 call to the routing provider for the
    route geometry itself.

A handful of cities (~2-3%) in the CSV don't have an exact match in the
reference table (typo'd city names, unincorporated places, etc.) -
those rows are skipped and reported at the end.

Usage:
    python manage.py load_stations
    python manage.py load_stations --fuel-csv path/to.csv --cities-csv path/to.csv --clear
"""
import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from stations.models import FuelStation

DEFAULT_FUEL_CSV = Path(settings.BASE_DIR) / "data" / "fuel-prices-for-be-assessment.csv"
DEFAULT_CITIES_CSV = Path(settings.BASE_DIR) / "data" / "us_cities.csv"


class Command(BaseCommand):
    help = "Load fuel station prices + lat/lon into the database."

    def add_arguments(self, parser):
        parser.add_argument("--fuel-csv", default=str(DEFAULT_FUEL_CSV))
        parser.add_argument("--cities-csv", default=str(DEFAULT_CITIES_CSV))
        parser.add_argument(
            "--clear", action="store_true", help="Delete existing FuelStation rows first."
        )

    def handle(self, *args, **options):
        fuel_csv = Path(options["fuel_csv"])
        cities_csv = Path(options["cities_csv"])

        if not fuel_csv.exists():
            self.stderr.write(self.style.ERROR(f"Fuel CSV not found: {fuel_csv}"))
            return
        if not cities_csv.exists():
            self.stderr.write(self.style.ERROR(f"Cities CSV not found: {cities_csv}"))
            return

        self.stdout.write("Building city/state -> (lat, lon) lookup ...")
        city_lookup = {}
        with cities_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row["CITY"].strip().upper(), row["STATE_CODE"].strip().upper())
                # keep first occurrence; good enough for routing purposes
                city_lookup.setdefault(key, (float(row["LATITUDE"]), float(row["LONGITUDE"])))

        self.stdout.write(f"Loaded {len(city_lookup)} city/state reference points.")

        if options["clear"]:
            deleted, _ = FuelStation.objects.all().delete()
            self.stdout.write(f"Cleared {deleted} existing stations.")

        to_create = []
        matched, unmatched = 0, 0
        unmatched_examples = set()

        with fuel_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                city = row["City"].strip()
                state = row["State"].strip()
                key = (city.upper(), state.upper())
                coords = city_lookup.get(key)
                if coords:
                    matched += 1
                else:
                    unmatched += 1
                    unmatched_examples.add(f"{city}, {state}")
                    continue  # skip rows we can't place on the map

                try:
                    price = float(row["Retail Price"])
                except (KeyError, ValueError):
                    continue

                to_create.append(
                    FuelStation(
                        opis_id=int(row["OPIS Truckstop ID"]),
                        name=row["Truckstop Name"].strip(),
                        address=row["Address"].strip(),
                        city=city,
                        state=state,
                        rack_id=int(row["Rack ID"]) if row.get("Rack ID") else None,
                        retail_price=price,
                        latitude=coords[0],
                        longitude=coords[1],
                    )
                )

        with transaction.atomic():
            FuelStation.objects.bulk_create(to_create, batch_size=1000)

        self.stdout.write(self.style.SUCCESS(
            f"Created {len(to_create)} stations. Matched {matched}, skipped {unmatched} "
            f"(no city/state match in reference data)."
        ))
        if unmatched_examples:
            sample = ", ".join(list(unmatched_examples)[:10])
            self.stdout.write(f"Examples of skipped city/state pairs: {sample}")
