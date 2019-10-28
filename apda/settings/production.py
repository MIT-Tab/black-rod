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
