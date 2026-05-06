from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import filters, permissions, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Attachment, Expense
from .serializers import AttachmentSerializer, ExpenseSerializer


class ExpenseViewSet(viewsets.ModelViewSet):
    queryset = Expense.objects.all().order_by("-created_at")
    serializer_class = ExpenseSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ["worksite", "worksite_standard", "supplier", "notes"]


class AttachmentViewSet(viewsets.ModelViewSet):
    queryset = Attachment.objects.all().order_by("-created_at")
    serializer_class = AttachmentSerializer
    permission_classes = [permissions.IsAuthenticated]


class AnalyticsBaseAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _split_csv(self, raw_value: str | None) -> list[str]:
        if not raw_value:
            return []
        return [item.strip() for item in raw_value.split(",") if item.strip()]

    def _bool_param(self, value: str | None):
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no"}:
            return False
        return None

    def get_date_field(self, request, default="paid_at"):
        date_field = request.query_params.get("date_field", default)
        if date_field not in {"created_at", "paid_at", "message_sent_at"}:
            return default
        return date_field

    def _date_range_lookup(self, date_field: str, operator: str) -> str:
        # paid_at is a DateField; created_at/message_sent_at are DateTimeField.
        if date_field == "paid_at":
            return f"{date_field}__{operator}"
        return f"{date_field}__date__{operator}"

    def get_filtered_queryset(self, request, include_date_filters=True):
        queryset = Expense.objects.all()

        date_field = self.get_date_field(request, default="paid_at")

        if include_date_filters:
            start_date_raw = request.query_params.get("start_date")
            end_date_raw = request.query_params.get("end_date")
            start_date = parse_date(start_date_raw) if start_date_raw else None
            end_date = parse_date(end_date_raw) if end_date_raw else None

            if start_date:
                queryset = queryset.filter(**{self._date_range_lookup(date_field, "gte"): start_date})
            if end_date:
                queryset = queryset.filter(**{self._date_range_lookup(date_field, "lte"): end_date})

        statuses = self._split_csv(request.query_params.get("status"))
        if statuses:
            queryset = queryset.filter(status__in=statuses)

        worksites = self._split_csv(request.query_params.get("worksite"))
        if worksites:
            queryset = queryset.filter(worksite_standard__in=worksites)

        vehicles = self._split_csv(request.query_params.get("vehicle"))
        if vehicles:
            queryset = queryset.filter(vehicle__in=vehicles)

        categories = self._split_csv(request.query_params.get("category"))
        if categories:
            queryset = queryset.filter(category__in=categories)

        sources = self._split_csv(request.query_params.get("source"))
        if sources:
            queryset = queryset.filter(source__in=sources)

        has_attachment = self._bool_param(request.query_params.get("has_attachment"))
        if has_attachment is True:
            queryset = queryset.filter(attachments__isnull=False)
        elif has_attachment is False:
            queryset = queryset.filter(attachments__isnull=True)

        return queryset.distinct()

    def _first_day_of_month(self, d: date):
        return d.replace(day=1)

    def _last_day_of_month(self, d: date):
        next_month = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        return next_month - timedelta(days=1)

    def _resolve_period(self, period_key: str):
        today = timezone.localdate()
        current_month_start = today.replace(day=1)
        current_year_start = date(today.year, 1, 1)

        if period_key == "ytd":
            return current_year_start, today

        if period_key == "current_month":
            return current_month_start, today

        if period_key == "previous_month":
            prev_month_last = current_month_start - timedelta(days=1)
            prev_month_start = prev_month_last.replace(day=1)
            return prev_month_start, prev_month_last

        if period_key == "current_year":
            return date(today.year, 1, 1), date(today.year, 12, 31)

        if period_key == "previous_year":
            return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)

        if period_key == "last_6_months":
            first_day_this_month = today.replace(day=1)
            six_month_window_start = (first_day_this_month - timedelta(days=155)).replace(day=1)
            return six_month_window_start, today

        return current_year_start, today


