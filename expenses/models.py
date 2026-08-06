from django.db import models
from django.conf import settings


def normalize_rut(value):
    raw_value = (value or "").strip()
    compact = "".join(char for char in raw_value.replace(".", "").replace("-", "") if char.isalnum())
    if len(compact) < 2:
        return raw_value.upper()
    return f"{compact[:-1]}-{compact[-1].upper()}"


class Expense(models.Model):
    STATUS = (
        ("incomplete", "Incompleto"),
        ("not_completed", "No completado"),
        ("pending", "Pendiente"),
        ("completed", "Parametrizado"),
        ("approved", "Aprobado"),
        ("rejected", "Rechazada"),
    )

    DOC_TYPE_CHOICES = [
        ("boleta", "Boleta"),
        ("factura", "Factura"),
        ("vale", "Vale"),
    ]

    EXPENSE_TYPE_CHOICES = [
        ("alimentacion", "Alimentación"),
        ("transporte", "Transporte"),
        ("alojamiento", "Alojamiento"),
        ("otro", "Otro"),
    ]
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=8, default="CLP")
    category = models.CharField(max_length=128, default="Sin Categoria")
    # Texto libre reportado por el usuario en WhatsApp
    worksite = models.CharField(max_length=255, blank=True, null=True)
    # Obra estandarizada elegida por administrador (catálogo)
    worksite_standard = models.CharField(max_length=255, blank=True, null=True)
    rindegastos_cost_center = models.CharField(max_length=255, blank=True, null=True)
    rindegastos_submitter = models.CharField(max_length=255, blank=True, null=True)
    supplier = models.CharField(max_length=128, blank=True)
    supplier_rut = models.CharField(max_length=32, blank=True, null=True)
    paid_at = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    wa_message_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    wa_sender_phone = models.CharField(max_length=50, null=True, blank=True)
    wa_media_id = models.CharField(max_length=255, blank=True, null=True)   # id de imagen WA
    wa_sender = models.ForeignKey(
        "AllowedSender",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expenses",
    )

    source = models.CharField(max_length=16, default="whatsapp")  # whatsapp/web
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    message_sent_at = models.DateTimeField(null=True, blank=True)

    document_type = models.CharField(max_length=20, choices=DOC_TYPE_CHOICES, blank=True, null=True)
    rindegastos_document_type = models.CharField(max_length=255, blank=True, null=True)
    document_number = models.CharField(max_length=64, blank=True, null=True)
    is_vehicle = models.BooleanField(default=False)
    # Valor seleccionado desde Rindegastos o texto capturado previamente por WhatsApp.
    vehicle = models.CharField(max_length=255, blank=True, null=True)
    fuel_km = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    fuel_liters = models.DecimalField(max_digits=12, decimal_places=3, blank=True, null=True)
    gasoline_type = models.CharField(max_length=16, blank=True, null=True)
    iva_amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    specific_tax_amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    rindegastos_tax = models.CharField(max_length=255, blank=True, null=True)
    tax_calculation_source = models.CharField(max_length=32, blank=True, default="")
    tax_calculation_metadata = models.JSONField(default=dict, blank=True)

    expense_type = models.CharField(max_length=255, blank=True, null=True)
    expense_type_other = models.CharField(max_length=255, blank=True, null=True)
    split_group_id = models.CharField(max_length=36, blank=True, null=True, db_index=True)
    split_parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="split_children",
    )
    split_index = models.PositiveSmallIntegerField(null=True, blank=True)
    split_total = models.PositiveSmallIntegerField(null=True, blank=True)
    decision_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="decided_expenses",
    )
    decision_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    wa_phone_number_id = models.CharField(max_length=128, blank=True, null=True)
    rindegastos_expense_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    rindegastos_integration_code = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    rindegastos_report_id = models.CharField(max_length=255, blank=True, null=True)
    rindegastos_uploaded_at = models.DateTimeField(null=True, blank=True)
    rindegastos_synced_at = models.DateTimeField(null=True, blank=True)
    rindegastos_status = models.CharField(max_length=255, blank=True, null=True)
    rindegastos_raw_payload = models.JSONField(default=dict, blank=True)


