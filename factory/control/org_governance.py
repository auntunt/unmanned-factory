"""Organisation tree, project ownership and scoped *read-only* management views.

A scope grant lets a member look at the projects owned by one organisation
subtree through the /api/v5/management endpoints. It is deliberately not an
execution grant: running, cancelling, approving or reading conversations keep
using the existing admin / team_projects rules. It also never widens any other
endpoint. Visibility is recomputed from the tables on every request, so a
revoke or a move takes effect on the next call.

Everything here lives in the same users.db as ``team_projects`` and
``team_audit``; every mutation appends to that append-only audit.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from factory.control.auth import AuthError
from factory.control.autonomy import valid_cost
from factory.control.store import now

KINDS = ('company', 'department', 'group')
KIND_LABELS = {'company': '公司', 'department': '部门', 'group': '小组'}
IN_PROGRESS = {'received', 'requirement_analysis', 'planning', 'queued', 'running', 'verifying', 'publishing'}
PENDING = {'needs_clarification', 'awaiting_approval', 'awaiting_spec_confirmation', 'needs_human'}
PENDING_REASON = {
    'needs_clarification': '等待补充信息', 'awaiting_approval': '等待批准执行方案',
    'awaiting_spec_confirmation': '等待确认规格', 'needs_human': '等待人工处理',
}
_UNIT_ID = re.compile(r'[0-9a-f]{32}')
# Unknown or out-of-scope targets answer identically, so a leader cannot probe
# which ids exist in another department.
NOT_VISIBLE = '范围不存在或你无权查看'


def _uid(value):
    return value if isinstance(value, str) and _UNIT_ID.fullmatch(value) else None


class OrgGovernance:
    def __init__(self, governance, store, usage=None):
        self.governance, self.store = governance, store
        # usage(run_id) -> {'known_cost_usd', 'unknown_cost_calls', 'calls'}
        self.usage = usage
        with governance.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS org_units(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                    parent_id TEXT REFERENCES org_units(id), created_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS org_units_single_root
                    ON org_units((parent_id IS NULL)) WHERE parent_id IS NULL;
                CREATE TABLE IF NOT EXISTS org_projects(
                    project_id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES org_units(id),
                    bound_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS org_scopes(
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    unit_id TEXT NOT NULL REFERENCES org_units(id),
                    granted_by TEXT NOT NULL, granted_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, unit_id));
            ''')

    # ---- tree helpers -------------------------------------------------
    @staticmethod
    def _units(db):
        return {row['id']: dict(row) for row in db.execute('SELECT id,name,kind,parent_id,created_at FROM org_units')}

    @staticmethod
    def _subtree(units, root_id):
        children = {}
        for unit in units.values():
            children.setdefault(unit['parent_id'], []).append(unit['id'])
        out, stack = set(), [root_id]
        while stack:
            current = stack.pop()
            if current in out:
                continue
            out.add(current)
            stack.extend(children.get(current, ()))
        return out

    @staticmethod
    def _path(units, unit_id):
        names, seen = [], set()
        while unit_id in units and unit_id not in seen:
            seen.add(unit_id)
            names.append(units[unit_id]['name'])
            unit_id = units[unit_id]['parent_id']
        return ' / '.join(reversed(names))

    def _audit(self, db, actor, action, data):
        self.governance._audit(db, actor, action, {**data, 'result': data.get('result', 'ok')})

    # ---- effective visibility (the single rule every read goes through) ----
    def visibility(self, user, db=None):
        """Return (is_admin, visible unit ids, visible project ids, units)."""
        if db is None:
            with self.governance.connect() as db:
                return self.visibility(user, db)
        row = db.execute('SELECT role,active FROM users WHERE id=?', (user['id'],)).fetchone()
        units = self._units(db)
        project_ids = {p['id'] for p in self.store.projects()}
        if not row or not row['active']:
            return False, set(), set(), units
        if row['role'] == 'admin':
            return True, set(units), project_ids, units
        visible_units = set()
        for grant in db.execute('SELECT unit_id FROM org_scopes WHERE user_id=?', (user['id'],)):
            if grant['unit_id'] in units:
                visible_units |= self._subtree(units, grant['unit_id'])
        bound = {r['project_id'] for r in db.execute('SELECT project_id,unit_id FROM org_projects') if r['unit_id'] in visible_units}
        return False, visible_units, bound & project_ids, units

    def me(self, user):
        with self.governance.connect() as db:
            admin, visible, _, units = self.visibility(user, db)
            grants = [r['unit_id'] for r in db.execute('SELECT unit_id FROM org_scopes WHERE user_id=? ORDER BY granted_at', (user['id'],))]
        scopes = [{'unit_id': g, 'name': units[g]['name'], 'path': self._path(units, g)} for g in grants if g in units]
        return {'management': admin or bool(scopes), 'admin': admin, 'scopes': scopes,
                'execution_note': '管理查看不包含执行、取消、批准或读取对话的权限；这些仍按项目分配与发起人判断。'}

    # ---- admin: organisation ------------------------------------------
    def tree(self):
        projects = {p['id']: p for p in self.store.projects()}
        with self.governance.connect() as db:
            units = self._units(db)
            bindings = {r['project_id']: r['unit_id'] for r in db.execute('SELECT project_id,unit_id FROM org_projects')}
            scopes = [dict(r) for r in db.execute(
                'SELECT org_scopes.user_id,org_scopes.unit_id,org_scopes.granted_by,org_scopes.granted_at,users.username '
                'FROM org_scopes JOIN users ON users.id=org_scopes.user_id ORDER BY org_scopes.granted_at')]
            users = [{'id': r['id'], 'username': r['username'], 'role': r['role'], 'active': bool(r['active'])}
                     for r in db.execute('SELECT id,username,role,active FROM users ORDER BY id')]
        for scope in scopes:
            scope['path'] = self._path(units, scope['unit_id'])
        return {
            'units': [{**u, 'kind_label': KIND_LABELS.get(u['kind'], u['kind']), 'path': self._path(units, u['id']),
                       'project_ids': sorted(pid for pid, uid in bindings.items() if uid == u['id'] and pid in projects)}
                      for u in sorted(units.values(), key=lambda u: (self._path(units, u['id'])))],
            'projects': [{'id': pid, 'name': p.get('name'), 'unit_id': bindings.get(pid)} for pid, p in projects.items()],
            'unassigned_project_ids': sorted(pid for pid in projects if pid not in bindings),
            'scopes': scopes, 'users': users,
        }

    def create_unit(self, name, kind, parent_id, actor):
        name = (name or '').strip()
        if not 1 <= len(name) <= 80 or kind not in KINDS:
            raise AuthError('组织名称或类型无效', 422)
        with self.governance.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            units = self._units(db)
            if parent_id is None:
                if any(u['parent_id'] is None for u in units.values()):
                    raise AuthError('已存在顶层组织；新组织必须指定上级', 409)
            elif _uid(parent_id) not in units:
                raise AuthError('上级组织不存在', 404)
            unit = {'id': uuid.uuid4().hex, 'name': name, 'kind': kind, 'parent_id': parent_id, 'created_at': now()}
            db.execute('INSERT INTO org_units VALUES(:id,:name,:kind,:parent_id,:created_at)', unit)
            units[unit['id']] = unit
            self._audit(db, actor, 'org.unit.created', {'unit_id': unit['id'], 'unit_path': self._path(units, unit['id']),
                                                         'kind': kind, 'parent_id': parent_id})
            return unit

    def update_unit(self, unit_id, actor, *, name=None, parent_id=None, move=False):
        with self.governance.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            units = self._units(db)
            if _uid(unit_id) not in units:
                raise AuthError('组织不存在', 404)
            unit = units[unit_id]
            before, parent_before = self._path(units, unit_id), unit['parent_id']
            if name is not None:
                name = name.strip()
                if not 1 <= len(name) <= 80:
                    raise AuthError('组织名称无效', 422)
                unit['name'] = name
            if move:
                if unit['parent_id'] is None or parent_id is None:
                    raise AuthError('顶层组织不能移动，也不能把组织移成顶层', 422)
                if _uid(parent_id) not in units:
                    raise AuthError('上级组织不存在', 404)
                if parent_id in self._subtree(units, unit_id):
                    raise AuthError('不能把组织移到自己或下级之下（会形成循环）', 422)
                unit['parent_id'] = parent_id
            db.execute('UPDATE org_units SET name=?,parent_id=? WHERE id=?', (unit['name'], unit['parent_id'], unit_id))
            self._audit(db, actor, 'org.unit.updated', {'unit_id': unit_id, 'unit_path_before': before,
                                                         'parent_id_before': parent_before, 'parent_id': unit['parent_id'],
                                                         'unit_path': self._path(units, unit_id)})
            return unit

    def delete_unit(self, unit_id, actor):
        with self.governance.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            units = self._units(db)
            if _uid(unit_id) not in units:
                raise AuthError('组织不存在', 404)
            if any(u['parent_id'] == unit_id for u in units.values()) or \
                    db.execute('SELECT 1 FROM org_projects WHERE unit_id=?', (unit_id,)).fetchone() or \
                    db.execute('SELECT 1 FROM org_scopes WHERE unit_id=?', (unit_id,)).fetchone():
                raise AuthError('组织下仍有下级、项目或查看授权，先移走再删除', 409)
            path = self._path(units, unit_id)
            db.execute('DELETE FROM org_units WHERE id=?', (unit_id,))
            self._audit(db, actor, 'org.unit.deleted', {'unit_id': unit_id, 'unit_path': path})

    def bind_project(self, project_id, unit_id, actor):
        project = self.store.project(project_id)  # KeyError -> 404
        with self.governance.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            units = self._units(db)
            if _uid(unit_id) not in units:
                raise AuthError('组织不存在', 404)
            prior = db.execute('SELECT unit_id FROM org_projects WHERE project_id=?', (project_id,)).fetchone()
            db.execute('INSERT INTO org_projects VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET unit_id=excluded.unit_id,bound_at=excluded.bound_at',
                       (project_id, unit_id, now()))
            self._audit(db, actor, 'org.project.bound', {
                'project_id': project_id, 'project_name': project.get('name'), 'unit_id': unit_id,
                'unit_path': self._path(units, unit_id),
                'previous_unit_id': prior['unit_id'] if prior else None,
                'previous_unit_path': self._path(units, prior['unit_id']) if prior else None})

    def unbind_project(self, project_id, actor):
        with self.governance.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            units = self._units(db)
            prior = db.execute('SELECT unit_id FROM org_projects WHERE project_id=?', (project_id,)).fetchone()
            if not prior:
                raise AuthError('项目未归属任何组织', 404)
            db.execute('DELETE FROM org_projects WHERE project_id=?', (project_id,))
            self._audit(db, actor, 'org.project.unbound', {'project_id': project_id, 'unit_id': prior['unit_id'],
                                                            'unit_path': self._path(units, prior['unit_id'])})

    def grant_scope(self, user_id, unit_id, actor):
        with self.governance.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            units = self._units(db)
            target = db.execute('SELECT id,username,role,active FROM users WHERE id=?', (user_id,)).fetchone()
            if not target:
                raise AuthError('成员不存在', 404)
            if _uid(unit_id) not in units:
                raise AuthError('组织不存在', 404)
            if target['role'] == 'admin':
                raise AuthError('管理员已可查看全部范围，无需授予组织查看权', 422)
            if not target['active']:
                raise AuthError('成员已停用，不能授予查看范围', 422)
            if db.execute('SELECT 1 FROM org_scopes WHERE user_id=? AND unit_id=?', (user_id, unit_id)).fetchone():
                raise AuthError('该成员已拥有这个组织的查看范围', 409)
            db.execute('INSERT INTO org_scopes VALUES(?,?,?,?)', (user_id, unit_id, str(actor), now()))
            self._audit(db, actor, 'org.scope.granted', {
                'user_id': user_id, 'target_username': target['username'], 'unit_id': unit_id,
                'unit_path': self._path(units, unit_id), 'grant': 'management.read'})

    def revoke_scope(self, user_id, unit_id, actor):
        with self.governance.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            units = self._units(db)
            row = db.execute('SELECT 1 FROM org_scopes WHERE user_id=? AND unit_id=?', (user_id, unit_id)).fetchone()
            if not row:
                raise AuthError('查看授权不存在', 404)
            target = db.execute('SELECT username FROM users WHERE id=?', (user_id,)).fetchone()
            db.execute('DELETE FROM org_scopes WHERE user_id=? AND unit_id=?', (user_id, unit_id))
            self._audit(db, actor, 'org.scope.revoked', {
                'user_id': user_id, 'target_username': target['username'] if target else None,
                'unit_id': unit_id, 'unit_path': self._path(units, unit_id), 'grant': 'management.read'})

    # ---- management reads ---------------------------------------------
    def _scope(self, user, unit_id):
        with self.governance.connect() as db:
            admin, visible_units, visible_projects, units = self.visibility(user, db)
            if not admin and not visible_units:
                raise AuthError('你没有管理查看范围', 403)
            bindings = {r['project_id']: r['unit_id'] for r in db.execute('SELECT project_id,unit_id FROM org_projects')}
            grants = [r['unit_id'] for r in db.execute('SELECT unit_id FROM org_scopes WHERE user_id=?', (user['id'],))]
            audit_rows = [dict(r) for r in db.execute(
                "SELECT id,actor,action,data,at FROM team_audit WHERE action LIKE 'org.%' ORDER BY id DESC LIMIT 500")]
            month = datetime.now(timezone.utc).strftime('%Y-%m')
            token_rows = [dict(r) for r in db.execute(
                "SELECT project_id,status,actual_tokens,reserved_tokens FROM token_calls WHERE month=? OR status IN ('reserved','unknown')", (month,))]
        if unit_id is not None:
            if _uid(unit_id) not in visible_units:
                raise AuthError(NOT_VISIBLE, 404)
            in_units = self._subtree(units, unit_id)
            project_ids = {pid for pid in visible_projects if bindings.get(pid) in in_units}
            label = self._path(units, unit_id)
        else:
            in_units = visible_units
            project_ids = set(visible_projects)
            label = '全部组织与未归属项目' if admin else '、'.join(self._path(units, g) for g in grants if g in units)
        return dict(admin=admin, units=units, in_units=in_units, project_ids=project_ids, bindings=bindings,
                    label=label, audit_rows=audit_rows, token_rows=token_rows, month=month,
                    grants=[g for g in grants if g in units])

    def _can_act(self, user, run):
        if user['role'] == 'admin':
            return True
        if run.get('source', {}).get('actor_id') != user['id']:
            return False
        with self.governance.connect() as db:
            return self.governance._can_run(db, user['id'], run['project_id'])

    @staticmethod
    def _title(run):
        """Plan title only. Request text can carry pasted material, so it never
        backs a management summary; without a plan the task gets a neutral id."""
        plan = run.get('plan')
        title = plan.get('title') if isinstance(plan, dict) else None
        if isinstance(title, str) and title.strip():
            title = ' '.join(title.split())
            return title if len(title) <= 80 else title[:80] + '…'
        return f"任务 {str(run.get('id', ''))[:8]}（尚无方案标题）"

    def _cost(self, runs):
        if self.usage is None:
            return {'recorded': False, 'known_cost_usd': None, 'unknown_cost_calls': 0, 'calls': 0}
        known, unknown, calls = 0.0, 0, 0
        for run in runs:
            try:
                u = self.usage(run['id'])
            except Exception:  # an unreadable run is unknown usage, never zero
                unknown += 1
                continue
            calls += u.get('calls', 0)
            unknown += u.get('unknown_cost_calls', 0)
            known += valid_cost(u.get('known_cost_usd')) or 0.0
        priced = calls - unknown
        return {'recorded': calls > 0, 'known_cost_usd': round(known, 4) if priced > 0 else None,
                'unknown_cost_calls': unknown, 'calls': calls}

    def overview(self, user, unit_id=None, days=30):
        scope = self._scope(user, unit_id)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        project_ids = scope['project_ids']
        projects = {p['id']: p for p in self.store.projects() if p['id'] in project_ids}
        # Filter first: nothing outside project_ids is counted, summed or listed.
        runs = [r for r in self.store.all_runs() if r.get('project_id') in project_ids]
        recent = [r for r in runs if str(r.get('updated_at') or r.get('created_at') or '') >= since]
        units, bindings = scope['units'], scope['bindings']

        def bucket(run):
            status = run.get('status')
            return ('in_progress' if status in IN_PROGRESS else 'pending' if status in PENDING else
                    'ready' if status == 'ready_for_review' else 'published' if status == 'published' else
                    'failed' if status == 'failed' else 'cancelled' if status == 'cancelled' else 'other')

        recent_ids = {r['id'] for r in recent}

        def count(items):
            out = {'in_progress': 0, 'pending': 0, 'ready': 0, 'published': 0, 'failed': 0}
            for run in items:
                b = bucket(run)
                if b in ('in_progress', 'pending') or (b in out and run['id'] in recent_ids):
                    out[b] += 1
            return out

        pending = []
        for run in runs:
            if bucket(run) != 'pending':
                continue
            can_act = self._can_act(user, run)
            pending.append({
                'run_id': run['id'], 'project_id': run['project_id'], 'project_name': projects[run['project_id']].get('name'),
                'title': self._title(run), 'status': run.get('status'), 'reason': PENDING_REASON[run['status']],
                'initiator': run.get('source', {}).get('actor'), 'updated_at': run.get('updated_at') or run.get('created_at'),
                'can_act': can_act,
                'action_hint': '你是发起人，可在工作台处理' if can_act and user['role'] != 'admin' else
                               '管理员可在工作台处理' if can_act else '需要发起人或管理员处理；管理查看不包含执行或批准权限'})
        project_views = []
        for pid, project in projects.items():
            prs = [r for r in runs if r['project_id'] == pid]
            initiators = sorted({r.get('source', {}).get('actor') for r in prs if r.get('source', {}).get('actor')})
            last = max((str(r.get('updated_at') or r.get('created_at') or '') for r in prs), default=None)
            project_views.append({'id': pid, 'name': project.get('name'), 'unit_id': bindings.get(pid),
                                  'unit_path': self._path(units, bindings[pid]) if pid in bindings else None,
                                  'counts': count(prs), 'initiators': initiators, 'last_activity_at': last,
                                  'run_count': len(prs)})
        project_views.sort(key=lambda p: p['last_activity_at'] or '', reverse=True)
        tokens = [r for r in scope['token_rows'] if r['project_id'] in project_ids]
        usage = {
            'window_days': days, 'runs_considered': len(recent), **self._cost(recent),
            'cost_source': '运行事件 usage.recorded（按窗口内更新过的运行汇总）',
            'tokens': {'month': scope['month'], 'settled_tokens': sum(r['actual_tokens'] or 0 for r in tokens if r['status'] == 'settled'),
                       'unknown_calls': sum(r['status'] == 'unknown' for r in tokens),
                       'reserved_calls': sum(r['status'] == 'reserved' for r in tokens),
                       'source': '模型调度台账 token_calls（本月，UTC）', 'recorded': bool(tokens)},
        }
        audit = self._audit_view(scope, filtered=not scope['admin'] or unit_id is not None)
        view = {
            'scope': {'unit_id': unit_id, 'label': scope['label'], 'admin': scope['admin'],
                      'units': [{'id': u, 'name': units[u]['name'], 'path': self._path(units, u),
                                 'kind_label': KIND_LABELS.get(units[u]['kind'], units[u]['kind'])}
                                for u in sorted(scope['in_units'], key=lambda u: self._path(units, u))],
                      'grants': [{'unit_id': g, 'path': self._path(units, g)} for g in scope['grants']]},
            'window': {'days': days, 'since': since, 'note': '进行中与待处理为当前状态；成果就绪、已发布、失败只统计窗口内更新过的任务。'},
            'generated_at': now(),
            'counts': count(runs), 'projects': project_views, 'pending': pending, 'usage': usage, 'audit': audit,
            'notes': {'published': '“已发布”指任务的发布步骤已完成（例如推送或 PR），不等于已上线。',
                      'responsibility': '责任主体取任务记录中的发起人；项目未记录负责人，不作推断。',
                      'execution': '管理查看不包含执行、取消、批准或读取对话的权限。'},
        }
        if scope['admin'] and unit_id is None:
            bound = set(bindings)
            view['unassigned_projects'] = [{'id': p['id'], 'name': p['name']} for p in project_views if p['id'] not in bound]
        return view

    def _audit_view(self, scope, filtered):
        """Org audit rows for the management view.

        An admin gets the append-only rows verbatim. Everyone else gets a
        projection: a row is chosen by the unit it happened in (or, for rows
        without a unit, by a project now in scope), and only whitelisted fields
        survive. Units and projects are named only if they are in scope *now*,
        with the path recomputed from today's tree, because recorded paths and
        "previous" fields describe where things used to be, which may be
        another department.
        """
        out = []
        for row in scope['audit_rows']:
            data = json.loads(row['data'])
            unit, project = data.get('unit_id'), data.get('project_id')
            if filtered:
                chosen = unit in scope['in_units'] if unit else project in scope['project_ids']
                if not chosen:
                    continue
            if not scope['admin']:
                data = self._redacted(data, scope)
            out.append({'id': row['id'], 'actor': row['actor'], 'action': row['action'], 'at': row['at'], 'data': data})
            if len(out) >= 50:
                break
        return out

    def _redacted(self, data, scope):
        units, in_units, projects = scope['units'], scope['in_units'], scope['project_ids']

        def unit_ref(uid):
            return self._path(units, uid) if uid in in_units else '范围外组织'

        view = {'result': data.get('result')}
        for key in ('kind', 'grant', 'target_username'):
            if key in data:
                view[key] = data[key]
        if data.get('unit_id'):
            view['unit_id'] = data['unit_id'] if data['unit_id'] in in_units else None
            view['unit_path'] = unit_ref(data['unit_id'])
        if data.get('project_id'):
            visible = data['project_id'] in projects
            view['project_id'] = data['project_id'] if visible else None
            view['project_name'] = data.get('project_name') if visible else '范围外项目'
        # Where a project or unit came from: shown only when that place is in
        # scope now; older rows recorded no id, so they cannot be shown at all.
        if 'previous_unit_path' in data:
            prev = data.get('previous_unit_id')
            view['previous_unit_path'] = unit_ref(prev) if prev else ('范围外组织' if data['previous_unit_path'] else None)
        if 'unit_path_before' in data:
            prev = data.get('parent_id_before')
            view['parent_path_before'] = unit_ref(prev) if prev else '范围外组织'
        return view

    def project_detail(self, user, project_id):
        scope = self._scope(user, None)
        if project_id not in scope['project_ids']:
            raise AuthError(NOT_VISIBLE, 404)
        project = self.store.project(project_id)
        runs = [r for r in self.store.all_runs() if r.get('project_id') == project_id]
        units, bindings = scope['units'], scope['bindings']
        items = []
        for run in runs[:100]:
            cost = self._cost([run])
            items.append({'run_id': run['id'], 'title': self._title(run), 'status': run.get('status'),
                          'initiator': run.get('source', {}).get('actor'),
                          'created_at': run.get('created_at'), 'updated_at': run.get('updated_at'),
                          'known_cost_usd': cost['known_cost_usd'], 'unknown_cost_calls': cost['unknown_cost_calls'],
                          'can_act': self._can_act(user, run)})
        return {'project': {'id': project_id, 'name': project.get('name'),
                            'unit_path': self._path(units, bindings[project_id]) if project_id in bindings else None},
                'runs': items, 'truncated': len(runs) > 100, 'generated_at': now(),
                'note': '仅显示任务摘要；对话、原始请求全文、日志与凭据不在管理视图中提供。'}
