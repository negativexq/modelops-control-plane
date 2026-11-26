"""add webhook_deliveries outbox

Revision ID: d9e3f7a1c5b2
Revises: c4d8b2a6e1f9
Create Date: 2026-11-26 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9e3f7a1c5b2'
down_revision: Union[str, Sequence[str], None] = 'c4d8b2a6e1f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'webhook_deliveries',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('deployment_id', sa.String(length=36), nullable=False),
        sa.Column('event', sa.String(length=64), nullable=False),
        sa.Column('url', sa.String(length=500), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column(
            'status',
            sa.Enum('PENDING', 'DELIVERED', 'FAILED', name='webhookdeliverystatus',
                    native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_status_code', sa.Integer(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_webhook_deliveries_deployment_id', 'webhook_deliveries', ['deployment_id'])
    op.create_index('ix_webhook_deliveries_status', 'webhook_deliveries', ['status'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_webhook_deliveries_status', table_name='webhook_deliveries')
    op.drop_index('ix_webhook_deliveries_deployment_id', table_name='webhook_deliveries')
    op.drop_table('webhook_deliveries')
