from django.views.generic import TemplateView
from django.contrib.auth.mixins import UserPassesTestMixin
from django.http import JsonResponse
import os
import requests
import re
from bs4 import BeautifulSoup

from core.models import Team, Debater, TOTY, NOTY, SOTY
from core.utils.rankings import update_toty, update_soty, update_noty, redo_rankings
from apda.settings.season_settings import SEASONS


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


class RankingsRecomputeView(UserPassesTestMixin, TemplateView):
    template_name = 'admin/rankings_recompute.html'
    
    def test_func(self):
        return self.request.user.is_superuser
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['seasons'] = SEASONS
        context['ranking_types'] = [
            ('toty', 'TOTY'),
            ('soty', 'SOTY'),
            ('noty', 'NOTY'),
        ]
        return context
    
    def post(self, request, *args, **kwargs):
        season = request.POST.get('season')
        ranking_type = request.POST.get('ranking_type')
        
        if not season or not ranking_type:
            return JsonResponse({'success': False, 'error': 'Season and ranking type are required'})
        
        try:
            ranking_funcs = {
                'toty': lambda: self._update_toty_rankings(season),
                'soty': lambda: self._update_soty_rankings(season), 
                'noty': lambda: self._update_noty_rankings(season)
            }
            
            if ranking_type in ranking_funcs:
                ranking_funcs[ranking_type]()
                return JsonResponse({
                    'success': True, 
                    'message': f'Successfully recomputed {ranking_type.upper()} rankings for season {season}'
                })
            
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    def _update_toty_rankings(self, season):
        for team in Team.objects.all():
            update_toty(team, season=season)
        redo_rankings(TOTY.objects.filter(season=season), season=season, cache_type='toty')
    
    def _update_soty_rankings(self, season):
        for debater in Debater.objects.all():
            update_soty(debater, season=season)
        redo_rankings(SOTY.objects.filter(season=season), season=season, cache_type='soty')
    
    def _update_noty_rankings(self, season):
        for debater in Debater.objects.all():
            update_noty(debater, season=season)