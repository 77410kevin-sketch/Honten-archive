from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, text

from app.database import engine, Base, AsyncSessionLocal
from app.models.user import User, Role, BU
from app.models.pcn_form import PCNForm, PCNDocument, PCNApproval
from app.models.supplier import Supplier, SupplierType
from app.models.customer import Customer
from app.models.npi_form import NPIForm, NPIDocument, NPIApproval, NPISupplierInvite
from app.models.qc_exception import (
    QCException, QCExceptionDocument, QCExceptionApproval,
)
from app.services.auth import hash_password
from app.routes import (
    auth, pcn_forms, drawing_checker, npi_forms, suppliers, customers, title_block,
    qc_exceptions,
)


# ── Seed 初始資料 ────────────────────────────────

async def seed_users():
    """建立測試帳號"""
    USERS = [
        {"username": "admin",      "display_name": "系統管理員",  "role": Role.ADMIN,     "bu": None},
        {"username": "eng01",      "display_name": "王工程師",    "role": Role.ENGINEER,  "bu": BU.ENERGY},
        {"username": "qa01",       "display_name": "李品保",      "role": Role.QC,        "bu": None},
        {"username": "ipqc",       "display_name": "品保（IQC/IPQC/OQC 共用）", "role": Role.QC, "bu": None},
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


async def seed_suppliers():
    """建立範例供應商主檔（用於 NPI 詢價派發）"""
    SUPPLIERS = [
        {"name": "新北方模具", "type": SupplierType.EXTERNAL, "contact": "王經理", "email": "w@sd.com.tw", "phone": "02-1234-5678"},
        {"name": "豐隆精密", "type": SupplierType.EXTERNAL, "contact": "李廠長", "email": "li@fx.com.tw", "phone": "03-2345-6789"},
        {"name": "昌泰五金", "type": SupplierType.EXTERNAL, "contact": "陳業務", "email": "chen@ct.com.tw", "phone": "04-3456-7890"},
        {"name": "久盛塑膠", "type": SupplierType.EXTERNAL, "contact": "張經理", "email": "js@jiusheng.com.tw", "phone": "02-4567-8901"},
        {"name": "機加工課", "type": SupplierType.INTERNAL, "contact": "陳課長", "email": "mach@honten.local", "phone": "分機 2301"},
        {"name": "模具課", "type": SupplierType.INTERNAL, "contact": "林課長", "email": "mold@honten.local", "phone": "分機 2401"},
    ]
    async with AsyncSessionLocal() as db:
        for s in SUPPLIERS:
            existing = await db.execute(select(Supplier).where(Supplier.name == s["name"]))
            if not existing.scalars().first():
                db.add(Supplier(
                    name=s["name"],
                    type=s["type"],
                    contact=s["contact"],
                    email=s["email"],
                    phone=s["phone"],
                    is_active=True,
                ))
        await db.commit()
    print("✅ 範例供應商建立完成")


async def run_migrations():
    """補齊新欄位（ALTER TABLE IF NOT EXISTS 等效）"""
    migrations = [
        # NPI 報價試算欄位
        "ALTER TABLE npi_forms ADD COLUMN quote_cost_data TEXT",
        "ALTER TABLE npi_forms ADD COLUMN quoted_unit_price FLOAT",
        "ALTER TABLE npi_forms ADD COLUMN bu_quote_note TEXT",
        # 派發明細欄位
        "ALTER TABLE npi_supplier_invites ADD COLUMN process_name VARCHAR(100)",
        "ALTER TABLE npi_supplier_invites ADD COLUMN material VARCHAR(100)",
        "ALTER TABLE npi_supplier_invites ADD COLUMN qty INTEGER",
        "ALTER TABLE npi_supplier_invites ADD COLUMN expected_lead_days INTEGER",
        "ALTER TABLE npi_supplier_invites ADD COLUMN drawing_doc_id INTEGER",
        "ALTER TABLE npi_supplier_invites ADD COLUMN tooling_cost FLOAT",
        # 階梯式 MOQ 報價（JSON：[{"qty":100,"price":500}, {"qty":500,"price":450}]）
        "ALTER TABLE npi_supplier_invites ADD COLUMN tier_data TEXT",
        # NPI 業務工作區 — 每張圖 T1 試模計畫 JSON
        "ALTER TABLE npi_forms ADD COLUMN t1_plan_data TEXT",
        # NPI 工程工作區 — 每站廠內料號/是否走途程 JSON
        "ALTER TABLE npi_forms ADD COLUMN eng_process_data TEXT",
        # NPI 採購議價覆寫（價格/模治具）JSON
        "ALTER TABLE npi_forms ADD COLUMN bargain_data TEXT",
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
        # QC 異常表新增欄位
        "ALTER TABLE qc_exceptions ADD COLUMN doc_type VARCHAR(20)",
        "ALTER TABLE qc_exceptions ADD COLUMN event_date_type VARCHAR(20)",
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
    await seed_suppliers()
    # 初始化圖面量測檢表 DB
    drawing_checker.init()
    yield


# ── App ──────────────────────────────────────────

app = FastAPI(title="HonTen PCN/ECN Demo", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key="honten-demo-secret-2026")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


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
app.include_router(drawing_checker.router)
app.include_router(npi_forms.router)
app.include_router(suppliers.router)
app.include_router(customers.router)
app.include_router(title_block.router)
app.include_router(qc_exceptions.router)


@app.get("/")
async def root(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/pcn-forms/")
