from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, text

from app.database import engine, Base, AsyncSessionLocal
from app.models.user import User, Role, BU
from app.models.pcn_form import PCNForm, PCNDocument, PCNApproval
from app.services.auth import hash_password
from app.routes import auth, pcn_forms


# ── Seed 初始資料 ────────────────────────────────

async def seed_users():
    """建立測試帳號"""
    USERS = [
        {"username": "admin",      "display_name": "系統管理員",  "role": Role.ADMIN,     "bu": None},
        {"username": "eng01",      "display_name": "王工程師",    "role": Role.ENGINEER,  "bu": BU.ENERGY},
        {"username": "qa01",       "display_name": "李品保",      "role": Role.QC,        "bu": None},
        {"username": "pd01",       "display_name": "張產線主管",  "role": Role.PROD_MGR,  "bu": None},
        {"username": "bh01",       "display_name": "陳BU主管",    "role": Role.BU,        "bu": BU.ENERGY},
        {"username": "engmgr",     "display_name": "林工程主管",  "role": Role.ENG_MGR,   "bu": None},
        {"username": "pc01",       "display_name": "黃採購",      "role": Role.PURCHASE,  "bu": None},
        {"username": "wh01",       "display_name": "趙倉管",      "role": Role.WAREHOUSE, "bu": None},
        {"username": "asst01",     "display_name": "周業助",      "role": Role.ASSISTANT, "bu": None},
        {"username": "sales01",    "display_name": "吳業務",      "role": Role.SALES,     "bu": BU.ENERGY},
        {"username": "hr01",       "display_name": "鄭人事",      "role": Role.HR,        "bu": None},
    ]
    async with AsyncSessionLocal() as db:
        for u in USERS:
            existing = await db.execute(select(User).where(User.username == u["username"]))
            if not existing.scalars().first():
                db.add(User(
                    username=u["username"],
                    display_name=u["display_name"],
                    hashed_password=hash_password("ht1234"),
                    role=u["role"],
                    bu=u["bu"],
                    is_active=True,
                ))
        await db.commit()
    print("✅ 測試帳號建立完成")


async def run_migrations():
    """補齊新欄位（ALTER TABLE IF NOT EXISTS 等效）"""
    migrations = [
        # PCNApproval 退回對象欄位
        "ALTER TABLE pcn_approvals ADD COLUMN reject_target VARCHAR(50)",
        # ECN 設計變更庫存盤點
        "ALTER TABLE pcn_forms ADD COLUMN inventory_data TEXT",
        # 新增採購帳號 pc01（若舊 purchase01 存在則改名）
        "UPDATE users SET username='pc01' WHERE username='purchase01'",
        # PCNForm 退回對象欄位（供列表過濾）
        "ALTER TABLE pcn_forms ADD COLUMN reject_to VARCHAR(50)",
        # 帳號更名
        "UPDATE users SET username='qa01' WHERE username='qc01'",
        "UPDATE users SET username='pd01' WHERE username='prodmgr01'",
        "UPDATE users SET username='bh01' WHERE username='buhead'",
    ]
    async with engine.begin() as conn:
        for sql in migrations:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass  # 欄位已存在則忽略


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建立資料表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 補欄位 migration
    await run_migrations()
    # 植入測試資料
    await seed_users()
    yield


# ── App ──────────────────────────────────────────

app = FastAPI(title="HonTen PCN/ECN Demo", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key="honten-demo-secret-2026")


# ── DB 注入 Middleware ───────────────────────────

@app.middleware("http")
async def db_session_middleware(request: Request, call_next):
    async with AsyncSessionLocal() as db:
        request.state.db = db
        response = await call_next(request)
    return response


# ── 路由 ─────────────────────────────────────────

app.include_router(auth.router)
app.include_router(pcn_forms.router)


@app.get("/")
async def root(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/pcn-forms/")
