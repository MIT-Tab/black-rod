"""
Admin-only views for paradigm CSV matching.
Temporary utility - should be removed after CSV matching is complete.
"""

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.db import transaction

from core.utils.paradigm_matcher import ParadigmCSVMatcher
from core.models import Debater


@staff_member_required
def paradigm_matcher_view(request):
    """
    Admin-only view for fuzzy matching CSV paradigm records to debaters.
    Shows unmatched CSV entries with potential debater matches.
    """
    matcher = ParadigmCSVMatcher()
    
    # Get statistics
    total_csv_records = len(matcher.csv_records)
    already_matched = len(matcher.get_already_matched_debaters())
    unmatched_records = matcher.get_unmatched_records()
    
    # Separate records with matches from those without
    records_with_matches = [r for r in unmatched_records if r['potential_matches']]
    records_without_matches = [r for r in unmatched_records if not r['potential_matches']]
    
    context = {
        'total_csv_records': total_csv_records,
        'already_matched': already_matched,
        'records_with_matches': records_with_matches,
        'records_without_matches': records_without_matches,
    }
    
    return render(request, 'admin/paradigm_matcher.html', context)


@staff_member_required
@require_POST
def apply_paradigm_match(request):
    """
    Apply a paradigm from CSV to a debater.
    """
    csv_name = request.POST.get('csv_name')
    debater_id = request.POST.get('debater_id')
    
    if not csv_name or not debater_id:
        return JsonResponse({'success': False, 'error': 'Missing parameters'})
    
    try:
        matcher = ParadigmCSVMatcher()
        debater = matcher.apply_match(csv_name, debater_id)
        
        if debater:
            return JsonResponse({
                'success': True,
                'message': f'Paradigm applied to {debater.first_name} {debater.last_name}'
            })
        else:
            return JsonResponse({'success': False, 'error': 'Debater not found'})
    
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@staff_member_required
@require_POST
def skip_paradigm_match(request):
    """
    Mark a CSV record as skipped (no match found).
    Note: This is temporary - we're not persisting skips since the tool is temporary.
    """
    return JsonResponse({'success': True, 'message': 'Record skipped'})
