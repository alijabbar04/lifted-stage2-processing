import datetime
import json
import os
from pathlib import Path
import sys
import threading

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import stage2_run_state as run_state


@pytest.fixture
def identity(monkeypatch):
    value = {"pid": os.getpid(), "kind": "test-clock", "created": "12345"}
    monkeypatch.setattr(run_state, "process_identity", lambda pid: dict(value) if pid == os.getpid() else None)
    return value


def make_publisher(tmp_path, identity, **kwargs):
    care = tmp_path / "care"
    care.mkdir(exist_ok=True)
    registry = tmp_path / "registry"
    return run_state.RunPublisher(
        care,
        mode="batch-finishing",
        registry_root=registry,
        heartbeat_seconds=kwargs.pop("heartbeat_seconds", 3600),
        **kwargs,
    )


def test_publisher_starts_atomic_snapshot_and_observer_is_read_only(tmp_path, identity):
    missing = tmp_path / "missing-registry"
    assert run_state.RunObserver(missing).list_runs() == []
    assert not missing.exists()

    publisher = make_publisher(tmp_path, identity)
    try:
        run = run_state.RunObserver(publisher.registry_root).get(publisher.run_id)
        assert run["schema"] == run_state.SNAPSHOT_SCHEMA
        assert run["owner"] == identity
        assert run["display_state"] == "running"
        assert run["care_home_dir"] == str((tmp_path / "care").resolve())
        assert not list(publisher.registry_root.glob("*.tmp"))
    finally:
        publisher.finish()


def test_update_whitelist_and_native_activity_mapping(tmp_path, identity):
    publisher = make_publisher(tmp_path, identity)
    try:
        with pytest.raises(ValueError, match="unsupported"):
            publisher.update(api_key="secret")
        publisher.update(phase="classification", completed=1, total=3, stats={"files": 4}, cost_gbp=1.25)
        publisher.event({
            "kind": "phase_progress",
            "phase": "audit",
            "completed": 2,
            "total": 3,
            "report": "audit.csv",
            "ignored_secret": "not persisted",
        })
        observer = run_state.RunObserver(publisher.registry_root)
        run = observer.get(publisher.run_id)
        assert (run["phase"], run["completed"], run["audit_report"]) == ("audit", 2, "audit.csv")
        encoded = publisher.events_path.read_text(encoding="utf-8")
        assert "ignored_secret" not in encoded
        assert [item["kind"] for item in observer.read_events(publisher.run_id)] == [
            "operation-started", "phase_progress"
        ]
    finally:
        publisher.finish()


@pytest.mark.parametrize("fields", [
    {"completed": -1},
    {"total": 1.5},
    {"cost_gbp": float("nan")},
    {"cost_gbp": float("inf")},
    {"stats": {"nested": {"unsafe": True}}},
    {"stats": {"cost": float("-inf")}},
    {"status": "verified-complete"},
])
def test_invalid_or_authoritative_updates_are_rejected(tmp_path, identity, fields):
    publisher = make_publisher(tmp_path, identity)
    try:
        with pytest.raises(ValueError):
            publisher.update(**fields)
    finally:
        publisher.finish()


def test_finish_is_idempotent_and_never_claims_verified_completion(tmp_path, identity):
    publisher = make_publisher(tmp_path, identity)
    first = publisher.finish(outcome="native-operation-completed")
    second = publisher.finish(outcome="different")
    assert first == second
    assert first["status"] == "operation-ended"
    assert first["operation_outcome"] == "native-operation-completed"
    assert run_state.RunObserver(publisher.registry_root).get(publisher.run_id)["display_state"] == "operation-ended"
    with pytest.raises(RuntimeError, match="finished"):
        publisher.update(phase="late")


def test_error_finish_is_failed(tmp_path, identity):
    publisher = make_publisher(tmp_path, identity)
    publisher.finish(error="synthetic failure")
    run = run_state.RunObserver(publisher.registry_root).get(publisher.run_id)
    assert run["display_state"] == "failed"
    assert run["error"] == "synthetic failure"


def test_pid_reuse_is_owner_ended_not_running(tmp_path, identity, monkeypatch):
    publisher = make_publisher(tmp_path, identity)
    try:
        monkeypatch.setattr(
            run_state,
            "process_identity",
            lambda pid: {"pid": pid, "kind": "test-clock", "created": "reused"},
        )
        run = run_state.RunObserver(publisher.registry_root).get(publisher.run_id)
        assert run["display_state"] == "owner-ended"
        assert run_state.RunObserver(publisher.registry_root).active_for(tmp_path / "care") == []
    finally:
        publisher.finish()


