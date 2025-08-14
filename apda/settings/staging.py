# pylint: disable=unused-wildcard-import,wildcard-import,unused-import
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration

from .base import *

DEBUG = False

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql_psycopg2',
        'NAME': 'black-rod',
        'USER': 'u_rodda',
        'PASSWORD': 'Cha1d234',
        'HOST': 'localhost',
        'PORT': '',
    }
}

ALLOWED_HOSTS = ['45.33.76.223', 'blackrod.apda.online']

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
    }
}



sentry_sdk.init(
    dsn="https://6c6eb92f6b7248a9a37e4b255eab4962@sentry.io/1811674",
    integrations=[DjangoIntegration()]
)
