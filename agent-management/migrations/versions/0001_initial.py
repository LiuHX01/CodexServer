"""Initial management records. Schema frozen in this migration, not imported from live models."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def record():
    return [
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        "agents",
        *record(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("base_url", sa.String(2000), nullable=False),
        sa.Column("credential", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("connection", sa.String(30), nullable=False),
        sa.Column("instance_id", sa.String(100)),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("remote_status", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
    )
    op.create_table(
        "sessions",
        *record(),
        sa.Column("agent_id", sa.String(32), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("provider_thread_id", sa.String(100)),
        sa.Column("source_instance_id", sa.String(100)),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("cwd", sa.Text()),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("observer_owner", sa.String(32)),
        sa.Column("observer_until", sa.DateTime(timezone=True)),
        sa.Column("event_cursor", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.UniqueConstraint("agent_id", "provider_thread_id"),
    )
    op.create_index("ix_sessions_agent_id", "sessions", ["agent_id"])
    op.create_table(
        "session_members",
        sa.Column("session_id", sa.String(32), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("principal", sa.String(100), primary_key=True),
        sa.Column("permission", sa.String(20), nullable=False),
    )
    op.create_table(
        "runs",
        *record(),
        sa.Column("session_id", sa.String(32), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("provider_run_id", sa.String(100), nullable=False),
        sa.Column("provider_turn_id", sa.String(100)),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("final_response", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("session_id", "provider_run_id"),
    )
    op.create_index("ix_runs_session_id", "runs", ["session_id"])
    op.create_table(
        "requests",
        *record(),
        sa.Column("session_id", sa.String(32), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("provider_request_id", sa.String(100), nullable=False),
        sa.Column("provider_run_id", sa.String(100)),
        sa.Column("method", sa.String(150), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("response", sa.JSON()),
        sa.Column("reason", sa.Text()),
        sa.Column("answered_by", sa.String(100)),
        sa.UniqueConstraint("session_id", "provider_request_id"),
    )
    op.create_index("ix_requests_session_id", "requests", ["session_id"])
    op.create_table(
        "commands",
        *record(),
        sa.Column("session_id", sa.String(32), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.UniqueConstraint("actor", "idempotency_key"),
    )
    op.create_index("ix_commands_session_id", "commands", ["session_id"])
    op.create_index("commands_queue", "commands", ["status", "created_at"])
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(32), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("source_instance_id", sa.String(100)),
        sa.Column("provider_event_id", sa.Integer()),
        sa.Column("type", sa.String(160), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "source_instance_id", "provider_event_id"),
    )
    op.create_index("events_session_cursor", "events", ["session_id", "id"])


def downgrade():
    for table in [
        "events",
        "commands",
        "requests",
        "runs",
        "session_members",
        "sessions",
        "agents",
    ]:
        op.drop_table(table)
