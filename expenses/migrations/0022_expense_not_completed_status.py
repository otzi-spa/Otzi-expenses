from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0021_expense_incomplete_whatsapp_conversation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="expense",
            name="status",
            field=models.CharField(
                choices=[
                    ("incomplete", "Incompleto"),
                    ("not_completed", "No completado"),
                    ("pending", "Pendiente"),
                    ("completed", "Parametrizado"),
                    ("approved", "Aprobado"),
                    ("rejected", "Rechazada"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
