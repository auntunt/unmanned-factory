"""Explicit registration and service-side availability for trusted business plugins.

Three business capabilities ship in the same distribution as webuddy itself:
信创化改造, 自动化运维 and 自动化三方接口适配.  They are *not* uploaded code and
this module is not a plugin marketplace -- there is no dynamic import, no path
derived from a request, and no way to register something that is not listed in
``DECLARATIONS`` below.  What a "plugin" buys us is a boundary: a stable id, the
Skill version its method comes from, the host capabilities it is allowed to
reach, and an availability state an administrator controls.

Deliberate non-goals, each of which would be a second source of truth:

* No second task model, executor, queue or database.  A plugin's business data
  stays in the tables that already own it; this module owns only availability.
* No per-request role parsing.  ``require`` is handed an already-resolved
  action kind by the host; an actor's authorization is still the project
  authorization every business port already performs.
* No claim of sandboxing.  These are in-process modules in one distribution.
  The isolation here is one of responsibility and authority, not of untrusted
  code execution, and nothing in this file should be read as the latter.

The availability states are about the *plugin*, not about a task:

``enabled``   新建与继续都允许。
``draining``  拒绝新建；已在跑的按原授权继续；查询/导出/取消照常。
``disabled``  不启动新执行（新建与继续都拒绝）；查询/导出/取消保留。

Hiding a button is not a stop.  Every surface -- HTTP, CLI, and anything added
later -- reaches the same ``PluginGate`` on the service side, because the port
they all assemble from is wrapped by ``gated`` before it is handed out.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from factory.control.store import Conflict, now as _now

#: The host contract these declarations are written against. A plugin declares
#: the range it is compatible with; the registry refuses one that is not.
CONTRACT_VERSION = 1

STATES = ('enabled', 'draining', 'disabled')

#: What a call wants to do, as the host classifies it -- never as a caller says.
#: ``always`` is for reading history, exporting a past delivery and cancelling,
#: which stay available in every state because a stopped plugin must not make a
#: customer's finished work unreachable.
ACTIONS = ('create', 'continue', 'always')

#: action kind -> the states in which it is accepted.
_ALLOWED = {
    'create': frozenset({'enabled'}),
    'continue': frozenset({'enabled', 'draining'}),
    'always': frozenset(STATES),
}

_REFUSAL = {
    ('create', 'draining'): '{name}正在排空，暂不接受新任务；已有任务仍可查询、导出与取消。',
    ('create', 'disabled'): '{name}已停用，不能新建任务；历史任务仍可查询与导出。',
    ('continue', 'disabled'): '{name}已停用，历史任务不会自动继续执行；需要继续请管理员先重新启用。',
}


@dataclass(frozen=True)
class PluginDeclaration:
    """What a plugin tells the host about itself. Static data, not behaviour."""

    id: str
    name: str
    version: int
    #: inclusive host-contract range this plugin is written against
    contract_min: int
    contract_max: int
    #: the Skill pack whose method version a task records. Same id as before.
    skill_pack_id: str
    #: business entry points that already exist, by surface.
    entry_points: dict = field(default_factory=dict)
    #: host capabilities the plugin is allowed to reach through existing ports.
    host_capabilities: tuple = ()
    #: keys the frontend uses to decide which extension points to render.
    ui_keys: tuple = ()
    #: Whether a real handler is wired. A plugin with no handler may be listed
    #: but never enabled -- an entry that looks ready while nothing is behind it
    #: is worse than a missing entry, because the refusal arrives after the
    #: customer has already committed to the workflow.
    executable: bool = False
    #: Whether a database that has never seen this plugin starts it on.
    #: Only ``issue-maintenance`` does, because its surface was already live
    #: before this module existed and seeding it off would have stopped a
    #: working feature. A newly wired scenario starts off and an administrator
    #: turns it on per customer, which is what PLUGIN-CONTRACT.md means by
    #: "按客户启用插件和项目配置" -- shipping it on by default would enable
    #: it for every existing customer at upgrade time without anyone deciding to.
    default_enabled: bool = False

    def compatible(self) -> bool:
        return self.contract_min <= CONTRACT_VERSION <= self.contract_max


DECLARATIONS: tuple[PluginDeclaration, ...] = (
    PluginDeclaration(
        id='issue-maintenance', name='自动化运维', version=1,
        contract_min=1, contract_max=1, skill_pack_id='issue-maintenance',
        default_enabled=True,
        entry_points={'http': '/api/v2/maintenance',
                      'cli': 'python -m factory.control.maintenance_cli',
                      'ui': '/maintenance'},
        host_capabilities=('project.authorize', 'run.dispatch', 'run.events',
                           'run.cost', 'repository.resolve', 'delivery.export'),
        ui_keys=('maintenance.tasks', 'maintenance.detail'),
        executable=True),
    PluginDeclaration(
        id='legacy-modernization', name='信创化改造', version=1,
        contract_min=1, contract_max=1, skill_pack_id='legacy-modernization',
        entry_points={'http': '/api/v2/modernization'},
        host_capabilities=('project.authorize', 'run.dispatch', 'run.events',
                           'run.cost', 'repository.resolve', 'delivery.export',
                           'project.memory', 'code.index'),
        ui_keys=(),
        executable=True),
    PluginDeclaration(
        id='api-adaptation', name='自动化三方接口适配', version=1,
        contract_min=1, contract_max=1, skill_pack_id='api-adaptation',
        entry_points={'http': '/api/v2/adaptation'},
        host_capabilities=('project.authorize', 'run.dispatch', 'run.events',
                           'run.cost', 'repository.resolve', 'delivery.export',
                           'project.memory'),
        ui_keys=(),
        executable=True),
)

_BY_ID = {d.id: d for d in DECLARATIONS}


def declarations() -> tuple[PluginDeclaration, ...]:
    return DECLARATIONS


def declaration(plugin_id: str) -> PluginDeclaration:
    """The declaration, or ``KeyError``. Never a fabricated default."""
    return _BY_ID[plugin_id]


def _default_state(decl: PluginDeclaration) -> str:
    """The state a database that has never seen this plugin starts it in.

    Seeded once, in ``_initialize``; an existing row is never rewritten, so a
    plugin an administrator turned off stays off across upgrades and a newly
    declared one does not quietly inherit someone else's decision.
    """
    if not (decl.executable and decl.compatible()):
        return 'disabled'
    return 'enabled' if decl.default_enabled else 'disabled'


class PluginAvailability:
    """Durable per-plugin availability, CAS-updated, with an append-only audit.

    The row is the authority. A process that has not been restarted since an
    administrator disabled a plugin still refuses, because every check reads
    this table rather than a value cached at assembly time.
    """

    def __init__(self, store):
        self.store = store
        self._initialize()

    def _initialize(self) -> None:
        with self.store.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS plugin_availability (
                    plugin_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    actor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plugin_availability_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plugin_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    data TEXT NOT NULL,
                    at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS no_plugin_audit_update
                    BEFORE UPDATE ON plugin_availability_audit
                    BEGIN SELECT RAISE(ABORT,'plugin availability audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_plugin_audit_delete
                    BEFORE DELETE ON plugin_availability_audit
                    BEGIN SELECT RAISE(ABORT,'plugin availability audit is append-only'); END;
                """)
            for decl in DECLARATIONS:
                state = _default_state(decl)
                db.execute(
                    'INSERT OR IGNORE INTO plugin_availability'
                    '(plugin_id,state,revision,updated_at,actor) VALUES (?,?,?,?,?)',
                    (decl.id, state, 1, _now(), 'system'))

    # -- reading ----------------------------------------------------------
    def state(self, plugin_id: str) -> dict:
        """The persisted availability row. ``KeyError`` for an unknown plugin.

        An unknown id is refused rather than defaulted: a typo in a call site
        must not silently read as "available".
        """
        decl = declaration(plugin_id)
        with self.store.connect() as db:
            row = db.execute(
                'SELECT state,revision,updated_at,actor FROM plugin_availability'
                ' WHERE plugin_id=?', (plugin_id,)).fetchone()
        if row is None:
            # The row is seeded in ``_initialize``; a missing one means the
            # table was tampered with or the store was swapped underneath us.
            # Fail closed rather than inventing an enabled default.
            return {'plugin_id': plugin_id, 'state': 'disabled', 'revision': 0,
                    'updated_at': None, 'actor': 'system',
                    'note': '可用性记录缺失，按停用处理'}
        return {'plugin_id': decl.id, 'state': row['state'],
                'revision': row['revision'], 'updated_at': row['updated_at'],
                'actor': row['actor']}

    def view(self, plugin_id: str) -> dict:
        decl = declaration(plugin_id)
        current = self.state(plugin_id)
        return {
            'id': decl.id, 'name': decl.name, 'version': decl.version,
            'skill_pack_id': decl.skill_pack_id,
            'contract': {'host': CONTRACT_VERSION, 'min': decl.contract_min,
                         'max': decl.contract_max, 'compatible': decl.compatible()},
            'entry_points': dict(decl.entry_points),
            'host_capabilities': list(decl.host_capabilities),
            'ui_keys': list(decl.ui_keys),
            'executable': decl.executable,
            'state': current['state'], 'revision': current['revision'],
            'updated_at': current['updated_at'], 'actor': current['actor'],
            'can_create': current['state'] in _ALLOWED['create'],
            'can_continue': current['state'] in _ALLOWED['continue'],
        }

    def all(self) -> list[dict]:
        return [self.view(d.id) for d in DECLARATIONS]

    def audit(self, plugin_id: str, *, limit: int = 100) -> list[dict]:
        declaration(plugin_id)
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT revision,actor,action,data,at FROM plugin_availability_audit'
                ' WHERE plugin_id=? ORDER BY id DESC LIMIT ?',
                (plugin_id, int(limit))).fetchall()
        return [{'revision': r['revision'], 'actor': r['actor'],
                 'action': r['action'], 'data': json.loads(r['data']),
                 'at': r['at']} for r in rows]

    # -- transition -------------------------------------------------------
    def set_state(self, plugin_id: str, state: str, *, actor: str,
                  expected_revision: int | None = None,
                  active_probe=None) -> dict:
        """Move a plugin's availability, refusing the transitions that would lie.

        ``active_probe`` is a callable returning how many of this plugin's
        executions are still live, as the *existing* engine counts them. It is
        injected rather than computed here because only the plugin's own port
        knows what "still running" means for it -- and a stop that silently
        cancelled a customer's task would be worse than a refusal.
        """
        decl = declaration(plugin_id)
        if state not in STATES:
            raise ValueError(f'未知的插件可用性状态：{state}')
        current = self.state(plugin_id)
        if expected_revision is not None and expected_revision != current['revision']:
            raise Conflict('插件可用性已被其他人修改，请刷新后重试')
        if state == current['state']:
            return self.view(plugin_id)
        if state == 'enabled':
            if not decl.executable:
                raise Conflict(f'{decl.name}尚未接入可执行处理器，不能启用')
            if not decl.compatible():
                raise Conflict(
                    f'{decl.name}声明的宿主契约范围 '
                    f'{decl.contract_min}-{decl.contract_max} 不包含当前宿主 {CONTRACT_VERSION}')
        if state == 'disabled' and current['state'] == 'enabled':
            # Stopping goes through draining on purpose: it is the only way an
            # administrator finds out that live work exists *before* the stop
            # lands, and the alternative -- stopping first and reporting the
            # orphans afterwards -- is exactly the silent abandon this refuses.
            raise Conflict(f'{decl.name}要先进入「排空中」，活跃执行清空后才能停用')
        if state == 'disabled' and current['state'] == 'draining':
            active = int(active_probe() if active_probe else 0)
            if active:
                raise Conflict(f'{decl.name}还有 {active} 个执行没有结束，清空后才能停用；'
                               '本操作不会自动取消客户任务')
        revision = current['revision'] + 1
        at = _now()
        with self.store.connect() as db:
            changed = db.execute(
                'UPDATE plugin_availability SET state=?,revision=?,updated_at=?,actor=?'
                ' WHERE plugin_id=? AND revision=?',
                (state, revision, at, actor, plugin_id, current['revision'])).rowcount
            if not changed:
                raise Conflict('插件可用性已被其他人修改，请刷新后重试')
            db.execute(
                'INSERT INTO plugin_availability_audit'
                '(plugin_id,revision,actor,action,data,at) VALUES (?,?,?,?,?,?)',
                (plugin_id, revision, actor, 'set_state',
                 json.dumps({'from': current['state'], 'to': state},
                            ensure_ascii=False), at))
        return self.view(plugin_id)


