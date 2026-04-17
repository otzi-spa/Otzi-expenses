from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AnalyticsCriticalPendingView,
    AnalyticsDataQualityView,
    AnalyticsKPIView,
    AnalyticsMonthlyTrendView,
    AnalyticsStatusBreakdownView,
    AnalyticsTopCategoriesView,
    AnalyticsTopWorksitesView,
    AnalyticsVariableAnalysisView,
    AnalyticsWorkerBreakdownView,
    AttachmentViewSet,
    ExpenseViewSet,
)

router = DefaultRouter()
router.register(r"expenses", ExpenseViewSet, basename="expense")
router.register(r"attachments", AttachmentViewSet, basename="attachment")

urlpatterns = [
    path("analytics/kpis/", AnalyticsKPIView.as_view(), name="analytics_kpis"),
    path("analytics/monthly-trend/", AnalyticsMonthlyTrendView.as_view(), name="analytics_monthly_trend"),
    path("analytics/status-breakdown/", AnalyticsStatusBreakdownView.as_view(), name="analytics_status_breakdown"),
    path("analytics/top-categories/", AnalyticsTopCategoriesView.as_view(), name="analytics_top_categories"),
    path("analytics/top-worksites/", AnalyticsTopWorksitesView.as_view(), name="analytics_top_worksites"),
    path("analytics/critical-pending/", AnalyticsCriticalPendingView.as_view(), name="analytics_critical_pending"),
    path("analytics/worker-breakdown/", AnalyticsWorkerBreakdownView.as_view(), name="analytics_worker_breakdown"),
    path("analytics/variable-analysis/", AnalyticsVariableAnalysisView.as_view(), name="analytics_variable_analysis"),
    path("analytics/data-quality/", AnalyticsDataQualityView.as_view(), name="analytics_data_quality"),
]

urlpatterns += router.urls
