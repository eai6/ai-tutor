"""The `ai-tutor` command.

An installed wheel has no manage.py, so without this the only way to run a
management command is::

    python -c "from django.core.management import execute_from_command_line as m; m(['x','migrate'])"

which is not something to put in a ministry's runbook.

Three jobs, in order:

1. Load configuration from a file before Django reads the environment. A
   systemd unit is a poor place to keep thirty `Environment=` lines and a
   signing key, and every deployment needs the same handful of values.
2. Point mutable state somewhere writable. site-packages is not, and is
   replaced on upgrade.
3. Provide the few commands an operator actually runs, and pass everything
   else through to Django so `createsuperuser`, `shell`, `dbshell` and the
   project's own commands keep working unchanged.

Plan: memory/pip_package_plan.md
"""
from __future__ import annotations

import os
import secrets
import shutil
import sys
from pathlib import Path

SETTINGS_MODULE = 'ai_tutor.config.settings'

#: Searched in order. The first that exists wins; AI_TUTOR_ENV_FILE overrides
#: the search entirely. /etc is first because that is where a system service's
#: configuration belongs and where an operator will look for it.
ENV_FILE_CANDIDATES = (
    Path('/etc/ai-tutor/ai-tutor.env'),
    Path.home() / '.config' / 'ai-tutor' / 'ai-tutor.env',
)

#: Values with no safe default. `init` generates the key and leaves the rest
#: for a person, because guessing a hostname produces a deployment that starts
#: and then rejects every request with a confusing 400.
REQUIRED = ('SECRET_KEY', 'ALLOWED_HOSTS', 'CSRF_TRUSTED_ORIGINS')


# ── configuration ──────────────────────────────────────────────────────────

def env_file_path() -> Path | None:
    """Where configuration is read from, or None if there is none yet."""
    explicit = os.getenv('AI_TUTOR_ENV_FILE')
    if explicit:
        return Path(explicit)
    for candidate in ENV_FILE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def load_env_file(path: Path | None) -> int:
    """Read KEY=value lines into the environment. Returns how many were set.

    The real environment always wins. A systemd `Environment=` line, a
    container's `-e`, or an operator's one-off `SECRET_KEY=... ai-tutor serve`
    must all override the file — otherwise overriding a single value for one
    run means editing the file and remembering to change it back.
    """
    if path is None or not path.is_file():
        return 0
    count = 0
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def default_data_dir() -> Path:
    """Where this deployment's state lives.

    /var/lib is correct for a system service, but only if we can actually
    write there — falling back keeps `ai-tutor` usable for someone evaluating
    it under their own account, which is how most first contact happens.
    """
    system = Path('/var/lib/ai-tutor')
    try:
        system.mkdir(parents=True, exist_ok=True)
        probe = system / '.write-test'
        probe.touch()
        probe.unlink()
        return system
    except OSError:
        return Path.home() / '.local' / 'share' / 'ai-tutor'


def bootstrap() -> None:
    """Prepare the environment, then hand over to Django."""
    load_env_file(env_file_path())
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', SETTINGS_MODULE)
    os.environ.setdefault('AI_TUTOR_DATA_DIR', str(default_data_dir()))
    Path(os.environ['AI_TUTOR_DATA_DIR']).mkdir(parents=True, exist_ok=True)


# ── commands ───────────────────────────────────────────────────────────────

