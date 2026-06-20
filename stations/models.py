from django.db import models


class FuelStation(models.Model):
    """A truckstop / fuel station with a retail diesel price.

    Populated from the provided fuel-prices CSV, joined against a static
    US-cities lat/lon reference table so we never have to geocode at
    request time (and never have to pay for a geocoding API call per
    station). See stations/management/commands/load_stations.py.
    """

    opis_id = models.IntegerField(db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    rack_id = models.IntegerField(null=True, blank=True)
    retail_price = models.DecimalField(max_digits=8, decimal_places=4)

    latitude = models.FloatField(null=True, blank=True, db_index=True)
    longitude = models.FloatField(null=True, blank=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
            models.Index(fields=["retail_price"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) - ${self.retail_price}"
