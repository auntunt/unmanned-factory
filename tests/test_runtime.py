from pathlib import Path

import pytest

from factory.control.runtime import RuntimeSettings, inspect_runtime, profile_blockers
from factory.control.store import Conflict, Store


def _settings(tmp_path, **kwargs):
    return RuntimeSettings(Store(tmp_path / "control.db"), **kwargs)


def _profiles(provider="codex", model="model"):
    return {role: {"provider": provider, "model": model}
            for role in ("planner", "cheap", "standard", "strong")}


def test_settings_seed_from_environment_only_once(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_PLANNER_MODEL", "first-model")
    settings = _settings(tmp_path)
    assert settings.get()["profiles"]["planner"]["model"] == "first-model"
    monkeypatch.setenv("FACTORY_PLANNER_MODEL", "second-model")
    assert RuntimeSettings(Store(tmp_path / "control.db")).get()["profiles"]["planner"]["model"] == "first-model"


def test_settings_update_is_complete_and_cas(tmp_path):
    settings = _settings(tmp_path)
    current = settings.get()
    changed = settings.update({"profiles": _profiles("claude"), "limits": current["limits"]},
                              current["revision"], "owner")
    assert changed["revision"] == 2
    assert len(settings.history()) == 2
    with pytest.raises(Conflict):
        settings.update({"profiles": _profiles(), "limits": current["limits"]}, 1, "owner")
    with pytest.raises(ValueError):
        settings.update({"profiles": _profiles("dsh"), "limits": current["limits"]}, 2, "owner")


@pytest.mark.parametrize("data", [
    {"profiles": {"planner": {"provider": "codex", "model": "m"}}, "limits": {}},
    {"profiles": _profiles(), "limits": {"timeout_s": 1, "max_parallel": 2, "max_tasks": 20, "unknown_cost_policy": "stop"}},
    {"profiles": {**_profiles(), "extra": {"provider": "codex", "model": "m"}}, "limits": {}},
])
def test_settings_reject_invalid_shapes(tmp_path, data):
    settings = _settings(tmp_path)
    with pytest.raises(ValueError):
        settings.update(data, 1, "owner")


def test_probe_rows_are_revision_scoped(tmp_path):
    settings = _settings(tmp_path)
    settings.record_probe({"id": "p1", "profile": "cheap", "provider": "codex", "model": "m",
                           "configuration_revision": 1, "checked_at": "2026-01-01T00:00:00+00:00",
                           "outcome": "failed", "message": "provider probe failed (ProviderError)"})
    assert settings.last_probes() == [{"id": "p1", "profile": "cheap", "provider": "codex", "model": "m",
                                       "configuration_revision": 1, "checked_at": "2026-01-01T00:00:00+00:00",
                                       "outcome": "failed", "message": "provider probe failed (ProviderError)"}]


def test_profile_blockers_do_not_claim_missing_auth(tmp_path):
    settings = _settings(tmp_path)
    # The result is intentionally about deterministic local blockers only.
    blockers = profile_blockers(settings.get()["profiles"]["planner"], "planner")
    assert any("model" in item for item in blockers)
    assert all("auth" not in item.lower() for item in blockers)


def test_runtime_shape_contains_host_and_readiness(tmp_path):
    result = inspect_runtime(_settings(tmp_path), static_dir=Path(tmp_path) / "dist")
    assert result["execution_mode"] == "local_sdk_children"
    assert set(result["readiness"]) == {"planning", "execution", "publishing"}
    assert {tool["id"] for tool in result["tools"]} == {"claude", "codex", "dsh"}
    assert "last_probes" in result


def test_import_and_auth_hint_do_not_imply_live_readiness(tmp_path, monkeypatch):
    import factory.control.runtime as runtime
    monkeypatch.setattr(runtime, '_sdk_inspect', lambda provider: {
        'installed': True, 'version': '1', 'runtime_version': '1',
        'import_status': 'ok', 'compatible': True, 'runtime_ok': True})
    monkeypatch.setattr(runtime, '_module_present', lambda provider: True)
    monkeypatch.setattr(runtime, '_binary_available', lambda name: True)
    monkeypatch.setenv('OPENAI_API_KEY', 'not-a-real-secret')
    settings = _settings(tmp_path, profiles=_profiles())
    report = inspect_runtime(settings)
    assert report['local_readiness']['planning'] is True
    assert report['readiness']['planning'] is False
    assert report['tools'][1]['runtime_status'] == 'available'
    assert 'not-a-real-secret' not in str(report)


def test_real_sdk_resolves_bundled_runtime_without_path_cli(monkeypatch):
    import factory.control.runtime as runtime
    monkeypatch.setenv('PATH', '')
    for provider in ('codex', 'claude'):
        if not runtime._module_present(provider):
            continue
        report = runtime._sdk_inspect(provider)
        assert report['runtime_ok'], provider
        assert report['compatible'], provider


def test_concurrent_settings_cas_has_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    settings = _settings(tmp_path)
    current = settings.get()
    def update():
        try:
            return settings.update({'profiles': _profiles(), 'limits': current['limits']}, 1, 'owner')['revision']
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: update(), range(2)))
    assert sorted(map(str, results)) == ['2', 'conflict']
    assert len(settings.history()) == 2