ENV_TEMPLATE = """\
# AI Tutor configuration. Read by `ai-tutor` before Django starts.
#
# Anything set in the real environment overrides what is here, so a one-off
# `SECRET_KEY=... ai-tutor serve` works without editing this file.

# ── required ──────────────────────────────────────────────────────────────
# Signs sessions, password-reset links and CSRF tokens. Anyone who learns this
# can forge a login as any user, including staff. Generated once, below.
SECRET_KEY={secret_key}

# The hostname students will type. Comma-separated if more than one.
# Django rejects any other Host header; that is what stops a request smuggled
# through another domain being trusted.
ALLOWED_HOSTS=tutor.education.gov.xx

# The same, WITH the scheme. Without it every form POST fails CSRF validation
# — including the login form, with no useful error anywhere.
CSRF_TRUSTED_ORIGINS=https://tutor.education.gov.xx

# ── database ──────────────────────────────────────────────────────────────
# Postgres is strongly recommended. Left unset, a SQLite file is used in the
# data directory, which is fine for one small school and not beyond it —
# pgvector is unavailable there, so knowledge-base search falls back to a
# slower exact scan.
# DATABASE_URL=postgres://aitutor:PASSWORD@localhost:5432/aitutor

# ── serving ───────────────────────────────────────────────────────────────
# Whether something in front of this terminates TLS. If you run a reverse
# proxy (Caddy, nginx) with a certificate, leave it true.
#
# Set false ONLY when serving plain HTTP on an isolated network. Getting it
# wrong is confusing rather than obvious: login returns 403 with no error,
# because Django marks the session and CSRF cookies Secure and a browser will
# not send those over http://.
HTTPS_EDGE=true

# Leave False. True exposes tracebacks — configuration and fragments of
# student data — to anyone who can reach an error page.
DEBUG=False

# ── the tutor ─────────────────────────────────────────────────────────────
# At least one, or the tutor cannot answer. Which model serves which purpose
# is configured in the admin afterwards, not here.
# ANTHROPIC_API_KEY=
# OPENAI_API_KEY=
# GOOGLE_API_KEY=

# ── where state lives ─────────────────────────────────────────────────────
# Database, uploads and collected static. Must be writable, and must survive
# an upgrade — never point this inside the installed package.
AI_TUTOR_DATA_DIR={data_dir}
"""


def cmd_init(argv: list[str]) -> int:
    """Create the data directory and a configuration file to fill in."""
    data_dir = Path(os.environ['AI_TUTOR_DATA_DIR'])
    target = Path(os.getenv('AI_TUTOR_ENV_FILE') or '')
    if not target.name:
        # Prefer /etc when we can write it; otherwise the user's config dir.
        for candidate in ENV_FILE_CANDIDATES:
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                target = candidate
                break
            except OSError:
                continue

    if target.exists():
        print(f'Configuration already exists at {target}. Leaving it alone.')
        print(f'Data directory: {data_dir}')
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ENV_TEMPLATE.format(
        secret_key=secrets.token_urlsafe(64),
        data_dir=data_dir,
    ))
    # The signing key is in this file.
    target.chmod(0o600)

    print(f'Wrote {target} (mode 600) with a freshly generated SECRET_KEY.')
    print(f'Data directory: {data_dir}')
    print()
    print('Now edit that file and set:')
    for name in ('ALLOWED_HOSTS', 'CSRF_TRUSTED_ORIGINS'):
        print(f'  {name}')
    print('  DATABASE_URL          (recommended; SQLite is used without it)')
    print('  one provider API key  (or the tutor cannot answer)')
    print()
    print('Then:  ai-tutor migrate && ai-tutor seed && ai-tutor createsuperuser')
    return 0


def cmd_serve(argv: list[str]) -> int:
    """Run the application under gunicorn."""
    import argparse

    parser = argparse.ArgumentParser(prog='ai-tutor serve')
    parser.add_argument('--bind', default=os.getenv('AI_TUTOR_BIND', '0.0.0.0:8000'))
    parser.add_argument('--workers', type=int, default=int(os.getenv('AI_TUTOR_WORKERS', '4')))
    parser.add_argument('--threads', type=int, default=int(os.getenv('AI_TUTOR_THREADS', '4')))
    # 300, not gunicorn's 30. A tutoring turn takes 20-90 seconds and longer on
    # modest hardware; the default kills the worker mid-answer.
    parser.add_argument('--timeout', type=int, default=int(os.getenv('AI_TUTOR_TIMEOUT', '300')))
    args = parser.parse_args(argv)

    missing = [name for name in REQUIRED if not os.getenv(name)]
    if missing:
        print(f'Refusing to serve: {", ".join(missing)} not set.', file=sys.stderr)
        print(f'Run `ai-tutor init`, or see {env_file_path() or "the config file"}.',
              file=sys.stderr)
        return 2

    try:
        from gunicorn.app.base import BaseApplication
    except ImportError:
        print('gunicorn is not installed in this environment.', file=sys.stderr)
        return 2

    class Served(BaseApplication):
        def load_config(self):
            for key, value in {
                'bind': args.bind, 'workers': args.workers,
                'threads': args.threads, 'timeout': args.timeout,
                'graceful_timeout': 30, 'accesslog': '-', 'errorlog': '-',
            }.items():
                self.cfg.set(key, value)

        def load(self):
            from ai_tutor.config.wsgi import application
            return application

    print(f'Serving on {args.bind} (data: {os.environ["AI_TUTOR_DATA_DIR"]})')
    Served().run()
    return 0


