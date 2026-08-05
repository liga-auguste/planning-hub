from django.urls import path
from . import planner_views

urlpatterns = [
    path('', planner_views.planner_start, name='planner_start'),
    path('questions/', planner_views.planner_questions, name='planner_questions'),
    path('review/', planner_views.planner_review, name='planner_review'),
    path('create/', planner_views.planner_create, name='planner_create'),
    path('regeln/', planner_views.rules_list, name='rules_list'),
    path('regeln/add/', planner_views.rule_add, name='rule_add'),
    path('regeln/toggle/<int:rule_id>/', planner_views.rule_toggle, name='rule_toggle'),
    path('regeln/update/<int:rule_id>/', planner_views.rule_update, name='rule_update'),
    path('regeln/delete/<int:rule_id>/', planner_views.rule_delete, name='rule_delete'),
    path('regeln/reorder/', planner_views.rule_reorder, name='rule_reorder'),
]
