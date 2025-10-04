from core.models import SchoolAdmin


def is_school_admin(request):
    if not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True
    return SchoolAdmin.objects.filter(user=request.user).exists()
