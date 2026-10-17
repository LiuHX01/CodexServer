import base64
import hashlib
import hmac
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select

from .models import Session, SessionMember


@dataclass(frozen=True)
class Principal:
    id: str
    role: str


class Security:
    def __init__(self, settings):
        self.settings = settings
        secret = settings.secret_key.get_secret_value()
        self.signer = URLSafeTimedSerializer(secret, salt="agentrelay-login-v1")
        self.vault = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))

    def authenticate_key(self, token: str) -> Principal:
        for key in self.settings.principals():
            if hmac.compare_digest(token.encode(), key.token.get_secret_value().encode()):
                return Principal(key.principal, key.role)
        raise HTTPException(401, "访问密钥无效")

    def authenticate(self, request: Request) -> Principal:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            return self.authenticate_key(authorization[7:])
        cookie = request.cookies.get("relay_session")
        if cookie:
            try:
                principal_id = self.signer.loads(cookie, max_age=8 * 3600)
                key = next(k for k in self.settings.principals() if k.principal == principal_id)
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    origin = request.headers.get("origin")
                    if origin and urlsplit(origin).netloc != request.headers.get("host"):
                        raise HTTPException(403, "拒绝跨站操作")
                return Principal(key.principal, key.role)
            except (BadSignature, SignatureExpired, StopIteration):
                pass
        raise HTTPException(401, "请先登录")

    def encrypt(self, value: str) -> str:
        return self.vault.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.vault.decrypt(value.encode()).decode()


def principal(request: Request):
    return request.app.state.security.authenticate(request)


def require_operator(actor: Principal):
    if actor.role not in {"admin", "operator"}:
        raise HTTPException(403, "当前身份只有查看权限")


def require_admin(actor: Principal):
    if actor.role != "admin":
        raise HTTPException(403, "此操作需要管理员权限")


async def authorize_session(db, session_id: str, actor: Principal, *, write=False):
    row = await db.get(Session, session_id)
    if row is None:
        raise HTTPException(404, "会话不存在")
    if write:
        require_operator(actor)
    if actor.role == "admin" or row.owner == actor.id:
        return row
    member = await db.scalar(
        select(SessionMember).where(
            SessionMember.session_id == session_id,
            SessionMember.principal == actor.id,
        )
    )
    if member is None or (write and member.permission != "operate"):
        raise HTTPException(403, "没有此会话的访问权限")
    return row
