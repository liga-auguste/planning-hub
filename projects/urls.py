from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('refresh/', views.refresh, name='refresh'),
    path('task/<str:task_id>/toggle/', views.toggle_task_view, name='toggle_task'),
    path('task/<str:task_id>/reschedule/', views.reschedule_task_view, name='reschedule_task'),
]