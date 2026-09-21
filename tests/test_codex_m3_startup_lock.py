"""M3 review: startup recovery must run only after the writer lock is held."""

from factory.control.app import create_app
from factory.control.service import Service
from factory.control.store import Store


class _NeverRuns:
    def run(self, *args, **kwargs):
        raise AssertionError("provider must not run in this probe")


def _profiles():
    return {
        role: {"provider": "codex", "model": "test"}
        for role in ("planner", "cheap", "standard", "strong")
    }


def test_losing_service_start_cannot_interrupt_the_active_writer(tmp_path):
    store = Store(tmp_path / "control.db")
    active = Service(store, runner=_NeverRuns(), profiles=_profiles())
    contender = None
    try:
        # This process is the only service allowed to recover or dispatch.
        active.recover()
        with store.connect() as db:
            db.execute(
                "INSERT INTO maintenance_jobs VALUES"
                "('live-job','conversation',1,'running',NULL,NULL,'before','before')"
            )

        contender = Service(store, runner=_NeverRuns(), profiles=_profiles())
        create_app(
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "repos",
            public_origin="http://testserver",
            service=contender,
            webhook_secret="test-webhook-secret",
        )

        assert active.maintenance_status("live-job")["status"] == "running", (
            "an app that has not acquired the worker lock cannot perform restart recovery"
        )
    finally:
        if contender is not None:
            contender.close()
        active.close()
