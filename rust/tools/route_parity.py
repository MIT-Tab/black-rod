"""Check that every Django application URL has a corresponding Rust route."""
from pathlib import Path
import re

import django
from django.urls import get_resolver, URLPattern


ROUTER = Path(__file__).resolve().parents[1] / "src/lib.rs"
PARAMETER = re.compile(r"<[^<>]+>")
RUST_PARAMETER = re.compile(r"\{[^{}]+}")
REGEX_ROUTES = {
    r"accounts/^confirm-email/(?P<key>[-:\w]+)/$": "/accounts/confirm-email/{}/",
    r"accounts/^password/reset/key/(?P<uidb36>[0-9A-Za-z]+)-(?P<key>.+)/$": "/accounts/password/reset/key/{}/",
}


def django_paths(patterns, prefix=""):
    for pattern in patterns:
        path = prefix + str(pattern.pattern)
        if path.startswith("admin/"):
            continue
        if isinstance(pattern, URLPattern):
            yield REGEX_ROUTES.get(path, PARAMETER.sub("{}", "/" + path))
        else:
            yield from django_paths(pattern.url_patterns, path)


def main():
    django.setup()
    expected = set(django_paths(get_resolver().url_patterns))
    registered = {RUST_PARAMETER.sub("{}", path)
                  for path in re.findall(r'\.route\(\s*"(/[^"\n]*)"', ROUTER.read_text(encoding="utf-8"))}
    missing = expected - registered
    assert not missing, "Django routes without Rust equivalents:\n" + "\n".join(sorted(missing))
    print(f"Found Rust registrations for all {len(expected)} Django routes outside model admin.")


if __name__ == "__main__":
    main()
