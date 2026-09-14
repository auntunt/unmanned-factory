"""Removed control-room endpoints fail closed without altering CLI replay."""
import pytest
from factory.cli import main
from tests.test_control_app import app_env, login


@pytest.mark.parametrize('path', ['/api/tasks', '/api/events', '/api/runtime', '/api/v2/not-a-route', '/api/v4/not-a-route'])
@pytest.mark.parametrize('method', ['GET', 'POST', 'PUT', 'DELETE'])
def test_unknown_api_returns_migration_404(app_env, path, method):
    client, _, _, _ = app_env
    headers = login(client)
    response = client.request(method, path, headers=headers)
    assert response.status_code == 404
    assert '旧版' in response.json()['detail']
    assert '工作台' in response.json()['detail']


def test_retired_api_command_is_rejected(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(['api'])
    assert stopped.value.code == 2
    assert 'invalid choice' in capsys.readouterr().err
