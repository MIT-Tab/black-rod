# Dependency audit — 2026-10-21

This update keeps the existing Django 4.2 application, Bootstrap 4 UI, jQuery 3,
and APDA Online authentication integration. It updates 57 Python pins, refreshes
compatible npm dependencies and their lockfile, and updates Node 20.19.4 to the
final Node 20 security release, 20.20.2.

## Audit results

| Scan | Before | After |
| --- | --- | --- |
| `npm audit`, including development dependencies | 62 affected package entries: 17 high, 43 moderate, 2 low | 0 |
| `pip-audit` against all 88 explicit Python pins | 135 findings in 17 packages | 15 findings in 2 packages |
| Distinct Python advisories, deduplicated by CVE where available | 83 | 11 |

`pip-audit` sometimes reports the same CVE through multiple advisory records;
the 135/15 counts are scanner findings, not distinct CVEs. Auditing the entire
fresh Python environment, including transitive dependencies, pip, setuptools,
and wheel, also leaves only the same 15 findings in Django and allauth. No
advisories were suppressed. These are point-in-time results, not a guarantee
against newly disclosed vulnerabilities.

The Python scanner was run separately from the application environment:

```sh
pip-audit -r requirements.txt --no-deps --disable-pip -f json
python -m pip freeze --all > /tmp/black-rod-resolved.txt
pip-audit -r /tmp/black-rod-resolved.txt --no-deps --disable-pip -f json
npm audit --json
```

The explicit pins permit the first scan without installing potentially
vulnerable versions. The second scan covers the complete installed dependency
set. The scanner is an audit tool, not an application requirement.

## Changes and compatibility

- Remove unused `precss`. Webpack uses Sass and Autoprefixer directly; nothing
  imports or configures precss. Removing it eliminates its obsolete PostCSS tree.
- Refresh compatible frontend/tooling versions, including Babel, Webpack,
  PostCSS, Sass, Papa Parse, jQuery UI, datepicker, and Select2. Keep Bootstrap,
  jQuery, Popper, ESLint, and Webpack loader major versions.
- Patch Django 4.2.24 to 4.2.30. Upgrade REST Framework to 3.17.2, which contains
  the relevant security fixes and still supports Django 4.2. REST Framework 3.18
  and formtools 2.7 require Django 5.2, so they are deferred.
- Update HTTP, OAuth/JWT, cryptography, certificates, XML/spreadsheet handling,
  SQL parsing, search, observability, and compatible utility dependencies.
  Haystack 3.4 removes the vulnerable Elasticsearch deserialization path and its
  dependency on `pkg_resources`.
- Include targeted security upgrades across release lines: cryptography 50
  with CFFI 2, urllib3 2, OAuthlib 4, Black 26 with pathspec 1, and pytest 9.
  These require no application API changes in this repository. The date-based
  packages certifi, pytz, regex, and diff-match-patch also receive updates.
- Replace the old `pip<25` / `setuptools<60` constraints with tested, patched
  tooling release lines. Update setup instructions to use the constraints.
- Use `.nvmrc` in CI and `npm ci` in CI/setup so the reviewed lockfile is used.

