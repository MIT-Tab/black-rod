# APDA login reliability and unified sign-in

## Confirmed findings

The live WordPress installation uses WP OAuth Server (`oauth2-provider`), LoginWP
(`peters-login-redirect`), and a hidden login URL. LoginWP's requested-return-URL
override is disabled. Its login_redirect callback runs at priority 999999999 and
can replace the OAuth authorization continuation with its homepage rule. That
matches the reported first-login failure: WordPress authentication succeeds but
the authorization code never returns to results. Retrying from results works
because the WordPress cookie now exists.

The forum integration restores only a successful user's same-origin
`/oauth/authorize` continuation for authorization-code requests with client ID,
state, and the exact results callback. LoginWP still controls other redirects;
failed logins and arbitrary callback URLs do not qualify. The provider continues
to validate the client, callback, authorization code, and OAuth state.

### Results callback failures during deployment

On November 20, 2026, WordPress returned users to the Results callback while a
Results deploy was reinstalling packages in the live virtualenv. Requests
failed before contacting the WordPress token endpoint because certifi's CA
bundle was missing. Django's error reporting then tried to read its own missing
error template. The [deployment log](https://github.com/MIT-Tab/black-rod/actions/runs/44760243569)
records both package removals before the callbacks and the completed installation
and worker restart afterward.

The Results deploy workflow serializes deployments and stops Supervisor's
`black-rod` process before invoking the host's `deploy.sh`. That script performs
the install and starts the service again. Results is unavailable during this
maintenance window. Afterward the workflow checks the CA bundle, Django's error template,
and Supervisor status before reporting success. If the deploy fails after the
stop, inspect the failed step and repair the release and dependencies before
starting `black-rod` again. Retrying the deploy can start from a stopped process.

Results' login button previously opened allauth's GET confirmation page. The new
login form submits directly through a CSRF-protected POST, eliminating that extra
Continue step and retaining the originally requested page. Existing linked
accounts complete the normal authorization-code callback without relinking.

## Session lifetime and deployment resilience

Results uses database-backed sessions, a 30-day expiration for remembered
logins, host-scoped HttpOnly cookies, SameSite=Lax, and Secure cookies in production.
Both password and OAuth logins honor the form's browser-only opt-out. WordPress
Remember Me sessions last 30 days; unchecked WordPress sessions retain their
normal browser-only behavior. These policies apply when a new login is issued;
existing browser-only sessions are not silently converted.

The default environment file is now resolved from the repository root, not the
worker's current directory. This prevents a different deployment/service working
directory from selecting a different signing-key configuration. An explicit
DOT_ENV_FILE override is retained. No signing keys or WordPress salts are written or rotated. If a worker was previously reading a different environment file, verify that its effective signing key matches the canonical file before rollout; a difference can require a one-time login.
Database sessions are tested across a new session-store instance and cache clear.

**Not yet established:** the exact cause of previously reported redeploy logouts.
Source inspection shows database sessions already were Django's default, so a
cache restart alone does not explain them. WordPress plugin deployment replaces
only plugin files and preserves production wp-config.php and account/session data.
The results server's untracked deploy.sh, service environment, signing-key
continuity, and browser cookies still need a production audit before attributing
all redeploy logouts to one cause. Never print configuration, signing keys,
authentication cookies, session IDs, or OAuth credentials during that audit.

The audit should verify a stable SECRET_KEY across workers and releases; a stable
WordPress salt set and site URL; the persistent session database; no deployment
commands that flush active sessions; and consistent canonical HTTPS hosts. Any
comparison of private values must happen entirely on the server, returning only
match/mismatch booleans. Do not rotate keys as an attempted fix: rotation itself
logs users out. Re-test with a dedicated test account through one deployment.

## Progress toward unified sign-in

WordPress is already the identity provider. Signing into results through it sets
the WordPress cookie and then a separate results cookie. With the continuation
fixed, that completes in one trip. An already signed-in WordPress user can click
results' login button and return through OAuth without entering credentials again
(subject to any provider-required consent). The two applications should continue
to use their own cookies and signing keys.

Fully automatic sign-in when opening either site is achievable, but it needs a
second phase. The installed provider's implementation of prompt=none sets
is_authorized=false and still sends anonymous users to WordPress login. It cannot
currently serve as a reliable silent sign-in probe.

A safe next phase is a provider-supported, validated, top-level silent OAuth
round trip for already linked accounts: return login_required without showing a
login form when no WordPress session exists; authorize existing sessions without
an extra page; let results attempt once per browser session, preserve next/state,
and stop on errors or explicit logout. Keep public results pages public and
preserve POST/CSRF protection for interactive login. This avoids iframe/third-party
cookie dependence. Account linking must remain an explicit authenticated action;
never merge accounts by matching names or emails.

Also define logout expectations before enabling automatic sign-in: local logout
must not immediately sign the user back in, while a deliberate “sign out of APDA”
action should revoke both app sessions. Results password-only accounts still need
an explicit WordPress link before participating in unified sign-in. Existing
WordPress sessions cannot be synthesized from a results cookie alone.

## References

- [Django session persistence and expiration](https://docs.djangoproject.com/en/4.2/topics/http/sessions/)
- [WordPress authentication cookie behavior](https://developer.wordpress.org/reference/functions/wp_set_auth_cookie/)
