"""Feature drift: is the traffic a canary serves still like the data it was
trained on?

Every other check waits for something: latency and errors for traffic, recall
for ground-truth labels that arrive minutes or days late. Drift needs neither a
comparison version nor labels, only the inputs the canary was asked to score
and a picture of its training data. That makes it the one quality signal
available from the first request.

The picture is a reference profile, written next to the model artifact at
training time (scripts/train_models.py, reference_profile.json): for each
feature, the training distribution over a fixed set of bins - deciles for
numeric features, the categories themselves for categorical ones. At
evaluation time the canary's live inputs from the reliability window are
counted into the same bins and compared per feature:

- PSI (population stability index), the industry's usual drift size, with the
  usual reading: below 0.1 stable, 0.1-0.25 moderate, above 0.25 a major shift.
  Raw PSI is biased upward on small samples - pure sampling noise over B bins
  adds about (B - 1) / n - so the check reports PSI minus that expected noise.
  Otherwise a healthy canary seen through 100 requests would look drifted on
  some of its 28 features by chance.
- A chi-square goodness-of-fit test of the live counts against the training
  proportions, Bonferroni-corrected for the number of features: "is this shift
  real or sampling noise?".

FAIL needs both, the same rule as the interval checks: a feature whose PSI is
past the limit *and* whose shift is significant at the evaluation's level.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import stats

PROFILE_FORMAT_VERSION = 1
NUMERIC_BINS = 10
# A bin no training row fell in still gets this share, so log(live / reference)
# stays finite when live traffic lands somewhere training never did.
_FLOOR = 1e-4
# Below this many live rows the chi-square approximation and the PSI correction
# are both too rough to act on (with decile bins, 50 rows is 5 expected per bin).
MINIMUM_SAMPLES = 50
OTHER_CATEGORY = "__other__"


def build_reference_profile(
    rows: Mapping[str, Sequence[Any]],
    numeric_features: Iterable[str],
    categorical_features: Iterable[str],
) -> dict[str, Any]:
    """The training-time picture the drift check compares live traffic to.
    `rows` maps a column name to its training values (a DataFrame works)."""
    features: dict[str, Any] = {}
    sample_count = 0
    for name in numeric_features:
        values = np.asarray(rows[name], dtype=float)
        sample_count = max(sample_count, values.size)
        quantiles = np.quantile(values, np.linspace(0, 1, NUMERIC_BINS + 1)[1:-1])
        edges = sorted({float(q) for q in quantiles})
        counts = np.bincount(_numeric_bins(values, edges), minlength=len(edges) + 1)
        features[name] = {
            "kind": "numeric",
            "edges": edges,
            "proportions": (counts / counts.sum()).tolist(),
        }
    for name in categorical_features:
        values_list = [str(v) for v in rows[name]]
        sample_count = max(sample_count, len(values_list))
        categories = sorted(set(values_list))
        index = {c: i for i, c in enumerate(categories)}
        counts = np.bincount([index[v] for v in values_list], minlength=len(categories) + 1)
        features[name] = {
            "kind": "categorical",
            "categories": categories,
            "proportions": (counts / counts.sum()).tolist(),
        }
    return {
        "format_version": PROFILE_FORMAT_VERSION,
        "sample_count": sample_count,
        "features": features,
    }


def _numeric_bins(values: np.ndarray, edges: list[float]) -> np.ndarray:
    # Bin i is (edges[i-1], edges[i]]; the first is open below, the last above.
    return np.searchsorted(np.asarray(edges, dtype=float), values, side="left")


def _live_counts(spec: Mapping[str, Any], values: list[Any]) -> np.ndarray | None:
    if spec["kind"] == "numeric":
        try:
            numeric = np.asarray(values, dtype=float)
        except (TypeError, ValueError):
            return None
        bins = _numeric_bins(numeric, list(spec["edges"]))
        return np.bincount(bins, minlength=len(spec["edges"]) + 1)
    categories: list[str] = list(spec["categories"])
    index = {c: i for i, c in enumerate(categories)}
    other = len(categories)  # values training never saw
    return np.bincount([index.get(str(v), other) for v in values], minlength=len(categories) + 1)


@dataclass(frozen=True)
class FeatureDrift:
    feature: str
    sample_count: int
    psi: float
    # PSI minus the (bins - 1) / n that sampling noise alone produces; never < 0.
    psi_corrected: float
    # Chi-square goodness-of-fit p-value against the training proportions.
    p_value: float


def feature_drift(feature: str, spec: Mapping[str, Any], values: list[Any]) -> FeatureDrift | None:
    counts = _live_counts(spec, values)
    if counts is None or counts.sum() == 0:
        return None
    n = int(counts.sum())
    reference = np.maximum(np.asarray(spec["proportions"], dtype=float), _FLOOR)
    reference = reference / reference.sum()
    # Half a request of smoothing per bin: an empty live bin is the normal case
    # at small n, and without it log(live / reference) blows up on it.
    live = (counts + 0.5) / (n + 0.5 * counts.size)
    psi = float(np.sum((live - reference) * np.log(live / reference)))
    bins = int(np.count_nonzero(np.asarray(spec["proportions"]) > 0)) or len(reference)
    psi_corrected = max(0.0, psi - (bins - 1) / n)
    return FeatureDrift(feature, n, psi, psi_corrected, _chi_square_p(counts, reference))


def _chi_square_p(counts: np.ndarray, reference: np.ndarray) -> float:
    """Goodness of fit of live counts to the training proportions. The
    chi-square approximation overstates significance for bins expecting fewer
    than 5 requests (a rare category, the last bin of a mostly-zero integer
    feature), so those are pooled into one bin first."""
    n = counts.sum()
    expected = reference * n
    small = expected < 5
    if small.any():
        pooled_obs = [counts[small].sum()]
        pooled_exp = [expected[small].sum()]
        observed = np.concatenate([counts[~small], pooled_obs])
        expected = np.concatenate([expected[~small], pooled_exp])
        if expected[-1] < 5 and observed.size > 1:
            # Still too small: fold it into the bin expecting the least.
            smallest = int(np.argmin(expected[:-1]))
            observed[smallest] += observed[-1]
            expected[smallest] += expected[-1]
            observed, expected = observed[:-1], expected[:-1]
    else:
        observed = counts
    if observed.size < 2:
        return 1.0
    p_value = float(stats.chisquare(observed, f_exp=expected).pvalue)
    return 1.0 if math.isnan(p_value) else p_value


@dataclass(frozen=True)
class DriftReport:
    sample_count: int
    features: list[FeatureDrift]

    @property
    def largest(self) -> FeatureDrift | None:
        return max(self.features, key=lambda f: f.psi_corrected, default=None)

    def adjusted_p_value(self, drift: FeatureDrift) -> float:
        """Bonferroni across every feature tested: one evaluation tests them
        all, so 28 features must not mean 28 chances at a false FAIL."""
        return min(1.0, drift.p_value * len(self.features))


def drift_report(profile: Mapping[str, Any], inputs: Sequence[Mapping[str, Any]]) -> DriftReport:
    """Compare live `inputs` (one dict of feature values per request) with a
    reference profile, feature by feature. Features a request didn't send are
    skipped for that request."""
    results: list[FeatureDrift] = []
    for name, spec in profile["features"].items():
        values = [row[name] for row in inputs if name in row and row[name] is not None]
        result = feature_drift(name, spec, values)
        if result is not None:
            results.append(result)
    return DriftReport(sample_count=len(inputs), features=results)
