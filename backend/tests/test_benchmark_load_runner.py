from pathlib import Path

import pytest

from scripts.benchmarks.load_runner import parse_locust_stats_csv

STATS_CSV_HEADER = (
    "Type,Name,Request Count,Failure Count,Median Response Time,Average Response Time,"
    "Min Response Time,Max Response Time,Average Content Size,Requests/s,Failures/s,"
    "50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%\n"
)


def _write_stats_csv(tmp_path: Path, aggregated_row: str) -> Path:
    path = tmp_path / "run_stats.csv"
    path.write_text(
        STATS_CSV_HEADER + "POST,/router/predict,1000,10,12,13.5,5,120,150,99.8,0.99,"
        "12,14,15,16,20,30,40,50,80,100,120\n" + aggregated_row
    )
    return path


def test_parse_locust_stats_csv_reads_aggregated_row(tmp_path: Path) -> None:
    aggregated = (
        "None,Aggregated,1000,10,12,13.5,5,120,150,99.8,0.99,12,14,15,16,20,30,40,50,80,100,120\n"
    )
    path = _write_stats_csv(tmp_path, aggregated)

    result = parse_locust_stats_csv(path, duration_seconds=60.0)

    assert result.total_requests == 1000
    assert result.total_failures == 10
    assert result.error_rate == pytest.approx(0.01)
    assert result.requests_per_second == pytest.approx(99.8)
    assert result.p50_ms == pytest.approx(12.0)
    assert result.p95_ms == pytest.approx(30.0)
    assert result.p99_ms == pytest.approx(50.0)
    assert result.duration_seconds == 60.0


def test_parse_locust_stats_csv_zero_requests_has_zero_error_rate(tmp_path: Path) -> None:
    aggregated = "None,Aggregated,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0\n"
    path = _write_stats_csv(tmp_path, aggregated)

    result = parse_locust_stats_csv(path, duration_seconds=10.0)

    assert result.total_requests == 0
    assert result.error_rate == 0.0


def test_parse_locust_stats_csv_missing_aggregated_row_raises(tmp_path: Path) -> None:
    path = tmp_path / "run_stats.csv"
    row = "POST,/router/predict,1000,10,12,13.5,5,120,150,99.8,0.99,"
    row += "12,14,15,16,20,30,40,50,80,100,120\n"
    path.write_text(STATS_CSV_HEADER + row)

    with pytest.raises(ValueError, match="Aggregated"):
        parse_locust_stats_csv(path, duration_seconds=60.0)
