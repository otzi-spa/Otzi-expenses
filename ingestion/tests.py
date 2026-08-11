import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone

from expenses.models import AllowedSender, Expense, WhatsAppExpenseConversation
from expenses.views import _expense_export_id
from ingestion.api.views_webhook import parse_nonnegative_decimal, user_states


class WhatsAppFuelFlowTests(TestCase):
    phone = "56911111111"
    phone_number_id = "phone-number-id"

    def setUp(self):
        get_user_model().objects.create_user(
            id=1,
            username="bot-owner@example.com",
            email="bot-owner@example.com",
            password="test",
        )
        AllowedSender.objects.create(
            phone=self.phone,
            first_name="Juan",
            last_name="Pérez",
            active=True,
        )
        user_states.clear()

    def tearDown(self):
        user_states.clear()

    def payload(self, message):
        return {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": self.phone_number_id},
                                "messages": [message],
                            }
                        }
                    ]
                }
            ]
        }

    def text_message(self, body, message_id):
        return {
            "from": self.phone,
            "id": message_id,
            "timestamp": "1770000000",
            "type": "text",
            "text": {"body": body},
        }

    def pdf_message(self, message_id, media_id="pdf-media-1", filename="comprobante.pdf"):
        return {
            "from": self.phone,
            "id": message_id,
            "timestamp": "1770000000",
            "type": "document",
            "document": {
                "id": media_id,
                "mime_type": "application/pdf",
                "filename": filename,
            },
        }

    def post_message(self, message):
        return self.client.post(
            "/webhook/whatsapp/",
            data=json.dumps(self.payload(message)),
            content_type="application/json",
        )

    @patch("ingestion.api.views_webhook.send_whatsapp_reply")
    @patch("ingestion.api.views_webhook.download_media_attachment")
    def test_combustible_flow_collects_vehicle_km_and_liters(self, download_mock, reply_mock):
        image = {
            "from": self.phone,
            "id": "image-message-1",
            "timestamp": "1770000000",
            "type": "image",
            "image": {"id": "media-1"},
        }
        self.assertEqual(self.post_message(image).status_code, 200)
        expense = Expense.objects.get(wa_message_id="image-message-1")
        self.assertEqual(expense.status, "incomplete")
        self.assertIsNone(expense.created_by)
        self.assertEqual(expense.wa_sender.phone, self.phone)
        self.assertEqual(expense.wa_phone_number_id, self.phone_number_id)
        conversation = WhatsAppExpenseConversation.objects.get(expense=expense)
        self.assertTrue(conversation.is_active)
        self.assertEqual(conversation.stage, "awaiting_doc_type")

        for index, answer in enumerate(
            ["1", "Obra Norte", "2", "Camion 12", "154320", "45,5", "Carga para faena norte"],
            start=1,
        ):
            if index == 2:
                user_states.clear()
            self.assertEqual(
                self.post_message(self.text_message(answer, f"text-{index}")).status_code,
                200,
            )

        expense = Expense.objects.get(wa_message_id="image-message-1")
        self.assertEqual(expense.category, "Combustibles")
        self.assertTrue(expense.is_vehicle)
        self.assertEqual(expense.vehicle, "Camion 12")
        self.assertEqual(expense.fuel_km, Decimal("154320"))
        self.assertEqual(expense.fuel_liters, Decimal("45.5"))
        self.assertEqual(expense.notes, "[Juan Pérez]\nCarga para faena norte")
        self.assertEqual(expense.status, "pending")
        self.assertIn(f"ID: {_expense_export_id(expense.pk)}", reply_mock.call_args.args[2])
        self.assertEqual(user_states[self.phone]["stage"], "done")
        conversation.refresh_from_db()
        self.assertFalse(conversation.is_active)
        self.assertEqual(conversation.stage, "done")
        self.assertIsNotNone(conversation.completed_at)
        download_mock.assert_called_once()
        self.assertGreaterEqual(reply_mock.call_count, 8)

    @patch("ingestion.api.views_webhook.send_whatsapp_reply")
    @patch("ingestion.api.views_webhook.download_media_attachment")
    def test_pdf_document_starts_expense_flow(self, download_mock, reply_mock):
        pdf = self.pdf_message("pdf-message-1", media_id="pdf-media-1")

        self.assertEqual(self.post_message(pdf).status_code, 200)

        expense = Expense.objects.get(wa_message_id="pdf-message-1")
        self.assertEqual(expense.status, "incomplete")
        self.assertEqual(expense.wa_media_id, "pdf-media-1")
        self.assertEqual(expense.wa_sender.phone, self.phone)
        conversation = WhatsAppExpenseConversation.objects.get(expense=expense)
        self.assertTrue(conversation.is_active)
        self.assertEqual(conversation.stage, "awaiting_doc_type")
        download_mock.assert_called_once_with("pdf-media-1", expense)
        self.assertIn("tipo de documento", reply_mock.call_args.args[2].lower())

    @patch("ingestion.api.views_webhook.send_whatsapp_reply")
    @patch("ingestion.api.views_webhook.download_media_attachment")
    def test_non_pdf_document_is_rejected(self, download_mock, reply_mock):
        document = {
            "from": self.phone,
            "id": "word-message-1",
            "timestamp": "1770000000",
            "type": "document",
            "document": {
                "id": "word-media-1",
                "mime_type": "application/msword",
                "filename": "documento.doc",
            },
        }

        self.assertEqual(self.post_message(document).status_code, 200)

        self.assertFalse(Expense.objects.filter(wa_message_id="word-message-1").exists())
        download_mock.assert_not_called()
        self.assertIn("fotos o pdf", reply_mock.call_args.args[2].lower())

    @patch("ingestion.api.views_webhook.send_whatsapp_reply")
    @patch("ingestion.api.views_webhook.download_media_attachment")
    def test_vehicle_flow_ends_with_user_comment(self, download_mock, reply_mock):
        image = {
            "from": self.phone,
            "id": "image-message-vehicle",
            "timestamp": "1770000000",
            "type": "image",
            "image": {"id": "media-vehicle"},
        }
        self.post_message(image)

        for index, answer in enumerate(
            ["2", "Obra Sur", "1", "Camioneta 4", "Repuesto para mantención"],
            start=1,
        ):
            self.post_message(self.text_message(answer, f"vehicle-text-{index}"))

        expense = Expense.objects.get(wa_message_id="image-message-vehicle")
        self.assertTrue(expense.is_vehicle)
        self.assertEqual(expense.vehicle, "Camioneta 4")
        self.assertEqual(expense.notes, "[Juan Pérez]\nRepuesto para mantención")
        self.assertEqual(user_states[self.phone]["stage"], "done")
        download_mock.assert_called_once()

    @patch("ingestion.api.views_webhook.send_whatsapp_reply")
    @patch("ingestion.api.views_webhook.download_media_attachment")
    def test_non_vehicle_flow_goes_directly_to_comment(self, download_mock, reply_mock):
        image = {
            "from": self.phone,
            "id": "image-message-no-vehicle",
            "timestamp": "1770000000",
            "type": "image",
            "image": {"id": "media-no-vehicle"},
        }
        self.post_message(image)

        answers = ["3", "Oficina central", "3"]
        for index, answer in enumerate(answers, start=1):
            self.post_message(self.text_message(answer, f"no-vehicle-text-{index}"))

        self.assertEqual(user_states[self.phone]["stage"], "awaiting_comment")
        last_prompt = reply_mock.call_args.args[2]
        self.assertIn("comentario", last_prompt.lower())
        self.assertNotIn("tipo de gasto", last_prompt.lower())

        self.post_message(self.text_message("Compra de útiles", "no-vehicle-comment"))

        expense = Expense.objects.get(wa_message_id="image-message-no-vehicle")
        self.assertFalse(expense.is_vehicle)
        self.assertEqual(expense.notes, "[Juan Pérez]\nCompra de útiles")
        self.assertEqual(user_states[self.phone]["stage"], "done")
        download_mock.assert_called_once()

    @patch("ingestion.api.views_webhook.send_whatsapp_reply")
    @patch("ingestion.api.views_webhook.download_media_attachment")
    def test_new_image_offers_to_resume_incomplete_conversation(self, download_mock, reply_mock):
        first_image = {
            "from": self.phone,
            "id": "resume-image-1",
            "timestamp": "1770000000",
            "type": "image",
            "image": {"id": "resume-media-1"},
        }
        second_image = {
            "from": self.phone,
            "id": "resume-image-2",
            "timestamp": "1770000100",
            "type": "image",
            "image": {"id": "resume-media-2"},
        }
        self.post_message(first_image)
        self.post_message(self.text_message("1", "resume-doc-type"))
        self.post_message(second_image)

        expense = Expense.objects.get(wa_message_id="resume-image-1")
        conversation = WhatsAppExpenseConversation.objects.get(expense=expense)
        self.assertEqual(Expense.objects.count(), 1)
        self.assertEqual(expense.status, "incomplete")
        self.assertEqual(conversation.stage, "awaiting_resume")
        self.assertEqual(conversation.context["resume_stage"], "awaiting_worksite")
        self.assertIn("obra/proyecto", reply_mock.call_args.args[2].lower())

        self.post_message(self.text_message("sí", "resume-yes"))

        conversation.refresh_from_db()
        self.assertEqual(conversation.stage, "awaiting_worksite")
        self.assertTrue(conversation.is_active)
        self.assertIn("obra/proyecto", reply_mock.call_args.args[2].lower())
        download_mock.assert_called_once()

    @patch("ingestion.api.views_webhook.send_whatsapp_reply")
    @patch("ingestion.api.views_webhook.download_media_attachment")
    def test_new_pdf_offers_to_resume_incomplete_conversation(self, download_mock, reply_mock):
        image = {
            "from": self.phone,
            "id": "resume-pdf-image-1",
            "timestamp": "1770000000",
            "type": "image",
            "image": {"id": "resume-pdf-media-1"},
        }
        pdf = self.pdf_message("resume-pdf-2", media_id="resume-pdf-media-2")

        self.post_message(image)
        self.post_message(self.text_message("1", "resume-pdf-doc-type"))
        self.post_message(pdf)

        expense = Expense.objects.get(wa_message_id="resume-pdf-image-1")
        conversation = WhatsAppExpenseConversation.objects.get(expense=expense)
        self.assertEqual(Expense.objects.count(), 1)
        self.assertEqual(conversation.stage, "awaiting_resume")
        self.assertEqual(conversation.context["resume_stage"], "awaiting_worksite")
        self.assertIn("obra/proyecto", reply_mock.call_args.args[2].lower())
        download_mock.assert_called_once()

    @override_settings(WHATSAPP_RESUME_AFTER_MINUTES=30)
    @patch("ingestion.api.views_webhook.send_whatsapp_reply")
    @patch("ingestion.api.views_webhook.download_media_attachment")
    def test_inactive_conversation_offers_to_resume_before_consuming_answer(
        self,
        download_mock,
        reply_mock,
    ):
        image = {
            "from": self.phone,
            "id": "inactive-image-1",
            "timestamp": "1770000000",
            "type": "image",
            "image": {"id": "inactive-media-1"},
        }
        self.post_message(image)
        self.post_message(self.text_message("1", "inactive-doc-type"))

        conversation = WhatsAppExpenseConversation.objects.get(phone=self.phone, is_active=True)
        WhatsAppExpenseConversation.objects.filter(pk=conversation.pk).update(
            updated_at=timezone.now() - timedelta(minutes=31),
        )
        user_states.clear()

        self.post_message(self.text_message("Obra que no debe consumirse", "inactive-return"))

        expense = Expense.objects.get(wa_message_id="inactive-image-1")
        conversation.refresh_from_db()
        self.assertFalse(expense.worksite)
        self.assertEqual(conversation.stage, "awaiting_resume")
        self.assertEqual(conversation.context["resume_stage"], "awaiting_worksite")
        self.assertIn("quedó pendiente", reply_mock.call_args.args[2].lower())
        self.assertIn("obra/proyecto", reply_mock.call_args.args[2].lower())

        self.post_message(self.text_message("sí", "inactive-resume-yes"))

        conversation.refresh_from_db()
        self.assertEqual(conversation.stage, "awaiting_worksite")
        self.assertIn("continuemos desde donde quedaste", reply_mock.call_args.args[2].lower())
        download_mock.assert_called_once()

    @patch("ingestion.api.views_webhook.send_whatsapp_reply")
    @patch("ingestion.api.views_webhook.download_media_attachment")
    def test_user_can_leave_expense_as_not_completed(self, download_mock, reply_mock):
        first_image = {
            "from": self.phone,
            "id": "abandon-image-1",
            "timestamp": "1770000000",
            "type": "image",
            "image": {"id": "abandon-media-1"},
        }
        second_image = {
            "from": self.phone,
            "id": "abandon-image-2",
            "timestamp": "1770000100",
            "type": "image",
            "image": {"id": "abandon-media-2"},
        }
        self.post_message(first_image)
        self.post_message(second_image)
        self.post_message(self.text_message("2", "resume-no"))

        expense = Expense.objects.get(wa_message_id="abandon-image-1")
        conversation = WhatsAppExpenseConversation.objects.get(expense=expense)
        self.assertEqual(Expense.objects.count(), 1)
        self.assertEqual(expense.status, "not_completed")
        self.assertFalse(conversation.is_active)
        self.assertEqual(conversation.stage, "not_completed")
        self.assertIn("envía nuevamente el comprobante", reply_mock.call_args.args[2])
        download_mock.assert_called_once()

    def test_decimal_parser_accepts_units_and_comma(self):
        self.assertEqual(parse_nonnegative_decimal("45,5 litros"), Decimal("45.5"))
        self.assertEqual(parse_nonnegative_decimal("154320 km"), Decimal("154320"))
        self.assertIsNone(parse_nonnegative_decimal("-1"))
        self.assertIsNone(parse_nonnegative_decimal("muchos"))
