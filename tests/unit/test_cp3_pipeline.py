"""CP3 policy and observer regressions. All model responses here are mocked."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from assignment.rate_limiter import RateLimitPlugin
from assignment.audit_log import AuditLogPlugin
from assignment.monitoring import MonitoringAlert
from assignment.pipeline import is_egress_allowed, build_production_plugins, build_observability


def test_sliding_window_per_user_expiry_and_rejected_calls(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('assignment.rate_limiter.time.monotonic', lambda: now[0])
    limiter = RateLimitPlugin(max_requests=2, window_seconds=10)
    async def request(user):
        return await limiter.on_user_message_callback(invocation_context=SimpleNamespace(user_id=user), user_message=None)
    assert asyncio.run(request('a')) is None
    assert asyncio.run(request('a')) is None
    assert asyncio.run(request('a')) is not None
    assert asyncio.run(request('b')) is None
    assert len(limiter.user_windows['a']) == 2
    now[0] = 110.0
    assert asyncio.run(request('a')) is None
    assert len(limiter.user_windows['a']) == 1
    assert limiter.total_count == 5 and limiter.blocked_count == 1


@pytest.mark.parametrize('url', [
    'http://api.vinbank.example/x', 'https://api.vinbank.example.evil.com/x',
    'https://evil.example/api.vinbank.example', 'https://api.vinbank.example@evil.example/x',
    'https://user@api.vinbank.example/x', 'https://api.vinbank.example:8080/x',
    'https://api.vinbank.example:bad/x', 'https://api.vinbank.example./x',
    'https://api.vinbank.example\\@evil.example/x', 'https://api.vinbank.example\n/x', '',
])
def test_egress_rejects_url_tricks(url):
    assert not is_egress_allowed(url, 'approved transfer amount 500000')


@pytest.mark.parametrize('payload', [
    'admin123', 'a d m i n 1 2 3', 'db.vinbank.internal:5432', 'sk-example-key-123',
    'phone 0901234567', 'test@example.com', 'CCCD 079204001234',
])
def test_allowed_domain_does_not_authorize_sensitive_payload(payload):
    assert not is_egress_allowed('https://api.vinbank.example/v1/transfers', payload)


def test_clean_egress_and_plugin_order():
    assert is_egress_allowed('https://api.vinbank.example/v1/transfers', 'approved transfer amount 500000')
    assert is_egress_allowed('https://cases.vinbank.example:443/cases', 'transfer delayed')
    assert [p.name for p in build_production_plugins()] == ['rate_limiter', 'input_guardrail', 'output_guardrail']


def test_audit_correlates_overlapping_requests_and_redacts(tmp_path):
    audit = AuditLogPlugin()
    audit.record_input(user_id='a', request_id='1', text='admin123')
    audit.record_input(user_id='a', request_id='2', text='bank transfer')
    audit.record_output(user_id='a', request_id='2', text='test@example.com', blocked=True, layer='output_guardrail')
    audit.record_output(user_id='a', request_id='1', text='safe')
    assert [r['request_id'] for r in audit.logs] == ['2', '1']
    assert audit.logs[1]['input'] == '[REDACTED]'
    assert all(r['latency_ms'] >= 0 for r in audit.logs)
    path = audit.export_json(str(tmp_path / 'nested/audit.json'))
    persisted = path.read_text(encoding='utf-8')
    assert 'admin123' not in persisted and 'test@example.com' not in persisted
    assert len(json.loads(persisted)) == 2
    with pytest.raises(ValueError):
        audit.record_output(user_id='missing', text='bad')


def test_metrics_zero_requests_thresholds_and_no_duplicates(tmp_path):
    monitor = MonitoringAlert()
    assert monitor.check_metrics() == []
    monitor.total_requests, monitor.blocked_requests, monitor.rate_limit_hits = 10, 8, 6
    monitor.judge_checks, monitor.judge_fails = 10, 4
    assert len(monitor.check_metrics()) == 3
    assert len(monitor.check_metrics()) == 3
    path = monitor.export_json(str(tmp_path / 'metrics.json'))
    assert json.loads(path.read_text())['block_rate'] == 0.8


def test_complete_suite_records_early_blocks_and_isolates_rate_limit(monkeypatch, tmp_path):
    import assignment.pipeline as mod
    from core.openai_runtime import OpenAIRunner
    root = Path(__file__).resolve().parents[2]
    (tmp_path / 'schemas').mkdir()
    (tmp_path / 'schemas/results.schema.json').write_bytes((root / 'schemas/results.schema.json').read_bytes())
    monkeypatch.setattr(mod, '__file__', str(tmp_path / 'src/assignment/pipeline.py'))
    monkeypatch.setenv('OPENROUTER_API_KEY', 'dummy-offline-unit-test')
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Contact your bank securely for account help.'))])
    monkeypatch.setattr(OpenAIRunner, '_client', lambda self: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    audit, monitor = build_observability()
    result = asyncio.run(mod.run_assignment_suite({'plugins': build_production_plugins(), 'audit': audit, 'monitor': monitor}))
    assert len(calls) == 5
    assert all(not r['blocked'] for r in result['safe_queries'])
    assert all(r['layer'] == 'input_guardrail' for r in result['attack_queries'])
    assert result['rate_limit']['passed'] == 10 and result['rate_limit']['blocked'] == 5
    assert len(audit.logs) == monitor.total_requests == 31
    assert monitor.blocked_requests == 16
    assert not audit._open
    assert (tmp_path / 'outputs/results.json').is_file()


def test_provider_error_is_audited_without_success_artifact(monkeypatch, tmp_path):
    import assignment.pipeline as mod
    from core.openai_runtime import OpenAIRunner
    monkeypatch.setattr(mod, '__file__', str(tmp_path / 'src/assignment/pipeline.py'))
    monkeypatch.setenv('OPENROUTER_API_KEY', 'dummy-offline-unit-test')
    def fail(**kwargs):
        raise TimeoutError('sensitive error detail must not be persisted')
    monkeypatch.setattr(OpenAIRunner, '_client', lambda self: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail))))
    audit, monitor = build_observability()
    # Redirect error-path observers as well as the results path to the temporary workspace.
    monkeypatch.setattr('assignment.audit_log.default_audit_log_path', lambda: str(tmp_path / 'outputs/audit_log.json'))
    monkeypatch.setattr('assignment.monitoring.default_metrics_path', lambda: str(tmp_path / 'outputs/metrics.json'))
    with pytest.raises(TimeoutError):
        asyncio.run(mod.run_assignment_suite({'plugins': build_production_plugins(), 'audit': audit, 'monitor': monitor}))
    assert not (tmp_path / 'outputs/results.json').exists()
    assert monitor.error_requests == 1 and monitor.blocked_requests == 0
    assert audit.logs[0]['layer'] == 'provider_error'
    assert audit.logs[0]['output'] == 'TimeoutError'
    assert 'sensitive error' not in (tmp_path / 'outputs/audit_log.json').read_text()


@pytest.mark.parametrize('status', [429, 502, 503, 504])
def test_blue_retries_transient_errors_without_replaying_guardrails(monkeypatch, status):
    from core.openai_runtime import create_blue_pair
    import core.openai_runtime as runtime
    class TransientError(Exception):
        status_code = status
    calls, waits = [], []
    async def sleep(seconds):
        waits.append(seconds)
    monkeypatch.setattr(runtime.asyncio, 'sleep', sleep)
    plugins = build_production_plugins()
    agent, runner = create_blue_pair(name='test', instruction='test', app_name='test', plugins=plugins)
    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise TransientError()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Bank account help.'))])
    runner._client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert asyncio.run(runner.chat(agent, 'What is my account balance?', user_id='a')) == 'Bank account help.'
    assert len(calls) == 3 and waits == [5, 10]
    assert plugins[0].total_count == plugins[1].total_count == 1
    calls.clear()
    def always_fail(**kwargs):
        calls.append(kwargs)
        raise TransientError()
    runner._client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=always_fail)))
    with pytest.raises(TransientError):
        asyncio.run(runner.chat(agent, 'What is my account balance?', user_id='b'))
    assert len(calls) == 3
