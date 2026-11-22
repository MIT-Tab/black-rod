"""Explain partial matches using the same records as the clue catalog."""

from django.contrib.humanize.templatetags.humanize import ordinal

PLACEMENTS = {
    2: "Reached finals",
    4: "Reached semifinals",
    8: "Reached quarterfinals",
}
COUNTS = {
    "speaks_seasons": ("Varsity speaker awards in", "season"),
    "varsity_seasons": ("Varsity placements in", "season"),
    "multiple_finals": ("Varsity team finals in", "season"),
    "placed_hosts": ("Varsity semis or better at", "host", " in one season"),
    "placed_partners": ("Placed with", "partner"),
    "partner_schools": ("Placed with partners from", "school"),
}
FIXED_CUTOFFS = {"top_speaker": 1, "multiple_finals": 2}
RANKS = {"host", "event", "speaker", "SOTY", "TOTY", "top_speaker"}


def threshold_for_clue(key):
    if key in FIXED_CUTOFFS:
        return key, FIXED_CUTOFFS[key]
    topic, _, cutoff = key.rpartition(":")
    if cutoff.isdecimal() and topic.split(":", 1)[0] in COUNTS.keys() | RANKS:
        return topic, int(cutoff)
    return None


def shortfall(progress, threshold, person):
    if threshold is None:
        return None
    topic, cutoff = threshold
    value = progress.get(topic, {}).get(person, 0)
    category = topic.split(":", 1)[0]
    if category in RANKS:
        rank = -value
        if rank <= cutoff:
            return None
        if category in {"host", "event"}:
            return next(
                (label for place, label in PLACEMENTS.items() if rank <= place),
                f"Best finish: {ordinal(rank)} place",
            )
        if category in {"speaker", "top_speaker"}:
            return f"Best varsity speaker rank: {ordinal(rank)}"
        return f"Best {category} rank: {ordinal(rank)}"

    if 0 < value < cutoff:
        prefix, unit, *suffix = COUNTS[category]
        plural = "" if value == 1 else "s"
        return f"{prefix} {value} {unit}{plural}{''.join(suffix)}"
    return None
