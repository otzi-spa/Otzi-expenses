# ingestion/views_webhook.py
import json, requests
from decimal import Decimal, InvalidOperation
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.utils import timezone
from datetime import datetime, timedelta, timezone as dt_timezone
from django.core.files.base import ContentFile
from expenses.models import (
    Expense,
    Attachment,
    AllowedSender,
    CategoryCatalog,
    ExpenseAuditLog,
    WhatsAppExpenseConversation,
)
from expenses.whatsapp_notifications import expense_trace_id
import hashlib, mimetypes

GRAPH_URL = "https://graph.facebook.com/v24.0"

# estado por teléfono
user_states = {}  # { phone: {"stage": "...", "expense_id": 123} }


def _conversation_state(conversation):
    return {
        "stage": conversation.stage,
        "expense_id": conversation.expense_id,
        "conversation_id": conversation.id,
        **(conversation.context or {}),
    }


def start_conversation(sender, phone, expense):
    conversation = WhatsAppExpenseConversation.objects.create(
        expense=expense,
        sender=sender,
        phone=phone,
        stage="awaiting_doc_type",
    )
    user_states[phone] = _conversation_state(conversation)
    return conversation


def get_conversation_state(phone):
    conversation = (
        WhatsAppExpenseConversation.objects.filter(phone=phone, is_active=True)
        .select_related("expense")
        .order_by("-created_at")
        .first()
    )
    if not conversation:
        user_states.pop(phone, None)
        return None
    state = _conversation_state(conversation)
    user_states[phone] = state
    return state


def update_conversation_state(phone, stage=None, **context_updates):
    conversation = (
        WhatsAppExpenseConversation.objects.filter(phone=phone, is_active=True)
        .order_by("-created_at")
        .first()
    )
    if not conversation:
        return None
    if stage is not None:
        conversation.stage = stage
    context = dict(conversation.context or {})
    context.update(context_updates)
    conversation.context = context
    conversation.save(update_fields=["stage", "context", "updated_at"])
    user_states[phone] = _conversation_state(conversation)
    return conversation


def finish_conversation(phone, stage="done"):
    conversation = (
        WhatsAppExpenseConversation.objects.filter(phone=phone, is_active=True)
        .order_by("-created_at")
        .first()
    )
    if not conversation:
        return None
    conversation.stage = stage
    conversation.is_active = False
    conversation.completed_at = timezone.now()
    conversation.save(update_fields=["stage", "is_active", "completed_at", "updated_at"])
    user_states[phone] = _conversation_state(conversation)
    return conversation

def norm(s: str) -> str:
    return (s or "").strip().lower()

def parse_choice(text, mapping):
    """
    mapping: dict[str, str] donde keys pueden ser "1","2","3" o "boleta"
    """
    t = norm(text)
    return mapping.get(t)


def parse_nonnegative_decimal(text):
    value = (text or "").strip().lower()
    for suffix in ("kilómetros", "kilometros", "kms", "km", "litros", "litro", "lts", "lt", "l"):
        if value.endswith(suffix):
            value = value[: -len(suffix)].strip()
            break
    value = value.replace(" ", "").replace(",", ".")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed >= 0 else None


def stage_prompt(stage):
    prompts = {
        "awaiting_doc_type": (
            "📄 ¿Qué tipo de documento es?\n"
            "1) Boleta\n2) Factura\n3) Vale\n\nResponde con 1, 2 o 3."
        ),
        "awaiting_worksite": "🏗️ ¿Para qué obra/proyecto es este gasto?",
        "awaiting_expense_scope": (
            "🚘 ¿A qué corresponde este gasto?\n"
            "1) Vehículo o equipo\n"
            "2) Combustible\n"
            "3) No corresponde a vehículo\n\n"
            "Responde 1, 2 o 3."
        ),
        "awaiting_vehicle": "🚚 ¿Cuál es el vehículo o equipo?",
        "awaiting_fuel_km": (
            "🛣️ ¿Cuál era el kilometraje al momento del carguío?\n"
            "Responde solo con el número, sin separador de miles."
        ),
        "awaiting_fuel_liters": (
            "⛽ ¿Cuántos litros de combustible cargó?\n"
            "Puedes usar coma o punto para los decimales."
        ),
        "awaiting_comment": "💬 Para finalizar, agrega un comentario sobre este gasto.",
    }
    return prompts.get(stage, "Continúa respondiendo la pregunta pendiente del gasto.")


