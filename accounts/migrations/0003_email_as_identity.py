from django.db import migrations, models


def sync_email_and_username(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    used_emails = set()

    for user in User.objects.all().order_by("id"):
        email = (user.email or "").strip().lower()
        if not email:
            username = (user.username or "").strip().lower()
            if username and "@" in username:
                email = username
            else:
                email = f"user-{user.id}@placeholder.local"

        if email in used_emails:
            local, domain = email.split("@", 1)
            email = f"{local}+{user.id}@{domain}"

        user.email = email
        user.username = email
        user.save(update_fields=["email", "username"])
        used_emails.add(email)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_update_user_roles"),
    ]

    operations = [
        migrations.RunPython(sync_email_and_username, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="user",
            name="email",
            field=models.EmailField(max_length=254, unique=True),
        ),
    ]