def cmd_seed(argv: list[str]) -> int:
    """Import the curriculum bundled in this release."""
    from django.conf import settings
    from django.core.management import execute_from_command_line

    pack = Path(settings.PACKAGE_DIR) / 'seed' / 'curriculum-pack.tar.gz'
    if not pack.exists():
        print(f'No bundled curriculum at {pack}.', file=sys.stderr)
        return 1
    execute_from_command_line(['ai-tutor', 'import_curriculum_pack', str(pack),
                               '--if-empty', *argv])
    return 0


def cmd_check(argv: list[str]) -> int:
    """What a deployment should look like before it faces students."""
    from django.core.management import execute_from_command_line
    execute_from_command_line(['ai-tutor', 'check', '--deploy', *argv])
    return 0


SYSTEMD_UNIT = """\
# /etc/systemd/system/ai-tutor.service
#
#   systemctl daemon-reload && systemctl enable --now ai-tutor
#
[Unit]
Description=AI Tutor
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User={user}
# Both paths are passed explicitly rather than left to discovery. A service
# does not inherit the shell that ran `ai-tutor systemd`, so without these it
# would look in /etc and /var/lib and quietly find neither.
Environment=AI_TUTOR_ENV_FILE={env_file}
Environment=AI_TUTOR_DATA_DIR={data_dir}
ExecStart={executable} serve
Restart=on-failure
RestartSec=5

# The application writes only to its data directory.
ReadWritePaths={data_dir}
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
# {home_note}
ProtectHome={protect_home}

[Install]
WantedBy=multi-user.target
"""


def cmd_systemd(argv: list[str]) -> int:
    """Print a unit file with this installation's real paths filled in."""
    executable = shutil.which('ai-tutor') or str(Path(sys.executable).parent / 'ai-tutor')
    env_file = env_file_path() or ENV_FILE_CANDIDATES[0]
    data_dir = Path(os.environ['AI_TUTOR_DATA_DIR'])

    # ProtectHome=yes makes /home unreadable to the service. That is the right
    # default for a system install, and fatal if `init` put the config or the
    # data under a user's home — the service would start and then be unable to
    # read its own configuration.
    home = Path.home()
    in_home = any(_is_within(p, home) for p in (env_file, data_dir))
    protect_home = 'read-only' if in_home else 'yes'
    home_note = ('Config or data lives under /home, so this is read-only rather '
                 'than yes.' if in_home
                 else 'Nothing this service needs lives under /home.')

    print(SYSTEMD_UNIT.format(
        user=os.getenv('SUDO_USER') or os.getenv('USER') or 'ai-tutor',
        executable=executable,
        data_dir=data_dir,
        env_file=env_file,
        protect_home=protect_home,
        home_note=home_note,
    ), end='')
    return 0


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


COMMANDS = {
    'init': cmd_init,
    'serve': cmd_serve,
    'seed': cmd_seed,
    'check': cmd_check,
    'systemd': cmd_systemd,
}

USAGE = """\
ai-tutor — a conversational tutoring platform

  ai-tutor init             create the data directory and a config file
  ai-tutor migrate          create or update the database
  ai-tutor seed             load the curriculum bundled in this release
  ai-tutor createsuperuser  create the first administrator
  ai-tutor serve            run the application
  ai-tutor check            report anything unsafe about this deployment
  ai-tutor systemd          print a systemd unit for this installation

Any other Django management command works too, e.g. `ai-tutor collectstatic`.
Configuration: {env_file}
Data:          {data_dir}
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `init` must run before there is any configuration, so bootstrap has to be
    # tolerant of a completely unconfigured machine. It is: it only reads a file
    # if one exists and creates the data directory.
    bootstrap()

    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE.format(env_file=env_file_path() or '(none yet — run `ai-tutor init`)',
                           data_dir=os.environ['AI_TUTOR_DATA_DIR']))
        return 0

    command, rest = argv[0], argv[1:]
    if command in COMMANDS:
        return COMMANDS[command](rest) or 0

    # Everything else is Django's. Keeping this passthrough is what makes the
    # project's own management commands available without wrapping each one.
    from django.core.management import execute_from_command_line
    execute_from_command_line(['ai-tutor', command, *rest])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
