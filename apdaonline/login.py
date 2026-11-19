"""One-step, CSRF-protected login with persistent or browser-only sessions."""
from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView

from .views import oauth2_login


class ResultsAuthenticationForm(AuthenticationForm):
    """Offer the same session lifetime choice for both login methods."""

    remember = forms.BooleanField(
        required=False, initial=True, label="Keep me signed in for 30 days"
    )


class ResultsLoginView(LoginView):
    """Start OAuth directly on POST while retaining Django's CSRF protection."""

    authentication_form = ResultsAuthenticationForm

    def post(self, request, *args, **kwargs):
        if request.POST.get("provider") == "apdaonline":
            request.session["apda_remember_login"] = bool(request.POST.get("remember"))
            return oauth2_login(request)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        self.request.session.set_expiry(
            settings.SESSION_COOKIE_AGE if form.cleaned_data["remember"] else 0
        )
        return response
