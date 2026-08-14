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


def test_recall_inconclusive_describes_edge_case() -> None:
    text = explain_policy_check(
        policy_name="minimum_recall", observed_value=None, threshold=0.8, result=INCONCLUSIVE
    )
    assert "insufficient data" in text
    assert "edge case" in text


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


def test_minimum_labeled_samples_inconclusive_mentions_counts() -> None:
    text = explain_policy_check(
        policy_name="minimum_labeled_samples",
        observed_value=5.0,
        threshold=30.0,
        result=INCONCLUSIVE,
    )
    assert "5/30" in text
    assert "recall was not evaluated" in text


def test_minimum_labeled_samples_pass() -> None:
    text = explain_policy_check(
        policy_name="minimum_labeled_samples", observed_value=50.0, threshold=30.0, result=PASS
    )
    assert "enough labeled canary predictions" in text
    assert "50/30" in text


def test_minimum_label_coverage_inconclusive_mentions_fraction_and_threshold() -> None:
    # The acceptance-criteria example: 12 predictions in the quality window, 3
    # labeled (coverage 0.25), threshold 0.50.
    text = explain_policy_check(
        policy_name="minimum_label_coverage",
        observed_value=0.25,
        threshold=0.5,
        result=INCONCLUSIVE,
        labeled_sample_count=3,
    )
    assert "3 of 12 predictions" in text
    assert "coverage 25%" in text
    assert "threshold 50%" in text
    assert "recall was not evaluated" in text


def test_minimum_label_coverage_pass() -> None:
    text = explain_policy_check(
        policy_name="minimum_label_coverage",
        observed_value=0.9,
        threshold=0.5,
        result=PASS,
        labeled_sample_count=45,
    )
    assert "45 of 50 predictions" in text
    assert "meeting" in text


def test_minimum_label_coverage_inconclusive_without_data() -> None:
    text = explain_policy_check(
        policy_name="minimum_label_coverage",
        observed_value=None,
        threshold=0.5,
        result=INCONCLUSIVE,
    )
    assert "insufficient data" in text


def test_minimum_positive_labels_inconclusive_mentions_counts() -> None:
    # The scenario the gate exists for: 71 labeled predictions, only 3 positive.
    text = explain_policy_check(
        policy_name="minimum_positive_labels",
        observed_value=3.0,
        threshold=30.0,
        result=INCONCLUSIVE,
        labeled_sample_count=71,
    )
    assert "3 of 71" in text
    assert "30" in text
    assert "recall was not evaluated" in text


def test_minimum_positive_labels_pass() -> None:
    text = explain_policy_check(
        policy_name="minimum_positive_labels",
        observed_value=40.0,
        threshold=30.0,
        result=PASS,
        labeled_sample_count=50,
    )
    assert "40 of 50" in text
    assert "statistically meaningful" in text


def test_unknown_policy_name_falls_back_to_generic_text() -> None:
    text = explain_policy_check(
        policy_name="some_future_policy", observed_value=1.0, threshold=2.0, result=FAIL
    )
    assert "some_future_policy" in text
    assert "fail" in text.lower()
