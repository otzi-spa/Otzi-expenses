from django.contrib import admin

from .models import Attachment, EmployeeFundMapping, Expense, ExpenseNotification, FundDepositInjectionAttempt, NotionFundSyncLog


admin.site.register(Expense)
admin.site.register(Attachment)
admin.site.register(ExpenseNotification)
admin.site.register(EmployeeFundMapping)
admin.site.register(NotionFundSyncLog)


@admin.register(FundDepositInjectionAttempt)
class FundDepositInjectionAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "internal_note",
        "status",
        "rindegastos_fund_id",
        "amount",
        "currency",
        "requested_payment_date",
        "actor",
        "started_at",
    )
    list_filter = ("status", "currency", "started_at")
    search_fields = ("internal_note", "rindegastos_fund_id", "rindegastos_admin_id", "error", "anomaly")
    readonly_fields = (
        "started_at",
        "updated_at",
        "completed_at",
        "request_payload",
        "response_payload",
        "before_fund_payload",
        "after_fund_payload",
        "detected_transaction",
    )
