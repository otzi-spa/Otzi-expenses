# ingestion/views_webhook.py
import json, requests
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from datetime import datetime, timezone as dt_timezone
from django.core.files.base import ContentFile
from expenses.models import Expense, Attachment, AllowedSender, ExpenseAuditLog, ExpenseTypeCatalog
import hashlib, mimetypes

GRAPH_URL = "https://graph.facebook.com/v24.0"

# estado por teléfono
user_states = {}  # { phone: {"stage": "...", "expense_id": 123} }

def norm(s: str) -> str:
    return (s or "").strip().lower()

def parse_choice(text, mapping):
    """
    mapping: dict[str, str] donde keys pueden ser "1","2","3" o "boleta"
    """
    t = norm(text)
    return mapping.get(t)

def get_active_expense_types():
    return list(
        ExpenseTypeCatalog.objects.filter(is_active=True)
        .order_by("name")
        .values_list("name", flat=True)
    )

def build_expense_type_prompt(expense_types):
    lines = ["🧾 ¿Qué tipo de gasto es?"]
    for idx, name in enumerate(expense_types, start=1):
        lines.append(f"{idx}) {name}")
    lines.append("")
    lines.append("Responde con el número o el nombre de la opción.")
    return "\n".join(lines)

def parse_expense_type_choice(text, expense_types):
    t = norm(text)
    if not t:
        return None
    if t.isdigit():
        idx = int(t)
        if 1 <= idx <= len(expense_types):
            return expense_types[idx - 1]
    by_name = {norm(name): name for name in expense_types}
    return by_name.get(t)


def log_whatsapp_event(expense: Expense, action: str, changes=None, reason: str = ""):
    ExpenseAuditLog.objects.create(
        expense=expense,
        expense_snapshot_id=expense.id,
        action=action,
        actor=None,
        actor_name="OtziBot",
        source="whatsapp",
        reason=reason,
        changes=changes or {},
    )

