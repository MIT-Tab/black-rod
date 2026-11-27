"""Record the Django API page descriptions and field order from the local fixture."""
from pathlib import Path
import json
import os

from django import setup
from django.test import Client


ROUTES = {
    "schools": "/api/schools/",
    "all-schools": "/api/schools/all/",
    "schedule": "/api/schedule/?season=2025",
    "standings": "/api/standings/?season=2025&limit=2",
    "replay": "/api/standings/replay/?season=2025",
    "through": "/api/standings/through-date/?season=2025&date=2026-01-01",
    "school": "/api/schools/7/detail/",
    "debater": "/api/debaters/5270/detail/",
    "debaters": "/api/debaters/7/",
    "team": "/api/teams/6719/detail/",
    "tournament": "/api/tournaments/1203/detail/",
    "guide": "/api/oty-guide/",
    "forum": "/api/private/forum-profiles/",
    "emails": "/api/private/debater-emails/",
}


def shape(value):
    if isinstance(value, dict):
        return {"keys": list(value), "children": {
            key: child for key, item in value.items() if (child := shape(item))
        }}
    if isinstance(value, list):
        for item in value:
            if child := shape(item):
                return {"item": child}
    return None


def main():
    os.environ.setdefault("SECRET_KEY", "disposable-local-parity-key")
    os.environ.setdefault("LIVE_MITTAB_DIRECTORY_URL", "http://127.0.0.1:1")
    setup()
    client = Client(HTTP_HOST="localhost", HTTP_ACCEPT="application/json")
    output = {}
    for name, path in ROUTES.items():
        description = client.options(path)
        assert description.status_code == 200, (name, description.status_code)
        response = client.get(path)
        output[name] = {
            "name": description.json()["name"],
            "description": description.json()["description"],
            "shape": shape(response.json()),
        }
    output_path = Path(__file__).resolve().parents[1] / "schema" / "drf.json"
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
