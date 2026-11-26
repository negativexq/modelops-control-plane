"""add statistical evidence to policy_evaluations

Revision ID: b8e4d2f1a9c7
Revises: a3f1c2d4e5b6
Create Date: 2026-11-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8e4d2f1a9c7'
down_revision: Union[str, Sequence[str], None] = 'a3f1c2d4e5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('policy_evaluations') as batch:
        batch.add_column(sa.Column('ci_low', sa.Float(), nullable=True))
        batch.add_column(sa.Column('ci_high', sa.Float(), nullable=True))
        batch.add_column(sa.Column('p_value', sa.Float(), nullable=True))
        batch.add_column(sa.Column('confidence_level', sa.Float(), nullable=True))
        batch.add_column(sa.Column('stat_method', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('policy_evaluations') as batch:
        batch.drop_column('stat_method')
        batch.drop_column('confidence_level')
        batch.drop_column('p_value')
        batch.drop_column('ci_high')
        batch.drop_column('ci_low')
