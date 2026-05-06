from django.contrib.auth.views import PasswordChangeDoneView, PasswordChangeView
from django.urls import reverse_lazy

from .models import UserAuditLog


class LoggedPasswordChangeView(PasswordChangeView):
    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("password_change_done")

    def form_valid(self, form):
        response = super().form_valid(form)
        UserAuditLog.objects.create(
            actor=self.request.user,
            target_user=self.request.user,
            action="password_changed",
            changes={"source": {"before": None, "after": "self_service"}},
        )
        return response


class LoggedPasswordChangeDoneView(PasswordChangeDoneView):
    template_name = "registration/password_change_done.html"
