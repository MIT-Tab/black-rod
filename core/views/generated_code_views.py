from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import TemplateView

from core.models import GeneratedCode


class GeneratedCodeView(LoginRequiredMixin, TemplateView):
    template_name = "core/generated_codes.html"
    _session_key = "latest_generated_code"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["generated_code"] = self.request.session.pop(self._session_key, None)
        return context

    def post(self, request, *args, **kwargs):
        generated_code = GeneratedCode.objects.create_for_user(request.user)
        request.session[self._session_key] = generated_code.code
        return redirect("core:generated_code")
