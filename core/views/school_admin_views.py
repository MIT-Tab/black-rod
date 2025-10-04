from datetime import date

from dal import autocomplete
from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Q, Count
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView, TemplateView, UpdateView, View
from django_filters import FilterSet, CharFilter

from core.models.debater import Debater
from core.models.school import School
from core.models.school_admin import SchoolAdmin

User = get_user_model()


class SchoolAdminMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and SchoolAdmin.objects.filter(user=self.request.user).exists()

    def get_school_admin_schools(self):
        return School.objects.filter(admins__user=self.request.user)


class SchoolAdminDashboardView(SchoolAdminMixin, TemplateView):
    template_name = "school_admin/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        schools = self.get_school_admin_schools()
        context['schools'] = schools
        context['is_superuser'] = self.request.user.is_superuser

        current_year = int(settings.CURRENT_SEASON)
        six_years_ago = str(current_year - 6)

        debaters_data = {}
        for school in schools:
            debaters = Debater.objects.filter(
                school=school,
                latest_season__gte=six_years_ago
            ).order_by('-latest_season', 'last_name', 'first_name')
            debaters_data[school.id] = debaters

        context['debaters_data'] = debaters_data
        context['six_years_ago'] = six_years_ago
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

        current_year = int(settings.CURRENT_SEASON)
        six_years_ago = str(current_year - 6)

        return Debater.objects.filter(
            school=school,
            latest_season__gte=six_years_ago
        ).order_by('-latest_season', 'last_name', 'first_name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        school_id = self.kwargs.get('school_id')
        context['school'] = get_object_or_404(School, id=school_id)
        context['six_years_ago'] = str(int(settings.CURRENT_SEASON) - 6)
        return context


class SchoolAdminDebaterForm(forms.ModelForm):
    class Meta:
        model = Debater
        fields = ('first_name', 'last_name', 'status', 'first_season', 'latest_season')

    def __init__(self, *args, school=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.user = user

        current_year = int(settings.CURRENT_SEASON)
        six_years_ago = current_year - 6
        oldest_year = 2004

        first_season_choices = [
            (str(year), f"{year}-{str(year+1)[2:]}")
            for year in range(current_year, oldest_year - 1, -1)
        ]

        latest_season_choices = [
            (str(year), f"{year}-{str(year+1)[2:]}")
            for year in range(current_year, six_years_ago - 1, -1)
        ]

        self.fields['first_season'] = forms.ChoiceField(
            choices=first_season_choices,
            initial=settings.CURRENT_SEASON
        )
        self.fields['latest_season'] = forms.ChoiceField(
            choices=latest_season_choices,
            initial=settings.CURRENT_SEASON
        )

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

        return cleaned_data


class SchoolAdminDebaterUpdateView(SchoolAdminMixin, UpdateView):
    model = Debater
    form_class = SchoolAdminDebaterForm
    template_name = "school_admin/debater_form.html"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)

        if not SchoolAdmin.objects.filter(user=self.request.user, school=obj.school).exists():
            raise Http404

        current_year = int(settings.CURRENT_SEASON)
        six_years_ago = str(current_year - 6)

        if obj.latest_season < six_years_ago:
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
        ).order_by('-active_debater_count', 'name')

        paginator = Paginator(schools, 15)
        page_number = self.request.GET.get('page')
        page_obj = paginator.get_page(page_number)

        school_data = []
        for school in page_obj:
            admins = SchoolAdmin.objects.filter(school=school).select_related('user')
            school_data.append({
                'school': school,
                'active_debater_count': school.active_debater_count,
                'admins': admins,
            })

        context['school_data'] = school_data
        context['page_obj'] = page_obj
        return context


class SchoolAdminAddView(UserPassesTestMixin, View):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

    def post(self, request, *args, **kwargs):
        school_id = request.POST.get('school_id')
        user_id = request.POST.get('user_id')

        if not school_id or not user_id:
            return JsonResponse({'error': 'Missing school_id or user_id'}, status=400)

        school = get_object_or_404(School, id=school_id)
        user = get_object_or_404(User, id=user_id)

        school_admin, created = SchoolAdmin.objects.get_or_create(
            user=user,
            school=school
        )

        return JsonResponse({
            'success': True,
            'created': created,
            'admin': {
                'id': school_admin.id,
                'user_id': user.id,
                'username': user.username,
                'email': user.email,
            }
        })


class SchoolAdminRemoveView(UserPassesTestMixin, View):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

    def post(self, request, *args, **kwargs):
        school_admin_id = request.POST.get('school_admin_id')

        if not school_admin_id:
            return JsonResponse({'error': 'Missing school_admin_id'}, status=400)

        school_admin = get_object_or_404(SchoolAdmin, id=school_admin_id)
        school_admin.delete()

        return JsonResponse({'success': True})


class UserAutocompleteView(autocomplete.Select2QuerySetView):
    def get_queryset(self):
        if not self.request.user.is_superuser:
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
