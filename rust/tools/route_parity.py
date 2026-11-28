"""Check that every Django application URL has a corresponding Rust route."""
from argparse import ArgumentParser
from pathlib import Path
import re

import django
from django.urls import get_resolver, URLPattern


RUST = Path(__file__).resolve().parents[1]
ROUTER = RUST / "src/routing.rs"
SLASH_ROUTES = RUST / "schema/slash_routes.txt"
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


def slash_routes(patterns, prefix=""):
    for route in patterns:
        pattern = route.pattern.regex.pattern
        combined = prefix + pattern.removeprefix("^")
        if combined.startswith("admin/"):
            continue
        if isinstance(route, URLPattern):
            if combined.endswith((r"/\Z", "/$")) and getattr(route.callback, "should_append_slash", True):
                yield "^/" + combined.replace(r"\Z", r"\z")
        else:
            yield from slash_routes(route.url_patterns, combined)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--write-slash-routes", action="store_true")
    args = parser.parse_args()
    django.setup()
    patterns = get_resolver().url_patterns
    slash_schema = "\n".join(sorted(set(slash_routes(patterns)))) + "\n"
    if args.write_slash_routes:
        SLASH_ROUTES.write_text(slash_schema, encoding="utf-8")
    else:
        assert SLASH_ROUTES.read_text(encoding="utf-8") == slash_schema, "Regenerate the APPEND_SLASH route schema."
    expected = set(django_paths(patterns))
    registered = {RUST_PARAMETER.sub("{}", path)
                  for path in re.findall(r'\.route\(\s*"(/[^"\n]*)"', ROUTER.read_text(encoding="utf-8"))}
    missing = expected - registered
    assert not missing, "Django routes without Rust equivalents:\n" + "\n".join(sorted(missing))
    print(f"Found Rust registrations for all {len(expected)} Django routes outside model admin.")


if __name__ == "__main__":
    main()
