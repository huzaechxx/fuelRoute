import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# --- Core security settings ---

DEBUG = os.environ.get("DEBUG", "False") == "True"

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured(
            "SECRET_KEY must be set in the environment. "
            "Generate one with: python -c \"from django.core.management.utils import "
            "get_random_secret_key; print(get_random_secret_key())\""
        )
    # Dev-only fallback — clearly labelled so it can never be mistaken for production.
    SECRET_KEY = "django-insecure-dev-only-key-do-not-use-in-production"  # noqa: S105

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

# --- Apps ---

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "stations",
    "routing",
]

# --- Middleware ---

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

# --- URLs / WSGI ---

ROOT_URLCONF = "fuelroute.urls"
WSGI_APPLICATION = "fuelroute.wsgi.application"

# --- Templates ---
# Required for Django's built-in debug/error pages even in an API-only project.

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

# --- Database ---

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# --- Cache ---

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        # NOTE: LocMemCache is process-local. In a multi-worker deployment
        # (gunicorn -w N) each worker maintains its own cache, so the same
        # route may be fetched from ORS up to N times before any worker's
        # cache warms up. Swap for Redis in production.
    }
}

# --- Misc Django settings ---

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Logging ---

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "routing": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "stations": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
    },
}

# --- Domain-specific settings ---

ORS_API_KEY = os.environ.get("ORS_API_KEY", "")

_mpg_raw = os.environ.get("VEHICLE_MPG", "10")
_range_raw = os.environ.get("VEHICLE_MAX_RANGE_MILES", "500")

try:
    VEHICLE_MPG = int(_mpg_raw)
    VEHICLE_MAX_RANGE_MILES = int(_range_raw)
except ValueError as exc:
    raise ImproperlyConfigured(
        f"VEHICLE_MPG and VEHICLE_MAX_RANGE_MILES must be integers. Got: {exc}"
    ) from exc

if VEHICLE_MPG <= 0:
    raise ImproperlyConfigured(
        f"VEHICLE_MPG must be a positive integer, got {VEHICLE_MPG}."
    )
if VEHICLE_MAX_RANGE_MILES <= 0:
    raise ImproperlyConfigured(
        f"VEHICLE_MAX_RANGE_MILES must be a positive integer, got {VEHICLE_MAX_RANGE_MILES}."
    )
