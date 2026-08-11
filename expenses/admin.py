from django.contrib import admin

from .models import Attachment, EmployeeFundMapping, Expense, ExpenseNotification, NotionFundSyncLog


admin.site.register(Expense)
admin.site.register(Attachment)
admin.site.register(ExpenseNotification)
admin.site.register(EmployeeFundMapping)
admin.site.register(NotionFundSyncLog)
