from django.db import models
from django.shortcuts import reverse


class ActiveSchoolManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(temporary=False)


class School(models.Model):
    name = models.CharField(max_length=64, blank=False, unique=True)
    short_name = models.CharField(max_length=64, blank=False, default="")

    included_in_oty = models.BooleanField(default=True, verbose_name="Included in OTY")
    temporary = models.BooleanField(default=False, db_index=True)
    synthetic = models.BooleanField(default=False, db_index=True)

    objects = ActiveSchoolManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ['name']

    @property
    def display_name(self):
        base = self.name
        return f"{base} (New)" if self.temporary else base

    def __str__(self):
        return self.display_name

    def get_absolute_url(self):
        return reverse("core:school_detail", kwargs={"pk": self.id})


class SchoolLookup(models.Model):
    server_name = models.CharField(max_length=64, blank=False, unique=True)

    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="school_lookups"
    )
