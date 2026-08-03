from app.worker.decide import WorkerAction, decide_action


def test_fail_always_rolls_back_regardless_of_traffic_stage() -> None:
    assert decide_action("FAIL", canary_at_full_traffic=False) == WorkerAction.ROLLBACK
    assert decide_action("FAIL", canary_at_full_traffic=True) == WorkerAction.ROLLBACK


def test_inconclusive_records_retry_never_touches_traffic() -> None:
    assert decide_action("INCONCLUSIVE", canary_at_full_traffic=False) == (
        WorkerAction.RECORD_INCONCLUSIVE
    )
    assert decide_action("INCONCLUSIVE", canary_at_full_traffic=True) == (
        WorkerAction.RECORD_INCONCLUSIVE
    )


def test_pass_advances_traffic_when_not_at_full_stage() -> None:
    assert decide_action("PASS", canary_at_full_traffic=False) == WorkerAction.ADVANCE_TRAFFIC


def test_pass_promotes_when_already_at_full_traffic() -> None:
    assert decide_action("PASS", canary_at_full_traffic=True) == WorkerAction.PROMOTE
