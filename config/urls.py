from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView
from django.conf import settings


urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("fuel.urls")),
    path('favicon.ico', RedirectView.as_view(url=settings.STATIC_URL + 'favicon.ico')),
]
