"""
NPI 模組通知服務（Demo 版 — 以 console log 模擬實際 LINE/SMTP 呼叫）

正式環境替換重點：
- _send_line_push: LINE Messaging API push / broadcast
- _send_mail: SMTP 或 Google Workspace API
- _copy_to_nas: mount NAS 路徑 shutil.copy 或透過 rsync
"""
import os
import shutil
import logging
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import User, Role
from app.models.npi_form import NPIForm, NPISupplierInvite, NPIDocument
from app.models.supplier import Supplier

logger = logging.getLogger(__name__)

# NAS 根路徑（正式環境從 .env 讀，Demo 寫本機）
NAS_ROOT = os.getenv("NPI_NAS_ROOT", "nas_npi")
UPLOAD_BASE = "uploads"
# 未回覆幾天後自動跟催
REMINDER_DAYS = int(os.getenv("NPI_REMINDER_DAYS", "2"))


# ── 低層 helpers ─────────────────────────────────

def _send_line_push(target: str, message: str):
    logger.info(f"[LINE] → {target}: {message}")
    print(f"\n📱 [LINE] {target}\n   {message}\n")


def _send_mail(to_addr: str, subject: str, body: str, attachments: Iterable[str] = ()):
    att = ", ".join(os.path.basename(a) for a in attachments) or "—"
    logger.info(f"[MAIL] → {to_addr} | {subject} | att={att}")
    print(f"\n📧 [MAIL] To: {to_addr}\n   Subject: {subject}\n   Attachments: {att}\n   Body: {body[:120]}...\n")


async def _users_by_role(db: AsyncSession, role: Role) -> list[User]:
    r = await db.execute(select(User).where(User.role == role, User.is_active == True))
    return list(r.scalars().all())


async def _notify_roles(db: AsyncSession, roles: Iterable[Role], message: str):
    for role in roles:
        for u in await _users_by_role(db, role):
            tag = u.line_user_id or f"{u.display_name}({u.role.value})"
            _send_line_push(tag, message)


# ── NPI 流程節點通知 ─────────────────────────────

async def notify_sales_submitted(db: AsyncSession, form: NPIForm):
    """業務 DRAFT → ENG_DISPATCH：通知工程"""
    msg = f"【NPI-RFQ 待工程排製程】{form.form_id} - {form.customer_name}/{form.product_name}"
    await _notify_roles(db, [Role.ENGINEER, Role.ENG_MGR], msg)


async def notify_quotes_dispatched(db: AsyncSession, form: NPIForm, invites: list[NPISupplierInvite]):
    """工程派發詢價：向每家供應商寄 mail（第一次）+ 通知業務"""
    for inv in invites:
        sup: Supplier | None = inv.supplier
        if not sup or not sup.email:
            logger.warning(f"Supplier {inv.supplier_id} 沒有 email，略過")
            continue
        subject = f"【鴻騰電子 RFQ 詢價】{form.form_id} - {form.product_name}"
        body = (
            f"您好 {sup.contact or ''}，\n"
            f"請針對本案提供報價與交期。\n"
            f"客戶：{form.customer_name}\n"
            f"產品：{form.product_name} / 型號：{form.product_model or '—'}\n"
            f"規格摘要：{form.spec_summary or '—'}\n"
            f"年需量：{form.annual_qty or '—'}  /  客戶回覆期限：{form.rfq_due_date or '—'}\n\n"
            f"請於 2 個工作天內回覆，否則系統將自動發信跟催。"
        )
        # 附件：圖面、規格書
        att_paths = []
        for d in form.documents:
            if d.category in ("圖面", "規格書"):
                att_paths.append(os.path.join(UPLOAD_BASE, f"npi_{form.id}", d.filename))
        _send_mail(sup.email, subject, body, att_paths)
        inv.first_sent_at = datetime.utcnow()
    # 業務 + 工程主管
    await _notify_roles(
        db, [Role.SALES, Role.ENG_MGR],
        f"【NPI-RFQ 已派發詢價】{form.form_id} 共 {len(invites)} 家供應商",
    )


async def notify_remind_overdue(db: AsyncSession, form: NPIForm, inv: NPISupplierInvite):
    """2 天未回覆自動跟催（由定時任務觸發）"""
    sup = inv.supplier
    if not sup or not sup.email:
        return
    subject = f"【跟催 RFQ 報價】{form.form_id} - {form.product_name}"
    body = (
        f"您好 {sup.contact or ''}，\n"
        f"本案詢價信已於 {inv.first_sent_at:%Y-%m-%d} 發出，尚未收到報價回覆，\n"
        f"煩請儘速提供報價，謝謝。"
    )
    _send_mail(sup.email, subject, body)
    inv.last_reminder_at = datetime.utcnow()
    inv.reminder_count = (inv.reminder_count or 0) + 1


async def auto_remind_non_responders(db: AsyncSession) -> int:
    """排程入口：掃全部 QUOTING 狀態的單，對超過 REMINDER_DAYS 未回的供應商發跟催信"""
    from app.models.npi_form import NPIFormStatus
    now = datetime.utcnow()
    r = await db.execute(
        select(NPIForm)
        .where(NPIForm.status == NPIFormStatus.QUOTING)
        .options(selectinload(NPIForm.invites).selectinload(NPISupplierInvite.supplier),
                 selectinload(NPIForm.documents))
    )
    forms = r.scalars().all()
    sent = 0
    for f in forms:
        for inv in f.invites:
            if inv.replied_at:
                continue
            if not inv.first_sent_at:
                continue
            last = inv.last_reminder_at or inv.first_sent_at
            if now - last >= timedelta(days=REMINDER_DAYS):
                await notify_remind_overdue(db, f, inv)
                sent += 1
    if sent:
        await db.commit()
    return sent


