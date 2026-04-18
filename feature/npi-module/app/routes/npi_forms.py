"""NPI 模組路由 — RFQ + NPI 兩階段流程

角色對應：
  sales       → 建單 / 業務補充 / 成本分析 / 發送客戶報價 / 提供最終版
  engineer    → 排製程、派發供應商詢價、回填報價、選供應商、開 ERP 模具請購單
  eng_mgr     → CC 知悉
  bu          → 核准 NPI 成案 / 退回
  purchase    → 採購議價回填
  admin       → 全權限
"""
import os, uuid, json, mimetypes
from urllib.parse import quote as urlquote
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.user import User, Role
from app.models.supplier import Supplier
from app.models.npi_form import (
    NPIForm, NPIDocument, NPIApproval, NPISupplierInvite,
    NPIFormStatus, NPIStage,
)
from app.services.auth import get_current_user
import app.services.npi_notification as notif

router    = APIRouter(prefix="/npi-forms")
templates = Jinja2Templates(directory="app/templates")
UPLOAD_BASE = "uploads"

# 可建單角色
_SALES_ROLES = (Role.SALES, Role.ADMIN)
_ENG_ROLES   = (Role.ENGINEER, Role.ADMIN)
_BU_ROLES    = (Role.BU, Role.ADMIN)
_PURCHASE_ROLES = (Role.PURCHASE, Role.ADMIN)

ATTACH_CATEGORIES = [
    "客戶詢價信", "規格書", "圖面",
    "供應商報價", "成本分析表", "客戶報價單",
    "模具請購單", "議價記錄", "其它",
]


# ── helpers ─────────────────────────────────────

def _upload_dir(form_pk: int) -> str:
    path = os.path.join(UPLOAD_BASE, f"npi_{form_pk}")
    os.makedirs(path, exist_ok=True)
    return path


async def _gen_form_id(db: AsyncSession) -> str:
    today  = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"NPI-{today}-"
    r = await db.execute(select(func.count()).where(NPIForm.form_id.like(f"{prefix}%")))
    n = r.scalar() or 0
    return f"{prefix}{str(n + 1).zfill(3)}"


async def _get_form_or_404(form_id: str, db: AsyncSession) -> NPIForm:
    r = await db.execute(
        select(NPIForm).where(NPIForm.form_id == form_id).options(
            selectinload(NPIForm.creator),
            selectinload(NPIForm.assigned_eng),
            selectinload(NPIForm.selected_quote_supplier),
            selectinload(NPIForm.invites).selectinload(NPISupplierInvite.supplier),
            selectinload(NPIForm.documents).selectinload(NPIDocument.uploader),
            selectinload(NPIForm.approvals).selectinload(NPIApproval.approver),
        )
    )
    form = r.scalars().first()
    if not form:
        raise HTTPException(status_code=404, detail="找不到此 NPI 單")
    return form


def _docs_by_cat(docs):
    out = {}
    for d in docs:
        out.setdefault(d.category or "其它", []).append(d)
    return out


async def _save_attachments(
    db: AsyncSession, form_pk: int, user_id: int,
    files: List[UploadFile], categories,
    invite_id: int | None = None,
):
    upload_dir = _upload_dir(form_pk)
    if isinstance(categories, str):
        cat_list = [c.strip() for c in categories.split(",")] if categories else []
    else:
        cat_list = list(categories)
    for idx, up in enumerate(files):
        if not up.filename:
            continue
        content = await up.read()
        if not content:
            continue
        ext = os.path.splitext(up.filename)[1] or ".bin"
        saved = f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(upload_dir, saved), "wb") as f:
            f.write(content)
        db.add(NPIDocument(
            form_id_fk    = form_pk,
            invite_id_fk  = invite_id,
            filename      = saved,
            original_name = up.filename,
            category      = cat_list[idx] if idx < len(cat_list) else "其它",
            uploaded_by   = user_id,
        ))


def _log_approval(form: NPIForm, user: User, action: str,
                  from_s: NPIFormStatus, to_s: NPIFormStatus,
                  comment: str = "", reject_target: str | None = None):
    return NPIApproval(
        form_id_fk=form.id, approver_id=user.id, action=action,
        comment=comment or None, reject_target=reject_target,
        from_status=from_s.value, to_status=to_s.value,
    )


