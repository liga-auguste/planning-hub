from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('refresh/', views.refresh, name='refresh'),
]