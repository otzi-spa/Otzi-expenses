from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0041_funddepositinjectionattempt"),
    ]

    operations = [
        migrations.AddField(
            model_name="funddepositinjectionattempt",
            name="audit_summary",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