class PluginGate:
    """One service-side check, shared by every surface of one plugin.

    Constructed with the availability store rather than with a state value, so
    a long-lived assembly (the app's router, a CLI process) cannot keep serving
    from the state that happened to be true when it was built.
    """

    def __init__(self, availability: PluginAvailability, plugin_id: str):
        self.availability = availability
        self.plugin_id = plugin_id
        self.declaration = declaration(plugin_id)

    def state(self) -> str:
        return self.availability.state(self.plugin_id)['state']

    def require(self, action: str) -> None:
        if action not in ACTIONS:
            # An unclassified action is refused, not waved through: a method
            # added later must be classified deliberately.
            raise ValueError(f'未知的插件动作类别：{action}')
        state = self.state()
        if state in _ALLOWED[action]:
            return
        template = _REFUSAL.get((action, state))
        if template is None:
            template = '{name}当前不可用（' + state + '），该操作被拒绝。'
        raise Conflict(template.format(name=self.declaration.name))


#: Which availability class each business-port method falls into. A method that
#: is not listed here is not reachable through the gated port at all, so adding
#: one to a port without classifying it produces an ``AttributeError`` at the
#: call site rather than an ungated business action.
MAINTENANCE_ACTIONS = {
    'create': 'create',
    'revise': 'create',
    'intervene': 'continue',
    'resume': 'continue',
    'get': 'always',
    'list': 'always',
    'events': 'always',
    'cancel': 'always',
    'export': 'always',
}