def test_unknown_process_identity_is_uncertain_not_falsely_owner_ended(tmp_path, identity, monkeypatch):
    publisher = make_publisher(tmp_path, identity)
    try:
        monkeypatch.setattr(run_state, "process_identity", lambda pid: None)
        monkeypatch.setattr(run_state, "_pid_definitely_absent", lambda pid: False)
        assert run_state.RunObserver(publisher.registry_root).get(publisher.run_id)["display_state"] == "stale-unverified"
    finally:
        publisher.finish()


def test_affirmatively_absent_process_is_owner_ended(tmp_path, identity, monkeypatch):
    publisher = make_publisher(tmp_path, identity)
    try:
        monkeypatch.setattr(run_state, "process_identity", lambda pid: None)
        monkeypatch.setattr(run_state, "_pid_definitely_absent", lambda pid: True)
        assert run_state.RunObserver(publisher.registry_root).get(publisher.run_id)["display_state"] == "owner-ended"
    finally:
        publisher.finish()


def test_access_denied_probe_remains_active_but_unverified(tmp_path, identity, monkeypatch):
    publisher = make_publisher(tmp_path, identity)
    try:
        monkeypatch.setattr(run_state, "process_identity", lambda pid: None)
        monkeypatch.setattr(run_state, "_pid_definitely_absent", lambda pid: False)
        observer = run_state.RunObserver(publisher.registry_root)
        run = observer.get(publisher.run_id)
        assert run["display_state"] == "stale-unverified"
        assert observer.active_for(tmp_path / "care")[0]["run_id"] == publisher.run_id
    finally:
        publisher.finish()


def test_old_heartbeat_after_sleep_is_stale_unverified_and_remains_active(tmp_path, identity, monkeypatch):
    publisher = make_publisher(tmp_path, identity)
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        old = now - datetime.timedelta(hours=4)
        with publisher._lock:
            publisher._snapshot["updated_utc"] = old.isoformat().replace("+00:00", "Z")
            run_state._write_atomic(publisher.snapshot_path, publisher._snapshot)
        monkeypatch.setattr(run_state, "_utc_now", lambda: now)
        observer = run_state.RunObserver(publisher.registry_root)
        assert observer.get(publisher.run_id)["display_state"] == "stale-unverified"
        assert [item["run_id"] for item in observer.active_for(tmp_path / "care")] == [publisher.run_id]
    finally:
        publisher.finish()


def test_corrupt_partial_oversize_and_wrong_schema_snapshots_are_ignored(tmp_path, identity):
    registry = tmp_path / "registry"
    registry.mkdir()
    partial_id = str(run_state.uuid.uuid4())
    (registry / f"{partial_id}.json").write_text('{"schema":', encoding="utf-8")
    huge_id = str(run_state.uuid.uuid4())
    (registry / f"{huge_id}.json").write_bytes(b"x" * (run_state.MAX_SNAPSHOT_BYTES + 1))
    wrong_id = str(run_state.uuid.uuid4())
    (registry / f"{wrong_id}.json").write_text(json.dumps({"schema": "future/v2"}), encoding="utf-8")
    observer = run_state.RunObserver(registry)
    assert observer.list_runs() == []
    assert observer.get("../../escape") is None


def test_observer_rejects_unrecognised_or_nonfinite_snapshot_data(tmp_path, identity):
    publisher = make_publisher(tmp_path, identity)
    publisher.finish()
    raw = json.loads(publisher.snapshot_path.read_text(encoding="utf-8"))
    raw["unexpected"] = {"content": "must not reach UI"}
    publisher.snapshot_path.write_text(json.dumps(raw), encoding="utf-8")
    assert run_state.RunObserver(publisher.registry_root).get(publisher.run_id) is None

    del raw["unexpected"]
    raw["cost_gbp"] = float("inf")
    publisher.snapshot_path.write_text(json.dumps(raw), encoding="utf-8")
    assert run_state.RunObserver(publisher.registry_root).get(publisher.run_id) is None


def test_observer_rejects_reparse_snapshot(tmp_path, identity):
    registry = tmp_path / "registry"
    registry.mkdir()
    target = tmp_path / "outside.json"
    run_id = str(run_state.uuid.uuid4())
    target.write_text("{}", encoding="utf-8")
    link = registry / f"{run_id}.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are not available to this test account")
    assert run_state.RunObserver(registry).get(run_id) is None


def test_care_home_filter_exclude_pid_and_multiple_observers(tmp_path, identity):
    publisher = make_publisher(tmp_path, identity)
    other_care = tmp_path / "other"
    other_care.mkdir()
    second = run_state.RunPublisher(other_care, mode="live", registry_root=publisher.registry_root, heartbeat_seconds=3600)
    try:
        first_observer = run_state.RunObserver(publisher.registry_root)
        second_observer = run_state.RunObserver(publisher.registry_root)
        assert [r["run_id"] for r in first_observer.list_runs(tmp_path / "care")] == [publisher.run_id]
        assert second_observer.active_for(tmp_path / "care", exclude_pid=os.getpid()) == []
        assert len(second_observer.list_runs()) == 2
    finally:
        publisher.finish()
        second.finish()


