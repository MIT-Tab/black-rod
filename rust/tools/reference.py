"""Gunicorn entry point for the local Django parity reference."""

import os
from urllib.parse import unquote, urljoin, urlsplit

from django.conf import settings
from django.core.wsgi import get_wsgi_application


if os.environ.get("BLACK_ROD_PARITY_PRODUCTION_ERRORS") == "1":
    settings.DEBUG = False

for feature in ("ENABLE_DEBATER_PROFILE_TAB_CARDS", "ENABLE_DEBATER_PARTNER_PIE_CHART", "ENABLE_TOURNAMENT_TAB_CARD_SEARCH"):
    if os.environ.get(feature) == "1":
        setattr(settings, feature, True)

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

smtp_port = os.environ.get("BLACK_ROD_PARITY_SMTP_PORT")
if smtp_port:
    if not smtp_port.isdecimal() or not 1 <= int(smtp_port) <= 65535:
        raise ValueError("The parity reference only connects to its local SMTP sink")
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST = "127.0.0.1"
    settings.EMAIL_PORT = int(smtp_port)
    settings.DEFAULT_FROM_EMAIL = "parity@example.test"
    settings.EMAIL_TIMEOUT = 5

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

if media_root := os.environ.get("BLACK_ROD_PARITY_MEDIA_ROOT"):
    settings.MEDIA_ROOT = media_root

if amendments := os.environ.get("ROUND_AMENDMENTS_FILE"):
    settings.ROUND_AMENDMENTS_FILE = amendments

application = get_wsgi_application()

oauth = os.environ.get("BLACK_ROD_OAUTH_BASE")
if oauth:
    parsed = urlsplit(oauth)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("The parity reference only connects to its local OAuth fixture")
    from apdaonline.views import APDAOnlineOAuth2Adapter

    APDAOnlineOAuth2Adapter.authorize_url = urljoin(oauth, "oauth/authorize")
    APDAOnlineOAuth2Adapter.access_token_url = urljoin(oauth, "oauth/token")
    APDAOnlineOAuth2Adapter.profile_url = urljoin(oauth, "oauth/me")


if os.environ.get("BLACK_ROD_PARITY_STATIC") == "1":
    from django.contrib.staticfiles.handlers import StaticFilesHandler

    application = StaticFilesHandler(application)