class RindegastosReconcileRun(models.Model):
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    since = models.DateField(null=True, blank=True)
    until = models.DateField(null=True, blank=True)
    max_pages = models.PositiveIntegerField(null=True, blank=True)
    fetched_count = models.PositiveIntegerField(default=0)
    matched_count = models.PositiveIntegerField(default=0)
    changed_count = models.PositiveIntegerField(default=0)
    diff_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, default=STATUS_RUNNING)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-started_at",)
        verbose_name = "Corrida reconciliación Rindegastos"
        verbose_name_plural = "Corridas reconciliación Rindegastos"

    def __str__(self):
        return f"Reconciliación Rindegastos #{self.pk or 'nueva'} - {self.status}"


class RindegastosExpenseSnapshot(models.Model):
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="rindegastos_snapshots")
    run = models.ForeignKey(
        RindegastosReconcileRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="snapshots",
    )
    rindegastos_expense_id = models.CharField(max_length=64, db_index=True)
    rindegastos_report_id = models.CharField(max_length=64, blank=True)
    payload_hash = models.CharField(max_length=64, db_index=True)
    normalized_payload = models.JSONField(default=dict)
    raw_payload = models.JSONField(default=dict)
    source_endpoint = models.CharField(max_length=32, default="getExpense")
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["expense", "payload_hash"]),
            models.Index(fields=["rindegastos_expense_id", "fetched_at"]),
        ]
        ordering = ("-fetched_at",)
        verbose_name = "Snapshot gasto Rindegastos"
        verbose_name_plural = "Snapshots gastos Rindegastos"

    def __str__(self):
        return f"Snapshot Rindegastos {self.rindegastos_expense_id} - gasto #{self.expense_id}"


class RindegastosExpenseDiff(models.Model):
    STATUS_OPEN = "open"
    STATUS_APPLIED = "applied"
    STATUS_IGNORED = "ignored"
    STATUS_RESOLVED = "resolved"

    SEVERITY_INFO = "info"
    SEVERITY_WARNING = "warning"
    SEVERITY_CONFLICT = "conflict"

    STATUS_CHOICES = [
        (STATUS_OPEN, "Abierta"),
        (STATUS_APPLIED, "Aplicada"),
        (STATUS_IGNORED, "Ignorada"),
        (STATUS_RESOLVED, "Resuelta"),
    ]
    SEVERITY_CHOICES = [
        (SEVERITY_INFO, "Info"),
        (SEVERITY_WARNING, "Advertencia"),
        (SEVERITY_CONFLICT, "Conflicto"),
    ]

    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="rindegastos_diffs")
    snapshot = models.ForeignKey(RindegastosExpenseSnapshot, on_delete=models.CASCADE, related_name="diffs")
    field_name = models.CharField(max_length=128)
    local_value = models.JSONField(null=True, blank=True)
    remote_value = models.JSONField(null=True, blank=True)
    severity = models.CharField(max_length=16, choices=SEVERITY_CHOICES, default=SEVERITY_INFO)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        indexes = [
            models.Index(fields=["expense", "status"]),
            models.Index(fields=["snapshot", "field_name"]),
        ]
        ordering = ("-created_at",)
        verbose_name = "Diferencia gasto Rindegastos"
        verbose_name_plural = "Diferencias gastos Rindegastos"

    def __str__(self):
        return f"{self.field_name} - gasto #{self.expense_id} - {self.status}"


class Attachment(models.Model):
    expense = models.ForeignKey(Expense, related_name="attachments", on_delete=models.CASCADE)
    file = models.FileField(upload_to="receipts/")
    checksum_sha256 = models.CharField(max_length=64, blank=True)
    content_type = models.CharField(max_length=64, blank=True)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AllowedSender(models.Model):
    first_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)
    phone = models.CharField(max_length=32, unique=True)
    email = models.EmailField(blank=True)
    active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        full = f"{self.first_name} {self.last_name}".strip()
        return full or self.phone


