from django.urls import path
from .views_webhook import whatsapp_webhook

urlpatterns = [
    path("", whatsapp_webhook, name="whatsapp_webhook"),
]