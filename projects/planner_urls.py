from django.urls import path
from . import planner_views

urlpatterns = [
    path('', planner_views.planner_start, name='planner_start'),
    path('questions/', planner_views.planner_questions, name='planner_questions'),
    path('review/', planner_views.planner_review, name='planner_review'),
    path('create/', planner_views.planner_create, name='planner_create'),
]