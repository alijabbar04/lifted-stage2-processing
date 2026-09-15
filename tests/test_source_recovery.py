"""Synthetic-only source recovery, secret handling and billing regressions."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock

import fitz
import pytest

from _load_app import load_app
import stage2_source_recovery as recovery

app = load_app()


def pdf(path, password=None, text="Synthetic document evidence", pages=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        if text:
            page.insert_text((50, 60), f"{text} {i}")
    options = dict(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="synthetic-owner",
                   user_pw=password) if password else {}
    doc.save(path, **options)
    doc.close()
    return path


def render(path):
    with fitz.open(path) as doc:
        text = " ".join(page.get_text() for page in doc)
        return [], text, list(range(len(doc))), len(doc), False


def setup(root, passwords=(None, "pass-a", "pass-b")):
    paths = [pdf(root / f"Worker {i % 40:02}" / f"source-{i}.pdf", password,
                 text=f"Synthetic document {i}") for i, password in enumerate(passwords)]
    state = app.BatchState(root)
    state.init("Synthetic", "claude-haiku-4-5", 1, {"post_run_audit": False})
    state.data["version"] = 5
    state.data["primary_inventory"] = {str(i): {"path": str(p), "fhash": recovery.digest(p),
        "worker": p.parent.name, "worker_dir": str(p.parent), "pages": 1} for i, p in enumerate(paths)}
    state.data["primary_submission_complete"] = False
    state.data["submitted_worker_scope"] = [{"name": p.name, "source_path": str(p)}
                                           for p in sorted({p.parent for p in paths})]
    state.save()
    controller = recovery.Recovery(state, "test", "synthetic")
    return controller, paths


def api():
    obj = Mock()
    obj.classify_payload.side_effect = lambda vocab, images, text, **kw: ("synthetic", [{"type": "text", "text": text}], 200)
    obj.build_batch_request.side_effect = lambda cid, system, blocks, mt: {"custom_id": cid, "params": {"system": system, "messages": blocks}}
    obj.submit_batch.side_effect = lambda chunk: {"id": f"batch-{chunk[0]['custom_id']}", "processing_status": "in_progress"}
    return obj


def reload(controller):
    return recovery.Recovery(app.BatchState(controller.state.dir), "test", "synthetic")


def test_precise_causes_and_all_pages(tmp_path):
    locked = pdf(tmp_path / "locked.pdf", "pass", pages=40)
    result = recovery.inspect_source(locked)
    assert result["cause"] == "password_protected_pdf"
    assert result["structural_validation"] == "requires_decryption"
    assert result["pages"] is None
    blank = pdf(tmp_path / "blank.pdf", text="")
    assert recovery.inspect_source(blank)["cause"] == "no_renderable_or_extractable_content"
    corrupt = tmp_path / "bad.pdf"
    corrupt.write_bytes(b"%PDF-1.7\ntruncated")
    assert recovery.inspect_source(corrupt)["cause"] == "corrupt_or_truncated_pdf"
    assert recovery.inspect_source(tmp_path / "missing.pdf")["cause"] == "source_missing"
    unsupported = tmp_path / "source.xyz"
    unsupported.write_bytes(b"unknown format")
    assert recovery.inspect_source(unsupported)["cause"] == "unsupported_format"
    assert recovery.inspect_source(blank, "0" * 64)["cause"] == "source_changed"


def test_whole_98_locked_scope_never_submits(tmp_path):
    controller, paths = setup(tmp_path, ["locked"] * 98)
    client = api()
    summary = controller.preflight(render)
    assert summary["locked"] == 98
    assert summary["workers"] == 40
    assert controller.data["preflight_complete"]
    assert len(controller.state.data["primary_render_exclusions"]) == 98
    client.submit_batch.assert_not_called()
    assert not controller.state.data["requests"]


@pytest.mark.parametrize("password,unlocked", [("pass-a", 1), ("wrong", 0), ("", 0)])
def test_password_group_and_no_secret_persistence(tmp_path, password, unlocked):
    controller, paths = setup(tmp_path)
    original = {p: p.read_bytes() for p in paths}
    controller.preflight(render)
    result = controller.unlock(["1", "2"], password, controller.token(["1", "2"]), render)
    assert result["unlocked"] == unlocked
    assert result["still_locked"] == 2 - unlocked
    assert paths[2].read_bytes() == original[paths[2]]
    if unlocked:
        r = controller.data["records"]["1"]
        assert Path(r["archive_path"]).read_bytes() == original[paths[1]]
        with fitz.open(paths[1]) as doc:
            assert not doc.needs_pass and not doc.is_encrypted
        assert r["state"] == "unlocked_validated"
    else:
        assert paths[1].read_bytes() == original[paths[1]]
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.suffix in {".json", ".bak"}:
            text = p.read_text(encoding="utf-8")
            assert "pass-a" not in text and "pass-b" not in text and "synthetic-owner" not in text
    reload(controller).verify_ledger()


def test_restart_does_not_rerender_validated_or_redecrypt(tmp_path):
    controller, _ = setup(tmp_path)
    controller.preflight(render)
    controller.unlock(["1"], "pass-a", controller.token(["1"]), render)
    restarted = reload(controller)
    renderer = Mock(side_effect=AssertionError("must not rerender ready evidence"))
    restarted.resume(renderer)
    restarted.preflight(renderer)
    renderer.assert_not_called()
    assert restarted.data["records"]["1"]["state"] == "unlocked_validated"
    assert restarted.data["records"]["2"]["state"] == "locked_waiting_for_password"


def test_default_wait_and_explicit_ready_subset(tmp_path):
    controller, _ = setup(tmp_path)
    controller.preflight(render)
    client = api()
    with pytest.raises(recovery.RecoveryError, match="Waiting"):
        recovery.submit_ready(controller, ["0"], controller.token(["0"]), client, "vocab")
    client.submit_batch.assert_not_called()
    recovery.submit_ready(controller, ["0"], controller.token(["0"]), client, "vocab", allow_partial=True)
    assert client.submit_batch.call_count == 1
    assert controller.summary()["unresolved"] == 2
    assert controller.state.data["primary_submission_complete"] is True


def test_multiple_delayed_password_scopes_never_repurchase(tmp_path):
    controller, _ = setup(tmp_path)
    controller.preflight(render)
    client = api()
    for ids, password in [(["0"], None), (["1"], "pass-a"), (["2"], "pass-b")]:
        controller = reload(controller)
        if password:
            controller.unlock(ids, password, controller.token(ids), render)
        recovery.submit_ready(controller, ids, controller.token(ids), client, "vocab", allow_partial=True)
    assert [call.args[0][0]["custom_id"] for call in client.submit_batch.call_args_list] == ["0", "1", "2"]
    assert len(controller.data["scopes"]) == 3
    for cid in ("0", "1", "2"):
        with pytest.raises(recovery.RecoveryError, match="Accepted"):
            recovery.submit_ready(controller, [cid], controller.token([cid]), client, "vocab")
    assert client.submit_batch.call_count == 3


def test_ambiguous_submission_cannot_repeat(tmp_path):
    controller, _ = setup(tmp_path, [None])
    controller.preflight(render)
    client = api()
    client.submit_batch.side_effect = TimeoutError("synthetic-secret-must-not-leak")
    with pytest.raises(recovery.RecoveryError, match="uncertain"):
        recovery.submit_ready(controller, ["0"], controller.token(["0"]), client, "vocab")
    restarted = reload(controller)
    with pytest.raises(recovery.RecoveryError):
        recovery.submit_ready(restarted, ["0"], restarted.token(["0"]), client, "vocab")
    assert client.submit_batch.call_count == 1
    assert "synthetic-secret-must-not-leak" not in restarted.state.path.read_text()


def test_source_and_decision_drift_blocks_paid_boundary(tmp_path):
    controller, paths = setup(tmp_path, [None])
    controller.preflight(render)
    token = controller.token(["0"])
    paths[0].write_bytes(b"changed")
    client = api()
    with pytest.raises(recovery.RecoveryError, match="changed"):
        recovery.submit_ready(controller, ["0"], token, client, "vocab")
    client.submit_batch.assert_not_called()


def test_replace_preserves_archive_and_rejects_stale_decision(tmp_path):
    controller, paths = setup(tmp_path, ["pass"])
    controller.preflight(render)
    before = paths[0].read_bytes()
    replacement = pdf(tmp_path / "replacement.pdf", text="Synthetic replacement")
    stale = controller.token(["0"])
    controller.data["review_marker"] = True
    controller.checkpoint()
    with pytest.raises(recovery.RecoveryError, match="stale"):
        controller.replace("0", replacement, stale, render)
    controller.replace("0", replacement, controller.token(["0"]), render)
    r = controller.data["records"]["0"]
    assert Path(r["archive_path"]).read_bytes() == before
    assert recovery.digest(paths[0]) == recovery.digest(replacement)
    assert r["original_hash"] != r["hash"]
    assert len(controller.data["ledger"]) >= 4


def test_quarantine_exact_and_reinstatement(tmp_path):
    controller, paths = setup(tmp_path, ["pass"])
    controller.preflight(render)
    before = paths[0].read_bytes()
    controller.quarantine(["0"], controller.token(["0"]))
    assert not paths[0].exists()
    r = controller.data["records"]["0"]
    assert Path(r["quarantine_path"]).read_bytes() == before
    assert recovery.exclusion_summary(controller.state)["count"] == 1
    controller = reload(controller)
    controller.reinstate(["0"], controller.token(["0"]))
    assert paths[0].read_bytes() == before
    assert controller.data["scope_outcome"] == "full_scope"


def test_completed_49_workers_immutable_during_recovery(tmp_path):
    controller, _ = setup(tmp_path, ["pass"])
    completed = {f"old-{i}": {"name": f"Old worker {i}", "completed": True,
        "costs": {"synthetic": i}, "finishing_operations": {"x": {"status": "complete"}}}
        for i in range(49)}
    controller.state.data["workers"] = copy.deepcopy(completed)
    controller.state.save()
    controller = reload(controller)
    controller.preflight(render)
    controller.unlock(["0"], "pass", controller.token(["0"]), render)
    assert controller.state.data["workers"] == completed


def test_void_save_verified_and_newer_schema_refused(tmp_path):
    controller, _ = setup(tmp_path, [None])
    save = controller.state.save
    controller.state.save = lambda: (save(), None)[1]
    controller.preflight(render)
    assert reload(controller).data["preflight_complete"]
    controller.state.data["version"] = 6
    save()
    with pytest.raises(app.DurableStateError, match="newer"):
        app.BatchState(tmp_path)


def test_terminal_receipt_records_exclusions_and_blocks_pending(tmp_path):
    controller, _ = setup(tmp_path, ["pass"])
    controller.preflight(render)
    with pytest.raises(app.DurableStateError, match="incomplete"):
        controller.state.finalize_applied()
    controller.quarantine(["0"], controller.token(["0"]))
    controller.state.data["processing_complete"] = True
    for source in controller.state.data["submitted_worker_scope"]:
        key = str(Path(source["source_path"]).resolve()).casefold()
        controller.state.data.setdefault("workers", {})[key] = {
            "name": source["name"], "source_dir": source["source_path"], "completed": True}
    terminal = controller.state.finalize_applied()
    receipt = json.loads(terminal.read_text())
    assert receipt["terminal_outcome"] == "completed_with_exclusions"
    assert receipt["source_exclusions"]["count"] == 1
    assert "not processed" in receipt["source_exclusions"]["statement"]
    assert not controller.state.path.exists()


def test_preflight_interruption_retains_scope_and_never_posts(tmp_path):
    controller, _ = setup(tmp_path)
    count = 0
    def stop():
        nonlocal count
        count += 1
        if count == 2:
            raise InterruptedError("synthetic stop")
    with pytest.raises(InterruptedError):
        controller.preflight(render, stop)
    controller = reload(controller)
    assert not controller.data["preflight_complete"]
    controller.preflight(render)
    assert len(controller.data["records"]) == 3


def test_ledger_tamper_rejected(tmp_path):
    controller, _ = setup(tmp_path)
    controller.preflight(render)
    controller.data["ledger"][0]["resolution"] = "tampered"
    controller.state.save()
    with pytest.raises(recovery.RecoveryError, match="integrity"):
        reload(controller)


def test_multiple_replacements_and_quarantine_preserve_every_version(tmp_path):
    controller, paths = setup(tmp_path, ["pass"])
    controller.preflight(render)
    original = paths[0].read_bytes()
    first = pdf(tmp_path / "first.pdf", text="First replacement")
    second = pdf(tmp_path / "second.pdf", text="Second replacement")
    controller.replace("0", first, controller.token(["0"]), render)
    controller.replace("0", second, controller.token(["0"]), render)
    controller.quarantine(["0"], controller.token(["0"]))
    record = controller.data["records"]["0"]
    assert {Path(a["path"]).read_bytes() for a in record["archives"]} == {
        original, first.read_bytes(), second.read_bytes()}
    assert Path(record["quarantine_path"]).read_bytes() == second.read_bytes()
    reload(controller).resume(render)


def test_reinstate_crash_after_publication_resumes_exactly(tmp_path, monkeypatch):
    controller, paths = setup(tmp_path, ["pass"])
    controller.preflight(render)
    original = paths[0].read_bytes()
    controller.quarantine(["0"], controller.token(["0"]))
    checkpoint = controller.checkpoint
    def interrupted():
        if controller.data["records"]["0"]["state"] == "needs_attention":
            raise RuntimeError("synthetic crash after atomic publication")
        checkpoint()
    monkeypatch.setattr(controller, "checkpoint", interrupted)
    with pytest.raises(RuntimeError):
        controller.reinstate(["0"], controller.token(["0"]))
    assert paths[0].read_bytes() == original
    restarted = reload(controller)
    assert restarted.data["records"]["0"].get("reinstate_intent")
    restarted.resume(render)
    assert restarted.data["scope_outcome"] == "full_scope"
    assert not restarted.data["records"]["0"].get("reinstate_intent")
    assert paths[0].read_bytes() == original


def test_replacement_crash_after_activation_resumes_exactly(tmp_path, monkeypatch):
    controller, paths = setup(tmp_path, ["pass"])
    controller.preflight(render)
    original = paths[0].read_bytes()
    replacement = pdf(tmp_path / "replacement.pdf", pages=3)
    checkpoint = controller.checkpoint
    def interrupted():
        if controller.data["records"]["0"].get("working_path"):
            raise RuntimeError("synthetic crash after atomic publication")
        checkpoint()
    monkeypatch.setattr(controller, "checkpoint", interrupted)
    with pytest.raises(RuntimeError):
        controller.replace("0", replacement, controller.token(["0"]), render)
    assert paths[0].read_bytes() == replacement.read_bytes()
    restarted = reload(controller)
    assert restarted.data["records"]["0"].get("pending_resolution")
    restarted.resume(render)
    r = restarted.data["records"]["0"]
    assert r["state"] == "unlocked_validated" and r["pages"] == 3
    assert Path(r["archive_path"]).read_bytes() == original
    assert restarted.state.data["primary_inventory"]["0"]["pages"] == 3


def test_quarantine_archive_corruption_blocks_resume(tmp_path):
    controller, _ = setup(tmp_path, ["pass"])
    controller.preflight(render)
    controller.quarantine(["0"], controller.token(["0"]))
    Path(controller.data["records"]["0"]["quarantine_path"]).write_bytes(b"corrupt")
    with pytest.raises(recovery.RecoveryError, match="archive"):
        reload(controller).resume(render)


def test_atomic_archive_never_publishes_partial_copy(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"synthetic source")
    target = tmp_path / "archive.bin"
    def interrupted(inp, out):
        out.write(b"partial")
        raise OSError("synthetic interruption")
    monkeypatch.setattr(recovery.shutil, "copyfileobj", interrupted)
    with pytest.raises(OSError):
        recovery.durable_copy(source, target, recovery.digest(source), exclusive=True)
    assert not target.exists()
    assert not list(tmp_path.glob(".recovery-copy-*"))


def test_cross_session_identical_source_reuses_paid_request(tmp_path):
    controller, paths = setup(tmp_path, [None, None])
    paths[1].write_bytes(paths[0].read_bytes())
    controller.state.data["primary_inventory"]["1"]["fhash"] = recovery.digest(paths[1])
    controller.state.save()
    controller = reload(controller)
    controller.preflight(render)
    client = api()
    recovery.submit_ready(controller, ["0"], controller.token(["0"]), client, "vocab")
    controller = reload(controller)
    submitted = recovery.submit_ready(controller, ["1"], controller.token(["1"]), client, "vocab")
    assert submitted == []
    assert client.submit_batch.call_count == 1
    assert recovery.request_aliases(controller.state) == {"1": "0"}
    assert list(controller.state.data["requests"]) == ["0"]


def test_legacy_accepted_hash_reuse_without_repurchase(tmp_path):
    controller, paths = setup(tmp_path, [None, None])
    paths[1].write_bytes(paths[0].read_bytes())
    inventory = controller.state.data["primary_inventory"]
    inventory["1"]["fhash"] = recovery.digest(paths[1])
    controller.state.data["requests"] = {"0": copy.deepcopy(inventory["0"])}
    controller.state.add_batch("legacy-batch", 1, "in_progress")
    controller.state.data["primary_submission_complete"] = True
    controller.state.save()
    controller = reload(controller)
    controller.preflight(render)
    client = api()
    recovery.submit_ready(controller, ["1"], controller.token(["1"]), client, "vocab")
    client.submit_batch.assert_not_called()
    assert recovery.request_aliases(controller.state) == {"1": "0"}


def test_alias_in_uncertain_scope_is_immutable_and_not_accepted(tmp_path):
    controller, paths = setup(tmp_path, [None, None])
    paths[1].write_bytes(paths[0].read_bytes())
    controller.state.data["primary_inventory"]["1"]["fhash"] = recovery.digest(paths[1])
    controller.state.save()
    controller = reload(controller)
    controller.preflight(render)
    client = api()
    client.submit_batch.side_effect = TimeoutError("synthetic")
    with pytest.raises(recovery.RecoveryError, match="uncertain"):
        recovery.submit_ready(controller, ["0", "1"], controller.token(["0", "1"]), client, "vocab")
    assert controller.summary()["submitted"] == 0
    assert controller.summary()["ambiguous"] == 2
    assert controller.summary()["unresolved"] == 2
    assert recovery.request_aliases(controller.state) == {}
    with pytest.raises(recovery.RecoveryError, match="Accepted"):
        controller.quarantine(["1"], controller.token(["1"]))


def test_reinspection_clears_preflight_marker_before_interrupt(tmp_path):
    controller, _ = setup(tmp_path, [None])
    controller.preflight(render)
    def stop():
        raise InterruptedError("synthetic")
    with pytest.raises(InterruptedError):
        controller.preflight(render, stop)
    assert reload(controller).data["preflight_complete"] is False


def test_staging_path_escape_is_rejected(tmp_path):
    controller, _ = setup(tmp_path, [None])
    controller.preflight(render)
    controller.data["run_id"] = "../../outside"
    with pytest.raises(recovery.RecoveryError, match="outside"):
        controller.prepare("0", render)


def test_missing_source_can_be_replaced_without_claiming_original_preserved(tmp_path):
    controller, paths = setup(tmp_path, [None])
    original_hash = recovery.digest(paths[0])
    paths[0].unlink()
    controller.preflight(render)
    assert controller.data["records"]["0"]["cause"] == "source_missing"
    replacement = pdf(tmp_path / "replacement.pdf", text="Replacement of missing source")
    controller.replace("0", replacement, controller.token(["0"]), render)
    r = controller.data["records"]["0"]
    assert r["state"] == "unlocked_validated"
    assert r["original_unavailable"] is True
    assert r["original_hash"] == original_hash
    assert r["archive_path"] == "" and r["archive_hash"] == ""
    assert paths[0].read_bytes() == replacement.read_bytes()


def test_changed_source_replacement_is_bound_to_observed_version(tmp_path):
    controller, paths = setup(tmp_path, [None])
    original_hash = recovery.digest(paths[0])
    changed = pdf(tmp_path / "changed.pdf", text="Changed active source")
    paths[0].write_bytes(changed.read_bytes())
    controller.preflight(render)
    assert controller.data["records"]["0"]["cause"] == "source_changed"
    replacement = pdf(tmp_path / "replacement.pdf", text="Chosen replacement")
    controller.replace("0", replacement, controller.token(["0"]), render)
    r = controller.data["records"]["0"]
    assert r["original_hash"] == original_hash
    assert Path(r["archive_path"]).read_bytes() == changed.read_bytes()
    assert paths[0].read_bytes() == replacement.read_bytes()


def test_missing_inventory_hash_never_silently_binds_later_source(tmp_path):
    controller, _ = setup(tmp_path, [None])
    controller.state.data["primary_inventory"]["0"]["fhash"] = ""
    controller.state.save()
    controller = reload(controller)
    controller.preflight(render)
    assert controller.data["records"]["0"]["cause"] == "source_changed"
    assert controller.summary()["ready"] == 0


def test_supported_text_and_ooxml_pass_local_inspection(tmp_path):
    import zipfile
    txt = tmp_path / "source.txt"
    txt.write_text("Synthetic text source", encoding="utf-8")
    assert recovery.inspect_source(txt)["cause"] == ""
    docx = tmp_path / "source.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="urn:test"><w:p><w:t>Synthetic OOXML text</w:t></w:p></w:document>')
    assert recovery.inspect_source(docx)["cause"] == ""
    binary_doc = tmp_path / "source.doc"
    binary_doc.write_bytes(b"\xd0\xcf\x11\xe0synthetic binary word")
    assert recovery.inspect_source(binary_doc)["cause"] == "unsupported_format"


def test_unlock_pause_preserves_completed_and_remaining_sources(tmp_path):
    controller, _ = setup(tmp_path, ["pass", "pass"])
    controller.preflight(render)
    count = 0
    def stop():
        nonlocal count
        count += 1
        if count == 2:
            raise InterruptedError("synthetic pause")
    with pytest.raises(InterruptedError):
        controller.unlock(["0", "1"], "pass", controller.token(["0", "1"]), render, stop=stop)
    restarted = reload(controller)
    assert restarted.data["records"]["0"]["state"] == "unlocked_validated"
    assert restarted.data["records"]["1"]["state"] == "locked_waiting_for_password"


@pytest.mark.parametrize("inventory", [None, {}])
def test_missing_legacy_inventory_cannot_become_empty_approved_preflight(tmp_path, inventory):
    controller, _ = setup(tmp_path, [None])
    if inventory is None:
        controller.state.data.pop("primary_inventory")
    else:
        controller.state.data["primary_inventory"] = inventory
    controller.state.save()
    controller = reload(controller)
    before = controller.state.path.read_bytes()
    with pytest.raises(recovery.RecoveryError, match="primary submission reconciliation"):
        controller.preflight(render)
    assert controller.state.path.read_bytes() == before
    assert controller.data is None


def test_unsupported_addition_blocks_quarantine_only_worker_completion(tmp_path):
    controller, paths = setup(tmp_path, ["pass"])
    controller.preflight(render)
    controller.quarantine(["0"], controller.token(["0"]))
    (paths[0].parent / "new-source.xyz").write_bytes(b"Synthetic untracked document")
    engine = app.Engine.__new__(app.Engine)
    with pytest.raises(app.FinishingInputChanged, match="unsupported current file"):
        engine._validate_batch_worker_records(paths[0].parent, [], controller.state)


def test_unsupported_addition_blocks_otherwise_applied_worker(tmp_path):
    controller, paths = setup(tmp_path, [None])
    controller.preflight(render)
    recovery.submit_ready(controller, ["0"], controller.token(["0"]), api(), "Passport")
    (paths[0].parent / "new-source.xyz").write_bytes(b"Synthetic untracked document")
    engine = app.Engine.__new__(app.Engine)
    with pytest.raises(app.FinishingInputChanged, match="unsupported current file"):
        engine._validate_batch_worker_records(paths[0].parent, [{"path": paths[0]}], controller.state)