class WhatsAppExpenseConversation(models.Model):
    expense = models.OneToOneField(
        Expense,
        on_delete=models.CASCADE,
        related_name="whatsapp_conversation",
    )
    sender = models.ForeignKey(
        AllowedSender,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="whatsapp_conversations",
    )
    phone = models.CharField(max_length=50, db_index=True)
    stage = models.CharField(max_length=64)
    context = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.phone} - gasto #{self.expense_id} - {self.stage}"


SYNC_STATUS = (
    ("manual", "Manual"),
    ("synced", "Sincronizado"),
    ("failed", "Error"),
)


class WorksiteCatalog(models.Model):
    name = models.CharField(max_length=255)
    external_id = models.CharField(max_length=255, blank=True, null=True)
    sync_status = models.CharField(max_length=16, choices=SYNC_STATUS, default="manual")
    last_synced_at = models.DateTimeField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Obra/Proyecto"
        verbose_name_plural = "Obras/Proyectos"

    def __str__(self):
        return self.name


class SupplierCatalog(models.Model):
    name = models.CharField(max_length=128, unique=True)
    rut = models.CharField(max_length=32, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Proveedor"
        verbose_name_plural = "Proveedores"
        ordering = ("name",)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.rut = normalize_rut(self.rut)
        super().save(*args, **kwargs)


class CategoryCatalog(models.Model):
    name = models.CharField(max_length=255, unique=True)
    external_id = models.CharField(max_length=255, blank=True, null=True)
    code = models.CharField(max_length=255, blank=True, null=True)
    currency = models.CharField(max_length=8, blank=True, null=True)
    sync_status = models.CharField(max_length=16, choices=SYNC_STATUS, default="manual")
    last_synced_at = models.DateTimeField(blank=True, null=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Política"
        verbose_name_plural = "Políticas"

    def __str__(self):
        return self.name


class ExpenseTypeCatalog(models.Model):
    name = models.CharField(max_length=255)
    policy = models.ForeignKey(
        CategoryCatalog,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="rindegastos_categories",
    )
    external_id = models.CharField(max_length=255, blank=True, null=True)
    group_name = models.CharField(max_length=255, blank=True, null=True)
    group_code = models.CharField(max_length=255, blank=True, null=True)
    account_code = models.CharField(max_length=255, blank=True, null=True)
    instructions = models.TextField(blank=True)
    sync_status = models.CharField(max_length=16, choices=SYNC_STATUS, default="manual")
    last_synced_at = models.DateTimeField(blank=True, null=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Categoría Rindegastos"
        verbose_name_plural = "Categorías Rindegastos"
        unique_together = ("policy", "name", "group_name")

    def __str__(self):
        return self.name


class RindegastosTaxCatalog(models.Model):
    policy = models.ForeignKey(CategoryCatalog, on_delete=models.CASCADE, related_name="rindegastos_taxes")
    name = models.CharField(max_length=255)
    tax_type = models.CharField(max_length=64, blank=True, null=True)
    value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    sync_status = models.CharField(max_length=16, choices=SYNC_STATUS, default="synced")
    last_synced_at = models.DateTimeField(blank=True, null=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Impuesto Rindegastos"
        verbose_name_plural = "Impuestos Rindegastos"
        unique_together = ("policy", "name", "tax_type")

    def __str__(self):
        return f"{self.policy.name} - {self.name}"


class RindegastosExpenseFieldCatalog(models.Model):
    policy = models.ForeignKey(CategoryCatalog, on_delete=models.CASCADE, related_name="rindegastos_expense_fields")
    name = models.CharField(max_length=255)
    field_type = models.CharField(max_length=64, blank=True, null=True)
    default_value = models.CharField(max_length=255, blank=True, null=True)
    default_code = models.CharField(max_length=255, blank=True, null=True)
    options = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    sync_status = models.CharField(max_length=16, choices=SYNC_STATUS, default="synced")
    last_synced_at = models.DateTimeField(blank=True, null=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Campo extra Rindegastos"
        verbose_name_plural = "Campos extra Rindegastos"
        unique_together = ("policy", "name")

    def __str__(self):
        return f"{self.policy.name} - {self.name}"


class TaxIndicatorValue(models.Model):
    indicator = models.CharField(max_length=32)
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    value = models.DecimalField(max_digits=14, decimal_places=4)
    source_url = models.URLField(max_length=500, blank=True)
    last_synced_at = models.DateTimeField(blank=True, null=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Indicador tributario"
        verbose_name_plural = "Indicadores tributarios"
        unique_together = ("indicator", "year", "month")
        ordering = ("-year", "-month", "indicator")

    def __str__(self):
        return f"{self.indicator} {self.month:02d}/{self.year}: {self.value}"


class FuelSpecificTaxRate(models.Model):
    effective_date = models.DateField()
    fuel_name = models.CharField(max_length=255)
    fuel_key = models.CharField(max_length=80)
    component_base = models.DecimalField(max_digits=12, decimal_places=4)
    component_variable = models.DecimalField(max_digits=12, decimal_places=4)
    resulting_tax = models.DecimalField(max_digits=12, decimal_places=4)
    unit = models.CharField(max_length=32)
    source_url = models.URLField(max_length=500, blank=True)
    last_synced_at = models.DateTimeField(blank=True, null=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tasa impuesto especifico combustible"
        verbose_name_plural = "Tasas impuesto especifico combustible"
        unique_together = ("effective_date", "fuel_key", "unit")
        ordering = ("-effective_date", "fuel_key")

    def __str__(self):
        return f"{self.fuel_name} {self.effective_date}: {self.resulting_tax} {self.unit}"


class RindegastosUserCatalog(models.Model):
    external_id = models.CharField(max_length=255, unique=True)
    first_name = models.CharField(max_length=255, blank=True, null=True)
    last_name = models.CharField(max_length=255, blank=True, null=True)
    full_name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    sync_status = models.CharField(max_length=16, choices=SYNC_STATUS, default="synced")
    last_synced_at = models.DateTimeField(blank=True, null=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Usuario Rindegastos"
        verbose_name_plural = "Usuarios Rindegastos"

    def __str__(self):
        return self.full_name or self.email or self.external_id


class ExpenseAuditLog(models.Model):
    ACTION_CHOICES = [
        ("created", "Creado"),
        ("updated", "Actualizado"),
        ("status_changed", "Estado cambiado"),
        ("status_change_blocked", "Cambio de estado bloqueado"),
        ("approved", "Aprobado"),
        ("rejected", "Rechazado"),
        ("decision_reverted", "Decisión revertida"),
        ("deleted", "Eliminado"),
        ("whatsapp_update", "Actualización WhatsApp"),
    ]

    expense = models.ForeignKey(
        Expense,
        related_name="audit_logs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    expense_snapshot_id = models.IntegerField()
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, default="updated")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    actor_name = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=32, default="web")
    reason = models.TextField(blank=True)
    changes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Expense #{self.expense_snapshot_id} - {self.action}"


class ExpenseNotification(models.Model):
    TYPE_REJECTION = "rejection"
    CHANNEL_WHATSAPP = "whatsapp"

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"

    TYPE_CHOICES = [
        (TYPE_REJECTION, "Rechazo"),
    ]
    CHANNEL_CHOICES = [
        (CHANNEL_WHATSAPP, "WhatsApp"),
    ]
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pendiente"),
        (STATUS_PROCESSING, "Procesando"),
        (STATUS_SENT, "Enviada"),
        (STATUS_FAILED, "Fallida"),
    ]

    expense = models.ForeignKey(
        Expense,
        related_name="notifications",
        on_delete=models.CASCADE,
    )
    notification_type = models.CharField(max_length=32, choices=TYPE_CHOICES, default=TYPE_REJECTION)
    channel = models.CharField(max_length=32, choices=CHANNEL_CHOICES, default=CHANNEL_WHATSAPP)
    recipient = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    decision_at = models.DateTimeField()
    template_name = models.CharField(max_length=128, blank=True)
    template_language = models.CharField(max_length=16, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["expense", "notification_type", "channel", "decision_at"],
                name="uniq_expense_notification_decision",
            )
        ]

    def __str__(self):
        return f"{self.get_notification_type_display()} {self.get_channel_display()} - gasto #{self.expense_id}"
