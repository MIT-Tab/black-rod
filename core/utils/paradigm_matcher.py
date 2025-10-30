"""
Paradigm CSV Matching Utility

This module provides functionality to match paradigm CSV records against existing debater records.
It should be removed once the CSV matching is complete.
"""

import csv
from pathlib import Path
from difflib import SequenceMatcher

from django.conf import settings
from django.db.models import Q

from core.models import Debater, School


class ParadigmCSVMatcher:
    """Utility class for matching CSV paradigm records to existing debaters."""
    
    CSV_PATH = Path(settings.BASE_DIR).parent / "[PUBLIC] APDA Paradigm Project - Paradigms.csv"
    
    def __init__(self):
        self.csv_records = []
        self.matched_records = []
        self.unmatched_records = []
        self._load_csv()
    
    def _load_csv(self):
        """Load and parse the CSV file."""
        if not self.CSV_PATH.exists():
            return
        
        with open(self.CSV_PATH, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                self.csv_records.append({
                    'name': row['Judge Name'].strip(),
                    'affiliation': row['Affiliation'].strip(),
                    'class_year': row['Class Year'].strip(),
                    'paradigm_link': row['Link to Paradigm'].strip(),
                    'date_submitted': row['Date Submitted'].strip(),
                })
    
    @staticmethod
    def _similarity(a, b):
        """Calculate similarity ratio between two strings."""
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()
    
    def _parse_name(self, name):
        """Parse a name into first and last name components."""
        parts = name.strip().split()
        if len(parts) == 0:
            return '', ''
        elif len(parts) == 1:
            return parts[0], ''
        else:
            return parts[0], ' '.join(parts[1:])
    
    def find_matches(self, csv_record, threshold=0.7):
        """
        Find potential debater matches for a CSV record.
        
        Args:
            csv_record: Dict with CSV record data
            threshold: Minimum similarity score (0-1) for name matching
        
        Returns:
            List of tuples (debater, similarity_score)
        """
        first_name, last_name = self._parse_name(csv_record['name'])
        
        # First try exact match
        exact_matches = Debater.objects.filter(
            first_name__iexact=first_name,
            last_name__iexact=last_name
        ).select_related('school')
        
        if exact_matches.exists():
            return [(d, 100.0) for d in exact_matches]
        
        # Try fuzzy matching on all debaters
        candidates = []
        
        # Get debaters with similar first or last names
        potential_debaters = Debater.objects.filter(
            Q(first_name__icontains=first_name[:3]) |
            Q(last_name__icontains=last_name[:3])
        ).select_related('school')[:100]  # Limit to avoid performance issues
        
        for debater in potential_debaters:
            debater_full_name = f"{debater.first_name} {debater.last_name}".strip()
            csv_full_name = csv_record['name'].strip()
            
            similarity = self._similarity(debater_full_name, csv_full_name)
            
            # Convert to percentage for display
            if similarity >= threshold:
                candidates.append((debater, similarity * 100))
        
        # Sort by similarity score descending
        candidates.sort(key=lambda x: x[1], reverse=True)
        
        return candidates[:10]  # Return top 10 matches
    
    def get_already_matched_debaters(self):
        """Get debaters that already have a paradigm set."""
        return Debater.objects.filter(paradigm__isnull=False).values_list('id', flat=True)
    
    def get_unmatched_records(self):
        """Get CSV records that haven't been matched yet."""
        matched_ids = set(self.get_already_matched_debaters())
        
        unmatched = []
        for record in self.csv_records:
            matches = self.find_matches(record)
            # Check if any match already has a paradigm
            has_matched = any(debater.id in matched_ids for debater, _ in matches)
            if not has_matched:
                unmatched.append({
                    'csv_record': record,
                    'potential_matches': matches
                })
        
        return unmatched
    
    def apply_match(self, csv_record_name, debater_id):
        """
        Apply a paradigm from CSV to a debater.
        
        Args:
            csv_record_name: Name from CSV record
            debater_id: ID of debater to update
        
        Returns:
            Updated Debater object or None if not found
        """
        # Find the CSV record
        csv_record = None
        for record in self.csv_records:
            if record['name'] == csv_record_name:
                csv_record = record
                break
        
        if not csv_record:
            return None
        
        try:
            debater = Debater.objects.get(id=debater_id)
            debater.paradigm = csv_record['paradigm_link']
            debater.save()
            return debater
        except Debater.DoesNotExist:
            return None