@csrf_exempt
def whatsapp_webhook(request):
    print('entro aca')
    # Verificación Meta (GET)
    if request.method == "GET":
        verify_token = settings.VERIFY_TOKEN
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")
        if mode == "subscribe" and token == verify_token:
            return HttpResponse(challenge)
        return HttpResponse(status=403)

    # Eventos (POST)
    payload = json.loads(request.body.decode("utf-8"))
    print("📩 WhatsApp event:", json.dumps(payload, indent=2))

    try:
        entry = payload["entry"][0]["changes"][0]["value"]
        phone_number_id = entry["metadata"]["phone_number_id"]

        if "messages" not in entry:
            return HttpResponse(status=200)

        message = entry["messages"][0]
        from_number = message["from"]
        msg_type = message["type"]

        # Solo procesar números autorizados
        sender = AllowedSender.objects.filter(phone=from_number, active=True, is_deleted=False).first()
        if not sender:
            print(f"🚫 Número no autorizado: {from_number}")
            send_whatsapp_reply(phone_number_id, from_number,
                "Hola, soy OtziBot, un bot hecho para ayudarte con la rendición de gastos. "
                "Por ahora no estás autorizado para enviar gastos. "
                "Si crees que es un error, contacta a un administrador.")
            return HttpResponse(status=200)

        # 1) Llega imagen: crear Expense y preguntar tipo documento
        if msg_type == "image":
            image_id = message["image"]["id"]
            timestamp = int(message["timestamp"])
            msg_dt = datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)

            exp = Expense.objects.create(
                wa_message_id=message["id"],
                wa_sender_phone=from_number,
                wa_sender=sender,
                wa_media_id=image_id,
                message_sent_at=msg_dt,
                status="pending",
                created_by_id=1,  # 👈 por ahora fijo
            )
            log_whatsapp_event(
                exp,
                action="created",
                changes={
                    "status": {"before": None, "after": exp.status},
                    "source": {"before": None, "after": exp.source},
                    "worksite": {"before": None, "after": exp.worksite},
                },
                reason="Gasto creado desde imagen WhatsApp",
            )
            download_media_attachment(image_id, exp)

            user_states[from_number] = {"stage": "awaiting_doc_type", "expense_id": exp.id}

            send_whatsapp_reply(
                phone_number_id, from_number,
                "📄 ¿Qué tipo de documento es?\n"
                "1) Boleta\n2) Factura\n3) Vale\n\nResponde con 1, 2 o 3."
            )
            return HttpResponse(status=200)

        # 2) Llega texto: avanzar flujo según stage
        if msg_type == "text":
            body = message["text"]["body"]
            state = user_states.get(from_number)

            if not state:
                send_whatsapp_reply(phone_number_id, from_number,
                    "👋 Para ingresar un gasto, envíame primero una foto (boleta/factura/vale)."
                )
                return HttpResponse(status=200)

            exp = Expense.objects.filter(id=state.get("expense_id")).first()
            if not exp:
                user_states.pop(from_number, None)
                send_whatsapp_reply(phone_number_id, from_number,
                    "⚠️ No encontré el gasto en curso. Por favor envía la foto nuevamente."
                )
                return HttpResponse(status=200)

            stage = state.get("stage")

            # A) Tipo documento
            if stage == "awaiting_doc_type":
                doc = parse_choice(body, {
                    "1": "boleta", "boleta": "boleta",
                    "2": "factura", "factura": "factura",
                    "3": "vale", "vale": "vale",
                })
                if not doc:
                    send_whatsapp_reply(phone_number_id, from_number,
                        "❌ No entendí. Responde con 1) Boleta, 2) Factura o 3) Vale."
                    )
                    return HttpResponse(status=200)

                exp.document_type = doc
                exp.save(update_fields=["document_type"])
                log_whatsapp_event(
                    exp,
                    action="whatsapp_update",
                    changes={"document_type": {"before": None, "after": doc}},
                    reason="Usuario indicó tipo de documento",
                )

                user_states[from_number]["stage"] = "awaiting_worksite"
                send_whatsapp_reply(phone_number_id, from_number, "🏗️ ¿Para qué obra/proyecto es este gasto?")
                return HttpResponse(status=200)

            # B) Obra (texto libre)
            if stage == "awaiting_worksite":
                exp.worksite = body.strip()
                exp.save(update_fields=["worksite"])
                log_whatsapp_event(
                    exp,
                    action="whatsapp_update",
                    changes={"worksite": {"before": None, "after": exp.worksite}},
                    reason="Usuario indicó obra reportada",
                )

                user_states[from_number]["stage"] = "awaiting_is_vehicle"
                send_whatsapp_reply(
                    phone_number_id, from_number,
                    "🚘 ¿Es para vehículo?\n1) Sí\n2) No\n\nResponde 1 o 2."
                )
                return HttpResponse(status=200)

            # C) ¿Vehículo? (sí/no)
            if stage == "awaiting_is_vehicle":
                yn = parse_choice(body, {
                    "1": "yes", "si": "yes", "sí": "yes",
                    "2": "no",  "no": "no",
                })
                if not yn:
                    send_whatsapp_reply(phone_number_id, from_number,
                        "❌ Responde 1) Sí o 2) No."
                    )
                    return HttpResponse(status=200)

                if yn == "yes":
                    exp.is_vehicle = True
                    exp.save(update_fields=["is_vehicle"])
                    log_whatsapp_event(
                        exp,
                        action="whatsapp_update",
                        changes={"is_vehicle": {"before": False, "after": True}},
                        reason="Usuario marcó gasto de vehículo",
                    )

                    user_states[from_number]["stage"] = "awaiting_vehicle"
                    send_whatsapp_reply(phone_number_id, from_number,
                        "🚚 ¿Cuál vehículo es? (texto libre por ahora)"
                    )
                    return HttpResponse(status=200)

                # No vehículo → tipo gasto
                exp.is_vehicle = False
                exp.save(update_fields=["is_vehicle"])
                log_whatsapp_event(
                    exp,
                    action="whatsapp_update",
                    changes={"is_vehicle": {"before": True, "after": False}},
                    reason="Usuario indicó que no es gasto de vehículo",
                )

                user_states[from_number]["stage"] = "awaiting_expense_type"
                expense_types = get_active_expense_types()
                if not expense_types:
                    user_states[from_number]["stage"] = "done"
                    send_whatsapp_reply(
                        phone_number_id,
                        from_number,
                        "✅ Gasto registrado. No hay tipos de gasto activos en el mantenedor por ahora.",
                    )
                    return HttpResponse(status=200)
                send_whatsapp_reply(phone_number_id, from_number, build_expense_type_prompt(expense_types))
                return HttpResponse(status=200)

            # D) Vehículo (texto libre)
            if stage == "awaiting_vehicle":
                exp.vehicle = body.strip()
                exp.save(update_fields=["vehicle"])
                log_whatsapp_event(
                    exp,
                    action="whatsapp_update",
                    changes={"vehicle": {"before": None, "after": exp.vehicle}},
                    reason="Usuario indicó vehículo",
                )

                user_states[from_number]["stage"] = "done"
                send_whatsapp_reply(phone_number_id, from_number, "✅ Gasto registrado. ¡Gracias!")
                return HttpResponse(status=200)

            # E) Tipo gasto
            if stage == "awaiting_expense_type":
                expense_types = get_active_expense_types()
                if not expense_types:
                    user_states[from_number]["stage"] = "done"
                    send_whatsapp_reply(
                        phone_number_id,
                        from_number,
                        "✅ Gasto registrado. No hay tipos de gasto activos en el mantenedor por ahora.",
                    )
                    return HttpResponse(status=200)
                et = parse_expense_type_choice(body, expense_types)
                if not et:
                    send_whatsapp_reply(phone_number_id, from_number, "❌ Opción no válida.\n" + build_expense_type_prompt(expense_types))
                    return HttpResponse(status=200)

                exp.expense_type = et
                exp.expense_type_other = None
                exp.save(update_fields=["expense_type", "expense_type_other"])
                log_whatsapp_event(
                    exp,
                    action="whatsapp_update",
                    changes={"expense_type": {"before": None, "after": et}},
                    reason="Usuario indicó tipo de gasto",
                )

                user_states[from_number]["stage"] = "done"
                send_whatsapp_reply(phone_number_id, from_number,
                    "✅ Gasto registrado. ¡Gracias!"
                )
                return HttpResponse(status=200)

            # fallback
            send_whatsapp_reply(phone_number_id, from_number,
                "👋 Si quieres ingresar un gasto nuevo, envíame una foto."
            )
            return HttpResponse(status=200)

    except Exception as e:
        print("❌ Error procesando webhook:", e)

    return HttpResponse(status=200)

