"""Long-lived project memory for the three business scenarios.

There is no new store here on purpose.  ``KnowledgeStore`` already keeps exactly
what SHARED.md asks a project fact to carry -- what it says (``title`` /
``content``), where it applies (``paths``), which code version it was true for
(``commit_sha``), where it came from (``provenance``), whether anyone confirmed
it (``status``), what kind of claim it is (``kind``) and when (``created_at``) --
with append-only versions and an audit trail.  A second table would be a second
answer to "what does this project require", which is the thing project memory
exists to prevent.

What this module adds is the small amount of convention the three scenarios need
to share one store:

* A scenario prefix on the title, because ``KnowledgeStore`` generates entry keys
  itself and a caller cannot choose one.  This is a *convention for grouping*,
  not an authorization boundary: anything that can write project knowledge can
  write a prefixed title, and ``recall`` says so rather than implying the
  grouping is enforced.
* One reading rule.  ``active`` means a human confirmed it; ``candidate`` means
  nobody has.  An unconfirmed entry never travels into an executor prompt as a
  requirement, because "the report suggested 达梦" and "the customer approved
  达梦" are different facts and the second one is the only one worth acting on.

Cross-scenario reads are the default, deliberately: a constraint recorded while
planning a 信创 migration is still true when a maintenance task touches the same
code, and scoping each scenario to its own island would recreate the three
separate memories this is meant to avoid.
"""
from __future__ import annotations

from factory.control.knowledge import KnowledgeStore

#: plugin id -> the Chinese scenario name used in the title prefix.
SCENARIOS = {
    'legacy-modernization': '信创',
    'issue-maintenance': '运维',
    'api-adaptation': '适配',
}

#: Only these count as something the system may act on.
CONFIRMED_STATUS = 'active'
UNCONFIRMED_STATUS = 'candidate'


def scenario_name(plugin_id: str) -> str:
    try:
        return SCENARIOS[plugin_id]
    except KeyError:
        raise KeyError(f'未知的业务场景：{plugin_id}') from None


def title_for(plugin_id: str, topic: str) -> str:
    """``[运维] 报表导出金额精度``. Refuses an empty topic rather than storing a bare tag."""
    topic = (topic or '').strip()
    if not topic:
        raise ValueError('项目记忆条目必须有主题')
    return f'[{scenario_name(plugin_id)}] {topic}'


def belongs_to(entry, plugin_id: str) -> bool:
    return str(entry.get('title', '')).startswith(f'[{scenario_name(plugin_id)}] ')


def record(store, project_id: str, *, plugin_id: str, topic: str, content: str,
           actor: str, kind: str = 'fact', status: str = UNCONFIRMED_STATUS,
           paths=(), commit_sha=None, key: str | None = None,
           expected_revision: int = 0) -> dict:
    """Write one project fact. New entries default to unconfirmed.

    ``status`` defaults to ``candidate`` because almost everything this system
    writes is something it observed, not something the customer agreed to.
    Promoting an entry to ``active`` is a separate act with a reviewer recorded
    against it, which is what makes "已确认" mean anything later.
    """
    memory = KnowledgeStore(store)
    data = {'kind': kind, 'status': status,
            'title': title_for(plugin_id, topic), 'content': content,
            'paths': list(paths)}
    if commit_sha:
        data['commit_sha'] = commit_sha
    return memory.put_entry(project_id, data, actor, key=key,
                            expected_revision=expected_revision)


def recall(store, project_id: str, *, plugin_id: str | None = None,
           confirmed_only: bool = False) -> list[dict]:
    """Everything this project knows, newest-revision only, retired excluded.

    ``plugin_id`` narrows to one scenario's prefix. Leaving it out is the normal
    case: a maintenance task should see a migration constraint recorded by the
    modernization workflow, not just its own notes.
    """
    entries = KnowledgeStore(store).entries(project_id)
    if plugin_id is not None:
        entries = [e for e in entries if belongs_to(e, plugin_id)]
    if confirmed_only:
        entries = [e for e in entries if e['status'] == CONFIRMED_STATUS]
    return entries


