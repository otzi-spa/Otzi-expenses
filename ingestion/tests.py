import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from expenses.models import AllowedSender, Expense
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
        AllowedSender.objects.create(phone=self.phone, active=True)
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

        for index, answer in enumerate(
            ["1", "Obra Norte", "2", "Camion 12", "154320", "45,5"],
            start=1,
        ):
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
        self.assertEqual(user_states[self.phone]["stage"], "done")
        download_mock.assert_called_once()
        self.assertGreaterEqual(reply_mock.call_count, 7)

    def test_decimal_parser_accepts_units_and_comma(self):
        self.assertEqual(parse_nonnegative_decimal("45,5 litros"), Decimal("45.5"))
        self.assertEqual(parse_nonnegative_decimal("154320 km"), Decimal("154320"))
        self.assertIsNone(parse_nonnegative_decimal("-1"))
        self.assertIsNone(parse_nonnegative_decimal("muchos"))
