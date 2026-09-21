"""Admin surface for business-plugin availability.

Prefix ``/api/v2/plugins``.  Reading is open to any signed-in user because the
frontend decides which entry points to render from it; every write is a
non-GET under ``/api/v2``, which the control-plane middleware already restricts
to administrators -- there is no second role check invented here.

This surface does not install, upload or remove anything.  The set of plugins
is the fixed list in ``plugins.DECLARATIONS``; all an administrator can change
is whether one accepts work.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from factory.control import plugins
from factory.control.store import Conflict


class _StateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    state: str = Field(min_length=1, max_length=20)
    #: Optional CAS. When given, the change is refused if someone else moved
    #: the plugin in the meantime, so two administrators cannot silently
    #: overwrite each other's decision.
    expected_revision: int | None = None


def _active_probe(store, plugin_id):
    """How many executions this plugin still has running, or ``None``.

    Only the maintenance plugin has a business surface today, so only it has a
    real probe. Returning a fabricated zero for the others would be harmless
    now and wrong the moment one of them ships, so each plugin that becomes
    executable must add its own counter here.
    """
    if plugin_id == 'issue-maintenance':
        from factory.control.issue_maintenance_webuddy import active_task_count
        return lambda: active_task_count(store)
    decl = plugins.declaration(plugin_id)
    if decl.executable:
        raise HTTPException(500, f'{decl.name} 已声明可执行但没有活跃执行判定，拒绝在不知道是否有在跑任务的情况下停用')
    # Not executable: it never started anything, so nothing can be running.
    return lambda: 0


def router(store, svc):
    availability = plugins.PluginAvailability(store)
    api = APIRouter(prefix='/api/v2/plugins')

    @api.get('')
    def list_plugins(request: Request):
        request.state.user  # signed in; the middleware already enforced it
        return {'contract_version': plugins.CONTRACT_VERSION,
                'plugins': availability.all()}

    @api.get('/{plugin_id}')
    def get_plugin(plugin_id: str, request: Request):
        request.state.user
        try:
            return availability.view(plugin_id)
        except KeyError:
            raise HTTPException(404, '没有这个业务插件') from None

    @api.post('/{plugin_id}/state')
    def set_state(plugin_id: str, body: _StateBody, request: Request):
        actor = request.state.user
        try:
            return availability.set_state(
                plugin_id, body.state, actor=actor['username'],
                expected_revision=body.expected_revision,
                active_probe=_active_probe(store, plugin_id))
        except KeyError:
            raise HTTPException(404, '没有这个业务插件') from None
        except Conflict:
            # ``Conflict`` subclasses ``ValueError``; it has its own 409 handler
            # and must not be reported as a malformed body.
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.get('/{plugin_id}/audit')
    def plugin_audit(plugin_id: str, request: Request):
        request.state.user
        try:
            return {'entries': availability.audit(plugin_id)}
        except KeyError:
            raise HTTPException(404, '没有这个业务插件') from None

    return api
