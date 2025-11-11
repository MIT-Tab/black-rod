from datetime import date

from dal import autocomplete
from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Count, Prefetch
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView, TemplateView, UpdateView, View
from django_filters import FilterSet, CharFilter

from core.models.debater import Debater
from core.models.debater_alias_group import DebaterAliasGroup
from core.models.school import School
from core.models.school_admin import SchoolAdmin

User = get_user_model()


def is_school_admin(user):
    return SchoolAdmin.objects.filter(user=user).exists()


def user_can_manage_school(user, school):
    return user.is_superuser or SchoolAdmin.objects.filter(user=user, school=school).exists()


def user_role_for_school(user, school):
    if user.is_superuser:
        return "superuser"

    admin_record = SchoolAdmin.objects.filter(user=user, school=school).first()
    if not admin_record:
        return None
    if admin_record.primary:
        return "primary"
    return "admin"


class SchoolAdminManagementPermissionMixin(UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (user.is_superuser or is_school_admin(user))


class SchoolAdminMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and is_school_admin(self.request.user)

    def get_school_admin_schools(self):
        return School.objects.filter(admins__user=self.request.user)

    def get_six_years_ago(self):
        """Helper to get the season from 6 years ago."""
        return str(int(settings.CURRENT_SEASON) - 6)


class SchoolAdminDashboardView(SchoolAdminMixin, TemplateView):
    template_name = "school_admin/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        schools = self.get_school_admin_schools().prefetch_related(
            'admins__user',
            Prefetch(
                'debaters',
                queryset=Debater.objects.filter(
                    latest_season__gte=self.get_six_years_ago()
                ).order_by('-latest_season', 'last_name', 'first_name'),
                to_attr='recent_debaters'
            )
        )
        six_years_ago = self.get_six_years_ago()

        context['schools'] = schools
        context['six_years_ago'] = six_years_ago
        context['is_superuser'] = self.request.user.is_superuser

        admins_by_school = {}
        roles_by_school = {}
        current_admin_by_school = {}
        for school in schools:
            admins = sorted(
                school.admins.all(),
                key=lambda admin: (-int(admin.primary), admin.user.username.lower())
            )
            admins_by_school[school.id] = admins
            roles_by_school[school.id] = user_role_for_school(self.request.user, school)
            current_admin_by_school[school.id] = next((a for a in admins if a.user_id == self.request.user.id), None)

        context['school_admins'] = admins_by_school
        context['school_admin_roles'] = roles_by_school
        context['current_school_admins'] = current_admin_by_school

        # Get debaters for each school
        debaters_data = {
            school.id: getattr(school, 'recent_debaters', [])
            for school in schools
        }
        context['debaters_data'] = debaters_data
        return context


class SchoolAdminDebaterFilter(FilterSet):
    name = CharFilter(method='filter_name', label='Name')

    class Meta:
        model = Debater
        fields = {
            'status': ['exact'],
        }

    def filter_name(self, queryset, name, value):
        return queryset.filter(
            Q(first_name__icontains=value) | Q(last_name__icontains=value)
        )


