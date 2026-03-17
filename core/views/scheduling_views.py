import json
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from core.models import SchedulerWorkspace, SchedulingRun
from core.scheduler_forms import SchedulerCSVUploadForm, SchedulerSettingsForm
from core.utils.scheduler import (
    SchedulerDataError,
    merge_scheduler_settings,
    summarize_scheduler_inputs,
)


class SchedulingDashboardView(UserPassesTestMixin, TemplateView):
    template_name = "admin/scheduling_dashboard.html"
    sample_directory = Path("/home/joey/scheduling")

    def test_func(self):
        return self.request.user.is_superuser

    def dispatch(self, request, *args, **kwargs):
        self.workspace = SchedulerWorkspace.get_solo()
        self._bootstrap_workspace()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        summary, summary_error = self._get_workspace_summary()
        latest_completed_run = (
            self.workspace.runs.filter(status=SchedulingRun.STATUS_COMPLETED)
            .select_related("created_by")
            .first()
        )
        context.update(
            {
                "workspace": self.workspace,
                "summary": summary,
                "summary_error": summary_error,
                "settings_form": kwargs.get("settings_form")
                or SchedulerSettingsForm(
                    settings_data=self.workspace.settings,
                    initial={"workspace_version": self.workspace.version},
                ),
                "upload_form": kwargs.get("upload_form")
                or SchedulerCSVUploadForm(
                    initial={"workspace_version": self.workspace.version}
                ),
                "current_settings": merge_scheduler_settings(self.workspace.settings),
                "recent_runs": self.workspace.runs.select_related("created_by")[:5],
                "latest_completed_run": latest_completed_run,
                "workspace_data_url": reverse("core:scheduling_workspace_data"),
                "save_run_url": reverse("core:scheduling_save_browser_run"),
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        if action == "save_settings":
            return self._save_settings(request)
        if action == "upload_csv":
            return self._upload_csv(request)
        messages.error(request, "Unknown scheduling action.")
        return redirect("core:scheduling_dashboard")

    def _bootstrap_workspace(self):
        if self.workspace.has_inputs:
            return
        schools_path = self.sample_directory / "schools.csv"
        dates_path = self.sample_directory / "dates.csv"
        if not schools_path.exists() or not dates_path.exists():
            return
        self.workspace.schools_csv = schools_path.read_text(encoding="utf-8")
        self.workspace.dates_csv = dates_path.read_text(encoding="utf-8")
        self.workspace.schools_filename = schools_path.name
        self.workspace.dates_filename = dates_path.name
        self.workspace.save(
            update_fields=[
                "schools_csv",
                "dates_csv",
                "schools_filename",
                "dates_filename",
                "updated_at",
            ]
        )

    def _get_workspace_summary(self):
        if not self.workspace.has_inputs:
            return None, None
        try:
            return (
                summarize_scheduler_inputs(
                    self.workspace.schools_csv,
                    self.workspace.dates_csv,
                ),
                None,
            )
        except SchedulerDataError as exc:
            return None, str(exc)

    def _decode_uploaded_file(self, uploaded_file):
        return uploaded_file.read().decode("utf-8-sig")

    def _conflict_message(self, current_workspace):
        updated_at = timezone.localtime(current_workspace.updated_at).strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
        updated_by = (
            current_workspace.updated_by.username
            if current_workspace.updated_by
            else "another admin"
        )
        return (
            "This scheduler page changed in the meantime. "
            f"Latest save: {updated_at} by {updated_by}. Reload and try again."
        )

    def _validate_workspace_version(self, submitted_version):
        current_workspace = SchedulerWorkspace.get_solo()
        if submitted_version != current_workspace.version:
            messages.error(self.request, self._conflict_message(current_workspace))
            return False
        self.workspace = current_workspace
        return True

    def _save_settings(self, request):
        form = SchedulerSettingsForm(
            request.POST,
            settings_data=self.workspace.settings,
        )
        upload_form = SchedulerCSVUploadForm(
            initial={"workspace_version": self.workspace.version}
        )
        if not form.is_valid():
            messages.error(request, "Please fix the scheduler settings below.")
            return self.render_to_response(
                self.get_context_data(
                    settings_form=form,
                    upload_form=upload_form,
                )
            )

        if not self._validate_workspace_version(form.cleaned_data["workspace_version"]):
            return redirect("core:scheduling_dashboard")

        cleaned = {
            key: value
            for key, value in form.cleaned_data.items()
            if key != "workspace_version"
        }
        self.workspace.settings = cleaned
        self.workspace.updated_by = request.user
        self.workspace.version += 1
        self.workspace.save(
            update_fields=["settings", "updated_by", "version", "updated_at"]
        )
        messages.success(request, "Scheduler settings saved.")
        return redirect("core:scheduling_dashboard")

    def _upload_csv(self, request):
        form = SchedulerCSVUploadForm(request.POST, request.FILES)
        settings_form = SchedulerSettingsForm(
            settings_data=self.workspace.settings,
            initial={"workspace_version": self.workspace.version},
        )
        if not form.is_valid():
            messages.error(request, "Please upload at least one CSV file.")
            return self.render_to_response(
                self.get_context_data(
                    settings_form=settings_form,
                    upload_form=form,
                )
            )

        if not self._validate_workspace_version(form.cleaned_data["workspace_version"]):
            return redirect("core:scheduling_dashboard")

        schools_csv_text = self.workspace.schools_csv
        dates_csv_text = self.workspace.dates_csv
        schools_filename = self.workspace.schools_filename
        dates_filename = self.workspace.dates_filename

        if form.cleaned_data.get("schools_csv"):
            schools_csv_text = self._decode_uploaded_file(form.cleaned_data["schools_csv"])
            schools_filename = form.cleaned_data["schools_csv"].name
        if form.cleaned_data.get("dates_csv"):
            dates_csv_text = self._decode_uploaded_file(form.cleaned_data["dates_csv"])
            dates_filename = form.cleaned_data["dates_csv"].name

        try:
            summary = summarize_scheduler_inputs(schools_csv_text, dates_csv_text)
        except SchedulerDataError as exc:
            form.add_error(None, str(exc))
            messages.error(request, "The uploaded CSV files could not be validated.")
            return self.render_to_response(
                self.get_context_data(
                    settings_form=settings_form,
                    upload_form=form,
                )
            )

        self.workspace.schools_csv = schools_csv_text
        self.workspace.dates_csv = dates_csv_text
        self.workspace.schools_filename = schools_filename
        self.workspace.dates_filename = dates_filename
        self.workspace.updated_by = request.user
        self.workspace.version += 1
        self.workspace.save(
            update_fields=[
                "schools_csv",
                "dates_csv",
                "schools_filename",
                "dates_filename",
                "updated_by",
                "version",
                "updated_at",
            ]
        )
        messages.success(
            request,
            "Scheduler CSVs saved. "
            f"{summary['school_count']} schools and {summary['date_count']} dates are ready.",
        )
        return redirect("core:scheduling_dashboard")


class SchedulingWorkspaceDataView(UserPassesTestMixin, TemplateView):
    def test_func(self):
        return self.request.user.is_superuser

    def get(self, request, *args, **kwargs):
        workspace = SchedulerWorkspace.get_solo()
        if not workspace.has_inputs:
            return JsonResponse(
                {"success": False, "error": "Upload the schools and dates CSVs first."},
                status=400,
            )

        try:
            summary = summarize_scheduler_inputs(
                workspace.schools_csv,
                workspace.dates_csv,
            )
        except SchedulerDataError as exc:
            return JsonResponse(
                {"success": False, "error": str(exc)},
                status=400,
            )

        return JsonResponse(
            {
                "success": True,
                "workspace": {
                    "version": workspace.version,
                    "updated_at": timezone.localtime(workspace.updated_at).isoformat(),
                    "schools_filename": workspace.schools_filename,
                    "dates_filename": workspace.dates_filename,
                    "schools_csv": workspace.schools_csv,
                    "dates_csv": workspace.dates_csv,
                    "settings": merge_scheduler_settings(workspace.settings),
                    "summary": {
                        "school_count": summary["school_count"],
                        "date_count": summary["date_count"],
                        "active_date_count": summary["active_date_count"],
                        "flexible_date_count": summary["flexible_date_count"],
                        "scenario_count": summary["scenario_count"],
                        "tags": summary["tags"],
                    },
                },
            }
        )


class SchedulingBrowserRunSaveView(UserPassesTestMixin, TemplateView):
    def test_func(self):
        return self.request.user.is_superuser

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (TypeError, ValueError):
            return JsonResponse(
                {"success": False, "error": "Invalid scheduling run payload."},
                status=400,
            )

        workspace = SchedulerWorkspace.get_solo()
        workspace_version = payload.get("workspace_version")
        if workspace_version != workspace.version:
            return JsonResponse(
                {
                    "success": False,
                    "error": "The shared scheduler workspace changed before this run could be saved. Reload the page and run it again.",
                },
                status=409,
            )

        status = payload.get("status") or SchedulingRun.STATUS_COMPLETED
        if status not in {
            SchedulingRun.STATUS_COMPLETED,
            SchedulingRun.STATUS_FAILED,
        }:
            return JsonResponse(
                {"success": False, "error": "Unsupported scheduling run status."},
                status=400,
            )

        run = SchedulingRun.objects.create(
            workspace=workspace,
            created_by=request.user,
            workspace_version=workspace.version,
            status=status,
            settings_snapshot=payload.get("settings_snapshot") or {},
            result=payload.get("result") or {},
            output_text=payload.get("output_text") or "",
            error_text=payload.get("error_text") or "",
            completed_at=timezone.now(),
        )
        return JsonResponse({"success": True, "run_id": run.pk})


class SchedulingWorkspaceCSVDownloadView(UserPassesTestMixin, TemplateView):
    def test_func(self):
        return self.request.user.is_superuser

    def get(self, request, *args, **kwargs):
        workspace = SchedulerWorkspace.get_solo()
        kind = kwargs.get("kind")
        if kind == "schools":
            filename = workspace.schools_filename or "schools.csv"
            content = workspace.schools_csv
        elif kind == "dates":
            filename = workspace.dates_filename or "dates.csv"
            content = workspace.dates_csv
        else:
            raise Http404("Unknown scheduler CSV kind.")

        if not content:
            raise Http404("That scheduler CSV has not been uploaded yet.")

        response = HttpResponse(content, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class SchedulingRunOutputDownloadView(UserPassesTestMixin, TemplateView):
    def test_func(self):
        return self.request.user.is_superuser

    def get(self, request, *args, **kwargs):
        try:
            run = SchedulingRun.objects.get(pk=kwargs["pk"])
        except SchedulingRun.DoesNotExist as exc:
            raise Http404("Scheduling run not found.") from exc

        if not run.output_text:
            raise Http404("Scheduling run output is empty.")

        response = HttpResponse(
            run.output_text,
            content_type="text/plain; charset=utf-8",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="scheduling-run-{run.pk}.txt"'
        )
        return response
