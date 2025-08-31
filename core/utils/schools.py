from core.models.debater import Debater


def get_debaters_for_season(school, season):
    debaters = list(
        Debater.objects.filter(
            school=school,
            first_season__lte=season,
            latest_season__gte=season
        )
    )
    
    def years_on_team(debater):
        try:
            return int(season) - int(debater.first_season) + 1
        except (ValueError, TypeError):
            return 0
    
    debaters.sort(key=years_on_team, reverse=True)
    return debaters