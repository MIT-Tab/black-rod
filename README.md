```
git clone <black-rod repo>
cd black-rod

# make sure to use python3 for the virtualenv
virtualenv venv
source venv/bin/activate
pip install -r requirements.txt

# set-up webpack assets
npm install

# load test data. username: tab password: password
python manage.py loaddata schools debaters tournaments

# Simultaneously runs webpack and the python server
./bin/dev-server
```