# ── 列表 ────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def list_npi(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = (
        select(NPIForm)
        .options(selectinload(NPIForm.creator))
        .order_by(NPIForm.created_at.desc())
    )
    u = current_user
    if u.role in (Role.ADMIN, Role.BU, Role.ENG_MGR):
        pass
    elif u.role == Role.SALES:
        q = q.where(or_(
            NPIForm.created_by == u.id,
            NPIForm.status.in_([
                NPIFormStatus.QUOTES_COLLECTED,
                NPIFormStatus.RFQ_DONE,
                NPIFormStatus.CLOSED,
            ]),
        ))
    elif u.role == Role.ENGINEER:
        q = q.where(or_(
            NPIForm.status.in_([
                NPIFormStatus.ENG_DISPATCH,
                NPIFormStatus.QUOTING,
                NPIFormStatus.QUOTES_COLLECTED,
                NPIFormStatus.NPI_STARTED,
            ]),
            and_(NPIForm.status == NPIFormStatus.RETURNED,
                 NPIForm.reject_to.in_(["工程師", "業務"])),
        ))
    elif u.role == Role.PURCHASE:
        q = q.where(NPIForm.status == NPIFormStatus.NPI_PENDING_PURCHASE)
    else:
        q = q.where(NPIForm.created_by == u.id)
    r = await db.execute(q)
    forms = r.scalars().all()
    return templates.TemplateResponse("npi_forms/list.html", {
        "request": request, "user": current_user,
        "forms": forms, "NPIFormStatus": NPIFormStatus, "NPIStage": NPIStage,
    })


# ── 新建 GET / POST（業務建立） ─────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_npi_page(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in _SALES_ROLES:
        raise HTTPException(status_code=403, detail="只有業務可以建立 NPI 單")
    return templates.TemplateResponse("npi_forms/new.html", {
        "request": request, "user": current_user,
        "ATTACH_CATEGORIES": ATTACH_CATEGORIES,
    })


@router.post("/new")
async def create_npi(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    customer_name:    str  = Form(...),
    customer_contact: str  = Form(""),
    customer_email:   str  = Form(""),
    product_name:     str  = Form(...),
    product_model:    str  = Form(""),
    spec_summary:     str  = Form(""),
    target_price:     str  = Form(""),
    annual_qty:       str  = Form(""),
    rfq_due_date:     str  = Form(""),
    bu:               str  = Form(""),
    sales_note:       str  = Form(""),
    attach_files:       List[UploadFile] = File(default=[]),
    attach_categories:  List[str] = Form(default=[]),
):
    if current_user.role not in _SALES_ROLES:
        raise HTTPException(status_code=403)
    form = NPIForm(
        form_id          = await _gen_form_id(db),
        stage            = NPIStage.RFQ,
        status           = NPIFormStatus.DRAFT,
        customer_name    = customer_name.strip(),
        customer_contact = customer_contact or None,
        customer_email   = customer_email or None,
        product_name     = product_name.strip(),
        product_model    = product_model or None,
        spec_summary     = spec_summary or None,
        target_price     = float(target_price) if target_price else None,
        annual_qty       = int(annual_qty) if annual_qty.isdigit() else None,
        rfq_due_date     = rfq_due_date or None,
        bu               = bu or None,
        sales_note       = sales_note or None,
        created_by       = current_user.id,
    )
    db.add(form)
    await db.flush()
    await _save_attachments(db, form.id, current_user.id, attach_files, attach_categories)
    await db.commit()
    return RedirectResponse(url=f"/npi-forms/{form.form_id}", status_code=303)


# ── 編輯（限草稿/退回 + 建單者）─────────────

@router.get("/{form_id}/edit", response_class=HTMLResponse)
async def edit_npi_page(
    form_id: str, request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await _get_form_or_404(form_id, db)
    if form.status not in (NPIFormStatus.DRAFT, NPIFormStatus.RETURNED):
        raise HTTPException(status_code=403, detail="目前狀態不允許編輯")
    if current_user.role != Role.ADMIN and form.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="只有建單者可編輯")
    return templates.TemplateResponse("npi_forms/edit.html", {
        "request": request, "user": current_user, "form": form,
        "docs_by_cat": _docs_by_cat(form.documents),
        "ATTACH_CATEGORIES": ATTACH_CATEGORIES,
    })


