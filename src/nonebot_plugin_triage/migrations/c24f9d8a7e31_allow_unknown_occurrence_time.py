"""允许无法从现场证据确认的发生时间保持未知。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c24f9d8a7e31"
down_revision = "a91bd7240e56"
branch_labels = None
depends_on = None

_PREFIX = "nonebot_plugin_triage_"


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table(_PREFIX + "bug_problem") as batch:
        batch.alter_column("first_observed_at", existing_type=sa.String(40), nullable=True)
        batch.alter_column("last_observed_at", existing_type=sa.String(40), nullable=True)
    with op.batch_alter_table(_PREFIX + "bug_occurrence") as batch:
        batch.alter_column("observed_at", existing_type=sa.String(40), nullable=True)


def downgrade(name: str = "") -> None:
    if name:
        return
    connection = op.get_bind()
    unknown_count = connection.execute(
        sa.text(f"SELECT count(*) FROM {_PREFIX}bug_occurrence WHERE observed_at IS NULL")
    ).scalar_one()
    unknown_count += connection.execute(
        sa.text(
            f"SELECT count(*) FROM {_PREFIX}bug_problem "
            "WHERE first_observed_at IS NULL OR last_observed_at IS NULL"
        )
    ).scalar_one()
    if unknown_count:
        raise RuntimeError("cannot downgrade while unknown occurrence times are recorded")
    with op.batch_alter_table(_PREFIX + "bug_occurrence") as batch:
        batch.alter_column("observed_at", existing_type=sa.String(40), nullable=False)
    with op.batch_alter_table(_PREFIX + "bug_problem") as batch:
        batch.alter_column("first_observed_at", existing_type=sa.String(40), nullable=False)
        batch.alter_column("last_observed_at", existing_type=sa.String(40), nullable=False)