#: What an entry *is*, derived from the two fields the knowledge store already
#: keeps. Confirming something does not make it an order, and an observation
#: this system wrote is not a customer requirement -- collapsing the four into
#: one "必须遵守" list was the defect the first review caught.
CONSTRAINT = 'constraint'   # 客户确认过的要求：本次必须遵守
DECISION = 'decision'       # 记下来的决定，尚未确认为长期要求
FACT = 'fact'               # 已确认的事实：背景，不是命令
OBSERVATION = 'observation'  # 未确认的观察：本系统记下的交付结果，或还没人确认的资料
LEAD = 'lead'               # 未确认的线索：可以去查，不能当依据

ROLE_LABELS = {
    CONSTRAINT: '必须遵守的已确认要求',
    DECISION: '已记录的决定（未确认为长期要求）',
    FACT: '已确认的事实（背景）',
    OBSERVATION: '未确认的观察（记录下来的结果或资料）',
    LEAD: '未确认的调查线索',
}

#: Only this role is binding. Everything else is context a reader may use and
#: an executor must not treat as an instruction.
BINDING_ROLES = (CONSTRAINT,)

#: How much memory text may travel into one prompt. A project that accumulates
#: a thousand confirmed entries must not silently push the actual task out of
#: the context window -- what does not fit is dropped *and counted*.
DEFAULT_BUDGET_CHARS = 6000


def role_of(entry) -> str:
    """Classify an entry from ``kind`` + ``status``. No new column."""
    kind, status = entry.get('kind'), entry.get('status')
    if kind == 'hypothesis':
        return LEAD
    if status != CONFIRMED_STATUS:
        # Unconfirmed either way. A delivery this system recorded and a
        # suggestion nobody has approved are both observations: neither is
        # something the next task must obey, and the difference between them
        # is not something this layer can tell from the row.
        return OBSERVATION if kind == 'fact' else DECISION
    return CONSTRAINT if kind == 'decision' else FACT


def _applies_to(entry, scope_paths) -> bool:
    """Whether a scoped entry is relevant to the code this task will touch.

    An entry with no ``paths`` is project-wide and always applies. One that
    named its scope applies only when the task actually goes near it: a
    confirmed requirement about ``db/schema.sql`` is not a requirement about
    every future task in the project, and carrying it into all of them is how
    "已确认" stops meaning anything.
    """
    paths = entry.get('paths') or []
    if not paths:
        return True
    if not scope_paths:
        # No declared scope means we cannot rule it out, so it travels -- but
        # as the entry's own scope note says where it applies.
        return True
    for entry_path in paths:
        for scope in scope_paths:
            if entry_path.startswith(scope) or scope.startswith(entry_path):
                return True
    return False


#: How much of a scoped constraint travels when the task's scope is not known
#: yet. Enough to recognise what it is about; the full text arrives in phase two.
SUMMARY_CHARS = 180

#: Binding requirements are never dropped to save room, and they are not
#: allowed to quietly overrun the budget either: the limit is the limit. When
#: they do not fit, the task is blocked rather than run against a subset or run
#: with a budget that only applied to everything else.
CONSTRAINT_CEILING_MULTIPLIER = 1


def _cost(entry) -> int:
    return len(entry.get('content') or '') + len(entry.get('title') or '') + 32


def _summarise(entry) -> dict:
    """A scoped *context* entry, cut down but still identifiable, and marked as cut.

    Only reached while the task's scope is unknown and the full set does not
    fit. Never applied to a binding requirement. Phase two (``refine_for_plan``)
    brings back the full text of whatever turns out to be in scope, and it runs
    automatically -- this never asks an executor to go and read something it has
    no way to read.
    """
    content = entry.get('content') or ''
    if len(content) <= SUMMARY_CHARS:
        return {**entry, 'summary_only': False}
    return {**entry, 'content': content[:SUMMARY_CHARS] + '…（背景资料，正文未完；'
                                '本次改动范围确定后会自动补齐，不需要你去取）',
            'summary_only': True}


