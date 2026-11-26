"""add sequential_look to policy_evaluations

Revision ID: f3a9c1e7d2b4
Revises: b8e4d2f1a9c7
Create Date: 2026-11-26 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1e7d2b4'
down_revision: Union[str, Sequence[str], None] = 'b8e4d2f1a9c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('policy_evaluations') as batch:
        batch.add_column(sa.Column('sequential_look', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('policy_evaluations') as batch:
        batch.drop_column('sequential_look')
