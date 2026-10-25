
Recommend setting up on Linux, macOS, or WSL. Windows can be problematic. Below assumes some basic installs (python, pip, venv, node, npm, pyenv)

##### Clone the repository
```bash
git clone <black-rod repo>
cd black-rod
```

##### Ensure SQLite 3 is installed (skip if already installed)
**For Ubuntu/Debian-based systems:**
```bash
sudo apt update && sudo apt install -y sqlite3
```

**For macOS using Homebrew:**
```bash
brew install sqlite
```

**For Windows:**
Download the installer from [SQLite Downloads](https://www.sqlite.org/download.html) and follow the instructions.

##### Install pyenv if you haven't already, or install the version of python listed in .python-version and verify its being used in your repo

##### Set up the Python virtual environment
Make sure to use `python3` for the virtual environment:
```bash
python -m venv venv
source venv/bin/activate
pip install --upgrade pip setuptools wheel -c constraints.txt
pip install -r requirements.txt -c constraints.txt
```

##### Set up Webpack assets
```bash
nvm install
nvm use
npm ci
npm run build
```

##### Load test data (Username: `tab`, Password: `password`)
```bash
python manage.py -d makemigrations
python manage.py -d migrate
python3 manage.py -d loaddata dev_fixtures
```

##### Create an admin user
```bash
python manage.py makesuperuser
```

##### Simultaneously run Webpack and the Python server
```bash
./bin/dev-server
```

##### Agent implementation workflow

The versioned [black-rod task skill](.agents/skills/black-rod-task/SKILL.md)
provides isolated worktree setup, private SQLite development/test databases,
dev servers, desktop/mobile screenshots, PR image links, and detached GitHub
Actions notifications. Start with [AGENTS.md](AGENTS.md). Codex and Claude share
the same checked-in skill; no user-level skill installation is required.
