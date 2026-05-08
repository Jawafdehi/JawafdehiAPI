from django.shortcuts import render, redirect
from django.http import HttpResponsePermanentRedirect
from .models import Case
from .redirect_map import LEGACY_CASE_MAP
import logging

logger = logging.getLogger(__name__)

def index(request):
    return render(request, 'index.html')

def docs(request):
    return render(request, 'docs.html')

def legacy_case_redirect(request, legacy_id):
    """
    Handle 301 redirects for legacy numeric case URLs.
    """
    case_key = LEGACY_CASE_MAP.get(str(legacy_id))
    if case_key:
        try:
            # Try to find by partial title match first (safest across DBs)
            case = Case.objects.filter(title__icontains=case_key).first()
            
            # If not found by title, try searching court_cases (JSONField)
            if not case:
                # In Postgres, icontains on JSONField might fail. 
                # We use a string-based search if possible or just skip.
                case = Case.objects.filter(court_cases__icontains=case_key).first()
                
            if case and case.slug:
                return HttpResponsePermanentRedirect(f'/case/{case.slug}')
            elif case:
                return HttpResponsePermanentRedirect(f'/case/{case.case_id}')
        except Exception as e:
            logger.error(f'Error in legacy_case_redirect for {legacy_id}: {e}')
            
    # Fallback: serve index.html with 200 (frontend will handle routing)
    return render(request, 'index.html')