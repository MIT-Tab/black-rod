from .base import *

DEBUG = False

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql_psycopg2',
        'NAME': 'standings',
        'USER': 'rodda',
        'PASSWORD': 'Cha1d234',
        'HOST': 'localhost',
        'PORT': '',
    }
}

ALLOWED_HOSTS = ['50.116.54.146', 'results.apda.online']

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.memcached.MemcachedCache',
        'LOCATION': '127.0.0.1:11211',
    }
}

#import sentry_sdk
#from sentry_sdk.integrations.django import DjangoIntegration

#sentry_sdk.init(
#    dsn="https://6c6eb92f6b7248a9a37e4b255eab4962@sentry.io/1811674",
#    integrations=[DjangoIntegration()]
#)
