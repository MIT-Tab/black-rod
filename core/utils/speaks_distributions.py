"""Individual preliminary speaks grouped by final record and empirical percentile."""

from collections import Counter, defaultdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


def summarize_sample(values):
    """Use linear interpolation at (n - 1) * p, including singleton samples."""
    ordered = sorted(values)
    summary = {"sample_size": len(ordered)}
    for percentile in (25, 50, 75):
        value = None
        if ordered:
            position = Decimal(len(ordered) - 1) * Decimal(percentile) / 100
            lower = int(position)
            upper = min(lower + 1, len(ordered) - 1)
            value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
        summary[f"p{percentile}"] = value
    return summary


def new_round_speaks_samples():
    return {"scores": [], "by_record": defaultdict(list), "round_counts": set(), "tournaments": set()}


def add_round_speaks_samples(samples, tournament, observations, speaker_cards, wins, unknown_outcomes):
    ambiguous = {
        (identity, number)
        for identity, card in speaker_cards.items()
        for number, count in Counter(number for number, _ in card).items() if count > 1
    }
    for number, identity, score, team in observations:
        if (identity, number) in ambiguous:
            continue
        record = None if team is None or team in unknown_outcomes else (wins[team], tournament.num_rounds - wins[team])
        samples["scores"].append(score)
        samples["by_record"][record].append(score)
        samples["round_counts"].add(tournament.num_rounds)
        samples["tournaments"].add(tournament.pk)


def summarize_round_speaks(samples):
    columns = []
    for rounds in sorted(samples["round_counts"]):
        for wins in range(rounds + 1):
            record = (wins, rounds - wins)
            columns.append(_summary_column(f"{record[0]}-{record[1]}", samples["by_record"][record]))
    if samples["by_record"][None]:
        columns.append(_summary_column("Unknown record", samples["by_record"][None]))
    overall = _summary_column("All records", samples["scores"])
    columns.append(overall)
    rows = [
        {"label": label, "values": [column[key] for column in columns]}
        for key, label in (("p25", "25th percentile"), ("p50", "50th percentile"),
                           ("p75", "75th percentile"), ("mean", "Mean"))
    ]
    correspondence = _percentile_correspondence(samples["scores"])
    return {
        "sample_size": overall["sample_size"], "tournament_count": len(samples["tournaments"]),
        "columns": columns, "rows": rows, "overall": overall,
        "correspondence": correspondence, "chart": _percentile_chart(correspondence),
    }


def _summary_column(label, values):
    return {"label": label, **summarize_sample(values),
            "mean": sum(values) / len(values) if values else None}


def _percentile_correspondence(values):
    """Exact empirical CDF, including unobserved whole scores between the bounds."""
    if not values:
        return []
    frequencies = Counter(values)
    lower = int(min(values).to_integral_value(rounding=ROUND_FLOOR))
    upper = int(max(values).to_integral_value(rounding=ROUND_CEILING))
    thresholds = sorted(set(frequencies) | {Decimal(score) for score in range(lower, upper + 1)})
    cumulative = 0
    rows = []
    for score in thresholds:
        count = frequencies[score]
        cumulative += count
        rows.append({
            "score": score, "sample_size": count, "cumulative_count": cumulative,
            "percentile": Decimal(cumulative) * 100 / len(values),
        })
    return rows


def _percentile_chart(rows):
    if not rows:
        return None
    lower, upper = rows[0]["score"], rows[-1]["score"]
    if lower == upper:
        lower, upper = lower - 1, upper + 1

    def x_coordinate(score):
        return round(56 + float((score - lower) / (upper - lower)) * 644, 2)

    path = ["M 56 250"]
    points = []
    for row in rows:
        x_value = x_coordinate(row["score"])
        y_value = round(250 - float(row["percentile"]) * 2.3, 2)
        path.append(f"H {x_value} V {y_value}")
        if row["sample_size"]:
            points.append({**row, "x": x_value, "y": y_value})
    path.append("H 700")
    return {
        "path": " ".join(path), "points": points,
        "x_ticks": [{"value": lower + (upper - lower) * Decimal(index) / 5,
                     "x": round(56 + 644 * index / 5, 2)} for index in range(6)],
        "y_ticks": [{"value": value, "y": round(250 - value * 2.3, 2)} for value in (0, 25, 50, 75, 100)],
    }
