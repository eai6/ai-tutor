# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the offline AI Tutor desktop app.

    pyinstaller AI-Tutor.spec --noconfirm

One binary that plays two roles: launched plainly it opens the window;
launched with ``--serve`` it runs the Django server (see desktop.py::main).
That is why there is a single Analysis here and not two — a frozen build has
no interpreter to hand a script to, so the app has to be able to re-invoke
itself.

Django resists freezing in three specific ways, each handled below:

  1. **Apps are discovered by string.** ``INSTALLED_APPS`` names modules that
     nothing imports directly, so PyInstaller's static analysis never sees
     them. Same for every ``migrations/`` module, which the migration loader
     imports by name at runtime. Both are collected explicitly.
  2. **Templates and static files are data, not code.** They must be copied in
     and then found again at runtime — settings computes paths from
     ``BASE_DIR``, which under PyInstaller is the unpack directory.
  3. **Database backends and serializers are imported by string** from
     settings, so they need to be named as hidden imports too.

Plan: memory/desktop_offline_app_plan.md
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = Path(os.path.abspath(SPECPATH))
IS_WIN = sys.platform.startswith('win')
IS_MAC = sys.platform == 'darwin'

# ── Application code that is only ever imported by name ─────────────────
DJANGO_APPS = [
    'apps.accounts', 'apps.curriculum', 'apps.media_library', 'apps.tutoring',
    'apps.llm', 'apps.safety', 'apps.dashboard', 'apps.api', 'apps.support',
    'apps.benchmark', 'apps.desktop',
]

hiddenimports = [
    'config.settings',
    'config.settings_desktop',
    'config.urls',
    'config.wsgi',
    'desktop_server',
    # Named as strings in settings: DATABASES['default']['ENGINE'] and the
    # serializer registry. Nothing imports them, so nothing pulls them in.
    'django.db.backends.sqlite3',
    'django.core.serializers.json',
    'django.template.loaders.filesystem',
    'django.template.loaders.app_directories',
    'waitress',
    'onnxruntime',
    'tokenizers',
]

# Third-party packages that settings references BY STRING — STORAGES backends,
# MIDDLEWARE entries, DRF classes. Naming the top-level package is not enough:
# it collects only __init__, so the first string import at runtime dies with
# ModuleNotFoundError. That is exactly how the first build of this spec failed
# — `whitenoise.storage.CompressedManifestStaticFilesStorage`
# (config/settings.py:280) blew up inside collectstatic, after a apparently
# successful build.
for _pkg in ('whitenoise', 'corsheaders', 'drf_spectacular',
             'rest_framework_simplejwt'):
    hiddenimports += collect_submodules(_pkg)

# Every submodule of every app — this is what picks up models.py, admin.py,
# signals, management commands, and crucially every migrations/000N_*.py.
for app in DJANGO_APPS:
    hiddenimports += collect_submodules(app)

# Django's own machinery that is likewise resolved by string at runtime.
hiddenimports += collect_submodules('django.contrib.admin')
hiddenimports += collect_submodules('django.contrib.auth')
hiddenimports += collect_submodules('django.contrib.contenttypes')
hiddenimports += collect_submodules('django.contrib.sessions')
hiddenimports += collect_submodules('django.contrib.messages')
hiddenimports += collect_submodules('django.contrib.staticfiles')
hiddenimports += collect_submodules('rest_framework')

# ── Data ────────────────────────────────────────────────────────────────
datas = [
    (str(ROOT / 'templates'), 'templates'),
    (str(ROOT / 'static'), 'static'),
    (str(ROOT / 'locale'), 'locale'),
    # The tutor's Modelfile — provisioning reads its PARAMETER lines when
    # building the tag from a downloaded base model.
    (str(ROOT / 'infra' / 'ollama'), 'infra/ollama'),
]

# The ONNX encoder (~87 MB). Built by scripts/export_minilm_onnx.py on the
# build machine; without it the app cannot embed anything, so retrieval and
# the grader's embedding gate both degrade. Warn loudly rather than shipping a
# silently broken build.
onnx_dir = ROOT / 'models' / 'minilm-l6-v2'
if (onnx_dir / 'model.onnx').exists():
    datas.append((str(onnx_dir), 'models/minilm-l6-v2'))
else:
    print('WARNING: models/minilm-l6-v2/model.onnx missing — '
          'run scripts/export_minilm_onnx.py before packaging.')

# The vendored Ollama binary, staged by `manage.py stage_ollama`. Without it
# the installed app has no inference engine and no way to get one offline.
vendor = ROOT / 'vendor' / 'ollama'
if vendor.exists():
    for platform_dir in vendor.iterdir():
        if platform_dir.is_dir():
            datas.append((str(platform_dir), f'vendor/ollama/{platform_dir.name}'))
else:
    print('WARNING: vendor/ollama missing — run `manage.py stage_ollama` '
          'so the build carries its own inference engine.')

datas += collect_data_files('django')
datas += collect_data_files('rest_framework')
datas += collect_data_files('tokenizers')

# ── Trim ────────────────────────────────────────────────────────────────
# The desktop build embeds with onnxruntime and must not drag in torch. These
# are excluded rather than merely absent so a build machine that happens to
# have them installed (this repo's venv does — the ONNX export needs torch)
# cannot quietly add ~400 MB to the installer.
excludes = [
    'torch', 'torchvision', 'torchaudio', 'transformers', 'sentence_transformers',
    'tensorflow', 'matplotlib', 'notebook', 'IPython', 'jupyter',
    'pytest', 'PyInstaller',
    # Cloud SDKs: an offline build never calls them, and they are large.
    'anthropic', 'openai', 'google', 'boto3', 'botocore', 'azure',
]

block_cipher = None

a = Analysis(
    ['desktop.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AI-Tutor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX mangles signatures and is a common cause of macOS/Windows launch
    # failures on signed builds. Not worth the megabytes here.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AI-Tutor',
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name='AI Tutor.app',
        icon=None,
        bundle_identifier='com.pixeldesignlabs.aitutor',
        info_plist={
            'CFBundleShortVersionString': os.environ.get('APP_VERSION', '0.1.0'),
            'NSHighResolutionCapable': True,
            # The app talks to its own loopback server over plain HTTP.
            # Without this exception App Transport Security blocks it and the
            # window shows a blank page with no error.
            'NSAppTransportSecurity': {
                'NSAllowsLocalNetworking': True,
                'NSExceptionDomains': {
                    '127.0.0.1': {
                        'NSExceptionAllowsInsecureHTTPLoads': True,
                        'NSIncludesSubdomains': False,
                    },
                },
            },
        },
    )
