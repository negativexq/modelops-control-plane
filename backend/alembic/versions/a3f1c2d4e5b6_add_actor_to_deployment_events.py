"""add actor to deployment_events

Revision ID: a3f1c2d4e5b6
Revises: 7d87fbdc22b8
Create Date: 2026-11-25 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f1c2d4e5b6'
down_revision: Union[str, Sequence[str], None] = '7d87fbdc22b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable, no backfill: older events simply have no recorded operator.
    op.add_column("deployment_events", sa.Column("actor", sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("deployment_events", "actor")
