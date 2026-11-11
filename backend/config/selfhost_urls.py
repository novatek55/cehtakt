from django.db import connection
from django.http import HttpResponse
from django.urls import path

from .urls import urlpatterns as source_patterns


def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("OK\n", content_type="text/plain")


core_routes = {
    "auth", "products", "processes", "ingredients", "areas", "company",
    "users", "roles", "reference", "stock", "production", "reports",
    "tasks", "produce", "sales", "purchases",
}
disabled_segments = {"marketplaces", "marketplace-stock", "ocr"}
urlpatterns = [path("healthz/", health)]
for route in source_patterns:
    segments = str(route.pattern).strip("/").split("/")
    if len(segments) >= 2 and segments[0] == "api" and segments[1] in core_routes:
        if disabled_segments.isdisjoint(segments):
            urlpatterns.append(route)
