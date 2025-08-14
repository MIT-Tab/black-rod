"""
WSGI config for apda project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/1.11/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apda.settings.base")

application = get_wsgi_application()
