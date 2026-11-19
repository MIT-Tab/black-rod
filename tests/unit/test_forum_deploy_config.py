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


def test_remote_deployment_script_handles_heredoc_and_stops_on_failure():
    """Run the shell consumed by ssh-action, including its script_stop expansion."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    config = workflow['jobs']['deploy']['steps'][1]['with']
    for token, expected in (('a' * 64, 0), ('invalid', 1)):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'venv/bin').mkdir(parents=True)
            (root / 'venv/bin/python').symlink_to(sys.executable)
            (root / '.env').write_text('UNCHANGED=keep\n')
            deploy = root / 'deploy.sh'
            deploy.write_text('#!/bin/sh\ntouch deployed\n')
            deploy.chmod(0o700)
            (root / 'install').write_text('#!/bin/sh\nexit 0\n')
            (root / 'install').chmod(0o700)
            script = config['script'].replace('/home/rodda/black-rod', directory)
            if config.get('script_stop'):
                # drone-ssh checks each line, even lines inside a heredoc.
                check = 'DRONE_SSH_PREV_COMMAND_EXIT_CODE=$?; if [ $DRONE_SSH_PREV_COMMAND_EXIT_CODE -ne 0 ]; then exit $DRONE_SSH_PREV_COMMAND_EXIT_CODE; fi;'
                script = '\n'.join(line + '\n' + check for line in script.splitlines())
            result = subprocess.run(['bash', '-c', script], capture_output=True,
                                    env={**os.environ, 'FORUM_PROFILE_API_TOKEN': token,
                                         'DOT_ENV_FILE': str(root / '.env'),
                                         'PATH': directory + os.pathsep + os.environ['PATH']})
            assert result.returncode == expected, result.stderr.decode()
            assert (root / 'deployed').exists() == (expected == 0)
            assert result.stdout == b''
            assert token.encode() not in result.stderr
