from urllib.parse import urlparse
import re
import requests
from django.conf import settings
from django.db import transaction
from core.models.debater import Debater
from core.models.school import School, SchoolLookup


class APIDataHandler:
    def __init__(self, request=None):
        self.request = request
        self._api_url = None
        self._debater_id_map = {}
        self._school_name_map = {}

    def _extract_reference_id(self, data):
        """Return the identifier used by the remote API for tracking debaters."""
        if not isinstance(data, dict):
            return None
        return data.get('tournament_id') or data.get('debater_id') or data.get('id')

    def should_use_api_data(self):
        return bool(self.request and self.get_api_url())

    def get_api_url(self):
        return self._api_url

    def set_api_url(self, api_url):
        self._api_url = self._clean_api_url(api_url)

    def clear_api_state(self):
        """Reset cached API metadata for the current handler instance."""
        self._api_url = None
        self._debater_id_map = {}

    def _clean_api_url(self, url):
        if not url:
            return None
        url = url.strip().rstrip('/')
        parsed = urlparse(url)
        if not parsed.scheme:
            url = f"https://{url}"
            parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

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
            
            result = response.json()
            
            return result
        except (requests.RequestException, ValueError):
            return None

    def get_new_schools_from_api(self):
        data = self._make_api_request('new-schools')
        if not data:
            return []
        new_school_names = data.get('new_schools', [])
        existing_school_names = set(School.objects.filter(name__in=new_school_names).values_list('name', flat=True))
        
        # Store mappings for exact matches so debaters can reference them
        existing_schools = School.objects.filter(name__in=existing_school_names)
        for school in existing_schools:
            self._school_name_map[school.name] = school.id
        
        filtered_schools = [name for name in new_school_names if name not in existing_school_names]
        return [{'name': name, 'included_in_oty': True, 'server_name': name} for name in filtered_schools]

    def get_new_debaters_from_api(self):
        data = self._make_api_request('new-debater-data')
        if not data:
            return []
        new_debater_data = data.get('new_debater_data', [])
        
        school_ids = {d.get('school_id') for d in new_debater_data if d.get('school_id') != -1}
        school_names = {d.get('school_name') for d in new_debater_data if d.get('school_id') == -1 and d.get('school_name')}
        schools_by_id = {s.id: s for s in School.objects.filter(id__in=school_ids)}
        schools_by_name = {s.name: s for s in School.objects.filter(name__in=school_names)}

        # Also check SchoolLookup for mapped names
        school_lookups = SchoolLookup.objects.filter(server_name__in=school_names).select_related('school')
        for lookup in school_lookups:
            schools_by_name[lookup.server_name] = lookup.school

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
            # Only resolve school by ID if it's a real APDA school ID (not -1)
            if school_id != -1 and school_id in schools_by_id:
                school = schools_by_id[school_id]
            # For new schools (school_id == -1), check if we have an exact match
            elif school_id == -1 and school_name:
                # First check if it's in schools_by_name (already loaded)
                if school_name in schools_by_name:
                    school = schools_by_name[school_name]
                # Then check if we already mapped it as an exact match
                elif school_name in self._school_name_map:
                    school_id_from_map = self._school_name_map[school_name]
                    try:
                        school = School.objects.get(id=school_id_from_map)
                    except School.DoesNotExist:
                        pass

            tournament_id = debater_data.get('debater_id')

            existing_match = self._find_existing_recent_debater(first_name, last_name, school)
            if existing_match and tournament_id:
                self.link_tournament_debater(tournament_id, existing_match)
                continue

            debater_dict = {
                'first_name': first_name,
                'last_name': last_name,
                'school': school,
                'school_name': school_name if school_id == -1 and not school else None,
                'tournament_id': tournament_id
            }
            debater_list.append(debater_dict)
        
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

        for data in debater_data:
            tournament_id = data.get('tournament_id')
            if data.get('school'):
                debaters_to_create.append(Debater(
                    first_name=data['first_name'],
                    last_name=data['last_name'],
                    school=data['school'],
                    first_season=settings.CURRENT_SEASON,
                    latest_season=settings.CURRENT_SEASON,
                    status=Debater.NOVICE
                ))
                debater_mapping_info.append(tournament_id)

        if not debaters_to_create:
            return 0

        created_debaters = Debater.objects.bulk_create(debaters_to_create, ignore_conflicts=False)
        if not created_debaters or not created_debaters[0].id:
            created_debaters = self._find_created_debaters(debaters_to_create)

        created_count = 0
        for i, tournament_id in enumerate(debater_mapping_info):
            if i >= len(created_debaters):
                continue
            debater_obj = created_debaters[i]
            if not debater_obj:
                continue

            created_count += 1
            if tournament_id:
                self.link_tournament_debater(tournament_id, debater_obj)

        return created_count

    def link_tournament_debater(self, tournament_id, debater):
        if not tournament_id or debater is None:
            return

        debater_id = getattr(debater, 'id', debater)
        if not debater_id:
            return

        tournament_key = str(tournament_id)
        self._debater_id_map[tournament_key] = debater_id

    def link_tournament_school(self, server_name, school):
        """Link a school name from the external server to a school in our DB."""
        if not server_name or school is None:
            return

        school_id = getattr(school, 'id', school)
        if not school_id:
            return

        self._school_name_map[server_name] = school_id

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
        for idx, team_pair in enumerate(team_placements):
            if isinstance(team_pair, list) and len(team_pair) == 2:
                debater_one_data = self._find_debater_from_ref(team_pair[0])
                debater_two_data = self._find_debater_from_ref(team_pair[1])
                tid_one = self._extract_reference_id(team_pair[0])
                tid_two = self._extract_reference_id(team_pair[1])

                team_dict = {
                    "debater_one": debater_one_data,
                    "debater_two": debater_two_data,
                    "debater_one_tournament_id": tid_one,
                    "debater_two_tournament_id": tid_two,
                }
                
                teams.append(team_dict)
        
        return teams

    def get_speakers_from_api(self, endpoint):
        data = self._make_api_request(endpoint)
        if not data:
            return []
        speaker_awards = data.get(endpoint.replace('-', '_'), [])
        speakers = []
        for speaker_data in speaker_awards:
            if isinstance(speaker_data, dict):
                debater_result = self._find_debater_from_ref(speaker_data)
                debater = debater_result
                tournament_id = self._extract_reference_id(speaker_data)
                if debater or tournament_id:
                    speakers.append({
                        "speaker": debater, 
                        "tie": False,
                        "tournament_id": tournament_id
                    })
        return speakers

    def _find_debater_from_ref(self, debater_ref):
        """
        Find a debater from API reference data.
        Returns the debater object if found.
        """
        if not isinstance(debater_ref, dict):
            return None

        apda_id = debater_ref.get('apda_id', -1)
        if apda_id not in (None, -1):
            try:
                return Debater.objects.select_related('school').get(id=apda_id)
            except Debater.DoesNotExist:
                pass

        tournament_id = self._extract_reference_id(debater_ref)
        if tournament_id:
            debater_id = self._debater_id_map.get(str(tournament_id))
            if debater_id:
                try:
                    return Debater.objects.select_related('school').get(id=debater_id)
                except Debater.DoesNotExist:
                    pass

        return None

    def _season_to_int(self, season_value):
        if not season_value:
            return None
        match = re.search(r'\d{4}', str(season_value))
        if not match:
            return None
        try:
            return int(match.group())
        except (TypeError, ValueError):
            return None

    def _is_recent_season(self, season_value):
        latest = self._season_to_int(season_value)
        current = self._season_to_int(settings.CURRENT_SEASON)
        if latest is None or current is None:
            return False
        return latest >= current - 2

    def _find_existing_recent_debater(self, first_name, last_name, school):
        if not (first_name and last_name and school):
            return None
        qs = Debater.objects.filter(
            first_name__iexact=first_name.strip(),
            last_name__iexact=last_name.strip(),
            school=school,
        )
        for debater in qs:
            if self._is_recent_season(debater.latest_season):
                return debater
        return None
