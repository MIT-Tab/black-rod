# pylint: disable=import-outside-toplevel
import django
from django.conf import settings


def pytest_configure():
    """Configure Django for pytest"""
    if not settings.configured:
        settings.configure(
            DEBUG_PROPAGATE_EXCEPTIONS=True,
            DATABASES={
                "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
            },
            SITE_ID=1,
            SECRET_KEY="test-secret-key-for-pytest-only",
            USE_I18N=True,
            USE_L10N=True,
            STATIC_URL="/static/",
            ROOT_URLCONF="apda.urls",
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [],
                "APP_DIRS": True,
                "OPTIONS": {
                    "context_processors": [
                        "django.template.context_processors.debug",
                        "django.template.context_processors.request",
                        "django.contrib.auth.context_processors.auth",
                        "django.contrib.messages.context_processors.messages",
                    ],
                },
            },
        ],
        MIDDLEWARE=[
            "django.middleware.security.SecurityMiddleware",
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
        ],
        INSTALLED_APPS=[
            "dal",
            "dal_select2",
            "django.contrib.admin",
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "django.contrib.messages",
            "django.contrib.staticfiles",
            "django.contrib.humanize",
            "django.contrib.sites",
            "webpack_loader",
            "menu_generator",
            "crispy_forms",
            "import_export",
            "django_tables2",
            "formtools",
            "haystack",
            "taggit",
            "django_summernote",
            "core.apps.CoreConfig",
            "django_extensions",
            "allauth",
            "allauth.account",
            "allauth.socialaccount",
            "apdaonline",
        ],
        PASSWORD_HASHERS=("django.contrib.auth.hashers.MD5PasswordHasher",),
        # Add authentication backends for allauth
        AUTHENTICATION_BACKENDS=[
            "django.contrib.auth.backends.ModelBackend",
            "allauth.account.auth_backends.AuthenticationBackend",
        ],
        # Add allauth settings for testing
        ACCOUNT_EMAIL_VERIFICATION="none",
        ACCOUNT_EMAIL_REQUIRED=False,
        ACCOUNT_AUTHENTICATION_METHOD="username",
        ACCOUNT_USER_MODEL_USERNAME_FIELD="username",
        SOCIALACCOUNT_AUTO_SIGNUP=True,
        SOCIALACCOUNT_EMAIL_VERIFICATION="none",
        LOGIN_REDIRECT_URL="/",
        LOGOUT_REDIRECT_URL="/",
        # Add SEASONS setting needed by forms
        SEASONS=tuple(
            (str(year), f"{year}-{str(year+1)[2:]}")
            for year in range(2025, 2003, -1)  # LATEST to OLDEST-1
        ),
        CURRENT_SEASON="2024",
        ENV="test",
        HAYSTACK_CONNECTIONS={
            "default": {
                "ENGINE": "haystack.backends.simple_backend.SimpleEngine",
            },
        },
        # Disable migrations for faster testing
        MIGRATION_MODULES={
            "core": None,
            "auth": None,
            "contenttypes": None,
            "sessions": None,
            "taggit": None,
        },
    )

    django.setup()


# Clean up - remove custom fixtures that cause scope issues
# pytest-django will handle database setup automatically
