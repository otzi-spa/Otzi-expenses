from django.db import models
from django.conf import settings

class Expense(models.Model):
    STATUS = (("pending","Pendiente"),("completed","Parametrizado"),("approved","Aprobado"),("rejected","Rechazada"))

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
    supplier = models.CharField(max_length=128, blank=True)
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
    is_vehicle = models.BooleanField(default=False)
    vehicle = models.CharField(max_length=255, blank=True, null=True)  # 👈 libre por ahora (luego lo cambiamos)

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


SYNC_STATUS = (
    ("manual", "Manual"),
    ("synced", "Sincronizado"),
    ("failed", "Error"),
)


class VehicleCatalog(models.Model):
    name = models.CharField(max_length=255)
    external_id = models.CharField(max_length=255, blank=True, null=True)
    sync_status = models.CharField(max_length=16, choices=SYNC_STATUS, default="manual")
    last_synced_at = models.DateTimeField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Vehículo/Maquinaria"
        verbose_name_plural = "Vehículos/Maquinarias"

    def __str__(self):
        return self.name


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


class CategoryCatalog(models.Model):
    name = models.CharField(max_length=255, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Categoría"
        verbose_name_plural = "Categorías"

    def __str__(self):
        return self.name


class ExpenseTypeCatalog(models.Model):
    name = models.CharField(max_length=255, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tipo de gasto"
        verbose_name_plural = "Tipos de gasto"

    def __str__(self):
        return self.name


class ExpenseAuditLog(models.Model):
    ACTION_CHOICES = [
        ("created", "Creado"),
        ("updated", "Actualizado"),
        ("status_changed", "Estado cambiado"),
        ("status_change_blocked", "Cambio de estado bloqueado"),
        ("approved", "Aprobado"),
        ("rejected", "Rechazado"),
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