@router.post("/{form_id}/edit")
async def update_npi(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    customer_name:    str  = Form(""),
    customer_contact: str  = Form(""),
    customer_email:   str  = Form(""),
    product_name:     str  = Form(""),
    product_model:    str  = Form(""),
    spec_summary:     str  = Form(""),
    target_price:     str  = Form(""),
    annual_qty:       str  = Form(""),
    rfq_due_date:     str  = Form(""),
    bu:               str  = Form(""),
    sales_note:       str  = Form(""),
    attach_files:       List[UploadFile] = File(default=[]),
    attach_categories:  List[str] = Form(default=[]),
):
    form = await _get_form_or_404(form_id, db)
    if form.status not in (NPIFormStatus.DRAFT, NPIFormStatus.RETURNED):
        raise HTTPException(status_code=403)
    if current_user.role != Role.ADMIN and form.created_by != current_user.id:
        raise HTTPException(status_code=403)
    if customer_name: form.customer_name = customer_name
    form.customer_contact = customer_contact or None
    form.customer_email   = customer_email or None
    if product_name: form.product_name = product_name
    form.product_model    = product_model or None
    form.spec_summary     = spec_summary or None
    form.target_price     = float(target_price) if target_price else None
    form.annual_qty       = int(annual_qty) if annual_qty.isdigit() else None
    form.rfq_due_date     = rfq_due_date or None
    form.bu               = bu or None
    form.sales_note       = sales_note or None
    form.updated_at       = datetime.utcnow()
    await _save_attachments(db, form.id, current_user.id, attach_files, attach_categories)
    await db.commit()
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 刪除附件 ───────────────────────────────

