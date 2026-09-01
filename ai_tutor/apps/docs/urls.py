"""Routes for the public documentation site."""

from django.urls import path

from . import views

app_name = 'docs'

urlpatterns = [
    path('', views.index, name='index'),
    # Before the slug route, or it is read as a section named "search-index".
    path('search-index.json', views.search_index, name='search_index'),
    # Slugs are matched against the hand-written index in playbook.py, so the
    # loose <str:> here cannot reach a template the index does not name.
    path('<slug:slug>/', views.section, name='section'),
]