def load_for_task(store, project_id, *, plugin_id=None, scope_paths=None,
                  budget_chars=DEFAULT_BUDGET_CHARS,
                  constraint_ceiling_chars=None) -> dict:
    """The memory one task actually loads, with a receipt of what was loaded.

    Three rules the first version got wrong:

    1. **The budget really binds.** It used to let the first entry through
       whatever its size, so a single 7000-character requirement sailed past a
       6000-character budget and then pushed the *next* binding requirement out.
    2. **Binding requirements are never dropped to save room.** Trimming a
       must-obey requirement for tokens and then coding anyway is worse than not
       starting: the work looks done and is judged against something the
       executor never saw. If the constraints alone do not fit, this returns
       ``blocked`` and the caller refuses to dispatch.
    3. **Unknown scope is not empty scope.** ``scope_paths=None`` means "nobody
       has said which files this touches yet", and a scoped requirement then
       travels as a marked summary rather than either being dropped (silently
       wrong) or loaded whole (guessing that it is relevant). ``refine_for_plan``
       fills in the full text once a plan names the paths.
    """
    try:
        entries = recall(store, project_id, plugin_id=plugin_id)
    except (KeyError, ValueError) as exc:
        return {'block': '（项目记忆暂时读不到，本次没有携带已确认约束）',
                'entries': [], 'refs': [], 'dropped': [], 'blocked': None,
                'scope_known': scope_paths is not None, 'error': str(exc)}

    scope_known = scope_paths is not None
    scope = tuple(scope_paths or ())
    selected, out_of_scope = [], []
    for entry in entries:
        if not scope_known or _applies_to(entry, scope):
            # Nothing is out of scope while nothing is in scope yet.
            selected.append(entry)
        else:
            out_of_scope.append(entry)

    # Summaries are for *context*, never for a requirement. A shortened
    # must-obey line is a requirement the executor cannot actually follow, and
    # deferring its full text to a later phase would mean the coding step runs
    # without it. Binding requirements therefore travel whole or the task is
    # blocked below; only non-binding context is ever cut down, and only when
    # the full load does not fit.
    if not scope_known and sum(_cost(e) for e in selected) > budget_chars:
        selected = [e if role_of(e) == CONSTRAINT or not (e.get('paths') or [])
                    else _summarise(e) for e in selected]

    order = {CONSTRAINT: 0, DECISION: 1, FACT: 2, OBSERVATION: 3, LEAD: 4}
    selected.sort(key=lambda e: (order[role_of(e)], e['title']))
    constraints = [e for e in selected if role_of(e) == CONSTRAINT]
    context = [e for e in selected if role_of(e) != CONSTRAINT]

    ceiling = (constraint_ceiling_chars
               or budget_chars * CONSTRAINT_CEILING_MULTIPLIER)
    # Measured against the *full* text, because that is what will be carried.
    constraint_cost = sum(_cost(e) for e in constraints)
    if constraint_cost > ceiling:
        oversized = [{'key': e['key'], 'title': e['title'], 'chars': _cost(e)}
                     for e in constraints]
        blocked = {
            'kind': 'memory.constraints_exceed_budget',
            'message': (f'本项目必须遵守的已确认要求共 {constraint_cost} 字，'
                        f'超过单次可携带上限 {ceiling} 字。不能为了长度丢掉'
                        '必遵要求就照常编码：请把任务切小（按文件范围拆分），'
                        '或先合并精简这些要求。'),
            'entries': oversized, 'total_chars': constraint_cost, 'ceiling': ceiling,
        }
        return {'block': blocked['message'], 'entries': [], 'refs': [],
                'dropped': [], 'blocked': blocked, 'scope_known': scope_known,
                'error': None}

    kept, dropped = list(constraints), []
    used = constraint_cost
    for entry in context:
        cost = _cost(entry)
        if used + cost > budget_chars:
            dropped.append({'key': entry['key'], 'title': entry['title'],
                            'role': role_of(entry), 'reason': 'budget',
                            'chars': cost})
            continue
        used += cost
        kept.append(entry)
    dropped += [{'key': e['key'], 'title': e['title'], 'role': role_of(e),
                 'reason': 'out_of_scope'} for e in out_of_scope]
    return {
        'block': render_block(kept, dropped=dropped, scope_paths=scope,
                              scope_known=scope_known),
        'entries': kept,
        # The immutable reference: a (key, revision) pair names one frozen
        # version in ``knowledge_entry_versions``, which is append-only.
        'refs': [{'key': e['key'], 'revision': e['revision'], 'title': e['title'],
                  'role': role_of(e), 'status': e['status'], 'kind': e['kind'],
                  'summary_only': bool(e.get('summary_only'))}
                 for e in kept],
        'dropped': dropped,
        'blocked': None,
        'scope_known': scope_known,
        'chars': used,
        'error': None,
    }