@router.post("/{form_id}/delete-doc/{doc_id}")
async def delete_doc(
    form_id: str, doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await _get_form_or_404(form_id, db)
    if form.status in (NPIFormStatus.CLOSED,):
        raise HTTPException(status_code=403)
    doc = await db.get(NPIDocument, doc_id)
    if not doc or doc.form_id_fk != form.id:
        raise HTTPException(status_code=404)
    # 只有上傳者 / admin 可刪
    if current_user.role != Role.ADMIN and doc.uploaded_by != current_user.id:
        raise HTTPException(status_code=403, detail="只有上傳者可刪除")
    fp = os.path.join(UPLOAD_BASE, f"npi_{form.id}", doc.filename)
    if os.path.exists(fp):
        os.remove(fp)
    await db.delete(doc)
    await db.commit()
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 附件預覽 ────────────────────────────────

@router.get("/doc/preview/{doc_id}")
async def preview_doc(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    doc = await db.get(NPIDocument, doc_id)
    if not doc:
        raise HTTPException(status_code=404)
    fp = os.path.join(UPLOAD_BASE, f"npi_{doc.form_id_fk}", doc.filename)
    if not os.path.exists(fp):
        raise HTTPException(status_code=404)
    mime, _ = mimetypes.guess_type(doc.original_name)
    mime = mime or "application/octet-stream"
    encoded = urlquote(doc.original_name, encoding="utf-8")
    disp = "inline" if (mime.startswith("image/") or mime == "application/pdf") else "attachment"
    return FileResponse(fp, media_type=mime,
                        headers={"Content-Disposition": f"{disp}; filename*=UTF-8''{encoded}"})


# ── 業務送審 → 交工程 ─────────────────────────

@router.post("/{form_id}/submit-to-eng")
async def submit_to_eng(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status not in (NPIFormStatus.DRAFT, NPIFormStatus.RETURNED):
        raise HTTPException(status_code=400, detail="目前狀態不允許送審")
    if current_user.role != Role.ADMIN and form.created_by != current_user.id:
        raise HTTPException(status_code=403)
    # 送審前驗證必要附件：至少要有 客戶詢價信 + (規格書或圖面)
    cats = {d.category for d in form.documents}
    if "客戶詢價信" not in cats:
        raise HTTPException(status_code=400, detail="請先上傳【客戶詢價信】")
    if not (cats & {"規格書", "圖面"}):
        raise HTTPException(status_code=400, detail="請至少上傳【規格書】或【圖面】")
    old = form.status
    form.status = NPIFormStatus.ENG_DISPATCH
    form.reject_to = None
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "SUBMIT_TO_ENG", old, form.status, comment))
    await db.commit()
    await notif.notify_sales_submitted(db, form)
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 工程排製程 + 派發供應商 ────────────────────

@router.post("/{form_id}/dispatch")
async def dispatch_quotes(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    eng_process_note: str = Form(""),
    supplier_ids:     List[int] = Form(default=[]),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.ENG_DISPATCH:
        raise HTTPException(status_code=400, detail="目前狀態非待工程派發")
    if current_user.role not in _ENG_ROLES:
        raise HTTPException(status_code=403)
    if not supplier_ids:
        raise HTTPException(status_code=400, detail="請至少選擇一家供應商")

    # 寫入派發明細
    r = await db.execute(select(Supplier).where(Supplier.id.in_(supplier_ids), Supplier.is_active == True))
    sups = list(r.scalars().all())
    if not sups:
        raise HTTPException(status_code=400, detail="選擇的供應商無效或已停用")
    now = datetime.utcnow()
    for s in sups:
        db.add(NPISupplierInvite(
            form_id_fk=form.id, supplier_id=s.id,
            invited_at=now,
        ))

    old = form.status
    form.eng_process_note = eng_process_note or None
    form.assigned_eng_id = current_user.id
    form.status = NPIFormStatus.QUOTING
    form.updated_at = now
    db.add(_log_approval(form, current_user, "DISPATCH", old, form.status,
                         f"派發 {len(sups)} 家：" + ", ".join(s.name for s in sups)))
    await db.commit()

    # 清空 session identity map 再重讀，確保 selectinload 拿到剛寫入的 invites
    db.expire_all()
    form = await _get_form_or_404(form_id, db)
    await notif.notify_quotes_dispatched(db, form, list(form.invites))
    await db.commit()
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 工程回填供應商報價（代填）──────────────────

@router.post("/{form_id}/invite/{invite_id}/reply")
async def fill_invite_reply(
    form_id: str, invite_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    quote_amount:   str  = Form(""),
    lead_time_days: str  = Form(""),
    quote_comment:  str  = Form(""),
    attach_files:   List[UploadFile] = File(default=[]),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.QUOTING:
        raise HTTPException(status_code=400, detail="目前狀態非報價中")
    if current_user.role not in (Role.ENGINEER, Role.ADMIN):
        raise HTTPException(status_code=403)
    inv = next((i for i in form.invites if i.id == invite_id), None)
    if not inv:
        raise HTTPException(status_code=404, detail="找不到派發紀錄")
    inv.quote_amount   = float(quote_amount) if quote_amount else None
    inv.lead_time_days = int(lead_time_days) if lead_time_days.isdigit() else None
    inv.quote_comment  = quote_comment or None
    inv.replied_at     = datetime.utcnow()
    # 附件以「供應商報價」類別並綁 invite_id
    if attach_files:
        cats = ["供應商報價"] * len(attach_files)
        await _save_attachments(db, form.id, current_user.id, attach_files, cats, invite_id=inv.id)
    await db.commit()
    # 重新載入並觸發通知+NAS 歸檔
    form = await _get_form_or_404(form_id, db)
    new_inv = next((i for i in form.invites if i.id == invite_id), None)
    if new_inv:
        await notif.notify_quote_replied(db, form, new_inv)
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 工程宣告「報價收齊，交業務成本分析」─────

@router.post("/{form_id}/finish-quotes")
async def finish_quotes(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.QUOTING:
        raise HTTPException(status_code=400)
    if current_user.role not in _ENG_ROLES:
        raise HTTPException(status_code=403)
    if not any(i.replied_at for i in form.invites):
        raise HTTPException(status_code=400, detail="尚無任一家供應商回覆報價")
    old = form.status
    form.status = NPIFormStatus.QUOTES_COLLECTED
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "FINISH_QUOTES", old, form.status, comment))
    await db.commit()
    await notif._notify_roles(db, [Role.SALES],
                             f"【RFQ 報價已收齊】{form.form_id} - 請製作成本分析與客戶報價單")
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 業務完成成本分析 → 發客戶報價 ──────────

@router.post("/{form_id}/send-customer-quote")
async def send_customer_quote(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cost_analysis_note: str = Form(""),
    comment: str = Form(""),
    attach_files:      List[UploadFile] = File(default=[]),
    attach_categories: List[str] = Form(default=[]),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.QUOTES_COLLECTED:
        raise HTTPException(status_code=400)
    if current_user.role not in _SALES_ROLES:
        raise HTTPException(status_code=403)
    if attach_files:
        await _save_attachments(db, form.id, current_user.id, attach_files, attach_categories)
        await db.commit()
        db.expire_all()
        form = await _get_form_or_404(form_id, db)
    cats = {d.category for d in form.documents}
    if "成本分析表" not in cats:
        raise HTTPException(status_code=400, detail="請先上傳【成本分析表】")
    if "客戶報價單" not in cats:
        raise HTTPException(status_code=400, detail="請先上傳【客戶報價單】")
    old = form.status
    form.cost_analysis_note = cost_analysis_note or None
    form.status = NPIFormStatus.RFQ_DONE
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "SEND_CUSTOMER_QUOTE", old, form.status, comment))
    await db.commit()
    await notif.notify_sales_cost_analysis_done(db, form)
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 業務宣告「客戶確定開發」→ 進入 NPI 階段 ─

@router.post("/{form_id}/start-npi")
async def start_npi(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.RFQ_DONE:
        raise HTTPException(status_code=400, detail="客戶尚未到 RFQ 結束")
    if current_user.role not in _SALES_ROLES:
        raise HTTPException(status_code=403)
    old = form.status
    form.stage  = NPIStage.NPI
    form.status = NPIFormStatus.NPI_STARTED
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "START_NPI", old, form.status, comment))
    await db.commit()
    await notif.notify_npi_started(db, form)
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 工程選供應商 + 開 ERP 模具請購單 → 送 BU ──

