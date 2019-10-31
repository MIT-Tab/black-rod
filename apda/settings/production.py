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

ALLOWED_HOSTS = ['50.116.54.146']

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.memcached.MemcachedCache',
        'LOCATION': '127.0.0.1:11211',
    }
}