def request_resume_confirmation(phone_number_id, from_number, conversation):
    if conversation.stage != "awaiting_resume":
        update_conversation_state(
            from_number,
            stage="awaiting_resume",
            resume_stage=conversation.stage,
        )
    resume_stage = (conversation.context or {}).get("resume_stage") or conversation.stage
    send_whatsapp_reply(
        phone_number_id,
        from_number,
        "⚠️ Tienes un gasto incompleto. Quedó pendiente este campo:\n\n"
        f"{stage_prompt(resume_stage)}\n\n"
        "¿Deseas seguir completándolo?\n1) Sí\n2) No",
    )


def conversation_needs_resume_confirmation(conversation):
    if conversation.stage == "awaiting_resume":
        return False
    inactivity_minutes = getattr(settings, "WHATSAPP_RESUME_AFTER_MINUTES", 30)
    return conversation.updated_at <= timezone.now() - timedelta(minutes=inactivity_minutes)


def reporter_label(sender):
    full_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
    return full_name or sender.phone


def request_final_comment(phone_number_id, from_number):
    update_conversation_state(from_number, stage="awaiting_comment")
    send_whatsapp_reply(
        phone_number_id,
        from_number,
        "💬 Para finalizar, agrega un comentario sobre este gasto.",
    )


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