The [urllib3 migration guide](https://urllib3.readthedocs.io/en/stable/v2-migration-guide.html)
documents its Python 3.10+, OpenSSL 1.1.1+, TLS 1.2 minimum, and stricter certificate
hostname handling. The tested Python 3.10 environment meets these requirements;
deployment environments must meet them too. See also the
[cryptography changelog](https://cryptography.io/en/latest/changelog/) and
[Node 20.20.2 security release](https://nodejs.org/en/blog/release/v20.20.2).

## Remaining work requiring migration

Django 4.2 is out of security support. Its final patch fixes the backported
issues but cannot clear advisories affecting unsupported releases. Plan a
Django 5.2 LTS upgrade with the associated Django plugin upgrades. See the
[4.2.30 release notes](https://docs.djangoproject.com/en/4.2/releases/4.2.30/).

| Remaining Django advisory | Area |
| --- | --- |
| [CVE-2026-15830](https://github.com/advisories/GHSA-q238-5cxm-5c9h) | GeoDjango geometry parsing |
| [CVE-2026-48587](https://github.com/advisories/GHSA-923m-gv2p-w5qp) | Whitespace in Vary headers |
| [CVE-2026-6873](https://github.com/advisories/GHSA-h7pc-vwp9-298g) | Signed-cookie salt ambiguity |
| [CVE-2026-8404](https://github.com/advisories/GHSA-8cjm-8mp7-r2xf) | Case-sensitive Cache-Control handling |
| [CVE-2026-48588](https://github.com/advisories/GHSA-3h9f-r86x-qvjx) | Cookie-dependent response caching |
| [CVE-2026-53877](https://github.com/advisories/GHSA-crhf-3pfg-w68w) | GeoDjango raster buffer handling |
| [CVE-2026-53878](https://github.com/advisories/GHSA-8qcx-xf44-272x) | Domain name validation |
| [CVE-2026-15307](https://github.com/advisories/GHSA-wvqv-fj8w-qmhm) | GeoDjango spatial lookup file/network access |

These advisories flag unsupported Django versions whose exposure was not fully
evaluated upstream. This project does not configure GeoDjango or Django's cache
middleware, and no direct use of `cache_page`, `get_signed_cookie`, or
`DomainNameValidator` was found. That limits observed exposure; it does not
establish that the deployment is unaffected.

Allauth remains at 0.51.0. Moving to 65.x changes provider APIs, middleware,
configuration, and database migrations around this project's custom OAuth
provider. That deserves separate authentication testing. See the
[allauth incompatible changes](https://docs.allauth.org/en/latest/release-notes/2023.html).

| Remaining allauth advisory | Area |
| --- | --- |
| [CVE-2025-65431](https://github.com/advisories/GHSA-8m3c-c723-h4p4) | Mutable Okta/NetIQ identifiers |
| [CVE-2025-65430](https://github.com/advisories/GHSA-qhmc-3mvr-f2j4) | Identity-provider tokens for inactive accounts |
| [CVE-2026-27982](https://github.com/advisories/GHSA-2jpr-83rg-v67j) | SAML IdP-initiated SSO redirects |

The configured custom APDA Online OAuth client does not enable these providers,
IdP token issuance, or SAML. The findings remain visible until allauth is migrated.

Other deferred updates include Bootstrap 5/jQuery 4/Popper 2, ESLint's new
configuration system, Webpack loader major versions, crispy-forms 2 (including
its separate Bootstrap template package), django-filter, taggit, tables2 3,
import-export 4, and other unrelated major release lines. Keep xlrd 1's existing
file-format support. Pylint 3.3 introduces a positional-argument rule and newer
djlint introduces 44 template findings; keep their current versions and astroid
to avoid changing lint policy or rewriting unrelated application code.

Node 20 is end-of-life even at its final patch. A supported Node LTS runtime
upgrade remains a follow-up; the runtime major is preserved here.

## Validation

- Fresh Python 3.10.16 environment installed from `requirements.txt` and
  `constraints.txt`; `pip check` passes.
- Node 20.20.2: `npm ci`, `npm run build`, `npm run lint`, and `npm audit` pass.
- Full configured unit/integration suite: **736 passed, 75.95% coverage**
  (required minimum: 70%). The 23 API tests also pass independently.
- Pylint: 10.00/10. Template lint: 100 files, zero errors.
- Django system checks pass.
- Spreadsheet smoke checks round-trip CSV, TSV, JSON, YAML, XLS, XLSX, and ODS
  through tablib and confirm all import-export admin formats remain available.
  HTML export escapes script tags. RSA JWT signing/verification and OAuth2
  authorization URL generation pass with the updated crypto/auth libraries.
- `makemigrations --check --dry-run` reports existing model/migration drift for
  `core.TournamentImport.import_type` and Summernote's attachment primary key.
  Summernote itself is unchanged; the pre-existing local environment contained
  an extra generated migration in site-packages. No migrations are generated or
  committed by this PR.

The local Whoosh index produced lock contention during the initial test runs,
including with the previous Haystack version. Tests pass with an isolated index
using the same Whoosh backend. To reproduce without sharing a developer index:

```sh
test_settings_dir=$(mktemp -d)
cat > "$test_settings_dir/dependency_audit_settings.py" <<'PY'
from apda.settings.base import *
import tempfile
HAYSTACK_CONNECTIONS = {
    "default": {
        "ENGINE": "haystack.backends.whoosh_backend.WhooshEngine",
        "PATH": tempfile.mkdtemp(prefix="black-rod-test-whoosh-"),
    }
}
PY
PYTHONPATH="$test_settings_dir:$PWD" \
  DJANGO_SETTINGS_MODULE=dependency_audit_settings \
  ENV=testing DEBUG=1 DOT_ENV_FILE=/dev/null pytest
```