async def notify_quote_replied(db: AsyncSession, form: NPIForm, inv: NPISupplierInvite):
    """供應商回覆報價後 → 通知工程 + 業務；報價檔自動落 NAS"""
    # 將報價附件複製到 NAS
    nas_dir = _ensure_nas_dir(form, "RFQ_Quotes")
    _copy_invite_quote_files_to_nas(form, inv, nas_dir)
    sup_name = inv.supplier.name if inv.supplier else "(未指定)"
    msg = f"【RFQ 供應商回覆】{form.form_id} - {sup_name} 金額 {inv.quote_amount or '—'}"
    await _notify_roles(db, [Role.ENGINEER, Role.SALES], msg)


async def notify_sales_cost_analysis_done(db: AsyncSession, form: NPIForm):
    """業務完成成本分析與客戶報價單 → 發送客戶 mail"""
    if form.customer_email:
        subject = f"【鴻騰電子 RFQ 報價回覆】{form.form_id} - {form.product_name}"
        body = (
            f"{form.customer_name} 您好，\n\n"
            f"針對貴司詢價案件，謹附上本公司之成本分析與正式報價單，敬請查收。\n"
            f"如有任何問題請隨時告知。\n\n"
            f"鴻騰電子 業務部 敬上"
        )
        att = []
        for d in form.documents:
            if d.category in ("成本分析表", "客戶報價單"):
                att.append(os.path.join(UPLOAD_BASE, f"npi_{form.id}", d.filename))
        _send_mail(form.customer_email, subject, body, att)
    await _notify_roles(db, [Role.BU, Role.SALES, Role.ENGINEER],
                       f"【RFQ 已發送客戶報價】{form.form_id} - {form.customer_name}")


async def notify_npi_started(db: AsyncSession, form: NPIForm):
    """客戶確定開發 → 通知工程選供應商並開 ERP 模具請購單"""
    await _notify_roles(
        db, [Role.ENGINEER, Role.ENG_MGR],
        f"【NPI 成案啟動】{form.form_id} - 請工程依成本分析選供應商並開 ERP 模具請購單",
    )


async def notify_npi_submit_bu(db: AsyncSession, form: NPIForm):
    """工程送 BU 核准"""
    await _notify_roles(
        db, [Role.BU],
        f"【NPI 待 BU 核准】{form.form_id} - {form.customer_name}/{form.product_name} 預估模具成本 {form.mould_cost_est or '—'}",
    )


async def notify_npi_approved(db: AsyncSession, form: NPIForm):
    """BU 核准 NPI → 通知採購議價"""
    await _notify_roles(
        db, [Role.PURCHASE],
        f"【NPI 待採購議價】{form.form_id} - 預估模具成本 {form.mould_cost_est or '—'}",
    )


async def notify_npi_rejected(db: AsyncSession, form: NPIForm, target: str):
    await _notify_roles(
        db, [Role.ENGINEER, Role.SALES],
        f"【NPI 退回 {target}】{form.form_id} - 請調整後重送",
    )


async def notify_npi_closed(db: AsyncSession, form: NPIForm):
    """結案：資料匯入 NAS，mail 業務 + BU 主管"""
    nas_dir = _ensure_nas_dir(form, "Closure")
    _copy_all_docs_to_nas(form, nas_dir)

    # mail 業務 + BU
    recipients: list[User] = []
    recipients += await _users_by_role(db, Role.SALES)
    recipients += await _users_by_role(db, Role.BU)
    subject = f"【NPI 結案通知】{form.form_id} - {form.customer_name}/{form.product_name}"
    body = (
        f"本案 NPI 已完成結案流程。\n"
        f"客戶：{form.customer_name}\n"
        f"產品：{form.product_name} / 型號：{form.product_model or '—'}\n"
        f"最終模具成本：{form.mould_cost_final or '—'}\n"
        f"NAS 資料夾：{nas_dir}\n"
    )
    for u in recipients:
        addr = (u.username + "@honten.local") if not getattr(u, "email", None) else u.email  # type: ignore
        _send_mail(addr, subject, body)

    await _notify_roles(db, [Role.SALES, Role.BU, Role.ENGINEER, Role.PURCHASE],
                       f"【NPI 已結案】{form.form_id} - 資料已匯入 NAS")


# ── NAS 匯出 ────────────────────────────────────

def _ensure_nas_dir(form: NPIForm, sub: str) -> str:
    path = os.path.join(NAS_ROOT, form.form_id, sub)
    os.makedirs(path, exist_ok=True)
    return path


def _copy_invite_quote_files_to_nas(form: NPIForm, inv: NPISupplierInvite, nas_dir: str):
    sup_name = (inv.supplier.name if inv.supplier else f"supplier_{inv.supplier_id}").replace("/", "_")
    for d in form.documents:
        if d.invite_id_fk != inv.id:
            continue
        src = os.path.join(UPLOAD_BASE, f"npi_{form.id}", d.filename)
        if not os.path.exists(src):
            continue
        dst = os.path.join(nas_dir, f"{sup_name}__{d.original_name}")
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            logger.warning(f"copy to NAS failed: {e}")


def _copy_all_docs_to_nas(form: NPIForm, nas_dir: str):
    for d in form.documents:
        src = os.path.join(UPLOAD_BASE, f"npi_{form.id}", d.filename)
        if not os.path.exists(src):
            continue
        cat = (d.category or "其它").replace("/", "_")
        sub = os.path.join(nas_dir, cat)
        os.makedirs(sub, exist_ok=True)
        try:
            shutil.copy2(src, os.path.join(sub, d.original_name))
        except Exception as e:
            logger.warning(f"copy to NAS failed: {e}")
