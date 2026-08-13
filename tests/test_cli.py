"""The `ai-tutor` command.

These pin the behaviours that decide whether a deployment is configured
correctly or merely appears to be. Most of them are about precedence and
refusal — the cases where doing something reasonable-looking would be worse
than doing nothing.
"""
import os
import stat
from pathlib import Path

import pytest

from ai_tutor import cli


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ('AI_TUTOR_ENV_FILE', 'AI_TUTOR_DATA_DIR', 'DJANGO_SETTINGS_MODULE',
                 'SECRET_KEY', 'ALLOWED_HOSTS', 'CSRF_TRUSTED_ORIGINS'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('AI_TUTOR_DATA_DIR', str(tmp_path / 'data'))
    return tmp_path


class TestEnvFile:

    def test_reads_key_values(self, clean_env, monkeypatch):
        f = clean_env / 'a.env'
        f.write_text('ALLOWED_HOSTS=school.example\nDEBUG=False\n')
        monkeypatch.delenv('ALLOWED_HOSTS', raising=False)
        assert cli.load_env_file(f) == 2
        assert os.environ['ALLOWED_HOSTS'] == 'school.example'

    def test_the_real_environment_wins(self, clean_env, monkeypatch):
        """A systemd Environment= line, a container's -e, or a one-off
        `SECRET_KEY=... ai-tutor serve` must all beat the file. Otherwise
        overriding one value for one run means editing the file and
        remembering to change it back."""
        f = clean_env / 'a.env'
        f.write_text('ALLOWED_HOSTS=from-file\n')
        monkeypatch.setenv('ALLOWED_HOSTS', 'from-environment')
        cli.load_env_file(f)
        assert os.environ['ALLOWED_HOSTS'] == 'from-environment'

    def test_ignores_comments_and_blanks(self, clean_env, monkeypatch):
        f = clean_env / 'a.env'
        f.write_text('# a comment\n\nALLOWED_HOSTS=x\n   \n')
        monkeypatch.delenv('ALLOWED_HOSTS', raising=False)
        assert cli.load_env_file(f) == 1

    def test_strips_quotes(self, clean_env, monkeypatch):
        f = clean_env / 'a.env'
        f.write_text('SECRET_KEY="quoted-value"\n')
        monkeypatch.delenv('SECRET_KEY', raising=False)
        cli.load_env_file(f)
        assert os.environ['SECRET_KEY'] == 'quoted-value'

    def test_a_missing_file_is_not_an_error(self, clean_env):
        """`init` has to run on a machine with no configuration at all."""
        assert cli.load_env_file(clean_env / 'nope.env') == 0
        assert cli.load_env_file(None) == 0

    def test_explicit_path_overrides_the_search(self, clean_env, monkeypatch):
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(clean_env / 'chosen.env'))
        assert cli.env_file_path() == clean_env / 'chosen.env'


class TestInit:

    def test_writes_a_config_with_a_generated_key(self, clean_env, monkeypatch, capsys):
        target = clean_env / 'ai-tutor.env'
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(target))
        cli.bootstrap()
        assert cli.cmd_init([]) == 0

        body = target.read_text()
        key = next(l for l in body.splitlines() if l.startswith('SECRET_KEY='))
        assert len(key.split('=', 1)[1]) >= 40

    def test_the_key_differs_every_time(self, clean_env, monkeypatch):
        keys = set()
        for i in range(2):
            target = clean_env / f'{i}.env'
            monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(target))
            cli.bootstrap()
            cli.cmd_init([])
            keys.add(next(l for l in target.read_text().splitlines()
                          if l.startswith('SECRET_KEY=')))
        assert len(keys) == 2

    def test_the_config_is_not_world_readable(self, clean_env, monkeypatch):
        """It holds the signing key — anyone who reads it can forge a staff login."""
        target = clean_env / 'ai-tutor.env'
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(target))
        cli.bootstrap()
        cli.cmd_init([])
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, oct(mode)

    def test_never_overwrites_an_existing_config(self, clean_env, monkeypatch, capsys):
        """Re-running init must not rotate the key out from under a live
        deployment — that logs every user out and voids password-reset links."""
        target = clean_env / 'ai-tutor.env'
        target.write_text('SECRET_KEY=original\n')
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(target))
        cli.bootstrap()
        cli.cmd_init([])
        assert target.read_text() == 'SECRET_KEY=original\n'

    def test_creates_the_data_directory(self, clean_env, monkeypatch):
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(clean_env / 'a.env'))
        cli.bootstrap()
        assert Path(os.environ['AI_TUTOR_DATA_DIR']).is_dir()


