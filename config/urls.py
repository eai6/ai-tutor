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
from django.views.i18n import JavaScriptCatalog

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
    # Public installer download page. No auth: someone who has never logged
    # in has to be able to install the app.
    path('download/', include('apps.desktop.download_urls')),
    path('admin/', admin.site.urls),
    path('api/v1/', include('apps.api.urls', namespace='api')),
    # Bootstrap status for the offline desktop shell's splash screen.
    # Inert on the server — nothing links to it. See apps/desktop/views.py.
    path('desktop/', include('apps.desktop.urls', namespace='desktop')),
    # i18n JS catalog so client-side gettext() calls can resolve the
    # same translations the templates use. Empty until M2/M3 ship
    # actual translations.
    path('jsi18n/', JavaScriptCatalog.as_view(), name='javascript-catalog'),
    # Standard Django language picker — accepts POST with `language=<code>`
    # and sets the LANGUAGE_SESSION_KEY cookie. The landing page footer
    # uses this so anonymous visitors can pick pt-mz before logging in
    # (they have no StudentProfile.preferred_locale yet).
    path('i18n/', include('django.conf.urls.i18n')),
    # PWA service worker — must live at the site root so it can
    # control all paths under /. The actual file is in static/pwa/.
    path('sw.js', service_worker, name='service_worker'),
    path('', include('apps.accounts.urls')),  # Landing page at root
    path('tutor/', include('apps.tutoring.urls')),
    path('dashboard/', include('apps.dashboard.urls')),
    path('support/', include('apps.support.urls')),
]

# Serve media on our own domain (school-network-friendly, behind the WAF).
# Azure and AWS deployments run the same image, so both streamers stay wired up
# and the configured one wins: S3 on ECS, Azure Blob on Container Apps, each
# range-capable with a filesystem fallback for anything not yet migrated.
# With neither configured, serve straight from the local media directory.
if getattr(settings, 'USE_S3_MEDIA', False):
    from apps.media_library.s3_media import serve_media
    urlpatterns += [
        path('media/<path:path>', serve_media),
    ]
elif getattr(settings, 'USE_BLOB_MEDIA', False):
    from apps.media_library.blob_media import serve_media
    urlpatterns += [
        path('media/<path:path>', serve_media),
    ]
else:
    urlpatterns += [
        path('media/<path:path>', serve, {'document_root': settings.MEDIA_ROOT}),
    ]
