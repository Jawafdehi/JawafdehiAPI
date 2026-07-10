from django.shortcuts import redirect, render


def index(request):
    return render(request, "index.html")


def docs(request):
    # The API reference is the auto-generated OpenAPI schema (drf-spectacular),
    # served as Swagger UI at /api/swagger/ (schema at /api/schema/). The old
    # hand-written docs.html advertised nonexistent /api/allegations/* routes, so
    # /docs/ now redirects to the live, always-accurate Swagger UI.
    return redirect("swagger-ui", permanent=False)
