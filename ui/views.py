from django.shortcuts import render


def privacy_policy(request):
    return render(request, "privacy_policy.html")


def data_deletion(request):
    return render(request, "data_deletion.html")


def terms_of_service(request):
    return render(request, "terms_of_service.html")
