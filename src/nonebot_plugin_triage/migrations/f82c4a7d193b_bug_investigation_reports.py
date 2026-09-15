"""保存维护者调查摘要和已确认的插件范围，历史记录不补造内容。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f82c4a7d193b"
down_revision = "edc3fe0967f9"
branch_labels = None
depends_on = None


def upgrade(name: str = "") -> None:
    if name:
        return
    for table in ("bug_problem", "bug_occurrence"):
        with op.batch_alter_table(f"nonebot_plugin_triage_{table}") as batch_op:
            batch_op.add_column(
                sa.Column("plugin_owners", sa.JSON(), nullable=False, server_default="[]")
            )
        with op.batch_alter_table(f"nonebot_plugin_triage_{table}") as batch_op:
            batch_op.alter_column("plugin_owners", server_default=None)
    with op.batch_alter_table("nonebot_plugin_triage_problem_decision") as batch_op:
        batch_op.add_column(sa.Column("investigation_summary", sa.Text(), nullable=True))


def downgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("nonebot_plugin_triage_problem_decision") as batch_op:
        batch_op.drop_column("investigation_summary")
    for table in ("bug_occurrence", "bug_problem"):
        with op.batch_alter_table(f"nonebot_plugin_triage_{table}") as batch_op:
            batch_op.drop_column("plugin_owners")
