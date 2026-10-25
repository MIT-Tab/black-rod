---
name: black-rod-task
description: Implement black-rod features, fixes, and maintenance using isolated worktrees, SQLite, tests, screenshots, PRs, and detached GitHub Actions notifications.
---

# Black-rod task workflow

Use this repo copy for implementation work. Codex discovers `.agents/skills`;
Claude Code uses the relative symlink in `.claude/skills`. Follow the user's scope;
push branches and create PRs when requested or already authorized.

Resolve helpers inside the current checkout:

```bash
S="$(git rev-parse --show-toplevel)/.agents/skills/black-rod-task/scripts"
```

## Start

Run `"$S/start-task" <kebab-case-slug>` to fetch `origin/master`, create a sibling
worktree on `codex/<slug>`, link an optional `.env`, and install Python and npm
dependencies. Keep the starting checkout's uncommitted work intact. If already in
a dedicated task worktree, continue there. Change into the printed directory and
resolve `S` again. Prerequisites: Bash, Git, Python 3.10 (see `.python-version`),
Node 20 (see `.nvmrc`), npm, and authenticated `gh` for PR/CI operations.
`BR_BOOTSTRAP_PYTHON` can select another path to Python 3.10.

Python uses `venv/bin/python`, `requirements.txt`, and `constraints.txt`, matching
this repo's CI. Screenshot dependencies live in `venv/screenshots`, separate from application
dependencies. For an existing worktree run `venv/bin/python -m venv venv/screenshots`
and `venv/screenshots/bin/python -m pip install -r .agents/skills/black-rod-task/requirements.txt`.
No global skill install or mit-tab checkout is needed. On Windows use WSL.

## Test

- `"$S/br-test" [pytest args]` runs pytest with logged output. With no arguments,
  it runs CI's unit/integration suite and 70% coverage requirement. Narrow while
  iterating, e.g. `"$S/br-test" tests/unit/test_access.py --no-cov -x`.
- `"$S/br" <command...>` provides the private development settings, e.g.
  `"$S/br" venv/bin/python manage.py makemigrations --check --dry-run`.
- Before handoff run CI's checks: `venv/bin/pylint apda apdaonline core api`,
  `venv/bin/djlint core/templates`, `npm run lint`, `npm run build`, and
  `"$S/br-test"`. Report existing failures precisely rather than changing unrelated code.
- For workflow-helper changes also run
  `venv/bin/python -m unittest discover -s .agents/skills/black-rod-task/tests -v`.

The helpers select `agent_settings`, force development mode, and isolate dev SQLite,
search indexes, ports, and state by checkout path under
`${TMPDIR:-/tmp}/black-rod-agent`. Tests use Django's process-private in-memory
SQLite database, matching CI; disk-backed test databases break tests that close
connections during live refresh. Development and tests use separate search indexes.
The helpers leave the application's existing SQLite
files alone, including `apda/db.sqlite3`. Never use production credentials/data
for this workflow. Plain commands without `br` retain the application's normal settings.

## Dev server and screenshots

For visible UI changes, `"$S/dev-up"` migrates private SQLite, loads the tracked
`dev_fixtures`, sets the local **tab / password** superuser, and starts webpack and
Django bound to `127.0.0.1`. It prints the URL and log paths. `--empty` skips the
fixture on first initialization; `--fresh` rebuilds only this checkout's private
dev DB. Seed additional feature data through `br ... manage.py shell`.
Fixture loading defers search indexing; for search UI work run
`"$S/br" venv/bin/python manage.py rebuild_index --noinput` after setup.
Stop background servers with `"$S/dev-down"`. Skip screenshots for changes
without visible UI, except when verifying the screenshot helper itself.

```bash
"$S/shot" --full / docs/img/<slug>/desktop.png
"$S/shot" --mobile --login / docs/img/<slug>/mobile.png
```

Supports `--wait CSS`, repeatable `--click CSS`, `--element CSS`, and `--js JS`.
Login uses `/accounts/login/` and allauth's `login` field. Selenium Manager finds
or downloads Chrome and its matching driver on first use (network required).
Install Chrome's system libraries if needed; `BR_CHROME_LIBS` optionally points
to an existing local library directory. View the printed small JPEG preview at
default detail; keep the full PNG for the PR.

## PR

Commit implementation changes. For screenshots run
`"$S/pr-images" docs/img/<slug>/*.png`; it commits only those images, pushes, and
prints commit-pinned Markdown links. Otherwise `git push -u origin HEAD`.
Write the PR description to a file, then:

```bash
gh pr create -R MIT-Tab/black-rod --base master --head "$(git branch --show-current)" \
  --title "..." --body-file <file>
```

## CI: detach once and end the turn

Run `"$S/ci-watch" --detach` once and **end the turn**. Never manually poll CI,
call `gh pr checks`, or repeatedly read watcher output/status. The helper quietly
checks GitHub every 60 seconds, waits for two stable completed snapshots of the
PR head, and includes failed GitHub Actions log tails in the final result.
A head change resets stability. It times out after three hours by default.

With `CODEX_THREAD_ID` (or explicit `--thread <id>`) and `codex queue`, completion
is queued to that thread. Never guess a different session ID. If notification
support is unavailable, report that limitation: detached mode without a thread
only saves a result at the printed path and cannot wake an agent. Agents with
native background completion notifications can instead run `"$S/ci-watch"` once
as a background task and end the turn. Plain terminal users can run it once in
foreground. Do not start parallel watchers; a new detached watch replaces the
previous watcher for the same checkout and PR.

On failure, use the reported logs, reproduce locally, fix, push, detach one new
watcher, and end the turn. On pass, give the final summary and PR link.

Save verbose output to logs and report failure tails. Give long-running commands
a 30-second initial wait; do not repeatedly poll running tools or rerun unchanged
passing checks.
