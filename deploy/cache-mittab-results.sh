#!/usr/bin/env bash
set -euo pipefail

export ENV=production
cd "$(dirname "$0")/.."

# A root-managed selector survives updates to the repository and its cron file.
selector=/etc/default/black-rod-mittab-runtime
runtime=django
if [[ -e "$selector" ]]; then
    runtime=$(<"$selector")
fi

case "$runtime" in
    django) exec venv/bin/python manage.py cache_mittab_results ;;
    rust)
        set -a
        # The production service and its maintenance job share the same season.
        source /etc/default/standings-rust
        set +a
        export DOT_ENV_FILE="$PWD/.env"
        export BLACK_ROD_RELEASE_ROOT=/var/lib/standings-rust/current
        exec "$BLACK_ROD_RELEASE_ROOT/black-rod" cache_mittab_results
        ;;
    *) echo "Invalid Standings snapshot runtime in $selector" >&2; exit 1 ;;
esac
