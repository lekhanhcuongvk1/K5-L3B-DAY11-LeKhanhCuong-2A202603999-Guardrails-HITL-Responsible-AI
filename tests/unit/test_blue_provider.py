"""Provider selection must never disguise direct Liquid as OpenRouter."""
import pytest
from core.config import get_blue_provider, get_blue_model, get_blue_api_key, blue_client_kwargs


def test_default_route_remains_lab_openrouter(monkeypatch):
    monkeypatch.delenv('BLUE_PROVIDER', raising=False)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fake-router')
    monkeypatch.setenv('LIQUID_API_KEY', 'fake-liquid')
    assert get_blue_provider() == 'openrouter'
    assert get_blue_model() == 'liquid/lfm-3-1.7b:free'
    assert get_blue_api_key() == 'fake-router'
    assert blue_client_kwargs()['base_url'] == 'https://openrouter.ai/api/v1'


def test_direct_route_pairs_official_endpoint_key_and_fixed_model(monkeypatch):
    monkeypatch.setenv('BLUE_PROVIDER', 'liquid')
    monkeypatch.setenv('LIQUID_API_KEY', 'fake-liquid')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fake-router')
    monkeypatch.setenv('LIQUID_MODEL', 'unrelated-model-must-not-be-selected')
    assert get_blue_provider() == 'liquid'
    assert get_blue_model() == 'lfm3-1.7b'
    assert blue_client_kwargs() == {'api_key': 'fake-liquid', 'base_url': 'https://api.liquid.ai/openai/v1'}
    monkeypatch.delenv('LIQUID_API_KEY')
    assert get_blue_api_key() == ''
    assert blue_client_kwargs()['api_key'] is None


def test_unknown_blue_provider_is_rejected(monkeypatch):
    monkeypatch.setenv('BLUE_PROVIDER', 'claude')
    with pytest.raises(ValueError):
        get_blue_provider()
