from django import forms
from django.contrib.auth import authenticate, login, logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render


class LoginForm(forms.Form):
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput)


def login_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = LoginForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        user = authenticate(
            request,
            username=form.cleaned_data["email"],
            password=form.cleaned_data["password"],
        )
        if user is not None:
            login(request, user)
            return redirect(request.GET.get("next") or "dashboard")
        form.add_error(None, "Invalid email or password.")

    return render(request, "login.html", {"form": form})


def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("login")
