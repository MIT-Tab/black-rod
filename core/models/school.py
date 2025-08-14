from django.db import models
from django.shortcuts import reverse


class School(models.Model):
    name = models.CharField(max_length=64, blank=False, unique=True)

    included_in_oty = models.BooleanField(default=True, verbose_name="Included in OTY")

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("core:school_detail", kwargs={"pk": self.id})


class SchoolLookup(models.Model):
    server_name = models.CharField(max_length=64, blank=False, unique=True)

    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="school_lookups"
    )
