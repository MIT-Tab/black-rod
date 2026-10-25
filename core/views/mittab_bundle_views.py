from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.shortcuts import redirect, render
from django.views import View

from core.forms import MittabBundleImportForm
from core.models import Tournament
from core.utils.mittab_bundle_import import (
    MittabBundleImportError,
    import_mittab_bundle,
    load_mittab_bundle,
)


class TournamentMittabBundleUploadView(UserPassesTestMixin, View):
    template_name = "tournaments/mittab_bundle_upload.html"
    form_class = MittabBundleImportForm

    def test_func(self):
        return self.request.user.is_superuser

    def get_tournament(self):
        tournament_id = self.request.GET.get("tournament") or self.request.POST.get(
            "tournament"
        )
        if not tournament_id:
            raise ValueError("Tournament ID must be provided as a URL parameter")
        return Tournament.objects.get(id=int(tournament_id))

    def get(self, request, *args, **kwargs):
        tournament = self.get_tournament()
        return render(
            request,
            self.template_name,
            {
                "form": self.form_class(),
                "tournament": tournament,
                "results_ready": self._results_ready(tournament),
            },
        )

    def post(self, request, *args, **kwargs):
        tournament = self.get_tournament()
        form = self.form_class(request.POST, request.FILES)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "form": form,
                    "tournament": tournament,
                    "results_ready": self._results_ready(tournament),
                },
            )

        try:
            summary = import_mittab_bundle(
                load_mittab_bundle(form.cleaned_data["bundle_file"]),
                tournament,
                actor=request.user,
            )
        except MittabBundleImportError as exc:
            form.add_error("bundle_file", str(exc))
            return render(
                request,
                self.template_name,
                {
                    "form": form,
                    "tournament": tournament,
                    "results_ready": self._results_ready(tournament),
                },
            )

        messages.success(request, self._build_success_message(summary))
        return redirect("core:tournament_detail", pk=tournament.id)

    @staticmethod
    def _results_ready(tournament):
        return tournament.team_results.exists() or tournament.speaker_results.exists()

    @staticmethod
    def _build_success_message(summary):
        return (
            "Mit-Tab bundle imported successfully. "
            f"Created {summary['rounds_created']} rounds, "
            f"updated {summary['rounds_updated']} rounds, "
            f"deleted {summary['rounds_deleted']} stale rounds."
        )
