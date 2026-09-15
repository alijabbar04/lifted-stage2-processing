"""Finite source budgets retain prior accepted classification commitments."""
import pytest

from test_source_recovery import setup, render, api, recovery


def prepared(root, count=2):
    controller, paths = setup(root, [None] * count)
    controller.preflight(render)
    controller.state.data["settings"]["max_budget_gbp"] = 20
    controller.checkpoint()
    return controller, paths


def submit(controller, client, ids, **kwargs):
    return recovery.submit_ready(controller, ids, controller.token(ids), client,
                                 "synthetic vocabulary", **kwargs)


def test_first_confirmed_scope_can_submit_multiple_chunks_under_finite_budget(tmp_path):
    controller, _ = prepared(tmp_path, 3)
    client = api()
    assert submit(controller, client, ["0", "1", "2"], max_requests=1) == ["0", "1", "2"]
    assert client.submit_batch.call_count == 3


@pytest.mark.parametrize("costs", [{}, {"primary_actual_gbp": 6},
    {"primary_actual_gbp": 6, "primary_accounted_batch_ids": ["a-different-batch"]}])
def test_prior_primary_acceptance_blocks_later_purchase_until_exact_accounting(tmp_path, costs):
    controller, _ = prepared(tmp_path)
    client = api()
    assert submit(controller, client, ["0"]) == ["0"]
    controller.state.data["costs"] = costs
    controller.checkpoint()
    with pytest.raises(recovery.RecoveryError, match="Apply accepted.*settle"):
        submit(controller, client, ["1"])
    client.submit_batch.assert_called_once()
    assert controller.data["records"]["1"]["state"] == "ready"


def test_pending_followup_commitment_blocks_even_when_recorded_actual_plus_extra_fits(tmp_path):
    controller, _ = prepared(tmp_path)
    client = api()
    assert submit(controller, client, ["0"]) == ["0"]
    controller.state.data["costs"] = {"primary_actual_gbp": 6,
        "primary_accounted_batch_ids": ["batch-0"], "followup_actual_gbp": 0}
    controller.state.data["followup"] = {"phase": "pending", "est_gbp": 9,
        "submission": {"status": "accepted", "batch_id": "old-followup"},
        "batches": [{"id": "old-followup", "request_ids": ["fu-0"]}]}
    controller.checkpoint()
    # £6 recorded + £10 new forecast fits £20, but omits the accepted £9
    # follow-up commitment. The shared (not merely GUI) boundary must wait.
    assert controller.summary()["cost_incurred_gbp"] + 10 < 20
    with pytest.raises(recovery.RecoveryError, match="not yet accounted"):
        submit(controller, client, ["1"])
    client.submit_batch.assert_called_once()


def test_settled_prior_primary_and_followup_allow_later_confirmed_scope(tmp_path):
    controller, _ = prepared(tmp_path)
    client = api()
    assert submit(controller, client, ["0"]) == ["0"]
    controller.state.data["followup"] = {"phase": "ended",
        "batches": [{"id": "old-followup", "request_ids": ["fu-0"]}]}
    controller.state.data["costs"] = {"primary_actual_gbp": 1,
        "primary_accounted_batch_ids": ["batch-0"], "followup_actual_gbp": 1,
        "followup_accounted_batch_ids": ["old-followup"]}
    controller.checkpoint()
    assert submit(controller, client, ["1"]) == ["1"]
    assert client.submit_batch.call_count == 2


def test_unlimited_budget_does_not_require_accounting_wait(tmp_path):
    controller, _ = prepared(tmp_path)
    controller.state.data["settings"]["max_budget_gbp"] = 0
    controller.checkpoint()
    client = api()
    assert submit(controller, client, ["0"]) == ["0"]
    assert submit(controller, client, ["1"]) == ["1"]
    assert client.submit_batch.call_count == 2


def test_uncertain_followup_billing_cannot_bypass_finite_budget_guard(tmp_path):
    controller, _ = prepared(tmp_path)
    controller.state.data["followup"] = {"phase": "ambiguous",
        "submission": {"status": "ambiguous"}, "batches": []}
    controller.checkpoint()
    client = api()
    with pytest.raises(recovery.RecoveryError, match="billing is uncertain"):
        submit(controller, client, ["0"])
    client.submit_batch.assert_not_called()
