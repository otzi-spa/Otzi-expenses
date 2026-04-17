from django.urls import path
from .views import me

urlpatterns = [
    path("auth/me/", me, name="accounts-me"),
]