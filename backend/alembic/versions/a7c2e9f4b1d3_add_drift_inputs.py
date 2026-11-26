"""record prediction inputs for the drift check

Revision ID: a7c2e9f4b1d3
Revises: f3a9c1e7d2b4
Create Date: 2026-11-26 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c2e9f4b1d3'
down_revision: Union[str, Sequence[str], None] = 'f3a9c1e7d2b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('prediction_metrics') as batch:
        batch.add_column(sa.Column('features', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('artifact', sa.String(length=200), nullable=True))
    with op.batch_alter_table('policy_evaluations') as batch:
        batch.add_column(sa.Column('detail', sa.String(length=200), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('policy_evaluations') as batch:
        batch.drop_column('detail')
    with op.batch_alter_table('prediction_metrics') as batch:
        batch.drop_column('artifact')
        batch.drop_column('features')
