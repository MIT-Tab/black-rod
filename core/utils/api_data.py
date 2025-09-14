import requests
from urllib.parse import urlparse
from django.db import transaction
from core.models.debater import Debater
from core.models.school import School


class APIDataHandler:
    def __init__(self, request=None):
        self.request = request
        self._api_url = None
        self._debater_id_map = {}
        if self.request:
            self._debater_id_map.update(self.request.session.get('tournament_debater_mapping', {}))
    
    def should_use_api_data(self):
        return bool(self.request and self.get_api_url())
    
    def get_api_url(self):
        if self._api_url is None and self.request:
            self._api_url = self.request.session.get('tournament_api_url', '')
        return self._api_url
    
    def set_api_url(self, api_url):
        self._api_url = self._clean_api_url(api_url)
        if self.request:
            self.request.session['tournament_api_url'] = self._api_url
    
    def _clean_api_url(self, url):
        if not url:
            return None
        url = url.strip().rstrip('/')
        parsed = urlparse(url)
        if not parsed.scheme:
            url = f"https://{url}"
            parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    
    def clear_api_url(self):
        self._api_url = None
        if self.request and 'tournament_api_url' in self.request.session:
            del self.request.session['tournament_api_url']
    
    def validate_api_connection(self):
        api_url = self.get_api_url()
        if not api_url:
            return True, None
        try:
            response = requests.get(f"{api_url}/api/new-debater-data", timeout=10)
            if response.status_code in [403, 409, 423]:
                try:
                    error_data = response.json()
                    return False, error_data.get('error', 'API error occurred')
                except ValueError:
                    return False, f"API returned status {response.status_code} with invalid JSON"
            response.raise_for_status()
            return True, None
        except requests.RequestException as e:
            return False, f"Failed to connect to API: {str(e)}"
    
    def _make_api_request(self, endpoint):
        api_url = self.get_api_url()
        if not api_url:
            return None
        try:
            response = requests.get(f"{api_url}/api/{endpoint}", timeout=10)
            response.raise_for_status()
            if not response.content.strip():
                return None
            return response.json()
        except (requests.RequestException, ValueError):
            return None
    
    def get_new_schools_from_api(self):
        data = self._make_api_request('new-schools')
        if not data:
            return []
        new_school_names = data.get('new_schools', [])
        existing_school_names = set(School.objects.filter(name__in=new_school_names).values_list('name', flat=True))
        filtered_schools = [name for name in new_school_names if name not in existing_school_names]
        return [{'name': name, 'included_in_oty': True} for name in filtered_schools]
    
    def get_new_debaters_from_api(self):
        data = self._make_api_request('new-debater-data')
        if not data:
            return []
        new_debater_data = data.get('new_debater_data', [])
        school_ids = {d.get('school_id') for d in new_debater_data if d.get('school_id') != -1}
        school_names = {d.get('school_name') for d in new_debater_data if d.get('school_id') == -1 and d.get('school_name')}
        schools_by_id = {s.id: s for s in School.objects.filter(id__in=school_ids)}
        schools_by_name = {s.name: s for s in School.objects.filter(name__in=school_names)}
        
        debater_list = []
        for debater_data in new_debater_data:
            name_parts = debater_data.get('name', '').strip().split()
            if not name_parts:
                continue
            first_name = name_parts[0]
            last_name = ' '.join(name_parts[1:])
            school_id = debater_data.get('school_id')
            school_name = debater_data.get('school_name', '').strip()
            
            school = None
            if school_id != -1 and school_id in schools_by_id:
                school = schools_by_id[school_id]
            elif school_id == -1 and school_name in schools_by_name:
                school = schools_by_name[school_name]
            
            debater_list.append({
                'first_name': first_name,
                'last_name': last_name,
                'school': school,
                'tournament_id': debater_data.get('debater_id')
            })
        return debater_list
    
    @transaction.atomic
    def create_schools_from_data(self, school_data):
        if not school_data:
            return School.objects.none()
        schools_to_create = [School(name=data['name'], included_in_oty=data['included_in_oty']) for data in school_data]
        School.objects.bulk_create(schools_to_create)
        return School.objects.filter(name__in=[data['name'] for data in school_data])
    
    @transaction.atomic
    def create_debaters_from_data(self, debater_data):
        debaters_to_create = []
        debater_mapping_info = []
        existing_mapping = self.request.session.get('tournament_debater_mapping', {}) if self.request else {}
        
        for data in debater_data:
            tournament_id = data.get('tournament_id')
            if tournament_id and str(tournament_id) in existing_mapping:
                continue
            if data.get('school'):
                debaters_to_create.append(Debater(
                    first_name=data['first_name'],
                    last_name=data['last_name'],
                    school=data['school']
                ))
                debater_mapping_info.append(tournament_id)
        
        if not debaters_to_create:
            return 0
        
        created_debaters = Debater.objects.bulk_create(debaters_to_create, ignore_conflicts=False)
        if not created_debaters or not created_debaters[0].id:
            created_debaters = self._find_created_debaters(debaters_to_create)
        
        tournament_to_db_mapping = existing_mapping.copy()
        for i, tournament_id in enumerate(debater_mapping_info):
            if tournament_id and i < len(created_debaters) and created_debaters[i]:
                tournament_to_db_mapping[str(tournament_id)] = created_debaters[i].id

        if self.request and tournament_to_db_mapping:
            self.request.session['tournament_debater_mapping'] = tournament_to_db_mapping
        self._debater_id_map.update(tournament_to_db_mapping)
        return len(debaters_to_create)
    
    def _find_created_debaters(self, debaters_to_create):
        created_debaters = []
        for debater in debaters_to_create:
            try:
                found_debater = Debater.objects.get(
                    first_name=debater.first_name,
                    last_name=debater.last_name,
                    school=debater.school
                )
                created_debaters.append(found_debater)
            except Debater.DoesNotExist:
                created_debaters.append(None)
            except Debater.MultipleObjectsReturned:
                found_debater = Debater.objects.filter(
                    first_name=debater.first_name,
                    last_name=debater.last_name,
                    school=debater.school
                ).order_by('-id').first()
                created_debaters.append(found_debater)
        return created_debaters
    
    def get_teams_from_api(self, endpoint):
        data = self._make_api_request(endpoint)
        if not data:
            return []
        team_placements = data.get(endpoint.replace('-', '_'), [])
        teams = []
        for team_pair in team_placements:
            if isinstance(team_pair, list) and len(team_pair) == 2:
                teams.append({
                    "debater_one": self._find_debater_from_ref(team_pair[0]),
                    "debater_two": self._find_debater_from_ref(team_pair[1]),
                })
        return teams
    
    def get_speakers_from_api(self, endpoint):
        data = self._make_api_request(endpoint)
        if not data:
            return []
        speaker_awards = data.get(endpoint.replace('-', '_'), [])
        speakers = []
        for speaker_data in speaker_awards:
            if isinstance(speaker_data, dict):
                debater = self._find_debater_from_ref(speaker_data)
                if debater:
                    speakers.append({"speaker": debater, "tie": False})
        return speakers
    
    def _find_debater_from_ref(self, debater_ref):
        if not isinstance(debater_ref, dict):
            return None
        
        apda_id = debater_ref.get('apda_id', -1)
        if apda_id != -1:
            try:
                return Debater.objects.select_related('school').get(id=apda_id)
            except Debater.DoesNotExist:
                pass
        
        tournament_id = debater_ref.get('tournament_id')
        if tournament_id:
            debater_id = (self._debater_id_map.get(str(tournament_id)) or 
                         (self.request and self.request.session.get('tournament_debater_mapping', {}).get(str(tournament_id))))
            if debater_id:
                try:
                    return Debater.objects.select_related('school').get(id=debater_id)
                except Debater.DoesNotExist:
                    pass
        return None
