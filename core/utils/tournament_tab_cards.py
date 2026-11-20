from math import isfinite

from django.db.models import Q

from core.models import Round, TournamentImport


def _valid_scores(scores):
    return isinstance(scores, list) and len(scores) == 3 and all(
        score is None or (
            isinstance(score, list) and len(score) == 2
            and all(
                not isinstance(value, bool)
                and (isinstance(value, int) or (isinstance(value, float) and isfinite(value)))
                for value in score
            )
        )
        for score in scores
    )


def _valid_speaker(speaker):
    return (
        isinstance(speaker, dict)
        and isinstance(speaker.get("name"), str) and bool(speaker["name"].strip())
        and speaker.get("status") in ("N", "V")
    )


def _valid_round(row):
    return (
        isinstance(row, dict)
        and row.get("side") in ("", "G", "O", "BYE")
        and row.get("result") in ("", "W", "L", "WF", "LF", "AD", "AW")
        and isinstance(row.get("opponents"), list) and len(row["opponents"]) <= 2
        and all(isinstance(name, str) for name in row["opponents"])
        and isinstance(row.get("judges"), str)
        and _valid_scores(row.get("scores"))
    )


def validate_tab_cards(cards):
    """Validate the optional public cards in the import bundle."""
    if cards is None:
        return None
    if not isinstance(cards, list):
        raise ValueError("Mit-Tab 'tab_cards' must be a list or null.")
    for card in cards:
        if not (
            isinstance(card, dict)
            and isinstance(card.get("speakers"), list) and len(card["speakers"]) == 2
            and all(_valid_speaker(speaker) for speaker in card["speakers"])
            and isinstance(card.get("rounds"), list) and all(_valid_round(row) for row in card["rounds"])
            and _valid_scores(card.get("totals"))
        ):
            raise ValueError("Mit-Tab bundle contains an invalid tab card.")
    return cards


def latest_mittab_import(tournament):
    return tournament.tournament_imports.filter(
        import_type=TournamentImport.ImportType.MITTAB_BUNDLE,
        round_metadata__round__tournament=tournament,
    ).distinct().first()


def legacy_prelims(tournament):
    return tournament.rounds.filter(stage=Round.Stage.PRELIM, round_number__gt=0).filter(
        Q(import_origin=TournamentImport.ImportType.MITTAB_BUNDLE)
        | Q(imported_metadata__sources__import_type=TournamentImport.ImportType.MITTAB_BUNDLE)
    ).distinct()


def has_tab_cards(tournament, source):
    if source is None:
        return False
    if "cards" in source.tab_cards:
        return bool(source.tab_cards["cards"])
    return legacy_prelims(tournament).exists()


def get_tournament_tab_cards(tournament, source):
    if "cards" in source.tab_cards:
        return source.tab_cards["cards"]
    return _legacy_cards(tournament)


_LEGACY_RESULTS = {
    Round.GOV: ("W", "L"),
    Round.OPP: ("L", "W"),
    Round.GOV_VIA_FORFEIT: ("WF", "LF"),
    Round.OPP_VIA_FORFEIT: ("LF", "WF"),
    Round.ALL_DROP: ("AD", "AD"),
    Round.ALL_WIN: ("AW", "AW"),
}


def _legacy_cards(tournament):
    rounds = list(legacy_prelims(tournament).select_related(
        "imported_metadata__gov_1_alias", "imported_metadata__gov_2_alias",
        "imported_metadata__opp_1_alias", "imported_metadata__opp_2_alias",
    ).prefetch_related(
        "gov__debaters", "opp__debaters", "imported_metadata__judges", "stats",
    ).order_by("round_number", "pk"))
    if not rounds:
        return []

    num_rounds = max(tournament.num_rounds, rounds[-1].round_number)
    cards = {}
    for round_obj in rounds:
        gov = _legacy_speakers(round_obj, "gov")
        opp = _legacy_speakers(round_obj, "opp")
        if not gov or not opp:
            continue
        results = _LEGACY_RESULTS.get(round_obj.victor, ("", ""))
        metadata = getattr(round_obj, "imported_metadata", None)
        judges = " - ".join(judge.original_name for judge in metadata.judges.all()) if metadata else ""
        scores = {
            stat.debater_id: (float(stat.speaks), float(stat.ranks))
            for stat in reversed(list(round_obj.stats.all()))
            if stat.speaks is not None and stat.ranks is not None
        }
        for team_id, side, speakers, opponents, result in (
            (round_obj.gov_id, "G", gov, opp, results[0]),
            (round_obj.opp_id, "O", opp, gov, results[1]),
        ):
            if team_id not in cards:
                cards[team_id] = {
                    "speakers": [{"name": name} for _, name in (speakers + speakers[:1])[:2]],
                    "rounds": [{"side": "", "result": "", "opponents": [], "judges": "", "scores": [None, None, None]} for _ in range(num_rounds)],
                    "totals": [None, None, None],
                }
            first_id, second_id = speakers[0][0], speakers[-1][0]
            cards[team_id]["rounds"][round_obj.round_number - 1] = {
                "side": "BYE" if round_obj.victor == Round.BYE else side,
                "result": result,
                "opponents": [name for _, name in opponents] if round_obj.victor != Round.BYE else [],
                "judges": judges,
                "scores": [scores.get(first_id), scores.get(second_id), None],
            }
    return list(cards.values())


def _legacy_speakers(round_obj, side):
    metadata = getattr(round_obj, "imported_metadata", None)
    aliases = [getattr(metadata, f"{side}_{position}_alias", None) for position in (1, 2)]
    speakers = [(alias.debater_id, alias.source_name) for alias in aliases if alias]
    return speakers or [(debater.pk, debater.name) for debater in getattr(round_obj, side).debaters.all()]
