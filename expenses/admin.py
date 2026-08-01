from django.contrib import admin

from .models import Attachment, Expense, ExpenseNotification


admin.site.register(Expense)
admin.site.register(Attachment)
admin.site.register(ExpenseNotification)
