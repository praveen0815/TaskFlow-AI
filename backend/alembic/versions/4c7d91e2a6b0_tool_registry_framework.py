"""tool registry and execution framework

Revision ID: 4c7d91e2a6b0
Revises: f1d0eb8dfa40
Create Date: 2026-11-26 09:44:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4c7d91e2a6b0"
down_revision: Union[str, None] = "f1d0eb8dfa40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_PERMISSIONS = (
    "'[\"tool:calculator.evaluate\",\"tool:datetime.now\","
    "\"tool:text.transform\",\"tool:json.process\","
    "\"tool:document.extract\",\"tool:workflow.lookup\","
    "\"tool:notification.draft\"]'"
)


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "tool_permissions",
            sa.JSON(),
            server_default=sa.text(_DEFAULT_PERMISSIONS),
            nullable=False,
        ),
    )
    op.create_table(
        "tool_executions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("tool_version", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'timed_out', 'denied', 'rejected')",
            name="ck_tool_executions_status",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tool_executions_owner_created_at",
        "tool_executions",
        ["owner_id", "created_at"],
    )
    op.create_table(
        "notification_drafts",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="draft",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("status = 'draft'", name="ck_notification_drafts_status"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_drafts_owner_created_at",
        "notification_drafts",
        ["owner_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_drafts_owner_created_at",
        table_name="notification_drafts",
    )
    op.drop_table("notification_drafts")
    op.drop_index(
        "ix_tool_executions_owner_created_at",
        table_name="tool_executions",
    )
    op.drop_table("tool_executions")
    op.drop_column("users", "tool_permissions")
