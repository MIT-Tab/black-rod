from django.db import models

class SiteSetting(models.Model):
    key = models.CharField(max_length=255, unique=True)
    value = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.key}: {self.value}"

    @classmethod
    def get_setting(cls, key, default=None):
        setting = cls.objects.filter(key=key).first()
        return setting.value if setting else default

    @classmethod
    def set_setting(cls, key, value):
        obj, created = cls.objects.update_or_create(key=key, defaults={"value": value})
        return obj
