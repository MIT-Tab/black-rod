"""Rarity bands shared by the game, answer review, and copied results."""

RARITY_TIERS = (
    {"key": "common", "label": "Common", "emoji": "🟩", "minimum": 1, "maximum": 24},
    {"key": "uncommon", "label": "Uncommon", "emoji": "🟦", "minimum": 25, "maximum": 49},
    {"key": "rare", "label": "Rare", "emoji": "🟪", "minimum": 50, "maximum": 74},
    {"key": "epic", "label": "Epic", "emoji": "🟧", "minimum": 75, "maximum": 89},
    {"key": "galaxy", "label": "Galaxy", "emoji": "🌌", "minimum": 90, "maximum": 100},
)


def rarity_tier(score):
    for tier in reversed(RARITY_TIERS):
        if score >= tier["minimum"]:
            return tier
    return RARITY_TIERS[0]