class AnalyticsKPIView(AnalyticsBaseAPIView):
    def get(self, request):
        queryset = self.get_filtered_queryset(request, include_date_filters=False)
        date_field = self.get_date_field(request, default="paid_at")

        today = timezone.localdate()
        month_start = today.replace(day=1)
        six_month_start = (month_start - timedelta(days=155)).replace(day=1)

        monthly_amount = queryset.filter(
            **{
                self._date_range_lookup(date_field, "gte"): month_start,
                self._date_range_lookup(date_field, "lte"): today,
            }
        ).aggregate(total_amount=Sum("amount"))["total_amount"]

        pending_not_parametrized = queryset.filter(status="pending").count()

        avg_ticket_parametrized = queryset.filter(status="completed", amount__isnull=False).aggregate(
            avg_ticket=Avg("amount")
        )["avg_ticket"]

        messages_last_6m = queryset.filter(
            source="whatsapp",
        ).filter(
            Q(message_sent_at__date__gte=six_month_start, message_sent_at__date__lte=today)
            | Q(
                message_sent_at__isnull=True,
                created_at__date__gte=six_month_start,
                created_at__date__lte=today,
            )
        ).count()

        return Response(
            {
                "period": {
                    "month_start": month_start.isoformat(),
                    "today": today.isoformat(),
                    "six_month_start": six_month_start.isoformat(),
                },
                "total_amount_current_month": float(monthly_amount or 0),
                "pending_not_parametrized_count": int(pending_not_parametrized or 0),
                "avg_ticket_parametrized": float(avg_ticket_parametrized or 0),
                "messages_received_last_6_months": int(messages_last_6m or 0),
            }
        )


class AnalyticsMonthlyTrendView(AnalyticsBaseAPIView):
    def get(self, request):
        months = int(request.query_params.get("months", 12))
        months = max(1, min(months, 36))

        base_queryset = self.get_filtered_queryset(request)
        date_field = self.get_date_field(request, default="paid_at")

        today = timezone.localdate()
        window_start = (today.replace(day=1) - timedelta(days=(months - 1) * 31)).replace(day=1)

        rows = (
            base_queryset.exclude(**{f"{date_field}__isnull": True}).filter(
                **{self._date_range_lookup(date_field, "gte"): window_start}
            )
            .annotate(month=TruncMonth(date_field))
            .values("month")
            .annotate(total_amount=Sum("amount"), total_count=Count("id"))
            .order_by("month")
        )

        data = [
            {
                "month": row["month"].isoformat() if row["month"] else None,
                "total_amount": float(row["total_amount"] or 0),
                "total_count": int(row["total_count"] or 0),
            }
            for row in rows
        ]

        return Response({"months": months, "data": data})


class AnalyticsStatusBreakdownView(AnalyticsBaseAPIView):
    def get(self, request):
        base_queryset = self.get_filtered_queryset(request)

        label_map = dict(Expense.STATUS)
        rows = base_queryset.values("status").annotate(count=Count("id"), total_amount=Sum("amount")).order_by("status")

        data = [
            {
                "status": row["status"],
                "label": label_map.get(row["status"], row["status"]),
                "count": int(row["count"] or 0),
                "total_amount": float(row["total_amount"] or 0),
            }
            for row in rows
        ]

        return Response({"data": data})


class AnalyticsTopCategoriesView(AnalyticsBaseAPIView):
    def get(self, request):
        limit = int(request.query_params.get("limit", 5))
        limit = max(1, min(limit, 20))

        base_queryset = self.get_filtered_queryset(request)

        rows = (
            base_queryset.exclude(Q(category__isnull=True) | Q(category=""))
            .values("category")
            .annotate(total_amount=Sum("amount"), total_count=Count("id"))
            .order_by("-total_amount", "-total_count")[:limit]
        )

        data = [
            {
                "category": row["category"],
                "total_amount": float(row["total_amount"] or 0),
                "total_count": int(row["total_count"] or 0),
            }
            for row in rows
        ]

        return Response({"limit": limit, "data": data})


class AnalyticsTopWorksitesView(AnalyticsBaseAPIView):
    def get(self, request):
        limit = int(request.query_params.get("limit", 5))
        limit = max(1, min(limit, 20))

        base_queryset = self.get_filtered_queryset(request)

        rows = (
            base_queryset.exclude(Q(worksite_standard__isnull=True) | Q(worksite_standard=""))
            .values("worksite_standard")
            .annotate(total_amount=Sum("amount"), total_count=Count("id"))
            .order_by("-total_amount", "-total_count")[:limit]
        )

        data = [
            {
                "worksite": row["worksite_standard"],
                "total_amount": float(row["total_amount"] or 0),
                "total_count": int(row["total_count"] or 0),
            }
            for row in rows
        ]

        return Response({"limit": limit, "data": data})


