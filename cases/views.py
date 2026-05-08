from django.shortcuts import render, redirect
from django.http import HttpResponsePermanentRedirect
from .models import Case
from .redirect_map import LEGACY_CASE_MAP

def index(request):
    return render(request, "index.html")

def docs(request):
    return render(request, "docs.html")

def legacy_case_redirect(request, legacy_id):
    """
    Handle 301 redirects for legacy numeric case URLs.
    
    Prioritizes canonical slugs and stable case IDs to ensure 
    one source of truth per fact (verified by Bishop).
    """
    target = LEGACY_CASE_MAP.get(str(legacy_id))
    if not target:
        return render(request, "index.html")

    # 1. Try exact slug match (fastest and most canonical)
    case = Case.objects.filter(slug=target).first()
    
    if not case:
        # 2. Try exact case_id match
        case = Case.objects.filter(case_id=target).first()
        
    if not case:
        # 3. Try searching for the case by key in court_cases or title
        # This handles legacy mappings that use Case Keys or Titles
        case = Case.objects.filter(court_cases__icontains=target).first()
        if not case:
            case = Case.objects.filter(title__icontains=target).first()
            
    if case:
        if case.slug:
            return HttpResponsePermanentRedirect(f"/case/{case.slug}")
        return HttpResponsePermanentRedirect(f"/case/{case.case_id}")
            
    # Final fallback: if target looks like a slug/path but case not found in DB
    # We still try to redirect to it to let the frontend handle the 404
    if "-" in target or target.startswith("case-"):
        return HttpResponsePermanentRedirect(f"/case/{target}")

    return render(request, "index.html")