def download_media_attachment(media_id: str, expense: Expense):
    meta = requests.get(
        f"{GRAPH_URL}/{media_id}",
        params={"access_token": settings.WA_TEMPORARY_TOKEN},
        timeout=10,
    )
    if meta.status_code != 200:
        print(f"⚠️ No se pudo obtener metadata media {media_id}: {meta.status_code} {meta.text}")
        return

    url = meta.json().get("url")
    if not url:
        print(f"⚠️ Metadata de media {media_id} sin url")
        return

    media_resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {settings.WA_TEMPORARY_TOKEN}"},
        timeout=20,
    )
    if media_resp.status_code != 200:
        print(f"⚠️ No se pudo descargar media {media_id}: {media_resp.status_code} {media_resp.text}")
        return

    content = media_resp.content
    content_type = media_resp.headers.get("Content-Type", "")
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else ".bin"
    filename = f"wa_{media_id}{ext or ''}"

    attachment = Attachment(
        expense=expense,
        content_type=content_type,
        checksum_sha256=hashlib.sha256(content).hexdigest(),
    )
    attachment.file.save(filename, ContentFile(content), save=False)
    attachment.save()
    print(f"📥 Media {media_id} guardada como attachment {attachment.id}")


def send_whatsapp_reply(phone_number_id, to_number, message):
    url = f"{GRAPH_URL}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WA_TEMPORARY_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message},
    }
    resp = requests.post(url, headers=headers, json=data)
    print(f"📤 Respuesta enviada ({resp.status_code}): {resp.text}")