class GatedPort:
    """A business port with one availability check in front of every method.

    Only the classified methods are exposed. This is the reason the check
    cannot be forgotten by a new surface: a surface does not get to choose
    whether to call ``require`` -- it only ever holds this object.
    """

    __slots__ = ('_port', '_gate', '_actions')

    def __init__(self, port, gate: PluginGate, actions: dict):
        object.__setattr__(self, '_port', port)
        object.__setattr__(self, '_gate', gate)
        object.__setattr__(self, '_actions', dict(actions))

    @property
    def gate(self) -> PluginGate:
        """For the few host paths that carry a decision to another subsystem.

        ``maintenance_routes.approve`` hands the approval to the run lifecycle
        rather than to the port, so it asks the gate itself. Exposing the gate
        is what keeps that path from being the one door with no check.
        """
        return object.__getattribute__(self, '_gate')

    @property
    def port(self):
        """The unwrapped port, for host code that is not a business action."""
        return object.__getattribute__(self, '_port')

    def __getattr__(self, name):
        actions = object.__getattribute__(self, '_actions')
        if name not in actions:
            raise AttributeError(
                f'{name} 没有归类到插件可用性动作，未经分类的业务方法不对外暴露')
        gate = object.__getattribute__(self, '_gate')
        target = getattr(object.__getattribute__(self, '_port'), name)

        def call(*args, **kwargs):
            gate.require(actions[name])
            return target(*args, **kwargs)

        call.__name__ = name
        return call


def gated(port, availability: PluginAvailability, plugin_id: str,
          actions: dict) -> GatedPort:
    return GatedPort(port, PluginGate(availability, plugin_id), actions)
