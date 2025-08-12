import os

oldest = 2005
if os.environ.get("ENV") == "development":
    latest = 2020
    DEFAULT_SEASON = '2019'
    CURRENT_SEASON = '2019'
elif os.environ.get("ENV") == "production":
    latest = 2025
    DEFAULT_SEASON = '2024'
    CURRENT_SEASON = '2024'
    

SEASONS = tuple(
    (str(year), f"{year}-{str(year+1)[2:]}") 
    for year in range(latest, oldest-1, -1)
    )

ONLINE_SEASONS = (
    '2020',
    '2021',
    )

QUAL_BAR = 10.5

