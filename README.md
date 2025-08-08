
Recommend setting up on Linux, macOS, or WSL. Windows can be problematic. Below assumes some basic installs (python, pip, venv, node, npm)

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

##### Set up the Python virtual environment
Make sure to use `python3` for the virtual environment:
```bash
virtualenv venv
source venv/bin/activate
pip install -r requirements.txt
```

##### Set up Webpack assets
```bash
npm install
```

##### Load test data (Username: `tab`, Password: `password`)
```bash
python manage.py -d makemigrations
python manage.py -d migrate
python3 manage.py -d loaddata coty debaters noty qual_points qual schools soty speaker_results team_results teams toty tournaments users
```

##### Create an admin user
```bash
python manage.py makesuperuser
```

##### Simultaneously run Webpack and the Python server
```bash
./bin/dev-server
```
