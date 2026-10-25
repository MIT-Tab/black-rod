# Shared checkout environment; source from the Bash helpers.
BR_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BR_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "black-rod-task: run inside a black-rod checkout" >&2; exit 2;
}
[ -f "$BR_ROOT/manage.py" ] && [ -d "$BR_ROOT/apda" ] || {
  echo "black-rod-task: expected a black-rod checkout" >&2; exit 2;
}
BR_ID="$(printf %s "$BR_ROOT" | cksum | cut -d' ' -f1)"
BR_SLUG="$(basename "$BR_ROOT")"
export BR_STATE="${TMPDIR:-/tmp}/black-rod-agent/${BR_SLUG}-${BR_ID}"
export BR_PORT="${BR_PORT:-$((9100 + BR_ID % 800))}"
export BR_PYTHON="$BR_ROOT/venv/bin/python"
# These helpers always use private local SQLite, even with a production .env.
export ENV=development DEBUG=1 DJANGO_SETTINGS_MODULE=agent_settings
export PYTHONPATH="$BR_SCRIPTS:$BR_ROOT${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$BR_STATE"
br_gh_auth() {
  if [ -z "${GH_TOKEN:-}" ] && ! gh auth status >/dev/null 2>&1; then
    GH_TOKEN="$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null |
      sed -n 's/^password=//p')"
    export GH_TOKEN
  fi
}
