from sqlalchemy import select

from ..models import Agent, Session, now


async def inspect_agent(database, adapters, agent_id):
    async with database.transaction() as db:
        agent = await db.get(Agent, agent_id)
    adapter = adapters.open(agent)
    try:
        status, profile = await adapter.inspect()
        async with database.transaction() as db:
            current = await db.get(Agent, agent_id)
            source = profile.get("instance_id") or "legacy"
            if current.instance_id and current.instance_id != source:
                sessions = (
                    await db.scalars(select(Session).where(Session.agent_id == agent_id))
                ).all()
                for session in sessions:
                    session.state = "unknown"
                    session.error = "执行端数据实例发生变化，旧会话绑定已冻结，请人工核对"
            current.instance_id = source
            current.capabilities = profile
            current.remote_status = status
            current.connection = "online" if status.get("backend_ready") else "unavailable"
            current.observed_at, current.error = now(), None
    except Exception:
        async with database.transaction() as db:
            current = await db.get(Agent, agent_id)
            current.connection = "unreachable"
            current.error = "无法联系执行端，请检查地址、服务状态和凭证"
            current.observed_at = now()
    finally:
        await adapter.close()
