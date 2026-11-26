from app.traffic_stages import at_final_stage
from app.worker.decide import WorkerAction, decide_action


def test_fail_always_rolls_back_regardless_of_traffic_stage() -> None:
    assert decide_action("FAIL", canary_at_final_stage=False) == WorkerAction.ROLLBACK
    assert decide_action("FAIL", canary_at_final_stage=True) == WorkerAction.ROLLBACK


def test_inconclusive_records_retry_never_touches_traffic() -> None:
    assert decide_action("INCONCLUSIVE", canary_at_final_stage=False) == (
        WorkerAction.RECORD_INCONCLUSIVE
    )
    assert decide_action("INCONCLUSIVE", canary_at_final_stage=True) == (
        WorkerAction.RECORD_INCONCLUSIVE
    )


def test_pass_advances_traffic_before_the_final_stage() -> None:
    assert decide_action("PASS", canary_at_final_stage=False) == WorkerAction.ADVANCE_TRAFFIC


def test_pass_promotes_at_the_final_stage() -> None:
    assert decide_action("PASS", canary_at_final_stage=True) == WorkerAction.PROMOTE


def test_the_final_stage_is_50_percent_not_100() -> None:
    assert not at_final_stage(0.10)
    assert not at_final_stage(0.25)
    assert at_final_stage(0.50)
    assert at_final_stage(0.75)  # created past the ramp's end
    assert at_final_stage(1.0)
