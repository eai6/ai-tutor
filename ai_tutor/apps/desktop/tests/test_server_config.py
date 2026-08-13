"""The desktop's one connection setting: which server to send work to."""
import pytest

from ai_tutor.apps.desktop import server_config
from ai_tutor.apps.desktop.models import DeviceState


class TestNormalise:

    @pytest.mark.parametrize('typed,expected', [
        ('tutor.education.gov.xx', 'https://tutor.education.gov.xx'),
        ('https://tutor.education.gov.xx', 'https://tutor.education.gov.xx'),
        ('https://tutor.education.gov.xx/', 'https://tutor.education.gov.xx'),
        # Pasted out of the browser address bar, path and all.
        ('https://tutor.education.gov.xx/dashboard/students/', 'https://tutor.education.gov.xx'),
        ('https://tutor.education.gov.xx:8443', 'https://tutor.education.gov.xx:8443'),
        ('  tutor.education.gov.xx  ', 'https://tutor.education.gov.xx'),
    ])
    def test_accepts_what_people_actually_type(self, typed, expected):
        assert server_config.normalise(typed) == expected

    def test_a_bare_hostname_becomes_https_not_http(self):
        assert server_config.normalise('school.example').startswith('https://')

    @pytest.mark.parametrize('bad', ['', '   ', 'ftp://school.example', 'https://'])
    def test_rejects_what_is_not_a_server(self, bad):
        with pytest.raises(server_config.ServerConfigError):
            server_config.normalise(bad)

    def test_refuses_plain_http_to_a_public_host(self, monkeypatch):
        """Student work would cross the internet in the clear."""
        monkeypatch.setattr(server_config, '_is_local_address', lambda h: False)
        with pytest.raises(server_config.ServerConfigError) as exc:
            server_config.normalise('http://tutor.education.gov.xx')
        assert 'unencrypted' in str(exc.value).lower()

    @pytest.mark.parametrize('host', [
        'http://localhost:8000', 'http://127.0.0.1:8000', 'http://192.168.1.50',
    ])
    def test_allows_plain_http_on_the_school_network(self, host):
        """A LAN deployment with no certificate is real, not a mistake."""
        assert server_config.normalise(host).startswith('http://')

    @pytest.mark.parametrize('typed,expected', [
        ('127.0.0.1:8890', 'http://127.0.0.1:8890'),
        ('localhost:8000', 'http://localhost:8000'),
        ('192.168.1.50', 'http://192.168.1.50'),
    ])
    def test_a_bare_address_on_this_network_defaults_to_http(self, typed, expected):
        """A school server on the LAN is almost never running TLS, so an
        https:// default would fail to connect for someone who typed exactly
        the right address."""
        assert server_config.normalise(typed) == expected


@pytest.mark.django_db
class TestSaveAndClear:

    def test_save_stores_the_normalised_url(self):
        server_config.save('tutor.education.gov.xx/dashboard/')
        assert DeviceState.load().server_url == 'https://tutor.education.gov.xx'

    def test_a_rejected_url_is_not_stored(self):
        with pytest.raises(server_config.ServerConfigError):
            server_config.save('not a server')
        assert DeviceState.load().server_url == ''

    def test_clear_returns_the_device_to_offline_only(self):
        server_config.save('school.example')
        server_config.clear()
        assert DeviceState.load().server_url == ''

    def test_clear_keeps_the_institution_binding(self):
        """Clearing the address must not orphan the lessons on the device."""
        state = DeviceState.load()
        state.institution_id = 4
        state.save()
        server_config.save('school.example')
        server_config.clear()
        assert DeviceState.load().institution_id == 4


@pytest.mark.django_db
class TestEffectiveServerUrl:

    def test_uses_the_saved_address(self, settings):
        settings.SYNC_SERVER_URL = ''
        server_config.save('school.example')
        assert DeviceState.load().effective_server_url == 'https://school.example'

    def test_the_setting_overrides_the_saved_address(self, settings):
        """A scripted rollout pins the address; the screen must not fight it."""
        settings.SYNC_SERVER_URL = 'https://pinned.example'
        server_config.save('typed-by-hand.example')
        assert DeviceState.load().effective_server_url == 'https://pinned.example'
        assert server_config.status()['pinned_by_admin'] is True

    def test_nothing_set_means_offline(self, settings):
        settings.SYNC_SERVER_URL = ''
        assert DeviceState.load().effective_server_url == ''

    def test_sync_endpoint_follows_the_saved_address(self, settings):
        from ai_tutor.apps.desktop import sync
        settings.SYNC_SERVER_URL = ''
        server_config.save('school.example')
        assert sync._endpoint('/api/v1/devices/sync/') == \
            'https://school.example/api/v1/devices/sync/'

    def test_sync_endpoint_is_none_when_offline(self, settings):
        from ai_tutor.apps.desktop import sync
        settings.SYNC_SERVER_URL = ''
        assert sync._endpoint('/api/v1/devices/sync/') is None


@pytest.mark.django_db
class TestScreen:

    def test_shows_the_form(self, client):
        r = client.get('/desktop/server/')
        assert r.status_code == 200
        assert b'server_url' in r.content

    def test_saving_a_good_address_confirms_it(self, client):
        r = client.post('/desktop/server/save/', {'server_url': 'school.example'})
        assert r.status_code == 200
        assert DeviceState.load().server_url == 'https://school.example'

    def test_a_bad_address_reports_the_reason(self, client):
        r = client.post('/desktop/server/save/', {'server_url': ''})
        assert r.status_code == 400
        assert DeviceState.load().server_url == ''

    def test_clearing_works_from_the_screen(self, client):
        server_config.save('school.example')
        r = client.post('/desktop/server/clear/', {})
        assert r.status_code == 200
        assert DeviceState.load().server_url == ''


@pytest.mark.django_db
class TestReachability:
    """The screen is useless if nothing links to it — the shell has no address
    bar, so an unlinked page cannot be reached at all."""

    def test_setup_screen_links_to_it(self, client):
        r = client.get('/desktop/setup/')
        assert r.status_code == 200
        assert b'/desktop/server/' in r.content

    def test_server_screen_links_back_to_setup(self, client):
        """Otherwise it is a dead end: no address bar, no back button."""
        r = client.get('/desktop/server/')
        assert b'/desktop/setup/' in r.content

    def test_settings_page_shows_the_panel_on_a_desktop_build(self, settings, client, django_user_model):
        settings.DESKTOP_BUILD = True
        user = django_user_model.objects.create_user('s1', password='pw12345678')
        client.force_login(user)
        r = client.get('/settings/')
        assert b'/desktop/server/' in r.content

    def test_settings_page_hides_the_panel_on_the_hosted_app(self, settings, client, django_user_model):
        """A hosted deployment has no 'this computer' to configure."""
        settings.DESKTOP_BUILD = False
        user = django_user_model.objects.create_user('s2', password='pw12345678')
        client.force_login(user)
        r = client.get('/settings/')
        assert b'/desktop/server/' not in r.content