class AnalyticsCriticalPendingView(AnalyticsBaseAPIView):
    def _completeness_payload(self, expense):
        checks = {
            "amount": expense.amount is not None,
            "currency": bool((expense.currency or "").strip()),
            "category": bool((expense.category or "").strip()) and expense.category != "Sin Categoria",
            "supplier": bool((expense.supplier or "").strip()),
            "worksite_reported": bool((expense.worksite or "").strip()),
            "worksite_standard": bool((expense.worksite_standard or "").strip()),
            "paid_at": bool(expense.paid_at),
            "document_type": bool((expense.document_type or "").strip()),
            "expense_type": bool((expense.expense_type or "").strip()),
        }
        if expense.is_vehicle:
            checks["vehicle"] = bool((expense.vehicle or "").strip())

        total = len(checks)
        filled = sum(1 for ok in checks.values() if ok)
        pct = round((filled / total) * 100, 2) if total else 0.0
        return filled, total, pct

    def get(self, request):
        limit = int(request.query_params.get("limit", 30))
        limit = max(1, min(limit, 200))

        queryset = self.get_filtered_queryset(request, include_date_filters=False)
        pending_qs = (
            queryset.filter(status="pending")
            .select_related("created_by", "wa_sender")
            .order_by("message_sent_at", "created_at")
        )

        rows = []
        for exp in pending_qs[:limit]:
            filled, total, pct = self._completeness_payload(exp)
            date_for_sort = exp.message_sent_at or exp.created_at
            rows.append(
                {
                    "id": exp.id,
                    "created_at": exp.created_at.isoformat() if exp.created_at else None,
                    "message_sent_at": exp.message_sent_at.isoformat() if exp.message_sent_at else None,
                    "paid_at": exp.paid_at.isoformat() if exp.paid_at else None,
                    "supplier": exp.supplier or "",
                    "amount": float(exp.amount or 0),
                    "currency": exp.currency or "",
                    "category": exp.category or "",
                    "worksite": exp.worksite_standard or exp.worksite or "",
                    "status": exp.status,
                    "completeness": {
                        "filled": filled,
                        "total": total,
                        "pct": pct,
                    },
                    "_sort_date": date_for_sort,
                }
            )

        rows.sort(key=lambda item: (item["_sort_date"] or timezone.now(), -item["completeness"]["pct"], item["id"]))
        for row in rows:
            row.pop("_sort_date", None)

        return Response(
            {
                "count": len(rows),
                "data": rows,
            }
        )


class AnalyticsWorkerBreakdownView(AnalyticsBaseAPIView):
    def get(self, request):
        queryset = (
            self.get_filtered_queryset(request)
            .select_related("created_by", "wa_sender")
            .order_by("-created_at")
        )

        aggregated = {}
        for exp in queryset:
            if exp.wa_sender:
                label = f"{(exp.wa_sender.first_name or '').strip()} {(exp.wa_sender.last_name or '').strip()}".strip()
                reporter = label or exp.wa_sender.phone or "Sin usuario"
            elif exp.created_by:
                reporter = exp.created_by.get_full_name() or exp.created_by.email
            elif exp.wa_sender_phone:
                reporter = exp.wa_sender_phone
            else:
                reporter = "Sin usuario"

            if reporter not in aggregated:
                aggregated[reporter] = {
                    "reporter": reporter,
                    "total_count": 0,
                    "total_amount": Decimal("0"),
                }

            aggregated[reporter]["total_count"] += 1
            aggregated[reporter]["total_amount"] += exp.amount or Decimal("0")

        data = list(aggregated.values())
        data.sort(key=lambda item: (item["total_amount"], item["total_count"]), reverse=True)

        payload = [
            {
                "reporter": row["reporter"],
                "total_count": int(row["total_count"]),
                "total_amount": float(row["total_amount"]),
            }
            for row in data
        ]

        return Response({"data": payload})


