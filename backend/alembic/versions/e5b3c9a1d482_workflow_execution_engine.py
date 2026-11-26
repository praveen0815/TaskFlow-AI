"""workflow execution engine

Revision ID: e5b3c9a1d482
Revises: 4c7d91e2a6b0
Create Date: 2026-11-26 09:59:18.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5b3c9a1d482"
down_revision: Union[str, None] = "4c7d91e2a6b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.drop_constraint("ck_workflow_executions_status", type_="check")
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=120)))
        batch_op.add_column(sa.Column("request_sha256", sa.String(length=64)))
        batch_op.add_column(
            sa.Column(
                "trigger_data",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "checkpoint_data",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("error_code", sa.String(length=80)))
        batch_op.add_column(sa.Column("error_message", sa.Text()))
        batch_op.add_column(
            sa.Column(
                "active_duration_seconds",
                sa.Float(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("run_started_at", sa.DateTime(timezone=True)))

    with op.batch_alter_table("workflow_steps") as batch_op:
        batch_op.drop_constraint("ck_workflow_steps_status", type_="check")
        batch_op.add_column(
            sa.Column(
                "attempt_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("error_code", sa.String(length=80)))
        batch_op.add_column(sa.Column("error_message", sa.Text()))

    op.create_index(
        "ix_workflow_executions_status_heartbeat",
        "workflow_executions",
        ["status", "heartbeat_at"],
    )

    op.execute(
        "UPDATE workflow_executions SET status = 'completed' WHERE status = 'succeeded'"
    )
    op.execute("UPDATE workflow_steps SET status = 'completed' WHERE status = 'succeeded'")

    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.create_check_constraint(
            "ck_workflow_executions_status",
            "status IN ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled')",
        )
        batch_op.create_unique_constraint(
            "uq_workflow_executions_owner_idempotency",
            ["owner_id", "idempotency_key"],
        )

    with op.batch_alter_table("workflow_steps") as batch_op:
        batch_op.create_check_constraint(
            "ck_workflow_steps_status",
            "status IN ('pending', 'ready', 'running', 'waiting_approval', "
            "'completed', 'failed', 'skipped', 'cancelled')",
        )
        batch_op.create_check_constraint(
            "ck_workflow_steps_attempt_count",
            "attempt_count >= 0",
        )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_executions_status_heartbeat",
        table_name="workflow_executions",
    )
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.drop_constraint("ck_workflow_executions_status", type_="check")
        batch_op.drop_constraint(
            "uq_workflow_executions_owner_idempotency",
            type_="unique",
        )

    with op.batch_alter_table("workflow_steps") as batch_op:
        batch_op.drop_constraint("ck_workflow_steps_status", type_="check")
        batch_op.drop_constraint("ck_workflow_steps_attempt_count", type_="check")

    op.execute(
        "UPDATE workflow_executions SET status = 'succeeded' WHERE status = 'completed'"
    )
    op.execute(
        "UPDATE workflow_executions SET status = 'running' WHERE status = 'paused'"
    )
    op.execute(
        "UPDATE workflow_steps SET status = 'succeeded' WHERE status = 'completed'"
    )
    op.execute(
        "UPDATE workflow_steps SET status = 'pending' WHERE status = 'ready'"
    )
    op.execute(
        "UPDATE workflow_steps SET status = 'running' WHERE status = 'waiting_approval'"
    )
    op.execute(
        "UPDATE workflow_steps SET status = 'failed' WHERE status = 'cancelled'"
    )

    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.create_check_constraint(
            "ck_workflow_executions_status",
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
        )
        batch_op.drop_column("run_started_at")
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("active_duration_seconds")
        batch_op.drop_column("error_message")
        batch_op.drop_column("error_code")
        batch_op.drop_column("checkpoint_data")
        batch_op.drop_column("trigger_data")
        batch_op.drop_column("request_sha256")
        batch_op.drop_column("idempotency_key")

    with op.batch_alter_table("workflow_steps") as batch_op:
        batch_op.create_check_constraint(
            "ck_workflow_steps_status",
            "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
        )
        batch_op.drop_column("error_message")
        batch_op.drop_column("error_code")
        batch_op.drop_column("attempt_count")
