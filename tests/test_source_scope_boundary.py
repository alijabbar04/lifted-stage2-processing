"""Whole-worker paid-boundary checks using only synthetic local documents."""
import os
from pathlib import Path

import pytest

from test_source_recovery import setup, pdf, render, api, recovery, app


def submit(controller, client, ids, **kwargs):
    return recovery.submit_ready(controller, ids, controller.token(ids), client,
                                 "synthetic vocabulary", **kwargs)


@pytest.mark.parametrize("extension", [".pdf", ".xyz", ".docx", ".txt"])
def test_added_source_after_preflight_blocks_any_post(tmp_path, extension):
    controller, paths = setup(tmp_path, (None,))
    controller.preflight(render)
    new_path = paths[0].parent / ("unexpected" + extension)
    if extension == ".pdf":
        pdf(new_path, text="Unexpected synthetic source")
    else:
        new_path.write_bytes(b"Unexpected synthetic source")
    client = api()
    with pytest.raises(recovery.RecoveryError, match="unexpected|changed"):
        submit(controller, client, ["0"])
    client.submit_batch.assert_not_called()
    assert not controller.data["scopes"]


def applied_scope(root, *, renamed):
    controller, paths = setup(root, (None, None))
    controller.preflight(render)
    assert submit(controller, api(), ["0"]) == ["0"]
    applied_path = paths[0]
    if renamed:
        applied_path = paths[0].parent / "Passport.pdf"
        paths[0].rename(applied_path)
    controller.state.data.setdefault("workers", {})[str(paths[0].parent.resolve()).casefold()] = {
        "source_path": str(paths[0].parent), "completed": False,
        "applied_records": [{"path": str(applied_path), "hash": recovery.digest(applied_path),
                             "name": "Passport", "group": "Crucial"}]}
    controller.checkpoint()
    return controller, paths, applied_path


@pytest.mark.parametrize("renamed", [False, True])
def test_exact_applied_path_and_hash_allow_unrelated_ready_scope(tmp_path, renamed):
    controller, _, _ = applied_scope(tmp_path, renamed=renamed)
    client = api()
    assert submit(controller, client, ["1"]) == ["1"]
    client.submit_batch.assert_called_once()


@pytest.mark.parametrize("renamed", [False, True])
def test_changed_applied_file_blocks_unrelated_ready_scope(tmp_path, renamed):
    controller, _, applied_path = applied_scope(tmp_path, renamed=renamed)
    pdf(applied_path, text="Changed synthetic applied document")
    client = api()
    with pytest.raises(recovery.RecoveryError, match="unexpected|changed"):
        submit(controller, client, ["1"])
    client.submit_batch.assert_not_called()
    assert len(controller.data["scopes"]) == 1


def test_explicit_applied_receipt_can_authorize_changed_orientation_hash(tmp_path):
    controller, _, applied_path = applied_scope(tmp_path, renamed=False)
    pdf(applied_path, text="Synthetic authorized transformed output")
    worker = controller.state.data["workers"][str(applied_path.parent.resolve()).casefold()]
    worker["applied_records"][0]["hash"] = recovery.digest(applied_path)
    controller.checkpoint()
    client = api()
    assert submit(controller, client, ["1"]) == ["1"]
    client.submit_batch.assert_called_once()


@pytest.mark.parametrize("name", ["_helper.json", "overwrite_order.csv", "desktop.ini",
    "Thumbs.db", ".DS_Store", "ehthumbs.db", ".dropbox", ".dropbox.attr",
    "~$temporary.docx", "temporary.tmp", ".scan.orientation-1234abcd.pdf"])
def test_initial_discovery_artifact_exclusions_also_apply_at_paid_boundary(tmp_path, name):
    controller, paths = setup(tmp_path, (None,))
    controller.preflight(render)
    artifact = paths[0].parent / name
    assert app.is_program_file(artifact) or app._is_junk(artifact)
    artifact.write_bytes(b"Synthetic internal artifact")
    client = api()
    assert submit(controller, client, ["0"]) == ["0"]
    client.submit_batch.assert_called_once()


def test_scope_added_between_chunks_blocks_next_post_but_retains_first(tmp_path):
    controller, paths = setup(tmp_path, (None, None))
    controller.preflight(render)
    client = api()

    def first_post(chunk):
        (paths[1].parent / "new-unsupported.xyz").write_bytes(b"Synthetic late source")
        return {"id": "synthetic-first-batch", "processing_status": "in_progress"}

    client.submit_batch.side_effect = first_post
    with pytest.raises(recovery.RecoveryError, match="unexpected|changed"):
        submit(controller, client, ["0", "1"], max_requests=1)
    client.submit_batch.assert_called_once()
    assert controller.data["records"]["0"]["state"] == "submitted"
    assert controller.data["records"]["1"]["state"] == "ready"
    assert controller.data["scopes"][0]["status"] == "accepted"


@pytest.mark.parametrize("builder_name", ["classify_payload", "build_batch_request"])
def test_scope_added_during_payload_construction_blocks_first_post(tmp_path, builder_name):
    controller, paths = setup(tmp_path, (None,))
    controller.preflight(render)
    client = api()
    builder = getattr(client, builder_name)
    original_builder = builder.side_effect

    def changed_during_build(*args, **kwargs):
        (paths[0].parent / "late-during-build.xyz").write_bytes(b"Synthetic late source")
        return original_builder(*args, **kwargs)

    builder.side_effect = changed_during_build
    with pytest.raises(recovery.RecoveryError, match="unexpected|changed"):
        submit(controller, client, ["0"])
    client.submit_batch.assert_not_called()


@pytest.mark.parametrize("name", [".scan.orientation-bad.pdf", ".unrecognised-helper", "new-source.xyz"])
def test_near_miss_artifact_names_are_not_exempted(tmp_path, name):
    controller, paths = setup(tmp_path, (None,))
    controller.preflight(render)
    unexpected = paths[0].parent / name
    assert not app.is_program_file(unexpected) and not app._is_junk(unexpected)
    unexpected.write_bytes(b"Synthetic unexpected source")
    client = api()
    with pytest.raises(recovery.RecoveryError, match="unexpected|changed"):
        submit(controller, client, ["0"])
    client.submit_batch.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_empty_added_worker_junction_blocks_paid_scope(tmp_path):
    import _winapi
    root = tmp_path / "scope"
    controller, paths = setup(root, (None,))
    controller.preflight(render)
    external = tmp_path / "external"
    external.mkdir()
    junction = paths[0].parent / "redirected-empty"
    _winapi.CreateJunction(str(external), str(junction))
    client = api()
    try:
        with pytest.raises(recovery.RecoveryError, match="outside|junction"):
            submit(controller, client, ["0"])
        client.submit_batch.assert_not_called()
    finally:
        os.rmdir(junction)
    assert external.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_safe_path_rejects_junction_and_descendants(tmp_path):
    import _winapi
    root, external = tmp_path / "scope", tmp_path / "external"
    root.mkdir()
    external.mkdir()
    (external / "synthetic.pdf").write_bytes(b"Synthetic external target")
    junction = root / "redirected"
    _winapi.CreateJunction(str(external), str(junction))
    try:
        for path in (junction, junction / "synthetic.pdf"):
            with pytest.raises(recovery.RecoveryError, match="outside|junction"):
                recovery.safe_path(path, root)
    finally:
        os.rmdir(junction)  # Remove only the junction, never the target tree.
    assert (external / "synthetic.pdf").read_bytes() == b"Synthetic external target"
