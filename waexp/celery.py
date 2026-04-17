# waexp/celery.py
import os
from celery import Celery
os.environ.setdefault("DJANGO_SETTINGS_MODULE","waexp.settings.dev")
app = Celery("waexp")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()