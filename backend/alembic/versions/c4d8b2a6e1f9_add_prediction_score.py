"""record each prediction's score for the prediction drift check

Revision ID: c4d8b2a6e1f9
Revises: a7c2e9f4b1d3
Create Date: 2026-11-26 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d8b2a6e1f9'
down_revision: Union[str, Sequence[str], None] = 'a7c2e9f4b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('prediction_metrics') as batch:
        batch.add_column(sa.Column('score', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('prediction_metrics') as batch:
        batch.drop_column('score')
