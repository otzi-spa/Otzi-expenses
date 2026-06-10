from decimal import Decimal, InvalidOperation
import re

from django.db import transaction
from django.utils import timezone

from .models import (
    CategoryCatalog,
    Expense,
    ExpenseTypeCatalog,
    RindegastosExpenseFieldCatalog,
    RindegastosTaxCatalog,
    RindegastosUserCatalog,
)
from .rindegastos_client import RindegastosClient


def as_text(value):
    if value is None:
        return ""
    return str(value).strip()


def normalized_name(value):
    return re.sub(r"\s+", " ", as_text(value)).casefold()


def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return as_text(value).lower() in {"1", "true", "yes", "active", "activo"}


def as_decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def user_full_name(payload):
    full_name = as_text(payload.get("Name") or payload.get("FullName") or payload.get("EmployeeName"))
    if full_name:
        return full_name
    first_name = as_text(payload.get("FirstName") or payload.get("Name"))
    last_name = as_text(payload.get("LastName") or payload.get("Surname"))
    return f"{first_name} {last_name}".strip() or as_text(payload.get("Email") or payload.get("Id"))


class RindegastosCatalogSync:
    def __init__(self, client=None):
        self.client = client or RindegastosClient()
        self.now = timezone.now()

    @transaction.atomic
    def sync_all(self, rebuild=False):
        stats = {
            "policies": 0,
            "categories": 0,
            "taxes": 0,
            "expense_fields": 0,
            "users": 0,
            "verified_policy_links": 0,
        }

        if rebuild:
            self.rebuild_synced_catalogs()

        policies = self.client.get_expense_policies(active_only=True)
        active_policy_ids = set()
        for policy_payload in policies:
            policy = self.sync_policy(policy_payload)
            active_policy_ids.add(policy.external_id)
            stats["policies"] += 1

            if policy.external_id:
                stats["categories"] += self.sync_policy_categories(policy)
                stats["taxes"] += self.sync_policy_taxes(policy)
                stats["expense_fields"] += self.sync_policy_expense_fields(policy)

        if active_policy_ids:
            CategoryCatalog.objects.filter(sync_status="synced").exclude(external_id__in=active_policy_ids).update(
                is_active=False,
                last_synced_at=self.now,
            )
        self.merge_manual_policy_duplicates()
        stats["verified_policy_links"] = self.verify_policy_links()

        stats["users"] = self.sync_users()
        return stats

    def rebuild_synced_catalogs(self):
        ExpenseTypeCatalog.objects.filter(sync_status="synced").delete()
        RindegastosTaxCatalog.objects.filter(sync_status="synced").delete()
        RindegastosExpenseFieldCatalog.objects.filter(sync_status="synced").delete()
        RindegastosUserCatalog.objects.filter(sync_status="synced").delete()

    def policy_linked_payload(self, policy, payload):
        return {
            **payload,
            "_OtziPolicyExternalId": policy.external_id,
            "_OtziPolicyName": policy.name,
        }

    def verify_policy_links(self):
        verified = 0
        models = (ExpenseTypeCatalog, RindegastosTaxCatalog, RindegastosExpenseFieldCatalog)
        for model in models:
            for item in model.objects.filter(sync_status="synced").select_related("policy"):
                source_external_id = as_text((item.raw_payload or {}).get("_OtziPolicyExternalId"))
                if not source_external_id:
                    continue
                if source_external_id != as_text(item.policy.external_id):
                    raise ValueError(
                        f"{model.__name__} #{item.pk} está asociado a la política "
                        f"{item.policy.external_id}, pero fue sincronizado desde {source_external_id}."
                    )
                verified += 1
        return verified

    def merge_manual_policy_duplicates(self):
        synced_policies = list(CategoryCatalog.objects.filter(external_id__isnull=False))
        manual_policies = list(CategoryCatalog.objects.filter(external_id__isnull=True, is_active=True))
        synced_by_normalized_name = {normalized_name(policy.name): policy for policy in synced_policies}

        for manual_policy in manual_policies:
            synced_policy = synced_by_normalized_name.get(normalized_name(manual_policy.name))
            if not synced_policy:
                continue
            Expense.objects.filter(category=manual_policy.name).update(category=synced_policy.name)
            manual_policy.is_active = False
            manual_policy.last_synced_at = self.now
            manual_policy.save(update_fields=["is_active", "last_synced_at"])

    def sync_policy(self, payload):
        external_id = as_text(payload.get("Id"))
        name = as_text(payload.get("Name"))
        defaults = {
            "name": name,
            "code": as_text(payload.get("Code")) or None,
            "currency": as_text(payload.get("Currency")) or None,
            "is_active": as_bool(payload.get("IsActive")),
            "sync_status": "synced",
            "last_synced_at": self.now,
            "raw_payload": payload,
        }

        if external_id:
            policy = CategoryCatalog.objects.filter(external_id=external_id).first()
            if policy:
                for field, value in defaults.items():
                    setattr(policy, field, value)
                policy.save(update_fields=list(defaults.keys()))
                return policy

        policy = next(
            (
                item
                for item in CategoryCatalog.objects.filter(external_id__isnull=True)
                if normalized_name(item.name) == normalized_name(name)
            ),
            None,
        )
        if policy:
            for field, value in {**defaults, "external_id": external_id}.items():
                setattr(policy, field, value)
            policy.save(update_fields=[*defaults.keys(), "external_id"])
            return policy

        policy, _ = CategoryCatalog.objects.update_or_create(name=name, defaults={**defaults, "external_id": external_id})
        return policy

    def sync_policy_categories(self, policy):
        payloads = self.client.get_expense_policy_categories(policy.external_id)
        active_keys = set()
        count = 0
        for payload in payloads:
            name = as_text(payload.get("Name"))
            group_name = as_text(payload.get("GroupName")) or None
            active_keys.add((name, group_name))
            defaults = {
                "external_id": as_text(payload.get("Id")) or None,
                "group_code": as_text(payload.get("GroupCode")) or None,
                "account_code": as_text(payload.get("AccountCode")) or None,
                "instructions": as_text(payload.get("Instructions")),
                "is_active": True,
                "sync_status": "synced",
                "last_synced_at": self.now,
                "raw_payload": self.policy_linked_payload(policy, payload),
            }
            ExpenseTypeCatalog.objects.update_or_create(
                policy=policy,
                name=name,
                group_name=group_name,
                defaults=defaults,
            )
            count += 1

        ExpenseTypeCatalog.objects.filter(policy=policy, sync_status="synced").exclude(
            name__in=[key[0] for key in active_keys]
        ).update(is_active=False, last_synced_at=self.now)
        return count

    def sync_policy_taxes(self, policy):
        payloads = self.client.get_expense_policy_taxes(policy.external_id)
        active_names = set()
        count = 0
        for payload in payloads:
            name = as_text(payload.get("Name"))
            tax_type = as_text(payload.get("Type")) or None
            active_names.add(name)
            RindegastosTaxCatalog.objects.update_or_create(
                policy=policy,
                name=name,
                tax_type=tax_type,
                defaults={
                    "value": as_decimal(payload.get("Value")),
                    "is_active": True,
                    "sync_status": "synced",
                    "last_synced_at": self.now,
                    "raw_payload": self.policy_linked_payload(policy, payload),
                },
            )
            count += 1

        RindegastosTaxCatalog.objects.filter(policy=policy, sync_status="synced").exclude(name__in=active_names).update(
            is_active=False,
            last_synced_at=self.now,
        )
        return count

    def sync_policy_expense_fields(self, policy):
        payloads = self.client.get_expense_policy_expense_fields(policy.external_id)
        active_names = set()
        count = 0
        for payload in payloads:
            name = as_text(payload.get("Name"))
            active_names.add(name)
            RindegastosExpenseFieldCatalog.objects.update_or_create(
                policy=policy,
                name=name,
                defaults={
                    "field_type": as_text(payload.get("Type")) or None,
                    "default_value": as_text(payload.get("DefaultValue")) or None,
                    "default_code": as_text(payload.get("DefaultCode")) or None,
                    "options": payload.get("Options") or [],
                    "is_active": True,
                    "sync_status": "synced",
                    "last_synced_at": self.now,
                    "raw_payload": self.policy_linked_payload(policy, payload),
                },
            )
            count += 1

        RindegastosExpenseFieldCatalog.objects.filter(policy=policy, sync_status="synced").exclude(
            name__in=active_names
        ).update(is_active=False, last_synced_at=self.now)
        return count

    def sync_users(self):
        payloads = self.client.get_users()
        active_ids = set()
        count = 0
        for payload in payloads:
            external_id = as_text(payload.get("Id"))
            if not external_id:
                continue
            active_ids.add(external_id)
            RindegastosUserCatalog.objects.update_or_create(
                external_id=external_id,
                defaults={
                    "first_name": as_text(payload.get("FirstName")) or None,
                    "last_name": as_text(payload.get("LastName") or payload.get("Surname")) or None,
                    "full_name": user_full_name(payload),
                    "email": as_text(payload.get("Email")),
                    "is_active": as_bool(payload.get("IsActive", True)),
                    "sync_status": "synced",
                    "last_synced_at": self.now,
                    "raw_payload": payload,
                },
            )
            count += 1

        if active_ids:
            RindegastosUserCatalog.objects.filter(sync_status="synced").exclude(external_id__in=active_ids).update(
                is_active=False,
                last_synced_at=self.now,
            )
        return count
