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
    rindegastos_expense_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    rindegastos_report_id = models.CharField(max_length=255, blank=True, null=True)
    rindegastos_uploaded_at = models.DateTimeField(null=True, blank=True)
    rindegastos_synced_at = models.DateTimeField(null=True, blank=True)
    rindegastos_status = models.CharField(max_length=255, blank=True, null=True)
    rindegastos_raw_payload = models.JSONField(default=dict, blank=True)

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
    rut = models.CharField(max_length=32)
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
