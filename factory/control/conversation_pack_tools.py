"""Let a chat run the capability packs THIS role has attached.

The model never names a user, a conversation, a server path or an arbitrary
package. It is handed the list the server derives from this conversation's role
and that role's live bindings, and it may run one of those by id. Every gate the
HTTP invocation path applies -- the binding must exist, an input artifact must
belong to the caller, the version is frozen into the task inside the same
transaction -- is the SAME PackStore call here, not a re-implementation.

What crosses into this module from the model is only: which of the offered packs,
which operation key from that pack's manifest, a file name, and text content. No
path, no shell, no network, no actor, no agent.
"""
from __future__ import annotations

import hashlib
import json
import re

from factory.control.capability_packs import MAX_ARTIFACT_BYTES, PackStore
from factory.control.pack_runtime import run_tool

DOWNLOAD_PREFIX = '/api/v4/capability-packs/artifacts/'
MAX_CHAT_INPUT_BYTES = 2 * 1024 * 1024
_SAFE_NAME = re.compile(r'[^A-Za-z0-9._一-鿿-]+')


class PackToolError(Exception):
    """Refusal the model should see verbatim, with a stable code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _clean_name(name, fallback='input.txt'):
    cleaned = _SAFE_NAME.sub('-', (name or '').strip())[:120].strip('-.')
    return cleaned or fallback


class PackTools:
    """Bound to one conversation and one actor; both are fixed by the server."""

    def __init__(self, store, cid, actor_id, *, actor_role='member'):
        self.store = store
        self.cid = cid
        self.actor_id = actor_id
        self.actor_role = actor_role

    # ---- scope -----------------------------------------------------------
    def _agent_id(self):
        from factory.control.agents import AgentStore
        try:
            conversation = AgentStore(self.store).conversation(self.cid)
        except KeyError:
            return None
        return (conversation or {}).get('agent_id')

    def _actor(self):
        return {'id': self.actor_id, 'role': self.actor_role}

    def available(self):
        """Real name, version, input constraints and limits of each attached pack.

        Derived from the live bindings of THIS conversation's role. A role with no
        bindings gets an empty list, which is the honest answer -- not an error and
        not a hint that some other package might work.
        """
        agent_id = self._agent_id()
        if not agent_id:
            return []
        packs = PackStore(self.store)
        offered = []
        for binding in packs.bindings(agent_id):
            if 'invoke' not in (binding.get('allowed_uses') or []):
                continue
            contract = binding.get('tool_contract') or {}
            permissions = contract.get('permissions') or {}
            try:
                manifest = (packs.version(binding['version_id']) or {}).get('manifest') or {}
            except KeyError:
                continue  # the pinned version is gone; nothing honest to offer
            tool = manifest.get('tool') or {}
            offered.append({
                'pack_id': binding['pack_id'],
                'name': binding.get('pack_name'),
                'tool_name': tool.get('name'),
                'version': binding.get('version'),
                'purpose': contract.get('purpose') or manifest.get('purpose'),
                'input_schema': contract.get('input_schema'),
                'output_schema': contract.get('output_schema'),
                'timeout_seconds': contract.get('timeout_seconds'),
                'max_output_bytes': permissions.get('max_output_bytes'),
                'max_input_bytes': MAX_CHAT_INPUT_BYTES,
                'environment': binding.get('environment'),
                'upgrade_available': binding.get('upgrade_available'),
            })
        return offered

    # ---- execution -------------------------------------------------------
    def run(self, pack_id, content, filename=None):
        """Run one attached pack on text this conversation produced.

        Returns a structured result for every outcome. A refusal raises
        PackToolError; a tool that ran and failed comes back as a result with the
        tool's real status and error, never as a success sentence.
        """
        if not isinstance(content, str) or not content.strip():
            raise PackToolError('empty_input', '没有可提交的内容')
        raw = content.encode('utf-8')
        if len(raw) > MAX_CHAT_INPUT_BYTES:
            raise PackToolError('input_too_large',
                                f'输入 {len(raw)} 字节超过 {MAX_CHAT_INPUT_BYTES} 字节上限')
        if len(raw) > MAX_ARTIFACT_BYTES:  # defence in depth; the cap above is smaller
            raise PackToolError('input_too_large', '输入超过产物存储上限')

        offered = {item['pack_id']: item for item in self.available()}
        chosen = offered.get(pack_id)
        if chosen is None:
            # Unknown or unattached. Say which ones exist rather than hinting that
            # some other identifier might be accepted.
            raise PackToolError('not_attached',
                                '该职能体没有挂靠这个能力包。可用：' +
                                (('、'.join(sorted(filter(None, (i['pack_id'] for i in offered.values()))))) or '（无）'))
        packs = PackStore(self.store)
        actor = self._actor()
        agent_id = self._agent_id()
        artifact = packs.put_artifact(actor_id=self.actor_id, name=_clean_name(filename),
                                      content=raw, role='input',
                                      validation_status='not_applicable', dedupe=True)
        # Deterministic from (conversation, pack, exact bytes): sending the same
        # content twice replays the first task instead of running the tool again,
        # while a revised draft is different bytes and therefore a new task whose
        # inputs and version say which result came from which attempt.
        digest = hashlib.sha256(
            '|'.join((str(self.cid), str(pack_id), artifact['sha256'])).encode()).hexdigest()
        operation = 'chat-' + digest[:40]

        task = packs.create_task(actor=actor, agent_id=agent_id, pack_id=pack_id,
                                 input_artifact_ids=[artifact['id']], operation_key=operation)
        if task.get('idempotent_replay'):
            # The replay record is the task as it was CREATED (queued), not as it is
            # now. Re-read the live row: only a task that never actually ran may be
            # dispatched again, otherwise an identical resend would convert the same
            # file a second time.
            current = packs.task(task['id'], actor=actor)
            if current['status'] != 'queued':
                return self._view(current, replay=True)

        version = packs.version(task['snapshot']['version_id'])
        files = packs.files(version_id=version['id'])
        packs.update_task(task['id'], {'status': 'running'}, event=('task.running', {}),
                          expected=('queued', 'cancel_requested'))
        outcome = run_tool(version, files,
                           [{'name': artifact['name'], 'content': raw, 'sha256': artifact['sha256']}])
        saved = [packs.put_artifact(actor_id=self.actor_id, name=out['name'], content=out['content'],
                                    role='output', task_id=task['id'], kind=out['kind'],
                                    validation_status=outcome['validation_status'])
                 for out in outcome['outputs']]
        packs.update_task(task['id'], {
            'status': outcome['status'], 'outputs': saved, 'error_code': outcome.get('error_code'),
            'error': outcome.get('error'), 'result': outcome.get('result'),
            'diagnostics': outcome.get('diagnostics', []), 'evidence': outcome['evidence'],
            'validation_status': outcome['validation_status'], 'duration_ms': outcome.get('duration_ms')},
            event=('task.' + outcome['status'], {'error_code': outcome.get('error_code'),
                                                 'outputs': [a['id'] for a in saved]}))
        return self._view(packs.task(task['id'], actor=actor))

    @staticmethod
    def _view(task, *, replay=False):
        """What the model is allowed to report: the tool's real outcome and the
        files that were actually saved."""
        snapshot = task.get('snapshot') or {}
        return {
            'task_id': task['id'], 'status': task['status'],
            'replayed': bool(replay),
            'pack_id': task.get('pack_id'), 'tool': snapshot.get('tool'),
            'version': snapshot.get('version'),
            'validation_status': task.get('validation_status'),
            'error_code': task.get('error_code'), 'error': task.get('error'),
            'inputs': [{'name': i.get('name'), 'sha256': i.get('sha256')} for i in (task.get('inputs') or [])],
            'outputs': [{'artifact_id': o['id'], 'name': o.get('name'), 'kind': o.get('kind'),
                         'size': o.get('size'), 'sha256': o.get('sha256'),
                         'download_path': DOWNLOAD_PREFIX + o['id'] + '/download'}
                        for o in (task.get('outputs') or [])],
            'evidence': (task.get('evidence') or {}),
        }
