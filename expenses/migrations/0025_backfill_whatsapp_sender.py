from django.db import migrations


def backfill_whatsapp_sender(apps, schema_editor):
    Expense = apps.get_model("expenses", "Expense")
    AllowedSender = apps.get_model("expenses", "AllowedSender")

    senders_by_phone = {
        sender.phone: sender.id
        for sender in AllowedSender.objects.filter(is_deleted=False)
    }
    whatsapp_expenses = Expense.objects.filter(source="whatsapp")
    for expense in whatsapp_expenses.iterator():
        update_fields = []
        sender_id = senders_by_phone.get(expense.wa_sender_phone)
        if sender_id and expense.wa_sender_id != sender_id:
            expense.wa_sender_id = sender_id
            update_fields.append("wa_sender")
        if expense.created_by_id is not None:
            expense.created_by_id = None
            update_fields.append("created_by")
        if update_fields:
            expense.save(update_fields=update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0024_expense_rindegastos_submitter"),
    ]

    operations = [
        migrations.RunPython(backfill_whatsapp_sender, migrations.RunPython.noop),
    ]
