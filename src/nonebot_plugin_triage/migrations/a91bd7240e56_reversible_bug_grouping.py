"""保存调查与发生的关联、拆分审计和指纹停用状态。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a91bd7240e56"
down_revision = "f82c4a7d193b"
branch_labels = None
depends_on = None

_PREFIX = "nonebot_plugin_triage_"


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table(_PREFIX + "bug_problem") as batch:
        batch.add_column(
            sa.Column("signature_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    with op.batch_alter_table(_PREFIX + "bug_problem") as batch:
        batch.alter_column("signature_enabled", server_default=None)
    with op.batch_alter_table(_PREFIX + "problem_decision") as batch:
        batch.add_column(sa.Column("occurrence_key", sa.String(64), nullable=True))
    op.create_table(
        _PREFIX + "problem_split",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "source_problem_id",
            sa.String(32),
            sa.ForeignKey(_PREFIX + "bug_problem.id"),
            nullable=False,
        ),
        sa.Column(
            "destination_problem_id",
            sa.String(32),
            sa.ForeignKey(_PREFIX + "bug_problem.id"),
            nullable=False,
        ),
        sa.Column("occurrence_key", sa.String(64), nullable=False),
        sa.Column("actor_scope_hmac", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.String(40), nullable=False),
        sa.Column("report_ids", sa.JSON(), nullable=False),
        sa.Column("decision_ids", sa.JSON(), nullable=False),
    )


def downgrade(name: str = "") -> None:
    if name:
        return
    # 拆分后降级会丢失停用约束并再次误合并，必须拒绝无损条件不成立的降级。
    count = (
        op.get_bind().execute(sa.text(f"SELECT count(*) FROM {_PREFIX}problem_split")).scalar_one()
    )
    if count:
        raise RuntimeError("cannot downgrade grouping after recorded splits")
    op.drop_table(_PREFIX + "problem_split")
    with op.batch_alter_table(_PREFIX + "problem_decision") as batch:
        batch.drop_column("occurrence_key")
    with op.batch_alter_table(_PREFIX + "bug_problem") as batch:
        batch.drop_column("signature_enabled")
