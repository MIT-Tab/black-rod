import secrets
import string

from django.conf import settings
from django.db import IntegrityError, models, transaction


class GeneratedCodeManager(models.Manager):
    CODE_ALPHABET = string.ascii_uppercase + string.digits
    CODE_LENGTH = 12
    MAX_GENERATION_ATTEMPTS = 16

    def create_for_user(self, user):
        for _ in range(self.MAX_GENERATION_ATTEMPTS):
            code = "".join(
                secrets.choice(self.CODE_ALPHABET) for _ in range(self.CODE_LENGTH)
            )
            try:
                with transaction.atomic():
                    return self.create(user=user, code=code)
            except IntegrityError:
                continue
        raise RuntimeError("Unable to generate a unique code after repeated attempts.")


class GeneratedCode(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="generated_codes",
    )
    code = models.CharField(max_length=12, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = GeneratedCodeManager()

    class Meta:
        ordering = ("-created_at", "-id")

    def __str__(self):
        return self.code
