"""Exercise the deployment's credential writer without real credentials."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml


WORKFLOW = Path(__file__).resolve().parents[2] / '.github/workflows/main.yml'


def credential_writer():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    script = workflow['jobs']['deploy']['steps'][1]['with']['script']
    return script.split("<<'PYTHON'\n", 1)[1].split('\nPYTHON', 1)[0]


def test_updates_only_integration_key_preserving_metadata():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / '.env'
        path.write_text('UNCHANGED="keep me"\nFORUM_PROFILE_API_TOKEN=old\n')
        path.chmod(0o640)
        before = path.stat()
        env = {**os.environ, 'DOT_ENV_FILE': str(path), 'FORUM_PROFILE_API_TOKEN': 'a' * 64}
        result = subprocess.run([sys.executable, '-c', credential_writer()], env=env,
                                capture_output=True, check=True)
        assert result.stdout == b'' and result.stderr == b''
        assert path.read_text() == 'UNCHANGED="keep me"\nFORUM_PROFILE_API_TOKEN=' + "'" + 'a' * 64 + "'\n"
        after = path.stat()
        assert (before.st_mode, before.st_uid, before.st_gid) == (after.st_mode, after.st_uid, after.st_gid)


def test_missing_or_invalid_secret_never_overwrites_config():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / '.env'
        original = 'UNCHANGED=keep\n'
        path.write_text(original)
        for token, expected in (('', 0), ('bad-token', 1)):
            result = subprocess.run([sys.executable, '-c', credential_writer()],
                                    env={**os.environ, 'DOT_ENV_FILE': str(path),
                                         'FORUM_PROFILE_API_TOKEN': token}, capture_output=True)
            assert result.returncode == expected
            assert token.encode() not in result.stdout if token else result.stdout == b''
            assert path.read_text() == original
