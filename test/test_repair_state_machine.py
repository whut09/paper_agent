from paper_agent.harness.repair import RepairAction, RepairState, RepairStateMachine
from paper_agent.schemas.findings import Finding


def finding(reason, *, asset_id=4, confidence=0.95, message="asset 4 crop issue"):
    return Finding.create(
        stage="visual_guard",
        severity="error",
        confidence=confidence,
        asset_id=asset_id,
        reason_code=reason,
        human_message=message,
        provenance=("local:geometry", "vision_model"),
    )


def test_table_repair_records_a_changed_transition():
    machine = RepairStateMachine(max_actions_per_asset=2, max_global_cost=8)
    steps = machine.plan([finding("table_truncated")])

    assert [step.action for step in steps] == [
        RepairAction.SCORE_CANDIDATES,
        RepairAction.EXPAND_BORDER,
    ]
    transition = machine.transition(
        steps[-1],
        before_signature="old-geometry",
        after_signature="new-geometry",
    )
    assert transition.changed is True
    assert transition.state_to == RepairState.RECAPTURE
    assert transition.before_signature != transition.after_signature
    assert transition.next_action == "split_at_border"


def test_identical_signature_is_blocked_and_not_counted_as_progress():
    machine = RepairStateMachine()
    step = machine.plan([finding("mixed_objects")])[0]
    transition = machine.transition(
        step,
        before_signature="same",
        after_signature="same",
    )

    assert transition.changed is False
    assert transition.state_to == RepairState.BLOCKED
    assert transition.next_action == "blocked"


def test_mixed_object_conflict_wins_over_truncation_without_expansion():
    machine = RepairStateMachine()
    steps = machine.plan(
        [
            finding("table_truncated", message="asset 4 table is truncated"),
            finding("mixed_objects", message="asset 4 contains a table and a figure"),
        ]
    )

    assert len(steps) == 1
    assert steps[0].reason_code == "mixed_objects"
    assert steps[0].action == RepairAction.SPLIT_OBJECTS
    assert steps[0].action not in {RepairAction.EXPAND_BORDER, RepairAction.RECAPTURE_ASSET}


def test_global_budget_and_per_asset_budget_stop_new_actions():
    budgeted = RepairStateMachine(max_actions_per_asset=2, max_global_cost=0.5)
    finding_item = finding("table_body_missing")

    assert budgeted.plan([finding_item], global_cost_used=0.5) == []

    machine = RepairStateMachine(max_actions_per_asset=2, max_global_cost=8)
    two_actions = machine.plan([finding_item])
    assert len(two_actions) == 2
    attempted = {step.attempt_key: 1 for step in two_actions}
    assert machine.plan([finding_item], attempted=attempted) == []