class SchoolAdminDebaterListView(SchoolAdminMixin, ListView):
    model = Debater
    template_name = "school_admin/debater_list.html"
    context_object_name = 'debaters'
    paginate_by = 50

    def get_queryset(self):
        school_id = self.kwargs.get('school_id')
        school = get_object_or_404(School, id=school_id)

        if not SchoolAdmin.objects.filter(user=self.request.user, school=school).exists():
            raise Http404

        return Debater.objects.filter(
            school=school,
            latest_season__gte=self.get_six_years_ago()
        ).order_by('-latest_season', 'last_name', 'first_name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['school'] = get_object_or_404(School, id=self.kwargs.get('school_id'))
        context['six_years_ago'] = self.get_six_years_ago()
        return context


class SchoolAdminDebaterForm(forms.ModelForm):
    alias_group = forms.ModelChoiceField(
        queryset=DebaterAliasGroup.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:alias_group_autocomplete"),
        required=False,
        help_text="Link this debater to an existing alias group if applicable.",
    )

    DINO_CONTACT_FIELDS = {
        'dino_to_contact_opt_in': {
            'label': "Open to Dino TO/observer outreach",
            'help_text': (
                "Visible only for Dinos. Enable if this debater wants tournaments to reach out about TOing."
            ),
        },
        'dino_judge_contact_opt_in': {
            'label': "Open to Dino judging outreach",
            'help_text': (
                "Visible only for Dinos. Enable if this debater is open to judging when tournaments need dinos."
            ),
        },
    }

    class Meta:
        model = Debater
        fields = (
            'first_name',
            'last_name',
            'status',
            'first_season',
            'latest_season',
            'alias_group',
            'dino_to_contact_opt_in',
            'dino_judge_contact_opt_in',
        )

    def __init__(self, *args, school=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.user = user
        self.show_dino_contact_fields = self._should_show_dino_contact_fields()

        current_year = int(settings.CURRENT_SEASON)
        six_years_ago = current_year - 6

        # Helper to create season choices
        def season_choices(start_year, end_year):
            return [(str(year), f"{year}-{str(year+1)[2:]}") for year in range(start_year, end_year - 1, -1)]

        self.fields['first_season'] = forms.ChoiceField(
            choices=season_choices(current_year, 2004),
            initial=settings.CURRENT_SEASON
        )
        self.fields['latest_season'] = forms.ChoiceField(
            choices=season_choices(current_year, six_years_ago),
            initial=settings.CURRENT_SEASON
        )

        for field_name, meta in self.DINO_CONTACT_FIELDS.items():
            field = self.fields.get(field_name)
            if not field:
                continue
            field.label = meta['label']
            field.help_text = meta['help_text']
            if not self.show_dino_contact_fields:
                field.widget = forms.HiddenInput()

    def _should_show_dino_contact_fields(self):
        if getattr(self.instance, "status", None) == Debater.DINO:
            return True

        if self.is_bound:
            status_key = self.add_prefix('status')
            status_value = self.data.get(status_key)
            try:
                return int(status_value) == Debater.DINO
            except (TypeError, ValueError):
                return False

        initial_status = self.initial.get('status')
        if initial_status is None:
            return False

        try:
            return int(initial_status) == Debater.DINO
        except (TypeError, ValueError):
            return initial_status == Debater.DINO

    def clean(self):
        cleaned_data = super().clean()
        first_season = cleaned_data.get('first_season')
        latest_season = cleaned_data.get('latest_season')

        if first_season and latest_season:
            if int(first_season) > int(latest_season):
                raise forms.ValidationError("First season cannot be after latest season.")

        current_year = int(settings.CURRENT_SEASON)
        six_years_ago = current_year - 6

        if latest_season and int(latest_season) < six_years_ago:
            raise forms.ValidationError(
                f"Latest season must be within the last 6 years (>= {six_years_ago})."
            )

        status = cleaned_data.get('status', getattr(self.instance, 'status', None))
        if status != Debater.DINO:
            for field_name in self.DINO_CONTACT_FIELDS:
                cleaned_data[field_name] = False

        return cleaned_data


class SchoolAdminDebaterUpdateView(SchoolAdminMixin, UpdateView):
    model = Debater
    form_class = SchoolAdminDebaterForm
    template_name = "school_admin/debater_form.html"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)

        if not SchoolAdmin.objects.filter(user=self.request.user, school=obj.school).exists():
            raise Http404

        # Ensure debater is within the last 6 years
        if obj.latest_season < self.get_six_years_ago():
            raise Http404

        return obj

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['school'] = self.object.school
        kwargs['user'] = self.request.user
        return kwargs

    def get_success_url(self):
        return reverse_lazy('core:school_admin_dashboard')


class SchoolAdminDebaterCreateView(SchoolAdminMixin, CreateView):
    model = Debater
    form_class = SchoolAdminDebaterForm
    template_name = "school_admin/debater_form.html"
    school = None  # Initialize class attribute

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        school_id = self.kwargs.get('school_id')
        self.school = get_object_or_404(School, id=school_id)

    def get(self, request, *args, **kwargs):
        if not SchoolAdmin.objects.filter(user=request.user, school=self.school).exists():
            return HttpResponseForbidden("You do not have permission to create debaters for this school.")
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if not SchoolAdmin.objects.filter(user=request.user, school=self.school).exists():
            return HttpResponseForbidden("You do not have permission to create debaters for this school.")

        today = date.today().isoformat()
        cache_key = f"debater_create_count_{request.user.id}_{today}"
        count = cache.get(cache_key, 0)

        if count >= 5:
            return HttpResponseForbidden(
                "You have reached the daily limit of 5 new debaters. "
                "Please contact a site administrator if you need to create more."
            )

        return super().post(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['school'] = self.school
        kwargs['user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.school = self.school

        existing = Debater.objects.filter(
            first_name__iexact=form.instance.first_name,
            last_name__iexact=form.instance.last_name,
            school=self.school
        ).first()

        if existing:
            form.add_error(None, "A debater with this name already exists at this school.")
            return self.form_invalid(form)

        today = date.today().isoformat()
        cache_key = f"debater_create_count_{self.request.user.id}_{today}"
        count = cache.get(cache_key, 0)
        cache.set(cache_key, count + 1, 86400)

        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy('core:school_admin_dashboard')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['school'] = self.school
        context['is_create'] = True
        return context


class SuperuserSchoolAdminManagementView(UserPassesTestMixin, TemplateView):
    template_name = "school_admin/superuser_management.html"

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        current_year = int(settings.CURRENT_SEASON)
        six_years_ago = str(current_year - 6)

        schools = School.objects.annotate(
            active_debater_count=Count(
                'debaters',
                filter=Q(debaters__latest_season__gte=six_years_ago)
            )
        ).prefetch_related('admins__user').order_by('-active_debater_count', 'name')

        paginator = Paginator(schools, 15)
        page_number = self.request.GET.get('page')
        page_obj = paginator.get_page(page_number)

        school_data = []
        for school in page_obj:
            admins = sorted(
                school.admins.all(),
                key=lambda admin: (-int(admin.primary), admin.user.username.lower())
            )
            school_data.append({
                'school': school,
                'active_debater_count': school.active_debater_count,
                'admins': admins,
            })

        context['school_data'] = school_data
        context['page_obj'] = page_obj
        return context


class SchoolAdminAddView(SchoolAdminManagementPermissionMixin, View):
    def post(self, request, *args, **kwargs):
        school_id = request.POST.get('school_id')
        user_id = request.POST.get('user_id')

        if not school_id or not user_id:
            return JsonResponse({'error': 'Missing school_id or user_id'}, status=400)

        school = get_object_or_404(School, id=school_id)
        user = get_object_or_404(User, id=user_id)

        if not user_can_manage_school(request.user, school):
            return JsonResponse({'error': 'You cannot manage admins for this school.'}, status=403)

        # Non-primary school admins can add admins, so no extra role gating here.
        school_admin = SchoolAdmin.objects.filter(user=user, school=school).first()
        created = False

        if not school_admin:
            with transaction.atomic():
                school_admin = SchoolAdmin.objects.create(
                    user=user,
                    school=school,
                )
                has_primary = SchoolAdmin.objects.filter(
                    school=school,
                    primary=True
                ).exclude(id=school_admin.id).exists()
                if not has_primary:
                    school_admin.primary = True
                    school_admin.save(update_fields=['primary'])
            created = True

        return JsonResponse({
            'success': True,
            'created': created,
            'admin': {
                'id': school_admin.id,
                'user_id': user.id,
                'username': user.username,
                'email': user.email,
                'primary': school_admin.primary,
            }
        })


class SchoolAdminRemoveView(SchoolAdminManagementPermissionMixin, View):
    def post(self, request, *args, **kwargs):
        school_admin_id = request.POST.get('school_admin_id')

        if not school_admin_id:
            return JsonResponse({'error': 'Missing school_admin_id'}, status=400)

        school_admin = get_object_or_404(SchoolAdmin, id=school_admin_id)

        if not user_can_manage_school(request.user, school_admin.school):
            return JsonResponse({'error': 'You cannot manage admins for this school.'}, status=403)

        requester_role = user_role_for_school(request.user, school_admin.school)

        if requester_role not in {'superuser', 'primary', 'admin'}:
            return JsonResponse({'error': 'Permission denied.'}, status=403)

        if requester_role == 'admin' and school_admin.user_id != request.user.id:
            return JsonResponse({'error': 'You can only remove yourself.'}, status=403)

        if requester_role == 'primary' and school_admin.user_id == request.user.id:
            return JsonResponse({'error': 'Transfer primary status before removing yourself.'}, status=400)

        if school_admin.primary:
            return JsonResponse({'error': 'Assign a new primary before removing this admin.'}, status=400)

        school_admin.delete()

        return JsonResponse({'success': True})


class SchoolAdminPrimaryUpdateView(SchoolAdminManagementPermissionMixin, View):
    def post(self, request, *args, **kwargs):
        school_admin_id = request.POST.get('school_admin_id')

        if not school_admin_id:
            return JsonResponse({'error': 'Missing school_admin_id'}, status=400)

        school_admin = get_object_or_404(SchoolAdmin, id=school_admin_id)

        if not user_can_manage_school(request.user, school_admin.school):
            return JsonResponse({'error': 'You cannot manage admins for this school.'}, status=403)

        requester_role = user_role_for_school(request.user, school_admin.school)
        if requester_role not in {'superuser', 'primary'}:
            return JsonResponse({'error': 'Only primaries or superusers can set the primary.'}, status=403)

        if requester_role == 'primary' and school_admin.user_id == request.user.id:
            return JsonResponse({'error': 'You are already the primary admin.'}, status=400)

        previous_primary = SchoolAdmin.objects.filter(
            school=school_admin.school,
            primary=True
        ).exclude(id=school_admin.id).first()

        with transaction.atomic():
            SchoolAdmin.objects.filter(
                school=school_admin.school,
                primary=True
            ).exclude(id=school_admin.id).update(primary=False)
            school_admin.primary = True
            school_admin.save(update_fields=['primary'])

        return JsonResponse({
            'success': True,
            'primary_admin_id': school_admin.id,
            'demoted_admin_id': previous_primary.id if previous_primary else None,
            'school_id': school_admin.school_id,
        })


class UserAutocompleteView(autocomplete.Select2QuerySetView):
    def get_queryset(self):
        user = self.request.user
        if not (user.is_superuser or is_school_admin(user)):
            return User.objects.none()

        qs = User.objects.all().order_by('username')

        if self.q:
            qs = qs.filter(
                Q(username__icontains=self.q) |
                Q(email__icontains=self.q) |
                Q(first_name__icontains=self.q) |
                Q(last_name__icontains=self.q)
            )

        return qs

    def get_result_label(self, item):
        if item.email:
            return f"{item.username} ({item.email})"
        return item.username
