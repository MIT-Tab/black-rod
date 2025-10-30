"""
Management command to export unmatched paradigm CSV records.
"""

import csv
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings

from core.utils.paradigm_matcher import ParadigmCSVMatcher


class Command(BaseCommand):
    help = 'Export unmatched paradigm CSV records to a new CSV file'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output',
            type=str,
            default='unmatched_paradigms.csv',
            help='Output CSV filename (default: unmatched_paradigms.csv)',
        )

    def handle(self, *args, **options):
        output_file = options['output']
        
        # Initialize matcher
        matcher = ParadigmCSVMatcher()
        
        self.stdout.write(f'Loaded {len(matcher.csv_records)} CSV records')
        
        # Get unmatched records
        unmatched = matcher.get_unmatched_records()
        
        # Count records with no matches
        no_matches = [r for r in unmatched if not r['potential_matches']]
        
        self.stdout.write(f'Found {len(no_matches)} records with no matches')
        
        # Write to CSV
        output_path = Path(settings.BASE_DIR).parent / output_file
        
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Name', 'School/Affiliation', 'Class Year', 'Paradigm Link', 'Date Submitted'])
            
            for record in no_matches:
                csv_rec = record['csv_record']
                writer.writerow([
                    csv_rec['name'],
                    csv_rec['affiliation'],
                    csv_rec['class_year'],
                    csv_rec['paradigm_link'],
                    csv_rec['date_submitted'],
                ])
        
        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully exported {len(no_matches)} unmatched records to {output_path}'
            )
        )
        
        # Also show records with matches for reference
        with_matches = [r for r in unmatched if r['potential_matches']]
        self.stdout.write(f'Note: {len(with_matches)} records have potential matches (not exported)')
