from django.db import models


class DebaterAliasGroup(models.Model):
    label = models.CharField(max_length=64, blank=True, default='')

    class Meta:
        verbose_name = "Debater Alias Group"
        verbose_name_plural = "Debater Alias Groups"
        ordering = ("label", "id")

    @property
    def name(self):
        return self.label or f"Alias Group #{self.pk}" if self.pk else "Alias Group"

    def __str__(self):
        return self.name
