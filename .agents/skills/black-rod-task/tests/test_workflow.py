"""Offline checks for watcher state transitions and checkout isolation."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
loader = importlib.machinery.SourceFileLoader('ci_watch', str(SCRIPTS / 'ci-watch'))
spec = importlib.util.spec_from_loader(loader.name, loader)
watch = importlib.util.module_from_spec(spec)
loader.exec_module(watch)


def snapshot(sha='abc', status='COMPLETED', conclusion='SUCCESS'):
    return {'headRefOid': sha, 'statusCheckRollup': [
        {'__typename': 'CheckRun', 'name': 'Tests', 'status': status,
         'conclusion': conclusion}]}


class WatchTests(unittest.TestCase):
    def wait(self, snapshots):
        with patch.object(watch, 'gh_json', side_effect=snapshots), \
             patch.object(watch.time, 'sleep'):
            return watch.wait_for_checks(1, 60, 600)

    def test_pending_and_empty_checks_do_not_complete(self):
        final = snapshot()
        sha, checks = self.wait([
            {'headRefOid': 'abc', 'statusCheckRollup': []},
            snapshot(status='IN_PROGRESS', conclusion=None), final, final])
        self.assertEqual(sha, 'abc')
        self.assertTrue(watch.ok(checks[0]))

    def test_new_head_resets_stability(self):
        sha, _ = self.wait([snapshot('old'), snapshot('new'), snapshot('new')])
        self.assertEqual(sha, 'new')

    def test_rerun_conclusion_resets_stability(self):
        _, checks = self.wait([snapshot(conclusion='FAILURE'), snapshot(), snapshot()])
        self.assertTrue(watch.ok(checks[0]))

    def test_transient_error_retries(self):
        self.assertEqual(self.wait([RuntimeError('network'), snapshot(), snapshot()])[0], 'abc')

    def test_timeout(self):
        with patch.object(watch, 'gh_json', return_value=snapshot(status='QUEUED')), \
             patch.object(watch.time, 'monotonic', side_effect=[0, 601]):
            with self.assertRaises(TimeoutError):
                watch.wait_for_checks(1, 60, 600)

    def test_legacy_status_checks(self):
        self.assertTrue(watch.done({'state': 'ERROR'}))
        self.assertFalse(watch.ok({'state': 'ERROR'}))
        self.assertFalse(watch.done({'state': 'PENDING'}))
        self.assertTrue(watch.ok({'state': 'SUCCESS'}))
        self.assertFalse(watch.ok(snapshot(conclusion='CANCELLED')['statusCheckRollup'][0]))

    def test_failure_includes_actions_tail_once_per_run(self):
        checks = [dict(snapshot(conclusion='FAILURE')['statusCheckRollup'][0],
                       name=name, detailsUrl=f'https://github.com/{watch.REPO}/actions/runs/123/job/{i}')
                  for i, name in enumerate(['Lint', 'Tests'])]
        with patch.object(watch, 'actions_log_tail', return_value='assertion failed') as logs:
            passed, summary = watch.report(12, 'abcdefghi', checks)
        self.assertFalse(passed)
        self.assertIn('Lint: FAILURE', summary)
        self.assertIn('Tests: FAILURE', summary)
        self.assertIn('assertion failed', summary)
        logs.assert_called_once()

    def test_external_log_url_is_not_fetched(self):
        with patch.object(watch.subprocess, 'run') as run:
            self.assertIsNone(watch.actions_log_tail('https://github.com/other/repo/actions/runs/1'))
        run.assert_not_called()

    def test_background_writes_result_and_queues_explicit_thread(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(watch, 'STATE', Path(directory)), \
             patch.object(watch, 'wait_for_checks', return_value=('abc', snapshot()['statusCheckRollup'])), \
             patch.object(watch, 'wake') as wake:
            args = type('Args', (), dict(pr=12, interval=60, timeout=600,
                                        thread='explicit-thread', only_pass=False))()
            watch.background(args)
            self.assertIn('CI PASSED', (Path(directory) / 'ci-watch-12.result').read_text())
            self.assertEqual(wake.call_args.args[0], 'explicit-thread')


class EnvironmentTests(unittest.TestCase):
    def test_private_state_is_distinct_and_forces_local_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            states = []
            for name in ('one', 'two'):
                checkout = Path(directory) / name
                checkout.mkdir()
                (checkout / 'manage.py').touch()
                (checkout / 'apda').mkdir()
                subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
                # Production values must not select an existing database.
                (checkout / '.env').write_text('ENV=production\n')
                result = subprocess.run(
                    ['bash', '-c', '. "$1/_env.sh"; python3 -c '\
                     '\'import os,json; print(json.dumps([os.environ[k] for k in '\
                     ' ["BR_STATE", "ENV", "DJANGO_SETTINGS_MODULE", "BR_PYTHON"]]))\'',
                     'bash', str(SCRIPTS)], cwd=checkout,
                    env={**os.environ, 'ENV': 'production', 'TMPDIR': directory},
                    capture_output=True, text=True, check=True)
                state, env, settings, python = json.loads(result.stdout)
                states.append(state)
                self.assertEqual(env, 'development')
                self.assertEqual(settings, 'agent_settings')
                self.assertEqual(python, str(checkout / 'venv/bin/python'))
            self.assertNotEqual(*states)


class StartTaskTests(unittest.TestCase):
    def test_creates_worktree_and_preserves_dirty_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            origin, checkout = base / 'origin.git', base / 'black-rod'
            subprocess.run(['git', 'init', '--bare', '-q', str(origin)], check=True)
            subprocess.run(['git', 'init', '-q', str(checkout)], check=True)

            def git(*args):
                return subprocess.run(['git', '-C', str(checkout), *args],
                                      check=True, capture_output=True, text=True)

            git('symbolic-ref', 'HEAD', 'refs/heads/master')
            (checkout / 'tracked.txt').write_text('committed')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                'commit', '-qm', 'initial')
            git('remote', 'add', 'origin', str(origin))
            git('push', '-q', 'origin', 'master')
            (checkout / 'tracked.txt').write_text('uncommitted')
            (checkout / '.env').write_text('ENV=development\n')
            mockbin = base / 'mockbin'
            mockbin.mkdir()
            # Stand in for installers; exercise real git worktree operations offline.
            installer = mockbin / 'python-bootstrap'
            installer.write_text(
                '#!/usr/bin/env python3\n'
                'from pathlib import Path\nimport shutil,sys\n'
                'if sys.argv[1:3] == ["-m", "venv"]:\n'
                '    target = Path(sys.argv[3]) / "bin/python"\n'
                '    target.parent.mkdir(parents=True, exist_ok=True)\n'
                '    shutil.copyfile(__file__, target)\n'
                '    target.chmod(0o755)\n')
            installer.chmod(0o755)
            npm = mockbin / 'npm'
            npm.write_text('#!/usr/bin/env bash\nexit 0\n')
            npm.chmod(0o755)
            env = {**os.environ, 'PATH': str(mockbin) + os.pathsep + os.environ['PATH'],
                   'BR_BOOTSTRAP_PYTHON': str(installer), 'TMPDIR': directory}
            result = subprocess.run([str(SCRIPTS / 'start-task'), 'example'],
                                    cwd=checkout, env=env, capture_output=True,
                                    text=True, check=True)
            worktree = base / 'black-rod-example'
            self.assertIn(str(worktree), result.stdout)
            self.assertEqual((checkout / 'tracked.txt').read_text(), 'uncommitted')
            self.assertEqual((worktree / 'tracked.txt').read_text(), 'committed')
            self.assertEqual((worktree / '.env').resolve(), checkout / '.env')
            self.assertTrue((worktree / 'venv/screenshots/bin/python').exists())
            branch = subprocess.check_output(['git', '-C', str(worktree),
                                              'branch', '--show-current'], text=True)
            self.assertEqual(branch.strip(), 'codex/example')
            again = subprocess.run([str(SCRIPTS / 'start-task'), 'example'],
                                   cwd=checkout, env=env, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn('already exists', again.stderr)


if __name__ == '__main__':
    unittest.main()