@router.post("/{form_id}/submit-bu")
async def submit_bu(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    selected_invite_id: int = Form(...),
    erp_req_no:     str  = Form(""),
    erp_req_data:   str  = Form(""),   # JSON snapshot
    mould_cost_est: str  = Form(""),
    comment:        str  = Form(""),
    attach_files:      List[UploadFile] = File(default=[]),
    attach_categories: List[str] = Form(default=[]),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.NPI_STARTED:
        raise HTTPException(status_code=400)
    if current_user.role not in _ENG_ROLES:
        raise HTTPException(status_code=403)
    inv = next((i for i in form.invites if i.id == selected_invite_id), None)
    if not inv or not inv.replied_at:
        raise HTTPException(status_code=400, detail="請選擇一家已報價的供應商")
    if attach_files:
        await _save_attachments(db, form.id, current_user.id, attach_files, attach_categories)
        await db.commit()
        db.expire_all()
        form = await _get_form_or_404(form_id, db)
        inv = next((i for i in form.invites if i.id == selected_invite_id), None)
    # 送審前驗證附件
    cats = {d.category for d in form.documents}
    if "模具請購單" not in cats:
        raise HTTPException(status_code=400, detail="請上傳【模具請購單】")

    for i in form.invites:
        i.is_selected = (i.id == selected_invite_id)
    form.selected_quote_supplier_id = inv.supplier_id
    form.erp_req_no     = erp_req_no or None
    form.erp_req_data   = erp_req_data or None
    form.mould_cost_est = float(mould_cost_est) if mould_cost_est else None

    old = form.status
    form.status = NPIFormStatus.NPI_PENDING_BU
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "SUBMIT_BU", old, form.status, comment))
    await db.commit()
    await notif.notify_npi_submit_bu(db, form)
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── BU 核准 ────────────────────────────────

@router.post("/{form_id}/approve")
async def approve_bu(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.NPI_PENDING_BU:
        raise HTTPException(status_code=400)
    if current_user.role not in _BU_ROLES:
        raise HTTPException(status_code=403)
    old = form.status
    form.status = NPIFormStatus.NPI_PENDING_PURCHASE
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "APPROVE", old, form.status, comment))
    await db.commit()
    await notif.notify_npi_approved(db, form)
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── BU 退回 ────────────────────────────────

@router.post("/{form_id}/reject")
async def reject_bu(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment:       str = Form(...),
    reject_target: str = Form("工程師"),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.NPI_PENDING_BU:
        raise HTTPException(status_code=400)
    if current_user.role not in _BU_ROLES:
        raise HTTPException(status_code=403)
    if not comment.strip():
        raise HTTPException(status_code=400, detail="退回原因不得為空")
    old = form.status
    form.status = NPIFormStatus.RETURNED
    form.reject_to = reject_target
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "REJECT", old, form.status,
                         comment.strip(), reject_target=reject_target))
    await db.commit()
    await notif.notify_npi_rejected(db, form, reject_target)
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 退回重送（工程師）──────────────────────