class TestServeRefusals:

    def test_refuses_without_the_required_settings(self, clean_env, monkeypatch, capsys):
        """Serving with a missing SECRET_KEY would either crash later or run on
        a known key. Refusing up front says which value is missing."""
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(clean_env / 'absent.env'))
        cli.bootstrap()
        assert cli.cmd_serve([]) == 2
        err = capsys.readouterr().err
        for name in cli.REQUIRED:
            assert name in err


class TestSystemdUnit:

    def _unit(self, capsys):
        cli.cmd_systemd([])
        return capsys.readouterr().out

    def test_passes_both_paths_explicitly(self, clean_env, monkeypatch, capsys):
        """A service does not inherit the shell that generated the unit. Without
        these it looks in /etc and /var/lib and quietly finds neither."""
        env_file = clean_env / 'a.env'
        env_file.write_text('SECRET_KEY=x\n')
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(env_file))
        cli.bootstrap()
        unit = self._unit(capsys)
        assert f'Environment=AI_TUTOR_ENV_FILE={env_file}' in unit
        assert f'Environment=AI_TUTOR_DATA_DIR={os.environ["AI_TUTOR_DATA_DIR"]}' in unit

    def test_protect_home_relaxes_when_config_lives_under_home(self, monkeypatch, tmp_path, capsys):
        """ProtectHome=yes hides /home from the service. With the config there,
        the unit would start and then fail to read its own settings."""
        home = tmp_path / 'home'
        (home / '.config').mkdir(parents=True)
        monkeypatch.setattr(Path, 'home', staticmethod(lambda: home))
        env_file = home / '.config' / 'ai-tutor.env'
        env_file.write_text('SECRET_KEY=x\n')
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(env_file))
        monkeypatch.setenv('AI_TUTOR_DATA_DIR', str(home / 'data'))
        cli.bootstrap()
        assert 'ProtectHome=read-only' in self._unit(capsys)

    def test_protect_home_stays_strict_for_a_system_install(self, clean_env, monkeypatch, capsys):
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(clean_env / 'a.env'))
        monkeypatch.setenv('AI_TUTOR_DATA_DIR', str(clean_env / 'data'))
        cli.bootstrap()
        assert 'ProtectHome=yes' in self._unit(capsys)

    def test_is_a_parseable_unit(self, clean_env, monkeypatch, capsys):
        import configparser
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(clean_env / 'a.env'))
        cli.bootstrap()
        parser = configparser.ConfigParser(strict=False)
        parser.read_string(self._unit(capsys))
        assert parser.sections() == ['Unit', 'Service', 'Install']
        assert parser['Service']['ExecStart'].endswith(' serve')


class TestDispatch:

    def test_help_lists_the_operator_commands(self, clean_env, monkeypatch, capsys):
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(clean_env / 'a.env'))
        assert cli.main([]) == 0
        out = capsys.readouterr().out
        for name in ('init', 'migrate', 'seed', 'serve', 'check', 'systemd'):
            assert name in out

    def test_unknown_commands_go_to_django(self, clean_env, monkeypatch):
        """The passthrough is what keeps createsuperuser, shell and the
        project's own management commands working without wrapping each one."""
        seen = {}
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(clean_env / 'a.env'))
        monkeypatch.setattr('django.core.management.execute_from_command_line',
                            lambda argv: seen.setdefault('argv', argv))
        cli.main(['createsuperuser', '--noinput'])
        assert seen['argv'][1:] == ['createsuperuser', '--noinput']

    def test_sets_the_settings_module(self, clean_env, monkeypatch):
        monkeypatch.setenv('AI_TUTOR_ENV_FILE', str(clean_env / 'a.env'))
        cli.bootstrap()
        assert os.environ['DJANGO_SETTINGS_MODULE'] == cli.SETTINGS_MODULE
