import hashlib, hmac
from fastapi import Request, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User

_SALT = "honten-demo-2026"


def hash_password(plain: str) -> str:
    return hmac.new(_SALT.encode(), plain.encode(), hashlib.sha256).hexdigest()


def verify_password(plain: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(plain), hashed)


async def get_current_user(request: Request) -> User:
    """從 session 取得目前登入使用者"""
    db: AsyncSession = request.state.db
    user_id = request.session.get("user_id")
    if not user_id:
        from fastapi.responses import RedirectResponse
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return user