@router.post("/{form_id}/resubmit")
async def resubmit(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.RETURNED:
        raise HTTPException(status_code=400)
    # 工程師退回 → 直接重送 BU；業務退回 → 回到業務草稿/成本分析
    if form.reject_to == "工程師":
        if current_user.role not in _ENG_ROLES:
            raise HTTPException(status_code=403)
        target = NPIFormStatus.NPI_PENDING_BU
    else:
        if current_user.role != Role.ADMIN and form.created_by != current_user.id:
            raise HTTPException(status_code=403)
        target = NPIFormStatus.QUOTES_COLLECTED
    old = form.status
    form.status = target
    form.reject_to = None
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "RESUBMIT", old, form.status, comment))
    await db.commit()
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 採購議價回填 + 結案 ────────────────────

@router.post("/{form_id}/purchase-close")
async def purchase_close(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    mould_cost_final: str = Form(...),
    purchase_note:    str = Form(""),
    attach_files:     List[UploadFile] = File(default=[]),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.NPI_PENDING_PURCHASE:
        raise HTTPException(status_code=400)
    if current_user.role not in _PURCHASE_ROLES:
        raise HTTPException(status_code=403)
    if not mould_cost_final:
        raise HTTPException(status_code=400, detail="請填入議價後模具成本")
    form.mould_cost_final = float(mould_cost_final)
    form.purchase_note    = purchase_note or None
    if attach_files:
        cats = ["議價記錄"] * len(attach_files)
        await _save_attachments(db, form.id, current_user.id, attach_files, cats)

    old = form.status
    form.status = NPIFormStatus.CLOSED
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "PURCHASE_CLOSE", old, form.status,
                         f"議價後成本 {form.mould_cost_final} / {purchase_note}"))
    await db.commit()
    db.expire_all()
    form = await _get_form_or_404(form_id, db)
    await notif.notify_npi_closed(db, form)
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 詳細頁（必須放最後，避免 path 衝突）─────

@router.get("/{form_id}", response_class=HTMLResponse)
async def detail_npi(
    form_id: str, request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await _get_form_or_404(form_id, db)
    # 列可選供應商（派發時用）
    r = await db.execute(select(Supplier).where(Supplier.is_active == True).order_by(Supplier.type, Supplier.name))
    suppliers = list(r.scalars().all())
    erp_req_rows = []
    if form.erp_req_data:
        try:
            erp_req_rows = json.loads(form.erp_req_data)
        except Exception:
            pass
    transition_combo = {
        "DRAFT→ENG_DISPATCH":              ("送審交工程",   "primary"),
        "RETURNED→NPI_PENDING_BU":         ("工程重送BU",  "primary"),
        "RETURNED→QUOTES_COLLECTED":       ("業務重送",    "primary"),
        "ENG_DISPATCH→QUOTING":            ("派發供應商",  "info"),
        "QUOTING→QUOTES_COLLECTED":        ("報價收齊",    "info"),
        "QUOTES_COLLECTED→RFQ_DONE":       ("發送客戶報價","success"),
        "RFQ_DONE→NPI_STARTED":            ("客戶確定開發","warning"),
        "NPI_STARTED→NPI_PENDING_BU":      ("送 BU 核准",  "warning"),
        "NPI_PENDING_BU→NPI_PENDING_PURCHASE": ("BU 核准",  "success"),
        "NPI_PENDING_BU→RETURNED":         ("BU 退回",     "danger"),
        "NPI_PENDING_PURCHASE→CLOSED":     ("採購結案",    "dark"),
    }
    return templates.TemplateResponse("npi_forms/detail.html", {
        "request": request, "user": current_user, "form": form,
        "docs_by_cat": _docs_by_cat(form.documents),
        "transition_combo": transition_combo,
        "NPIFormStatus": NPIFormStatus, "NPIStage": NPIStage,
        "suppliers": suppliers,
        "ATTACH_CATEGORIES": ATTACH_CATEGORIES,
        "erp_req_rows": erp_req_rows,
    })


# ── 手動跟催入口（admin 測試用）─────────────

@router.post("/_run-reminders")
async def run_reminders(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != Role.ADMIN:
        raise HTTPException(status_code=403)
    n = await notif.auto_remind_non_responders(db)
    return {"reminders_sent": n}
