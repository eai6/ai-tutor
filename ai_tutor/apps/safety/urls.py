"""
Safety app URL patterns.
"""

from django.urls import path
from . import views

app_name = 'safety'

urlpatterns = [
    # Privacy dashboard
    path('privacy/', views.privacy_dashboard, name='privacy_dashboard'),
    
    # Consent management
    path('consent/<str:consent_type>/', views.update_consent, name='update_consent'),
    
    # Data portability (GDPR)
    path('export/', views.export_my_data, name='export_data'),
    path('delete/', views.delete_my_data, name='delete_data'),
    
    # Legal pages
    path('privacy-policy/', views.privacy_policy, name='privacy_policy'),
    path('terms/', views.terms_of_service, name='terms_of_service'),
    
    # Parental consent
    path('parental-consent/', views.parental_consent_form, name='parental_consent'),
]
