from app.control_plane.models import PolicyEvaluationResult
from app.policy.explain import explain_policy_check

PASS = PolicyEvaluationResult.PASS
FAIL = PolicyEvaluationResult.FAIL
INCONCLUSIVE = PolicyEvaluationResult.INCONCLUSIVE


def test_minimum_requests_pass_mentions_both_sides() -> None:
    text = explain_policy_check(
        policy_name="minimum_requests", observed_value=100.0, threshold=100.0, result=PASS
    )
    assert "both sides" in text
    assert "100" in text


def test_minimum_requests_inconclusive_without_canary_weight_is_generic() -> None:
    text = explain_policy_check(
        policy_name="minimum_requests",
        observed_value=5.0,
        threshold=100.0,
        result=INCONCLUSIVE,
    )
    assert "insufficient data" in text
    assert "stable side" not in text


def test_minimum_requests_inconclusive_at_full_canary_names_stable_side() -> None:
    text = explain_policy_check(
        policy_name="minimum_requests",
        observed_value=0.0,
        threshold=100.0,
        result=INCONCLUSIVE,
        canary_weight=1.0,
    )
    assert "stable side" in text
    assert "100% of traffic" in text
    assert "not a bug" in text


def test_minimum_requests_inconclusive_at_partial_canary_stays_generic() -> None:
    text = explain_policy_check(
        policy_name="minimum_requests",
        observed_value=5.0,
        threshold=100.0,
        result=INCONCLUSIVE,
        canary_weight=0.1,
    )
    assert "stable side" not in text
    assert "insufficient data" in text


def test_latency_inconclusive_when_no_data() -> None:
    text = explain_policy_check(
        policy_name="latency_p95_increase", observed_value=None, threshold=20.0, result=INCONCLUSIVE
    )
    assert "insufficient data" in text


def test_latency_fail_mentions_percentages() -> None:
    text = explain_policy_check(
        policy_name="latency_p95_increase", observed_value=45.0, threshold=20.0, result=FAIL
    )
    assert "45.0%" in text
    assert "20%" in text


def test_latency_pass() -> None:
    text = explain_policy_check(
        policy_name="latency_p95_increase", observed_value=5.0, threshold=20.0, result=PASS
    )
    assert "within" in text


def test_error_rate_inconclusive_when_no_data() -> None:
    text = explain_policy_check(
        policy_name="max_error_rate", observed_value=None, threshold=5.0, result=INCONCLUSIVE
    )
    assert "insufficient data" in text


def test_error_rate_fail() -> None:
    text = explain_policy_check(
        policy_name="max_error_rate", observed_value=50.0, threshold=5.0, result=FAIL
    )
    assert "50.00%" in text
    assert "exceeds" in text


def test_recall_inconclusive_names_actual_label() -> None:
    text = explain_policy_check(
        policy_name="minimum_recall", observed_value=None, threshold=0.8, result=INCONCLUSIVE
    )
    assert "actual_label not available" in text
    assert "insufficient data" not in text  # distinct reason from the traffic-count cases


def test_recall_fail() -> None:
    text = explain_policy_check(
        policy_name="minimum_recall", observed_value=0.5, threshold=0.8, result=FAIL
    )
    assert "below the required" in text


def test_recall_pass() -> None:
    text = explain_policy_check(
        policy_name="minimum_recall", observed_value=0.9, threshold=0.8, result=PASS
    )
    assert "meets" in text


def test_unknown_policy_name_falls_back_to_generic_text() -> None:
    text = explain_policy_check(
        policy_name="some_future_policy", observed_value=1.0, threshold=2.0, result=FAIL
    )
    assert "some_future_policy" in text
    assert "fail" in text.lower()
