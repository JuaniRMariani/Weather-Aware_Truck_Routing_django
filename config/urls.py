from django.urls import path

from routing.api import health, route_plan

urlpatterns = [path("api/health/", health), path("api/route-plans/", route_plan)]
