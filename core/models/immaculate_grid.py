from django.conf import settings
from django.db import models


class ImmaculateGridHiddenDebater(models.Model):
    debater = models.OneToOneField(
        "core.Debater",
        on_delete=models.CASCADE,
        help_text="Exclude this debater and linked profiles from clue text. They qualify as guesses when they match the clues.",
    )

    class Meta:
        verbose_name = "Immaculate Grid hidden debater"

    def __str__(self):
        return str(self.debater)


class ImmaculateGridBoard(models.Model):
    date = models.DateField(unique=True)
    rows = models.JSONField()
    columns = models.JSONField()
    answers = models.JSONField()
    aliases = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return str(self.date)


class ImmaculateGridPlay(models.Model):
    board = models.ForeignKey(ImmaculateGridBoard, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    guesses = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("board", "user"), name="one_immaculate_play_per_user_board")
        ]

    def __str__(self):
        return f"{self.user} on {self.board}"
