from django.urls import path
from .views import (
    dashboard,
    expense_action,
    expense_create,
    expense_detail,
    expense_list,
    attachment_serve,
    settings_view,
    settings_categories,
    settings_expense_types,
    settings_users,
    settings_vehicles,
    settings_worksites,
)
urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("gastos/", expense_list, name="expense_list"),
    path("expenses/create/", expense_create, name="expense_create"),
    path("expenses/<int:pk>/", expense_detail, name="expense_detail"),
    path("expenses/<int:pk>/action/<str:action>/", expense_action, name="expense_action"),
    path("attachments/<int:pk>/", attachment_serve, name="attachment_serve"),
    path("configuracion/", settings_view, name="settings"),
    path("configuracion/usuarios/", settings_users, name="settings_users"),
    path("configuracion/vehiculos/", settings_vehicles, name="settings_vehicles"),
    path("configuracion/obras/", settings_worksites, name="settings_worksites"),
    path("configuracion/categorias/", settings_categories, name="settings_categories"),
    path("configuracion/tipos-gasto/", settings_expense_types, name="settings_expense_types"),
]
