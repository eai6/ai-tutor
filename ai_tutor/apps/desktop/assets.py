"""Model assets acquired after the app is installed.

The tutor model already works this way (``provisioning.py``): the installer
does not carry 2.5 GB of weights, and a school with no internet is given them
on a USB stick instead. These are the two smaller assets on the same footing.

  * the MiniLM ONNX encoder — retrieval and the grader's embedding gate
  * the Piper voice — read-aloud

Both install into ``BASE_DIR/models``, which on the desktop build is the
application-data directory: writable, per-user, and surviving an upgrade. That
is also exactly where ``kb_storage._onnx_dir()`` and
``audio_service.piper_voice_dirs()`` already look, so nothing needs to be told
where the files went.

Two routes, mirroring provisioning:

  * **From a directory** — a folder from a USB stick, produced by
    ``manage.py build_asset_pack``. Works with no internet at any point.
  * **From the network** — for whoever has bandwidth. Convenience, never a
    dependency.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Asset:
    """One installable asset.

    ``required_for_lessons`` is the whole reason this class carries metadata
    rather than being a bare path: it decides whether a device that lacks the
    file may still teach. See ``readiness.lesson_prerequisites``.
    """
    key: str
    label: str
    dirname: str                  # under BASE_DIR/models
    marker: str                   # file whose presence means "installed"
    required_for_lessons: bool
    url: str = ''
    extra_files: tuple = ()       # fetched alongside the marker, same dir
    # Places an earlier build or another image may already have put the file.
    # Searched when reporting status so the setup screen cannot claim an asset
    # is missing while the code that uses it is happily reading it elsewhere.
    fallback_dirs: tuple = ()

    def directory(self) -> Path:
        from django.conf import settings
        override = {
            'minilm': getattr(settings, 'MINILM_ONNX_DIR', ''),
            'piper': getattr(settings, 'PIPER_VOICE_DIR', ''),
        }.get(self.key, '')
        if override:
            return Path(override)
        return Path(settings.BASE_DIR) / 'models' / self.dirname

    def marker_path(self) -> Path:
        return self.directory() / self.marker

    def search_dirs(self) -> list:
        return [self.directory()] + [Path(d) for d in self.fallback_dirs]

    def installed(self) -> bool:
        try:
            return any((d / self.marker).is_file() for d in self.search_dirs())
        except Exception:                            # noqa: BLE001
            return False


_HF_PIPER = ('https://huggingface.co/rhasspy/piper-voices/resolve/main'
             '/en/en_US/lessac/medium')

# The encoder has no public single-file URL that matches what
# scripts/export_minilm_onnx.py produces, so it is file-install only until a
# hosted copy exists. Leaving url empty is not an oversight — it makes the
# network route unavailable for that asset rather than silently wrong.
ASSETS = (
    Asset(
        key='minilm',
        label='Content search encoder',
        dirname='minilm-l6-v2',
        marker='model.onnx',
        required_for_lessons=True,
        extra_files=('tokenizer.json',),
    ),
    Asset(
        key='piper',
        label='Read-aloud voice',
        dirname='piper',
        marker='en_US-lessac-medium.onnx',
        # Speech is an accessibility feature layered on a text tutor. Blocking
        # a school's lessons on a missing voice would cost far more than it
        # protects.
        required_for_lessons=False,
        url=f'{_HF_PIPER}/en_US-lessac-medium.onnx',
        extra_files=('en_US-lessac-medium.onnx.json',),
        # Mirrors audio_service._PIPER_FALLBACK_DIRS: the Jetson image and the
        # container builds place the voice themselves.
        fallback_dirs=(
            '/models/piper',
            str(Path('~/.local/share/piper_voices').expanduser()),
        ),
    ),
)


def by_key(key: str) -> Asset | None:
    for asset in ASSETS:
        if asset.key == key:
            return asset
    return None


def status() -> list:
    """What the setup screen shows: one row per asset."""
    return [
        {
            'key': a.key,
            'label': a.label,
            'installed': a.installed(),
            'required': a.required_for_lessons,
            'path': str(a.directory()),
        }
        for a in ASSETS
    ]


def missing_required() -> list:
    return [a for a in ASSETS if a.required_for_lessons and not a.installed()]


def self_dirname(asset: Asset) -> str:
    """The per-asset subfolder name used by build_asset_pack."""
    return asset.dirname


def install_from_directory(asset: Asset, source: str) -> None:
    """Copy an asset in from a folder — the USB-stick route.

    Copies to a temporary name and renames into place, so an interrupted copy
    cannot leave a half-written file that ``installed()`` would then call
    good.
    """
    src_dir = Path(source).expanduser()
    if not src_dir.is_dir():
        raise ValueError(f'Not a directory: {src_dir}')

    # build_asset_pack nests each asset in its own subfolder, and the setup
    # screen offers ONE folder picker for all of them — so accept either the
    # pack root or a folder holding this asset's files directly. Without this,
    # picking the pack the command just produced would fail.
    if (src_dir / self_dirname(asset) / asset.marker).is_file():
        src_dir = src_dir / self_dirname(asset)

    dest_dir = asset.directory()
    dest_dir.mkdir(parents=True, exist_ok=True)

    wanted = (asset.marker,) + tuple(asset.extra_files)
    for name in wanted:
        src = src_dir / name
        if not src.is_file():
            raise ValueError(f'{src_dir} has no {name}')

    # Marker last: until it lands, the asset reads as not installed.
    for name in tuple(asset.extra_files) + (asset.marker,):
        src = src_dir / name
        tmp = dest_dir / f'.{name}.partial'
        shutil.copyfile(src, tmp)
        tmp.replace(dest_dir / name)

    logger.info('[assets] installed %s from %s', asset.key, src_dir)


def install_from_url(asset: Asset, *, timeout: int = 300) -> None:
    """Fetch an asset over the network. Only for assets that declare a URL."""
    import urllib.request

    if not asset.url:
        raise ValueError(f'{asset.key} has no download URL; install from a file')

    dest_dir = asset.directory()
    dest_dir.mkdir(parents=True, exist_ok=True)

    base = asset.url.rsplit('/', 1)[0]
    downloads = [(name, f'{base}/{name}') for name in asset.extra_files]
    downloads.append((asset.marker, asset.url))   # marker last, as above

    for name, url in downloads:
        tmp = dest_dir / f'.{name}.partial'
        logger.info('[assets] downloading %s', url)
        with urllib.request.urlopen(url, timeout=timeout) as resp, \
                open(tmp, 'wb') as handle:
            shutil.copyfileobj(resp, handle)
        tmp.replace(dest_dir / name)

    logger.info('[assets] installed %s from %s', asset.key, asset.url)