def test_observer_does_not_write_or_prune_registry(tmp_path, identity):
    publisher = make_publisher(tmp_path, identity)
    try:
        before = {path.name: path.read_bytes() for path in publisher.registry_root.iterdir()}
        observer = run_state.RunObserver(publisher.registry_root)
        observer.list_runs()
        observer.get(publisher.run_id)
        observer.active_for(tmp_path / "care")
        observer.read_events(publisher.run_id)
        after = {path.name: path.read_bytes() for path in publisher.registry_root.iterdir()}
        assert after == before
    finally:
        publisher.finish()


def test_event_reader_skips_malformed_or_foreign_lines(tmp_path, identity):
    publisher = make_publisher(tmp_path, identity)
    try:
        foreign = str(run_state.uuid.uuid4())
        with publisher.events_path.open("ab") as stream:
            stream.write(b"not-json\n")
            stream.write(json.dumps({
                "schema": run_state.EVENT_SCHEMA,
                "run_id": foreign,
                "timestamp_utc": "2026-01-01T00:00:00Z",
                "kind": "foreign",
            }).encode("utf-8") + b"\n")
            stream.write(json.dumps({
                "schema": run_state.EVENT_SCHEMA,
                "run_id": publisher.run_id,
                "timestamp_utc": "2026-01-01T00:00:00Z",
                "kind": "injected",
                "unexpected": {"content": "must not reach UI"},
            }).encode("utf-8") + b"\n")
        events = run_state.RunObserver(publisher.registry_root).read_events(publisher.run_id)
        assert [event["kind"] for event in events] == ["operation-started"]
    finally:
        publisher.finish()


def test_event_journal_rotates_bounded_and_reader_spans_backup(tmp_path, identity, monkeypatch):
    monkeypatch.setattr(run_state, "MAX_EVENT_BYTES", 900)
    publisher = make_publisher(tmp_path, identity)
    try:
        for number in range(15):
            publisher.event({"kind": f"step-{number}", "label": "x" * 80})
        backup = publisher.events_path.with_name(publisher.events_path.name + ".1")
        assert backup.is_file()
        assert publisher.events_path.stat().st_size <= run_state.MAX_EVENT_BYTES
        assert backup.stat().st_size <= run_state.MAX_EVENT_BYTES
        kinds = [item["kind"] for item in run_state.RunObserver(publisher.registry_root).read_events(publisher.run_id, 5)]
        assert kinds == [f"step-{number}" for number in range(10, 15)]
    finally:
        publisher.finish()


def test_heartbeat_advances_snapshot_and_stops_at_finish(tmp_path, identity):
    publisher = make_publisher(tmp_path, identity, heartbeat_seconds=0.05)
    first = publisher.snapshot_path.stat().st_mtime_ns
    deadline = run_state.time.monotonic() + 2
    while publisher.snapshot_path.stat().st_mtime_ns == first and run_state.time.monotonic() < deadline:
        run_state.time.sleep(0.02)
    assert publisher.snapshot_path.stat().st_mtime_ns != first
    publisher.finish()
    thread = publisher._thread
    assert not thread.is_alive()
    finished_bytes = publisher.snapshot_path.read_bytes()
    run_state.time.sleep(0.08)
    assert publisher.snapshot_path.read_bytes() == finished_bytes


def test_concurrent_readers_never_observe_partial_snapshot(tmp_path, identity):
    publisher = make_publisher(tmp_path, identity)
    observed = []
    try:
        def read_many():
            observer = run_state.RunObserver(publisher.registry_root)
            for _ in range(50):
                observed.append(observer.get(publisher.run_id))

        reader = threading.Thread(target=read_many)
        reader.start()
        for value in range(1, 20):
            publisher.update(completed=value, total=20)
        reader.join(timeout=5)
        assert observed and all(item is not None for item in observed)
    finally:
        publisher.finish()


def test_registry_directory_has_expected_application_scope(monkeypatch, tmp_path):
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert run_state.registry_directory() == tmp_path / "Lifted" / "Stage2" / "runs"
    else:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert run_state.registry_directory() == tmp_path / "lifted" / "stage2" / "runs"


def test_real_process_identity_has_creation_token():
    identity = run_state.process_identity(os.getpid())
    assert identity is not None
    assert identity["pid"] == os.getpid()
    assert identity["created"]
