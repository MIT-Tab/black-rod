from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    can_view_private_videos = models.BooleanField(default=False)