def refine_for_plan(store, project_id, *, scope_paths, previous_refs,
                    plugin_id=None, budget_chars=DEFAULT_BUDGET_CHARS) -> dict:
    """Phase two: the plan named the files, so re-load memory against them.

    Called once a plan exists. Anything that travelled as a summary and is
    actually in scope now arrives in full; anything scoped elsewhere is dropped
    and said to be dropped. The result is meant to be appended to the run's
    frozen basis, not to replace it silently -- the caller records both.
    """
    loaded = load_for_task(store, project_id, plugin_id=plugin_id,
                           scope_paths=scope_paths, budget_chars=budget_chars)
    if loaded['error'] or loaded['blocked']:
        return {**loaded, 'added': [], 'previous_refs': list(previous_refs or [])}
    before = {(r['key'], r.get('summary_only', False))
              for r in (previous_refs or [])}
    seen_keys = {r['key'] for r in (previous_refs or [])}
    added = [r for r in loaded['refs']
             if r['key'] not in seen_keys or (r['key'], True) in before]
    return {**loaded, 'added': added, 'previous_refs': list(previous_refs or [])}


def render_block(entries, *, dropped=(), scope_paths=(), scope_known=True) -> str:
    """The memory section a prompt carries, grouped by what each entry is.

    Confirmed requirements are the only group presented as binding. Leads carry
    their content too -- the first version showed only titles, which made an
    open question permanently unreadable -- but under a heading that says they
    are not a basis for a decision.
    """
    if not entries and not dropped:
        return ''
    grouped: dict[str, list] = {}
    for entry in entries:
        grouped.setdefault(role_of(entry), []).append(entry)
    lines: list[str] = []
    if scope_paths:
        lines.append(f"本次改动范围：{'、'.join(scope_paths)}")
        lines.append('')
    elif not scope_known:
        summarised = [e for e in entries if e.get('summary_only')]
        if summarised:
            lines.append(f'本次改动范围尚未确定，其中 {len(summarised)} 条'
                         '「有适用范围的背景资料」先给了摘要；计划确定要动哪些'
                         '文件之后会自动补齐正文。必须遵守的要求都是完整的。')
            lines.append('')
    for role in (CONSTRAINT, DECISION, FACT, OBSERVATION, LEAD):
        rows = grouped.get(role)
        if not rows:
            continue
        if role == CONSTRAINT:
            lines.append(f'{ROLE_LABELS[role]}（本次必须遵守）：')
        elif role in (LEAD, OBSERVATION, DECISION):
            lines.append(f'{ROLE_LABELS[role]}（只说明存在这个问题，'
                         '不作为依据，也不是已批准的目标）：')
        else:
            lines.append(f'{ROLE_LABELS[role]}（供参考，不是本次的命令）：')
        for entry in rows:
            where = f"（适用：{'、'.join(entry['paths'])}）" if entry.get('paths') else ''
            version = (f"［记录于 {entry['commit_sha'][:12]}］"
                       if entry.get('commit_sha') else '')
            lines.append(f"- {entry['title']}{where}{version}：{entry['content']}")
        lines.append('')
    if dropped:
        budget = [d for d in dropped if d['reason'] == 'budget']
        scoped = [d for d in dropped if d['reason'] == 'out_of_scope']
        if scoped:
            lines.append(f'另有 {len(scoped)} 条记忆的适用范围与本次改动无关，未加载。')
        if budget:
            lines.append(f'另有 {len(budget)} 条记忆因长度上限未加载，'
                         '需要时按条目 key 单独调取，不要假设它们不存在。')
    return '\n'.join(lines).strip()


def constraints_block(entries) -> str:
    """Backwards-compatible rendering for callers that already hold entries.

    Kept so existing call sites keep working, but it now routes through the
    role-aware renderer: the old behaviour folded every ``active`` entry into
    "本次必须遵守", which turned a confirmed background fact into an order.
    """
    return render_block(list(entries))