def extract_supported_whatsapp_media(message):
    msg_type = message.get("type")
    if msg_type == "image":
        image = message.get("image") or {}
        media_id = image.get("id")
        if not media_id:
            return None
        return {
            "id": media_id,
            "kind": "imagen",
            "content_type": image.get("mime_type") or "",
            "filename": "",
        }
    if msg_type == "document":
        document = message.get("document") or {}
        media_id = document.get("id")
        mime_type = (document.get("mime_type") or "").split(";")[0].strip().lower()
        filename = document.get("filename") or ""
        if not media_id:
            return None
        if mime_type != "application/pdf" and not filename.lower().endswith(".pdf"):
            return {"unsupported": True, "kind": "documento"}
        return {
            "id": media_id,
            "kind": "PDF",
            "content_type": document.get("mime_type") or "application/pdf",
            "filename": filename,
        }
    return None


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

        active_conversation = (
            WhatsAppExpenseConversation.objects.filter(phone=from_number, is_active=True)
            .select_related("expense")
            .order_by("-created_at")
            .first()
        )

        media = extract_supported_whatsapp_media(message)
        if media and media.get("unsupported"):
            send_whatsapp_reply(
                phone_number_id,
                from_number,
                "Por ahora puedo recibir fotos o PDF del comprobante. Envía el comprobante nuevamente en uno de esos formatos.",
            )
            return HttpResponse(status=200)

        # 1) Llega comprobante: crear Expense y preguntar tipo documento
        if media:
            if active_conversation:
                request_resume_confirmation(phone_number_id, from_number, active_conversation)
                return HttpResponse(status=200)

            media_id = media["id"]
            timestamp = int(message["timestamp"])
            msg_dt = datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)

            exp = Expense.objects.create(
                wa_message_id=message["id"],
                wa_sender_phone=from_number,
                wa_sender=sender,
                wa_media_id=media_id,
                wa_phone_number_id=phone_number_id,
                message_sent_at=msg_dt,
                status="incomplete",
                source="whatsapp",
            )
            log_whatsapp_event(
                exp,
                action="created",
                changes={
                    "status": {"before": None, "after": exp.status},
                    "source": {"before": None, "after": exp.source},
                    "worksite": {"before": None, "after": exp.worksite},
                },
                reason=f"Gasto creado desde {media['kind']} WhatsApp",
            )
            download_media_attachment(media_id, exp)

            start_conversation(sender, from_number, exp)

            send_whatsapp_reply(
                phone_number_id, from_number,
                "📄 ¿Qué tipo de documento es?\n"
                "1) Boleta\n2) Factura\n3) Vale\n\nResponde con 1, 2 o 3."
            )
            return HttpResponse(status=200)

        # 2) Llega texto: avanzar flujo según stage
        if msg_type == "text":
            body = message["text"]["body"]

            if active_conversation and conversation_needs_resume_confirmation(active_conversation):
                request_resume_confirmation(phone_number_id, from_number, active_conversation)
                return HttpResponse(status=200)

            state = get_conversation_state(from_number)

            if not state:
                send_whatsapp_reply(phone_number_id, from_number,
                    "👋 Para ingresar un gasto, envíame primero una foto o PDF del comprobante (boleta/factura/vale)."
                )
                return HttpResponse(status=200)

            exp = Expense.objects.filter(id=state.get("expense_id")).first()
            if not exp:
                WhatsAppExpenseConversation.objects.filter(phone=from_number, is_active=True).update(
                    is_active=False,
                    stage="missing_expense",
                    completed_at=timezone.now(),
                )
                user_states.pop(from_number, None)
                send_whatsapp_reply(phone_number_id, from_number,
                    "⚠️ No encontré el gasto en curso. Por favor envía el comprobante nuevamente."
                )
                return HttpResponse(status=200)

            stage = state.get("stage")

            if stage == "awaiting_resume":
                resume_choice = parse_choice(
                    body,
                    {
                        "1": "yes",
                        "si": "yes",
                        "sí": "yes",
                        "continuar": "yes",
                        "2": "no",
                        "no": "no",
                        "cancelar": "no",
                    },
                )
                if not resume_choice:
                    send_whatsapp_reply(
                        phone_number_id,
                        from_number,
                        "❌ Responde 1) Sí, para continuar, o 2) No, para dejar el gasto como no completado.",
                    )
                    return HttpResponse(status=200)

                if resume_choice == "yes":
                    resume_stage = state.get("resume_stage") or "awaiting_doc_type"
                    update_conversation_state(from_number, stage=resume_stage)
                    send_whatsapp_reply(
                        phone_number_id,
                        from_number,
                        "Continuemos desde donde quedaste:\n\n" + stage_prompt(resume_stage),
                    )
                    return HttpResponse(status=200)

                before_status = exp.status
                exp.status = "not_completed"
                exp.save(update_fields=["status"])
                log_whatsapp_event(
                    exp,
                    action="status_changed",
                    changes={"status": {"before": before_status, "after": exp.status}},
                    reason="Usuario decidió no continuar el flujo de WhatsApp",
                )
                finish_conversation(from_number, stage="not_completed")
                send_whatsapp_reply(
                    phone_number_id,
                    from_number,
                    "El gasto quedó como No completado. Para registrar uno nuevo, envía nuevamente el comprobante.",
                )
                return HttpResponse(status=200)

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

                update_conversation_state(from_number, stage="awaiting_worksite")
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

                update_conversation_state(from_number, stage="awaiting_expense_scope")
                send_whatsapp_reply(
                    phone_number_id, from_number,
                    "🚘 ¿A qué corresponde este gasto?\n"
                    "1) Vehículo o equipo\n"
                    "2) Combustible\n"
                    "3) No corresponde a vehículo\n\n"
                    "Responde 1, 2 o 3."
                )
                return HttpResponse(status=200)

            # C) Alcance vehículo/combustible
            if stage == "awaiting_expense_scope":
                scope = parse_choice(body, {
                    "1": "vehicle", "vehiculo": "vehicle", "vehículo": "vehicle",
                    "equipo": "vehicle", "vehiculo o equipo": "vehicle", "vehículo o equipo": "vehicle",
                    "2": "fuel", "combustible": "fuel", "bencina": "fuel", "diesel": "fuel", "diésel": "fuel",
                    "3": "no", "no": "no", "ninguno": "no",
                })
                if not scope:
                    send_whatsapp_reply(phone_number_id, from_number,
                        "❌ Responde 1) Vehículo o equipo, 2) Combustible o 3) No corresponde."
                    )
                    return HttpResponse(status=200)

                if scope in {"vehicle", "fuel"}:
                    before_category = exp.category
                    exp.is_vehicle = True
                    if scope == "fuel":
                        policy, _ = CategoryCatalog.objects.get_or_create(
                            name="Combustibles",
                            defaults={"is_active": True},
                        )
                        if not policy.is_active:
                            policy.is_active = True
                            policy.save(update_fields=["is_active"])
                        exp.category = policy.name
                    exp.save(update_fields=["is_vehicle", "category"])
                    log_whatsapp_event(
                        exp,
                        action="whatsapp_update",
                        changes={
                            "is_vehicle": {"before": False, "after": True},
                            "category": {"before": before_category, "after": exp.category},
                        },
                        reason=(
                            "Usuario indicó gasto de combustible"
                            if scope == "fuel"
                            else "Usuario marcó gasto de vehículo"
                        ),
                    )

                    update_conversation_state(
                        from_number,
                        stage="awaiting_vehicle",
                        expense_scope=scope,
                    )
                    send_whatsapp_reply(phone_number_id, from_number,
                        "🚚 ¿Cuál es el vehículo o equipo?"
                    )
                    return HttpResponse(status=200)

                # No vehículo → comentario final
                exp.is_vehicle = False
                exp.vehicle = None
                exp.fuel_km = None
                exp.fuel_liters = None
                exp.save(update_fields=["is_vehicle", "vehicle", "fuel_km", "fuel_liters"])
                log_whatsapp_event(
                    exp,
                    action="whatsapp_update",
                    changes={"is_vehicle": {"before": True, "after": False}},
                    reason="Usuario indicó que no es gasto de vehículo",
                )

                request_final_comment(phone_number_id, from_number)
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

                if state.get("expense_scope") == "fuel":
                    update_conversation_state(from_number, stage="awaiting_fuel_km")
                    send_whatsapp_reply(
                        phone_number_id,
                        from_number,
                        "🛣️ ¿Cuál era el kilometraje al momento del carguío?\n"
                        "Responde solo con el número, sin separador de miles.",
                    )
                    return HttpResponse(status=200)

                request_final_comment(phone_number_id, from_number)
                return HttpResponse(status=200)

            # E) Kilometraje para combustible
            if stage == "awaiting_fuel_km":
                fuel_km = parse_nonnegative_decimal(body)
                if fuel_km is None:
                    send_whatsapp_reply(
                        phone_number_id,
                        from_number,
                        "❌ Ingresa un kilometraje válido. Ejemplo: 154320",
                    )
                    return HttpResponse(status=200)

                before = exp.fuel_km
                exp.fuel_km = fuel_km
                exp.save(update_fields=["fuel_km"])
                log_whatsapp_event(
                    exp,
                    action="whatsapp_update",
                    changes={"fuel_km": {"before": before, "after": str(fuel_km)}},
                    reason="Usuario indicó kilometraje de carguío",
                )

                update_conversation_state(from_number, stage="awaiting_fuel_liters")
                send_whatsapp_reply(
                    phone_number_id,
                    from_number,
                    "⛽ ¿Cuántos litros de combustible cargó?\n"
                    "Puedes usar coma o punto para los decimales.",
                )
                return HttpResponse(status=200)

            # F) Litros para combustible
            if stage == "awaiting_fuel_liters":
                fuel_liters = parse_nonnegative_decimal(body)
                if fuel_liters is None or fuel_liters <= 0:
                    send_whatsapp_reply(
                        phone_number_id,
                        from_number,
                        "❌ Ingresa una cantidad de litros mayor a cero. Ejemplo: 45,5",
                    )
                    return HttpResponse(status=200)

                before = exp.fuel_liters
                exp.fuel_liters = fuel_liters
                exp.save(update_fields=["fuel_liters"])
                log_whatsapp_event(
                    exp,
                    action="whatsapp_update",
                    changes={"fuel_liters": {"before": before, "after": str(fuel_liters)}},
                    reason="Usuario indicó litros cargados",
                )

                request_final_comment(phone_number_id, from_number)
                return HttpResponse(status=200)

            # G) Comentario final común a todos los flujos
            if stage == "awaiting_comment":
                comment = body.strip()
                if not comment:
                    send_whatsapp_reply(
                        phone_number_id,
                        from_number,
                        "❌ El comentario no puede quedar vacío. Escribe un comentario para finalizar.",
                    )
                    return HttpResponse(status=200)

                before = exp.notes
                user_comment = f"[{reporter_label(sender)}]\n{comment}"
                exp.notes = f"{exp.notes.rstrip()}\n\n{user_comment}" if exp.notes.strip() else user_comment
                exp.status = "pending"
                if not exp.wa_phone_number_id:
                    exp.wa_phone_number_id = phone_number_id
                exp.save(update_fields=["notes", "status", "wa_phone_number_id"])
                log_whatsapp_event(
                    exp,
                    action="whatsapp_update",
                    changes={
                        "notes": {"before": before, "after": exp.notes},
                        "status": {"before": "incomplete", "after": "pending"},
                    },
                    reason="Usuario agregó comentario final",
                )

                finish_conversation(from_number)
                final_lines = [
                    "✅ Gasto registrado correctamente.",
                    "",
                    f"ID: {expense_trace_id(exp)}",
                ]
                send_whatsapp_reply(
                    phone_number_id,
                    from_number,
                    "\n".join(final_lines),
                )
                return HttpResponse(status=200)

            # fallback
            send_whatsapp_reply(phone_number_id, from_number,
                "👋 Si quieres ingresar un gasto nuevo, envíame una foto o PDF del comprobante."
            )
            return HttpResponse(status=200)

    except Exception as e:
        print("❌ Error procesando webhook:", e)

    return HttpResponse(status=200)

def download_media_attachment(media_id: str, expense: Expense):
    meta = requests.get(
        f"{GRAPH_URL}/{media_id}",
        params={"access_token": settings.WA_ACCESS_TOKEN},
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
        headers={"Authorization": f"Bearer {settings.WA_ACCESS_TOKEN}"},
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
        "Authorization": f"Bearer {settings.WA_ACCESS_TOKEN}",
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
