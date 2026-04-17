# ingestion/api/views.py
import json, os, requests
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from expenses.models import Expense

VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN","changeme")

@csrf_exempt
def whatsapp_webhook(request):
    print('entro aca entonces?')
    if request.method == "GET":
        # Verificación Webhook de Meta
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return HttpResponse(challenge, status=200)
        return HttpResponse("Forbidden", status=403)

    if request.method == "POST":
        payload = json.loads(request.body.decode("utf-8"))
        # TODO: parsear mensajes y, si traen media, crear Expense(pending) con wa_message_id
        # y encolar tarea Celery para descargar el media por media_id.
        return JsonResponse({"status": "ok"})