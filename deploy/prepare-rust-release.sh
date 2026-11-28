#!/usr/bin/env bash
set -euo pipefail
umask 077

revision=${1:-}
checksum=${2:-}
if [[ ! $revision =~ ^[0-9a-f]{40}$ || ! $checksum =~ ^[0-9a-f]{64}$ || $# != 2 ]]; then
    echo 'Usage: standings-prepare-rust REVISION SHA256 < release.tar.gz' >&2
    exit 2
fi

base=/var/lib/standings-rust
install -d -m 0750 -o root -g rodda "$base" "$base/releases"
exec 9>"$base/prepare.lock"
flock 9
incoming=$(mktemp -d "$base/releases/.incoming.XXXXXXXX")
trap 'rm -rf -- "$incoming"' EXIT
cat >"$incoming/release.tar.gz"
printf '%s  %s\n' "$checksum" "$incoming/release.tar.gz" | sha256sum --check --status

target="$base/releases/$revision"
if [[ ! -d "$target" ]]; then
    mkdir "$incoming/payload"
    python3 - "$incoming/release.tar.gz" "$incoming/payload" <<'PY'
from pathlib import PurePosixPath
import sys
import tarfile

def release_member(member, destination):
    path = PurePosixPath(member.name)
    if not (member.isdir() or member.isfile()):
        raise tarfile.FilterError("Expected a release file or directory")
    if path.parts and not (
        path.parts in [("black-rod",), ("webpack-stats.json",), ("revision",)]
        or path.parts[:2] in [("apda", "assets"), ("rust", "static")]
    ):
        raise tarfile.FilterError("Unexpected member in release archive")
    return tarfile.data_filter(member, destination)

with tarfile.open(sys.argv[1], "r:gz") as archive:
    archive.extractall(sys.argv[2], filter=release_member)
PY
    payload="$incoming/payload"
    test "$(cat "$payload/revision")" = "$revision"
    test -x "$payload/black-rod"
    DOT_ENV_FILE=/dev/null "$payload/black-rod" --help >/dev/null
    python3 - "$payload" <<'PY'
import json
from pathlib import Path
import sys

release = Path(sys.argv[1])
manifest = json.loads((release / "webpack-stats.json").read_text())
bundles = manifest.get("chunks", {}).get("main", [])
if manifest.get("status") != "done" or not bundles or any(
    Path(name).name != name or not (release / "apda/assets/webpack_bundles" / name).is_file()
    for name in bundles
):
    raise SystemExit("Missing release webpack bundles")
PY
    chgrp -R rodda "$payload"
    chmod -R g+rX,o-rwx "$payload"
    mv -- "$payload" "$target"
else
    test "$(cat "$target/revision")" = "$revision"
fi

ln -s "releases/$revision" "$incoming/prepared"
mv -Tf -- "$incoming/prepared" "$base/prepared"
printf 'Prepared Standings release %s\n' "$revision"
