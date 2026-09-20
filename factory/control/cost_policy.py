"""Platform-wide default cost policy for newly created projects.

Two distinct things are kept apart on purpose:

``budget_usd``
    The project's own ceiling. ``None`` keeps the long-standing monitoring
    behaviour: usage is recorded, no dollar ceiling reaches any provider.

``budget_source``
    Where that ceiling comes from. ``'explicit'`` (the default, and what every
    legacy row without the field means) uses ``budget_usd`` verbatim.
    ``'inherit'`` defers to this policy, so an admin can set one default for new
    projects without rewriting anything already stored.

Because ``None`` already means "unlimited" for an explicit project, it can never
also mean "inherit"; the source field carries that, not the amount.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from pathlib import Path

from factory.control.store import Conflict

INHERIT = 'inherit'
EXPLICIT = 'explicit'
SOURCES = (EXPLICIT, INHERIT)


def validate_default_budget(value):
    """Accept ``None`` (monitor only) or a finite positive dollar amount."""
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1000000:
        raise ValueError('默认停止线必须是 0 到 1000000 之间的正数，或留空表示仅监测')
    return float(value)


class CostPolicy:
    """Revisioned JSON policy beside the store, same shape as the ops channel."""

    def __init__(self, store):
        self.store = store
        self.path = Path(store.path).with_name('cost-policy.json')
        self.lock = threading.Lock()

    def config(self):
        if not self.path.exists():
            # Unconfigured must never add a hidden hard stop.
            return {'revision': 0, 'default_project_budget_usd': None}
        data = json.loads(self.path.read_text())
        return {'revision': int(data.get('revision', 0)),
                'default_project_budget_usd': data.get('default_project_budget_usd')}

    def configure(self, revision, default_project_budget_usd):
        amount = validate_default_budget(default_project_budget_usd)
        with self.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            data = self.config()
            if data['revision'] != revision:
                raise Conflict('费用策略已更新，请刷新')
            data = {'revision': revision + 1, 'default_project_budget_usd': amount}
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.cost-policy-')
            try:
                with os.fdopen(fd, 'w') as output:
                    json.dump(data, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return self.config()

    def default_budget_usd(self):
        return self.config()['default_project_budget_usd']

    def effective_budget_usd(self, project):
        """Resolve the ceiling actually enforced for one project."""
        if not isinstance(project, dict):
            raise ValueError('项目必须是字典')
        if project.get('budget_source') == INHERIT:
            return self.default_budget_usd()
        return project.get('budget_usd')

    def resolved(self, project):
        """Return the project with ``budget_usd`` replaced by the effective ceiling.

        Every billing read goes through ``project['budget_usd']``, so resolving
        once here keeps inheritance out of the accounting code. The stored row is
        untouched; ``budget_source`` stays visible for the UI and the audit.
        """
        effective = self.effective_budget_usd(project)
        if effective == project.get('budget_usd'):
            return project
        return {**project, 'budget_usd': effective}
