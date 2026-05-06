from django.db import migrations, models


def migrate_operator_to_reviewer(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(role="operator").update(role="reviewer")


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(migrate_operator_to_reviewer, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[
                    ("admin", "Admin"),
                    ("reviewer", "Reviewer"),
                    ("viewer", "Viewer"),
                ],
                default="reviewer",
                max_length=16,
            ),
        ),
    ]
