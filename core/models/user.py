from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    class Meta:
        permissions = (('view_accounts_only_video', 'View Accounts Only Video'),)
