"""Guards the generated notebook against the mistakes that cost a Colab run."""
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GEN = ROOT / 'offline_eval' / '_make_colab_nb_mt100.py'
NB = ROOT / 'offline_eval' / 'colab_mt100_qwen.ipynb'
RUN_MATRIX = ROOT / 'offline_eval' / 'run_matrix.sh'

ARMS = ('qwen3.5-2b-jetson', 'qwen3-4b-jetson', 'qwen3-8b-jetson',
        'qwen3.6-27b-instruct', 'qwen3-30b-a3b-jetson')


def _source():
    subprocess.run([sys.executable, str(GEN)], cwd=ROOT, check=True)
    nb = json.loads(NB.read_text())
    return '\n'.join(''.join(c['source']) for c in nb['cells'])


def test_notebook_is_valid_and_regenerates_deterministically():
    first = NB.read_text() if NB.exists() else None
    _source()
    assert json.loads(NB.read_text())['nbformat'] == 4
    if first is not None:
        assert NB.read_text() == first, 'generator is not deterministic'


def test_runs_all_five_qwen_arms():
    src = _source()
    for arm in ARMS:
        assert arm in src, f'{arm} missing from the notebook'


def test_targets_the_v2_subset_and_mt100_sweep():
    src = _source()
    assert '--subset v2' in src
    assert 'mt100' in src


def test_pins_the_known_good_ollama_version():
    assert "0.30.7" in _source(), 'tool-call parsing is version-sensitive'


def test_builds_every_arm_from_its_modelfile():
    src = _source()
    for arm in ARMS:
        assert f'Modelfile.{arm}' in src, f'{arm} must build from its Modelfile'


# --- fix round 1: the checks above only prove the notebook MENTIONS the
# Modelfile pattern in prose (Cell 8.5's markdown table); they pass even if
# the actual build mechanism — run_matrix.sh's `ollama create` branch — were
# deleted or reverted to a bare `ollama pull`. The three tests below check
# the real mechanism instead of a string in prose.

def test_run_cell_invokes_run_matrix_sh():
    """The notebook must actually hand off to run_matrix.sh, not reimplement
    (and potentially diverge from) its Modelfile-vs-bare-pull logic inline."""
    src = _source()
    assert re.search(r'bash\s+offline_eval/run_matrix\.sh', src), \
        'the eval cell must invoke offline_eval/run_matrix.sh'


def test_run_matrix_still_builds_modelfile_tags_via_ollama_create():
    """run_matrix.sh is where the real build-vs-pull decision lives. Guard
    that file directly: if this branch is ever "simplified" back to a bare
    `ollama pull <tag>`, every arm silently loses its num_ctx pin (the exact
    failure mode described in the generator's CRITICAL docstring note) even
    though every notebook-text test above would stay green."""
    assert RUN_MATRIX.exists(), f'{RUN_MATRIX} is missing'
    src = RUN_MATRIX.read_text()
    assert 'infra/ollama/Modelfile.$tag' in src, \
        'run_matrix.sh no longer checks for a per-tag Modelfile'
    assert re.search(r'if\s*\[\[\s*-f\s*"\$MODELFILE"\s*\]\]', src), \
        'run_matrix.sh no longer branches on Modelfile presence'
    assert 'ollama create "$tag" -f "$MODELFILE"' in src, \
        'run_matrix.sh no longer builds the tag from its Modelfile via `ollama create`'


def test_every_arm_has_a_real_modelfile_on_disk():
    """Belt-and-suspenders on top of the mechanism check above: the five
    Modelfiles this board depends on must actually exist, independent of
    anything the notebook or run_matrix.sh claims about them."""
    for arm in ARMS:
        mf = ROOT / 'infra' / 'ollama' / f'Modelfile.{arm}'
        assert mf.is_file(), f'{mf} does not exist on disk'
