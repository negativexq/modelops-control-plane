import enum


class WorkerAction(enum.Enum):
    ADVANCE_TRAFFIC = "advance_traffic"
    PROMOTE = "promote"
    ROLLBACK = "rollback"
    RECORD_INCONCLUSIVE = "record_inconclusive"


def decide_action(overall_result: str, canary_at_full_traffic: bool) -> WorkerAction:
    """Pure decision function - no I/O, so it's trivially unit-testable.

    - FAIL always rolls back, regardless of traffic stage.
    - INCONCLUSIVE never acts on traffic; it just records the retry (the control
      plane freezes the deployment once policy_config.max_inconclusive_retries is
      exceeded - see service.record_inconclusive).
    - PASS advances to the next traffic stage, or promotes if the canary is already
      at 100%.
    """
    if overall_result == "FAIL":
        return WorkerAction.ROLLBACK
    if overall_result == "INCONCLUSIVE":
        return WorkerAction.RECORD_INCONCLUSIVE
    return WorkerAction.PROMOTE if canary_at_full_traffic else WorkerAction.ADVANCE_TRAFFIC
