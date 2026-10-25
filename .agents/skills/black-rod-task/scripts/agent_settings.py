"""Local helper settings. Never modify the checkout's existing application DB."""
import os
from pathlib import Path

from apda.settings.base import *  # noqa: F403  pylint: disable=wildcard-import,unused-wildcard-import

_STATE = Path(os.environ["BR_STATE"])
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(_STATE / "dev.sqlite3"),
    }
}
ENV = "development"
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
# Keep search-index writes private as well as database writes.
HAYSTACK_CONNECTIONS = {
    "default": {"ENGINE": "haystack.backends.whoosh_backend.WhooshEngine",
                "PATH": str(_STATE / ("test_whoosh_index" if os.environ.get("BR_TESTING") else "whoosh_index"))}
}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Fixture deserialization emits raw saves; defer indexing until data is loaded.
if os.environ.get("BR_LOADING_FIXTURE") == "1":
    HAYSTACK_SIGNAL_PROCESSOR = "haystack.signals.BaseSignalProcessor"
