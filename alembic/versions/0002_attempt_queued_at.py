"""attempt queued_at

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('job_attempts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('queued_at', sa.DateTime(timezone=True), nullable=True))



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('job_attempts', schema=None) as batch_op:
        batch_op.drop_column('queued_at')

