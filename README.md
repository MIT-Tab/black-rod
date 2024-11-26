Recommend setting up on linux, macos or wsl. Windows is a mess

# Clone the repository
git clone <black-rod repo>
cd black-rod

# Ensure SQLite 3 is installed (skip if already installed)
# For Ubuntu/Debian-based systems:
sudo apt update && sudo apt install -y sqlite3

# For macOS using Homebrew:
brew install sqlite

# For Windows, download the installer from https://www.sqlite.org/download.html and follow the instructions.

# Set up the Python virtual environment
# Make sure to use python3 for the virtualenv
virtualenv venv
source venv/bin/activate
pip install -r requirements.txt

# Set up Webpack assets
npm install

# Load test data. Username: tab Password: password
python manage.py makemigrations
python manage.py migrate
python3 manage.py loaddata coty debaters noty qual_points qual schools soty speaker_results team_results teams toty tournaments users

# Create an admin user
python manage.py makesuperuser

# Simultaneously run Webpack and the Python server
./bin/dev-server
