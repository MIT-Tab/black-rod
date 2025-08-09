from django.views.generic import TemplateView
from django.contrib.auth.mixins import UserPassesTestMixin
import os
import requests
import re
from bs4 import BeautifulSoup

from core.utils.generics import CustomMixin


class AdminToolsView(UserPassesTestMixin, TemplateView):
    template_name = 'admin/admin_tools.html'
    
    def test_func(self):
        return self.request.user.is_superuser

class MitTabDashboardView(UserPassesTestMixin, TemplateView):
    template_name = 'admin/mittab_dashboard.html'
    
    def test_func(self):
        return self.request.user.is_superuser
    
    def get_tournament_data(self):
        nu_tab_url = os.environ.get('NU_TAB_URL', 'https://nu-tab.com')
        tournaments = []
        error_message = None
        
        try:
            response = requests.get(nu_tab_url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            content_div = soup.find('div', {'id': 'content'})
            
            if not content_div:
                error_message = "Content div not found in the response"
                return tournaments, error_message
            
            links = content_div.find_all('a', href=True)
            
            for link in links:
                href = link.get('href', '')
                text = link.get_text(strip=True)
                
                match = re.match(r'^(.*?)\.nu-tab\.com$', text)
                if match:
                    tournament_name = match.group(1)
                    if href.startswith('http'):
                        tournament_url = href
                    else:
                        tournament_url = f"http://{text}"
                    
                    tournaments.append({
                        'name': tournament_name,
                        'url': tournament_url
                    })
            
        except requests.RequestException as e:
            error_message = f"Failed to fetch data from nu-tab.com: {str(e)}"
        except Exception as e:
            error_message = f"Error parsing tournament data: {str(e)}"
        
        return tournaments, error_message
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tournaments, error_message = self.get_tournament_data()
        
        context['tournaments'] = tournaments
        context['error_message'] = error_message
        context['nu_tab_url'] = os.environ.get('NU_TAB_URL', 'https://nu-tab.com')
        
        return context