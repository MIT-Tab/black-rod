"""Houses shared runtime ELO constants plus normalization/filter helpers for seasons, school names, and runtime tournament exclusion."""


import re
import unicodedata
from datetime import date

DEFAULT_RATING = 1500.0
DEFAULT_K_MAX = 40.0
DEFAULT_K_MIN = 10.0
DEFAULT_K_DECAY_SCALE = 75.0
DEFAULT_HIGHER_ELO_WIN_SHARE = 50.0
DEFAULT_HIGHER_ELO_LOSS_SHARE = 50.0

PARTNER_MODE = "partners"
INDIVIDUAL_MODE = "ignore_partners"

SPACE_RE = re.compile(r"\s+")
UNAFFILIATED_RE = re.compile(r"\bunaffiliated\b", re.IGNORECASE)
PROAM_RE = re.compile(r"\bpro\s*ams?\b", re.IGNORECASE)
NOVICE_RE = re.compile(r"\bnovice\b", re.IGNORECASE)
NOVICE_QUAL_TYPES = {9}
PROAM_QUAL_TYPES = {7, 13}


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def season_to_int(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if "-" in text:
        text = text.split("-", 1)[0]
    if text.isdigit():
        return int(text)
    return None


def parse_iso_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def normalize_school_name(value):
    text = str(value or "").strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = SPACE_RE.sub(" ", text).strip().lower()
    if UNAFFILIATED_RE.search(text):
        return "unaffiliated"
    return text


def should_exclude_tournament(tournament, include_novice, include_proam):
    qual_type = to_int(getattr(tournament, "qual_type", None))
    if (not include_novice) and qual_type in NOVICE_QUAL_TYPES:
        return True
    if qual_type in PROAM_QUAL_TYPES:
        return True

    haystacks = [
        str(getattr(tournament, "name", "") or ""),
        str(getattr(tournament, "short_name", "") or ""),
        str(getattr(tournament, "manual_name", "") or ""),
    ]
    if (not include_novice) and any(NOVICE_RE.search(value) for value in haystacks):
        return True
    if any(PROAM_RE.search(value) for value in haystacks):
        return True
    return False
