"""Gunicorn entry point for the local Django parity reference."""

import os
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.core.wsgi import get_wsgi_application


if os.environ.get("BLACK_ROD_PARITY_CACHE_MODE") == "cold":
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}

memcached = os.environ.get("BLACK_ROD_PARITY_MEMCACHED_ADDR")
if memcached and os.environ.get("BLACK_ROD_PARITY_CACHE_MODE") != "cold":
    host, _, port = memcached.rpartition(":")
    if host != "127.0.0.1" or not port.isdecimal():
        raise ValueError("The parity reference only connects to local Memcached")
    settings.CACHES = {"default": {
        "BACKEND": "django.core.cache.backends.memcached.PyMemcacheCache",
        "LOCATION": memcached,
    }}

postgres_url = os.environ.get("BLACK_ROD_PARITY_POSTGRES_URL")
if postgres_url:
    parsed = urlsplit(postgres_url)
    if parsed.hostname != "127.0.0.1":
        raise ValueError("The parity reference only connects to local PostgreSQL")
    settings.DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/"),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname,
            "PORT": parsed.port,
        }
    }

application = get_wsgi_application()

if os.environ.get("BLACK_ROD_PARITY_STATIC") == "1":
    from django.contrib.staticfiles.handlers import StaticFilesHandler

    application = StaticFilesHandler(application)
