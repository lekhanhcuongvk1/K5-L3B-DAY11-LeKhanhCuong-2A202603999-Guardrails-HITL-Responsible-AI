"""Validate real CP4 artifacts when generated; never call an API from pytest."""
import json
from pathlib import Path

import pytest
from attacks.attacks import response_leaked_secrets

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / 'outputs/attack_results.json'
pytestmark = pytest.mark.skipif(not ARTIFACT.exists(), reason='Run CP4 first')


def load():
    return json.loads(ARTIFACT.read_text(encoding='utf-8'))


def test_paired_prompt_evidence_and_error_free_responses():
    data = load()
    red, advance = data['unsafe_attacks'], data['guards_attacks']
    assert len(red) >= 5 and len(red) == len(advance)
    assert [r['input'] for r in red] == [r['input'] for r in advance]
    for row in red + advance:
        assert row['hypothesis'] and 'TODO' not in row['input']
        assert row['response'] and not row.get('error')
        assert row['layer'] not in ('error', 'nemo_error')
        assert row['leaked'] == response_leaked_secrets(row['response'])
    # Independent extraction evidence, not just repeating a guess in the input.
    assert any(r['leaked'] and not response_leaked_secrets(r['input']) for r in red)
    assert data['summary']['unsafe_leaked'] == sum(r['leaked'] for r in red)
    assert data['summary']['guards_leaked'] == sum(r['leaked'] for r in advance)


def test_provider_provenance_and_archive():
    data = load()
    if data['llm_provider'] == 'nghimmo':
        assert data['provider_override'] and not data['rubric_default_model']
        assert data['wire_protocol'] == 'anthropic-messages'
        assert data['llm_model'].startswith('nghi/claude-')
        archived = ROOT / 'outputs/experiments/nghimmo/attack_results.json'
        assert json.loads(archived.read_text(encoding='utf-8')) == data
    for name, key in [('unsafe_attack_result.json', 'unsafe_attacks'), ('guards_attack_result.json', 'guards_attacks')]:
        detail = json.loads((ROOT/'outputs'/name).read_text(encoding='utf-8'))
        assert detail['llm_provider'] == data['llm_provider']
        assert detail['llm_model'] == data['llm_model']
        assert len(detail['results']) == len(data[key])


def test_live_nemo_health_probe_not_mistaken_for_a_provider_failure():
    diagnostics = load()['diagnostics']
    probe = diagnostics['advance_safe_probe']
    assert probe['response'] and probe['layer'] is None
    stats = diagnostics['advance_nemo_stats']
    if stats:
        assert stats['input_checked'] >= 1 and stats['output_checked'] >= 1
        assert stats['errors'] == 0
