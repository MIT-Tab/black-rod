"""Verify login continuation, CSRF, and durable session behavior."""
from importlib import import_module
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from allauth.account.signals import user_logged_in
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings


class LoginSessionsTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="login-test", password="test-password")

    def test_password_login_expiry_choice_and_safe_destination(self):
        for remember, browser_only in (("on", False), ("", True)):
            self.client.logout()
            response = self.client.post('/auth/login/', {
                'username': 'login-test', 'password': 'test-password',
                'remember': remember, 'next': '/core/debaters/42',
            })
            self.assertRedirects(response, '/core/debaters/42', fetch_redirect_response=False)
            self.assertEqual(self.client.session.get_expire_at_browser_close(), browser_only)
        response = self.client.post('/auth/login/', {
            'username': 'login-test', 'password': 'test-password', 'next': 'https://evil.example/',
        })
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_oauth_starts_on_first_post_and_preserves_next(self):
        response = self.client.post('/auth/login/', {
            'provider': 'apdaonline', 'remember': 'on', 'next': '/core/debaters/42',
        })
        self.assertEqual(response.status_code, 302)
        url = urlparse(response.url)
        self.assertEqual(url.netloc, 'apda.online')
        self.assertEqual(url.path, '/oauth/authorize')
        query = parse_qs(url.query)
        self.assertIn('state', query)
        self.assertEqual(query['response_type'], ['code'])
        self.assertEqual(self.client.session['socialaccount_state'][0]['next'], '/core/debaters/42')
        self.assertTrue(self.client.session['apda_remember_login'])

    def test_linked_oauth_callback_finishes_without_second_login(self):
        from allauth.socialaccount.models import SocialAccount
        SocialAccount.objects.create(user=self.user, provider='apdaonline', uid='42')
        response = self.client.post('/auth/login/', {
            'provider': 'apdaonline', 'remember': 'on', 'next': '/core/debaters/42',
        })
        state = parse_qs(urlparse(response.url).query)['state'][0]
        profile = Mock()
        profile.json.return_value = {
            'ID': 42, 'user_nicename': self.user.username, 'user_email': '',
            'display_name': 'Example Member', 'user_roles': [],
        }
        with patch('allauth.socialaccount.providers.oauth2.client.OAuth2Client.get_access_token',
                   return_value={'access_token': 'test-only-token', 'token_type': 'Bearer'}), \
                patch('apdaonline.views.requests.get', return_value=profile):
            result = self.client.get('/accounts/apdaonline/login/callback/', {
                'code': 'test-only-code', 'state': state,
            })
        self.assertRedirects(result, '/core/debaters/42', fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))
        self.assertFalse(self.client.session.get_expire_at_browser_close())

    def test_oauth_login_signal_applies_browser_only_choice(self):
        request = type('Request', (), {'session': self.client.session})()
        request.session['apda_remember_login'] = False
        user_logged_in.send(sender=type(self.user), request=request, user=self.user)
        self.assertTrue(request.session.get_expire_at_browser_close())
        self.assertNotIn('apda_remember_login', request.session)

    def test_login_post_requires_csrf_and_get_does_not_start_oauth(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post('/auth/login/', {'provider': 'apdaonline'}).status_code, 403)
        response = client.get('/auth/login/?next=/core/debaters/42')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('socialaccount_state', client.session)
        self.assertContains(response, 'Keep me signed in for 30 days')

    @override_settings(SESSION_ENGINE='django.contrib.sessions.backends.db')
    def test_session_survives_cache_clear_and_new_store(self):
        self.client.force_login(self.user)
        original = self.client.session
        cache.clear()
        store = import_module(settings.SESSION_ENGINE).SessionStore(session_key=original.session_key)
        self.assertEqual(store['_auth_user_id'], str(self.user.pk))
        fresh_client = Client()
        fresh_client.cookies = self.client.cookies.copy()
        response = fresh_client.get('/auth/login/')
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_persistent_session_policy(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 30 * 24 * 60 * 60)
        self.assertFalse(settings.SESSION_SAVE_EVERY_REQUEST)
        self.assertFalse(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)

    def test_worker_cwd_does_not_change_default_environment_file(self):
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tempfile

        root = Path(__file__).resolve().parents[2]
        # Intercept dotenv loading: exercise path selection without reading any .env.
        script = '''
from pathlib import Path
import sys
from unittest.mock import patch
with patch("dotenv.load_dotenv") as load:
    import apda.settings.base as config
    assert Path(load.call_args.args[0]) == Path(sys.argv[1]) / ".env"
    assert config.SECRET_KEY == "test-only-stable-signing-key"
'''
        environment = {**os.environ, 'ENV': 'development', 'DEBUG': '0',
                       'SECRET_KEY': 'test-only-stable-signing-key', 'PYTHONPATH': str(root)}
        environment.pop('DOT_ENV_FILE', None)
        # The child runs outside the repository; retain branch-coverage configuration.
        if environment.get('COV_CORE_DATAFILE'):
            environment['COV_CORE_CONFIG'] = str(root / 'pyproject.toml')
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c', script, str(root)],
                                    cwd=directory, env=environment, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
