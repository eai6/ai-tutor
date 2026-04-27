"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve
from django.shortcuts import redirect
from django.http import FileResponse

from apps.dashboard.views_health import health_check


def service_worker(_request):
    """Serve sw.js from the site root so its scope can cover the
    whole app. Service workers can't control paths above their own
    URL unless the response includes Service-Worker-Allowed."""
    path_to_sw = settings.BASE_DIR / 'static' / 'pwa' / 'sw.js'
    response = FileResponse(open(path_to_sw, 'rb'), content_type='application/javascript')
    response['Cache-Control'] = 'no-cache'
    response['Service-Worker-Allowed'] = '/'
    return response


urlpatterns = [
    path('health/', health_check),
    path('admin/', admin.site.urls),
    path('api/v1/', include('apps.api.urls', namespace='api')),
    # PWA service worker — must live at the site root so it can
    # control all paths under /. The actual file is in static/pwa/.
    path('sw.js', service_worker, name='service_worker'),
    path('', include('apps.accounts.urls')),  # Landing page at root
    path('tutor/', include('apps.tutoring.urls')),
    path('dashboard/', include('apps.dashboard.urls')),
]

# Always serve media (Azure Files mount in production)
urlpatterns += [
    path('media/<path:path>', serve, {'document_root': settings.MEDIA_ROOT}),
]
