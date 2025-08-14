from core.models.round import Round
from core.utils.generics import (
    CustomDetailView,
)


class RoundDetailView(CustomDetailView):
    public_view = True
    model = Round
    template_name = "rounds/detail.html"
