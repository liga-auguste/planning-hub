from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('mein-plan/', views.my_plan, name='my_plan'),
    path('stats/', views.stats, name='stats'),
    path('mein-plan/download/', views.download_plan, name='download_plan'),
    path('refresh/', views.refresh, name='refresh'),
    path('task/<str:task_id>/toggle/', views.toggle_task_view, name='toggle_task'),
    path('task/<str:task_id>/reschedule/', views.reschedule_task_view, name='reschedule_task'),
    path('session-task/<str:task_id>/toggle/', views.toggle_session_task, name='toggle_session_task'),
    path('timelapse/', views.set_timelapse_date, name='set_timelapse_date'),
    path('timelapse/preload/', views.preload_timelapse_summary, name='preload_timelapse_summary'),
    path('impressum/', views.impressum, name='impressum'),
    path('datenschutz/', views.datenschutz, name='datenschutz'),
]
