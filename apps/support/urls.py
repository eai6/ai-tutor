"""Help-assistant URL routes — mounted under /support/."""

from django.urls import path

from . import views

app_name = 'support'

urlpatterns = [
    path('chat/start/', views.chat_start, name='chat_start'),
    path('chat/<int:conv_id>/message/', views.chat_message, name='chat_message'),
    path('chat/<int:conv_id>/confirm/', views.chat_confirm, name='chat_confirm'),
    path('chat/<int:conv_id>/escalate/', views.chat_escalate, name='chat_escalate'),
]
