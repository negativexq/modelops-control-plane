"""Data-migration test for 7d87fbdc22b8 (authoritative routing state and
durable ground truth labels) - the schema-only alembic check doesn't exercise
the INSERT ... SELECT data migration inside it at all, and losing labeled
data during that migration is exactly the kind of bug that check can't catch.

Builds a fresh SQLite DB at the *previous* revision, seeds it with rows shaped
like the pre-Sprint-14 schema (a PredictionMetric row with actual_label
already set, and a still-unmatched PendingLabel row), upgrades to head, and
verifies both landed in ground_truth_labels with the right values - including
occurred_at, which only pending_labels had a real one for.
"""

import sqlite3
import uuid
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from app.config import settings

BACKEND_DIR = Path(__file__).resolve().parent.parent
PREVIOUS_REVISION = "060d50b59428"


def _alembic_config(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    # Deliberately NOT Config(str(BACKEND_DIR / "alembic.ini")): alembic/env.py
    # calls logging.config.fileConfig(config.config_file_name) whenever
    # config_file_name is set, which reconfigures the whole process's logging
    # (handlers, propagation) - fine standalone, but this test shares a process
    # with every other test file, and clobbering global logging config broke an
    # unrelated caplog-based test elsewhere in the suite. Leaving
    # config_file_name unset (config.config_file_name is None) makes env.py's
    # `if config.config_file_name is not None: fileConfig(...)` guard skip that
    # call entirely - script_location and sqlalchemy.url are set explicitly
    # below anyway, so alembic.ini's own copies of them are never needed.
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # alembic/env.py reads settings.database_url (not the MODELOPS_DATABASE_URL
    # env var directly) every time it runs - and `settings` is a module-level
    # singleton resolved once at import time, almost certainly already imported
    # by some other test module before this one runs. Patching the env var alone
    # wouldn't reach it; patching the already-constructed singleton's attribute
    # does, since env.py re-reads it fresh on every command.upgrade() call.
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    return config


def test_migration_carries_existing_labels_into_ground_truth_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "migration-test.db"
    config = _alembic_config(db_path, monkeypatch)

    command.upgrade(config, PREVIOUS_REVISION)

    conn = sqlite3.connect(db_path)
    try:
        deployment_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO deployments (id, model_name, stable_version, canary_version, status, "
            "inconclusive_retry_count, automation_paused, version_id) "
            "VALUES (?, 'fraud-model', 'v1', 'v2-good', 'PROMOTED', 0, 0, 1)",
            (deployment_id,),
        )

        # A label already matched to a PredictionMetric under the old schema -
        # no occurred_at ever existed for this row (see the migration's own
        # comment on why label_ingested_at fills both columns for these).
        matched_prediction_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO prediction_metrics (id, deployment_id, model_version, latency_ms, "
            "status_code, prediction, actual_label, prediction_id, label_ingested_at) "
            "VALUES (?, ?, 'v2-good', 12.5, 200, 1, 1, ?, '2026-08-01T12:00:00+00:00')",
            (str(uuid.uuid4()), deployment_id, matched_prediction_id),
        )

        # A label that never found its PredictionMetric (still in pending_labels) -
        # this one DOES have a real occurred_at.
        pending_prediction_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO pending_labels "
            "(id, prediction_id, actual_label, occurred_at, ingested_at) "
            "VALUES (?, ?, 0, '2026-08-01T11:30:00+00:00', '2026-08-01T11:45:00+00:00')",
            (str(uuid.uuid4()), pending_prediction_id),
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(config, "head")

    conn = sqlite3.connect(db_path)
    try:
        rows = {
            row[0]: row
            for row in conn.execute(
                "SELECT prediction_id, actual_label, occurred_at, ingested_at "
                "FROM ground_truth_labels"
            ).fetchall()
        }
    finally:
        conn.close()

    assert len(rows) == 2

    matched = rows[matched_prediction_id]
    assert matched[1] == 1
    # Both occurred_at and ingested_at fall back to the old label_ingested_at.
    assert matched[2] == "2026-08-01T12:00:00+00:00"
    assert matched[3] == "2026-08-01T12:00:00+00:00"

    pending = rows[pending_prediction_id]
    assert pending[1] == 0
    assert pending[2] == "2026-08-01T11:30:00+00:00"
    assert pending[3] == "2026-08-01T11:45:00+00:00"
