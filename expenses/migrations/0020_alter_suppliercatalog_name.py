from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0019_suppliercatalog"),
    ]

    operations = [
        migrations.AlterField(
            model_name="suppliercatalog",
            name="name",
            field=models.CharField(max_length=128, unique=True),
        ),
    ]
