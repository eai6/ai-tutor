"""Guards the generated notebook against the mistakes that cost a Colab run."""
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GEN = ROOT / 'offline_eval' / '_make_colab_nb_mt100.py'
NB = ROOT / 'offline_eval' / 'colab_mt100_qwen.ipynb'

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
