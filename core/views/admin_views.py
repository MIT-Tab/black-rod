from django.views.generic import TemplateView
from django.contrib.auth.mixins import UserPassesTestMixin

from core.utils.generics import CustomMixin


class AdminToolsView(UserPassesTestMixin, TemplateView):
    template_name = 'admin/admin_tools.html'
    
    def test_func(self):
        return self.request.user.is_superuser
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = 'Admin Tools'
        return context
