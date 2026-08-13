"""The wheel must contain what a running deployment needs.

Two things go wrong quietly when packaging a Django application, and both fail
at runtime rather than at build time — which means CI stays green and the
person who finds out is whoever installed it:

  * a dependency added to requirements.txt but not to pyproject.toml, so the
    wheel installs and then ImportErrors on a code path nobody hit in testing;
  * a non-Python asset left out of the wheel, so the app imports fine and then
    500s on the first page render because no template was shipped.

Plan: memory/pip_package_plan.md
"""
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / 'pyproject.toml'


def _requirements() -> list[str]:
    lines = (ROOT / 'requirements.txt').read_text().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith('#')]


@pytest.fixture(scope='module')
def project() -> dict:
    with PYPROJECT.open('rb') as fh:
        return tomllib.load(fh)


class TestDependencies:
    """pyproject.toml and requirements.txt are two copies of one list."""

    def test_they_have_not_drifted(self, project):
        declared = project['project']['dependencies']
        required = _requirements()

        missing = sorted(set(required) - set(declared))
        extra = sorted(set(declared) - set(required))
        assert not missing, (
            f'in requirements.txt but not pyproject.toml: {missing}. '
            'A wheel built now would be missing these.')
        assert not extra, (
            f'in pyproject.toml but not requirements.txt: {extra}. '
            'The wheel would install something the Docker image does not have.')

    def test_every_dependency_is_constrained(self, project):
        """An unpinned dependency makes the install unreproducible, which for a
        ministry deployment means a machine that worked last month may not
        install the same platform today."""
        loose = [d for d in project['project']['dependencies']
                 if not any(op in d for op in ('==', '>=', '<=', '~=', '>', '<'))]
        assert not loose, f'unconstrained: {loose}'


class TestPackagedAssets:
    """Everything the app reads at runtime has to be inside the package."""

    def test_vendored_assets_are_rescued_from_the_ignore_rule(self, project):
        """.gitignore has a bare `vendor/` that matches ai_tutor/static/vendor/.
        Git tracks those files anyway (tracking beats ignoring) but hatchling
        reads the pattern and drops them, so the wheel shipped without KaTeX,
        DOMPurify and the fonts — it built, imported and served, and only broke
        in a browser, on maths. `artifacts` is the override."""
        artifacts = project['tool']['hatch']['build']['targets']['wheel']['artifacts']
        assert any('static/vendor' in a for a in artifacts)

    @pytest.mark.parametrize('asset,probe', [
        ('templates', 'base.html'),
        ('static', 'vendor'),
        ('locale', 'pt_MZ'),
        ('seed', 'curriculum-pack.tar.gz'),
    ])
    def test_asset_actually_exists_in_the_package(self, asset, probe):
        """Guards the reverse mistake: declared in pyproject, absent on disk."""
        assert (ROOT / 'ai_tutor' / asset / probe).exists()

    def test_settings_resolve_assets_against_the_package(self):
        """Bundled assets must follow the code, not the data directory. If these
        were BASE_DIR-relative they would break the moment the wheel is
        installed, because site-packages/ai_tutor's parent holds nothing."""
        from django.conf import settings

        package = Path(settings.PACKAGE_DIR)
        assert Path(settings.TEMPLATES[0]['DIRS'][0]).is_relative_to(package)
        assert Path(settings.STATICFILES_DIRS[0]).is_relative_to(package)
        assert Path(settings.LOCALE_PATHS[0]).is_relative_to(package)

    def test_mutable_state_stays_out_of_the_package(self):
        """MEDIA_ROOT inside the package would be wiped on every upgrade."""
        from django.conf import settings

        assert not Path(settings.MEDIA_ROOT).is_relative_to(Path(settings.PACKAGE_DIR))


class TestMetadata:

    def test_requires_python_matches_what_we_actually_run(self, project):
        """Django 6.0 needs 3.12+, and the Dockerfile is python:3.12-slim."""
        assert project['project']['requires-python'] == '>=3.12'

    def test_the_package_is_the_only_one_shipped(self, project):
        """`apps` and `config` as top-level names in site-packages is the thing
        the whole namespace move existed to prevent."""
        assert project['tool']['hatch']['build']['targets']['wheel']['packages'] == ['ai_tutor']


WHEEL_DIR = ROOT / 'dist_wheel'


def _built_wheel():
    return next(iter(sorted(WHEEL_DIR.glob('*.whl'))), None) if WHEEL_DIR.exists() else None


@pytest.mark.skipif(_built_wheel() is None,
                    reason='no wheel built; run `python -m build --wheel --outdir dist_wheel`')
class TestBuiltWheel:
    """Assertions against the artefact itself.

    Config-level checks proved insufficient: the earlier version asserted the
    assets were declared, and the wheel still shipped 9 of 88 static files.
    """

    @pytest.fixture(scope='class')
    def names(self):
        import zipfile
        return set(zipfile.ZipFile(_built_wheel()).namelist())

    def test_ships_every_tracked_file(self, names):
        import subprocess
        tracked = subprocess.run(['git', 'ls-files', 'ai_tutor'],
                                 capture_output=True, text=True, cwd=ROOT).stdout.split()
        missing = sorted(f for f in tracked
                         if f not in names and not f.endswith('.DS_Store'))
        assert not missing, f'{len(missing)} tracked files absent from the wheel: {missing[:10]}'

    @pytest.mark.parametrize('probe', [
        'ai_tutor/templates/base.html',
        'ai_tutor/static/vendor/js/katex.min.js',
        'ai_tutor/locale/pt_MZ/LC_MESSAGES/django.po',
        'ai_tutor/seed/curriculum-pack.tar.gz',
        'ai_tutor/config/wsgi.py',
    ])
    def test_runtime_essentials_are_present(self, names, probe):
        assert probe in names

    def test_installs_exactly_one_top_level_name(self, names):
        """`apps` and `config` in site-packages is what the move existed to stop."""
        tops = {n.split('/')[0] for n in names}
        assert tops == {'ai_tutor', 'ai_tutor-0.1.0.dist-info'}