class AnalyticsVariableAnalysisView(AnalyticsBaseAPIView):
    DIMENSION_FIELD_MAP = {
        "category": "category",
        "worksite": "worksite_standard",
        "supplier": "supplier",
        "vehicle": "vehicle",
        "status": "status",
    }

    def _dimension_label(self, dimension: str, raw_value):
        value = (raw_value or "").strip() if isinstance(raw_value, str) else raw_value
        if not value:
            return "Sin dato"
        if dimension == "status":
            return dict(Expense.STATUS).get(value, value)
        return value

    def get(self, request):
        metric = request.query_params.get("metric", "amount")
        metric = metric if metric in {"amount", "count"} else "amount"

        period = request.query_params.get("period", "ytd")
        mode = request.query_params.get("mode", "total")
        mode = mode if mode in {"total", "monthly"} else "total"

        dimension = request.query_params.get("dimension", "category")
        dimension = dimension if dimension in self.DIMENSION_FIELD_MAP else "category"
        dimension_field = self.DIMENSION_FIELD_MAP[dimension]

        top_n = int(request.query_params.get("top_n", 6))
        top_n = max(2, min(top_n, 12))

        period_start, period_end = self._resolve_period(period)
        date_field = self.get_date_field(request, default="paid_at")

        queryset = self.get_filtered_queryset(request, include_date_filters=False).filter(
            **{
                self._date_range_lookup(date_field, "gte"): period_start,
                self._date_range_lookup(date_field, "lte"): period_end,
            }
        ).exclude(**{f"{date_field}__isnull": True})

        aggregate_expr = Sum("amount") if metric == "amount" else Count("id")

        if mode == "total":
            rows = (
                queryset.values(dimension_field)
                .annotate(metric_value=aggregate_expr)
                .order_by("-metric_value")[:top_n]
            )

            labels = [self._dimension_label(dimension, row[dimension_field]) for row in rows]
            values = [float(row["metric_value"] or 0) for row in rows]

            return Response(
                {
                    "mode": "total",
                    "period": period,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "dimension": dimension,
                    "metric": metric,
                    "labels": labels,
                    "values": values,
                }
            )

        top_dimension_rows = (
            queryset.values(dimension_field)
            .annotate(metric_value=aggregate_expr)
            .order_by("-metric_value")[:top_n]
        )
        top_values = [self._dimension_label(dimension, row[dimension_field]) for row in top_dimension_rows]

        monthly_rows = (
            queryset.annotate(month=TruncMonth(date_field))
            .values("month", dimension_field)
            .annotate(metric_value=aggregate_expr)
            .order_by("month")
        )

        month_labels = []
        month_index = {}
        for row in monthly_rows:
            if not row["month"]:
                continue
            month_label = row["month"].strftime("%Y-%m")
            if month_label not in month_index:
                month_index[month_label] = len(month_labels)
                month_labels.append(month_label)

        if not month_labels:
            return Response(
                {
                    "mode": "monthly",
                    "period": period,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "dimension": dimension,
                    "metric": metric,
                    "categories": [],
                    "series": [],
                }
            )

        series_map = {label: [0.0] * len(month_labels) for label in top_values}
        series_map["Otros"] = [0.0] * len(month_labels)

        for row in monthly_rows:
            if not row["month"]:
                continue
            month_label = row["month"].strftime("%Y-%m")
            idx = month_index[month_label]
            dim_label = self._dimension_label(dimension, row[dimension_field])
            value = float(row["metric_value"] or 0)
            if dim_label in series_map:
                series_map[dim_label][idx] += value
            else:
                series_map["Otros"][idx] += value

        series = [{"name": label, "data": data} for label, data in series_map.items() if any(v != 0 for v in data)]

        return Response(
            {
                "mode": "monthly",
                "period": period,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "dimension": dimension,
                "metric": metric,
                "categories": month_labels,
                "series": series,
            }
        )


class AnalyticsDataQualityView(AnalyticsBaseAPIView):
    def get(self, request):
        base_queryset = self.get_filtered_queryset(request)

        total = base_queryset.count()
        if total == 0:
            return Response(
                {
                    "total": 0,
                    "completeness_pct": 0.0,
                    "metrics": {
                        "missing_amount": {"count": 0, "pct": 0.0},
                        "missing_category": {"count": 0, "pct": 0.0},
                        "missing_worksite": {"count": 0, "pct": 0.0},
                        "missing_supplier": {"count": 0, "pct": 0.0},
                        "without_attachment": {"count": 0, "pct": 0.0},
                    },
                }
            )

        missing_amount = base_queryset.filter(amount__isnull=True).count()
        missing_category = base_queryset.filter(Q(category__isnull=True) | Q(category="") | Q(category="Sin Categoria")).count()
        missing_worksite = base_queryset.filter(Q(worksite_standard__isnull=True) | Q(worksite_standard="")).count()
        missing_supplier = base_queryset.filter(Q(supplier__isnull=True) | Q(supplier="")).count()
        with_attachment = base_queryset.filter(attachments__isnull=False).values("id").distinct().count()
        without_attachment = total - with_attachment

        completeness_checks = [
            total - missing_amount,
            total - missing_category,
            total - missing_worksite,
            total - missing_supplier,
            with_attachment,
        ]
        completeness_ratio = sum(completeness_checks) / (total * len(completeness_checks))

        def metric_payload(count_value: int):
            return {
                "count": count_value,
                "pct": round((count_value / total) * 100, 2),
            }

        return Response(
            {
                "total": total,
                "completeness_pct": round(completeness_ratio * 100, 2),
                "metrics": {
                    "missing_amount": metric_payload(missing_amount),
                    "missing_category": metric_payload(missing_category),
                    "missing_worksite": metric_payload(missing_worksite),
                    "missing_supplier": metric_payload(missing_supplier),
                    "without_attachment": metric_payload(without_attachment),
                },
            }
        )
