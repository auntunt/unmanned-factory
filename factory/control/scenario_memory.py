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


def constraints_block(entries) -> str:
    """The memory section an executor prompt carries, or ''.

    Confirmed entries travel with their content, because acting on them is the
    point. Unconfirmed ones travel as titles under an explicit heading that says
    they are not a basis for a decision: dropping them entirely would hide that
    the question exists, and quoting them alongside the confirmed ones would let
    an unreviewed guess be read as a requirement.
    """
    confirmed = [e for e in entries if e['status'] == CONFIRMED_STATUS]
    unconfirmed = [e for e in entries if e['status'] == UNCONFIRMED_STATUS]
    lines: list[str] = []
    if confirmed:
        lines.append('项目已确认的长期约束（本次必须遵守）：')
        for entry in confirmed:
            where = f"（适用：{'、'.join(entry['paths'])}）" if entry['paths'] else ''
            version = f"［记录于 {entry['commit_sha'][:12]}］" if entry.get('commit_sha') else ''
            lines.append(f"- {entry['title']}{where}{version}：{entry['content']}")
    if unconfirmed:
        lines.append('')
        lines.append('以下条目尚未经人工确认，只说明存在这个问题，不作为依据，也不要当成已批准的目标：')
        for entry in unconfirmed:
            lines.append(f"- {entry['title']}")
    return '\n'.join(lines)
