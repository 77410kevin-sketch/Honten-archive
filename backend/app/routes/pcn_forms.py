"""
PCN/ECN 路由
流程：DRAFT → PENDING_QC → PENDING_PRODUCTION → PENDING_BU_APPROVAL → APPROVED / RETURNED → CLOSED
"""
import os, uuid, json
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.user import User, Role, BU
from app.models.pcn_form import PCNForm, PCNDocument, PCNApproval, PCNFormStatus, PCNType
from app.services.auth import get_current_user
import app.services.notification as notif

router    = APIRouter(prefix="/pcn-forms")
templates = Jinja2Templates(directory="app/templates")

UPLOAD_BASE = "uploads"


# ── 工具函式 ────────────────────────────────────

def _pcn_upload_dir(form_id: int) -> str:
    path = os.path.join(UPLOAD_BASE, f"pcn_{form_id}")
    os.makedirs(path, exist_ok=True)
    return path


async def _gen_form_id(db: AsyncSession, pcn_type: str) -> str:
    """產生流水號，格式：PCN-20260413-001 / ECN-20260413-001"""
    today = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"{pcn_type}-{today}-"
    result = await db.execute(
        select(func.count()).where(PCNForm.form_id.like(f"{prefix}%"))
    )
    count = result.scalar() or 0
    return f"{prefix}{str(count + 1).zfill(3)}"


async def _get_form_or_404(form_id: str, db: AsyncSession) -> PCNForm:
    result = await db.execute(
        select(PCNForm)
        .where(PCNForm.form_id == form_id)
        .options(
            selectinload(PCNForm.creator),
            selectinload(PCNForm.assigned_qc),
            selectinload(PCNForm.assigned_prod_mgr),
            selectinload(PCNForm.documents).selectinload(PCNDocument.uploader),
            selectinload(PCNForm.approvals).selectinload(PCNApproval.approver),
        )
    )
    form = result.scalars().first()
    if not form:
        raise HTTPException(status_code=404, detail="找不到此表單")
    return form


def _docs_by_category(documents):
    cats = {}
    for doc in documents:
        cats.setdefault(doc.category or "其它", []).append(doc)
    return cats


# ── 列表 ────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def list_pcn_forms(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(PCNForm).options(
        selectinload(PCNForm.creator),
        selectinload(PCNForm.assigned_qc),
        selectinload(PCNForm.assigned_prod_mgr),
    ).order_by(PCNForm.created_at.desc())

    role = current_user.role

    # 品保：只看指派給自己或 PENDING_QC 的單
    if role == Role.QC:
        q = q.where(
            (PCNForm.assigned_qc_id == current_user.id) |
            (PCNForm.status == PCNFormStatus.PENDING_QC)
        )
    # 產線主管：只看指派給自己或 PENDING_PRODUCTION 的單
    elif role == Role.PROD_MGR:
        q = q.where(
            (PCNForm.assigned_prod_mgr_id == current_user.id) |
            (PCNForm.status == PCNFormStatus.PENDING_PRODUCTION)
        )
    # BU Head：只看自己 BU
    elif role == Role.BU:
        q = q.where(PCNForm.bu == current_user.bu.value if current_user.bu else True)
    # 工程師：自己建的單
    elif role == Role.ENGINEER:
        q = q.where(PCNForm.created_by == current_user.id)
    # admin 看全部

    result = await db.execute(q)
    forms  = result.scalars().all()

    return templates.TemplateResponse("pcn_forms/list.html", {
        "request": request,
        "user":    current_user,
        "forms":   forms,
        "PCNFormStatus": PCNFormStatus,
    })


# ── 新建 GET ─────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_pcn_form_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (Role.ENGINEER, Role.ADMIN):
        raise HTTPException(status_code=403, detail="只有工程師可以建立 PCN/ECN")

    # 取得品保與產線主管列表供指派
    qc_result   = await db.execute(select(User).where(User.role == Role.QC, User.is_active == True))
    pm_result   = await db.execute(select(User).where(User.role == Role.PROD_MGR, User.is_active == True))
    qc_users    = qc_result.scalars().all()
    pm_users    = pm_result.scalars().all()

    return templates.TemplateResponse("pcn_forms/new.html", {
        "request":   request,
        "user":      current_user,
        "qc_users":  qc_users,
        "pm_users":  pm_users,
        "PCNType":   PCNType,
        "BU":        BU,
    })


# ── 新建 POST ────────────────────────────────────

@router.post("/new")
async def create_pcn_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    pcn_type:           str = Form(...),
    bu:                 str = Form(...),
    product_name:       str = Form(...),
    product_model:      str = Form(""),
    change_description: str = Form(...),
    change_reason:      str = Form(""),
    effective_date:     str = Form(""),
    assigned_qc_id:     str = Form(""),
    assigned_prod_mgr_id: str = Form(""),
    # 附件
    attach_files:       List[UploadFile] = File(default=[]),
    attach_categories:  str = Form(""),
):
    if current_user.role not in (Role.ENGINEER, Role.ADMIN):
        raise HTTPException(status_code=403, detail="只有工程師可以建立 PCN/ECN")

    form_id_str = await _gen_form_id(db, pcn_type)

    form = PCNForm(
        form_id             = form_id_str,
        type                = PCNType(pcn_type),
        status              = PCNFormStatus.DRAFT,
        bu                  = bu,
        product_name        = product_name,
        product_model       = product_model or None,
        change_description  = change_description,
        change_reason       = change_reason or None,
        effective_date      = effective_date or None,
        created_by          = current_user.id,
        assigned_qc_id      = int(assigned_qc_id) if assigned_qc_id else None,
        assigned_prod_mgr_id= int(assigned_prod_mgr_id) if assigned_prod_mgr_id else None,
    )
    db.add(form)
    await db.flush()  # 取得 form.id

    # 上傳附件
    upload_dir = _pcn_upload_dir(form.id)
    cat_list   = [c.strip() for c in attach_categories.split(",")] if attach_categories else []
    for idx, upload in enumerate(attach_files):
        if not upload.filename:
            continue
        ext      = os.path.splitext(upload.filename)[1]
        saved    = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(upload_dir, saved)
        with open(filepath, "wb") as f:
            f.write(await upload.read())
        doc = PCNDocument(
            form_id_fk   = form.id,
            filename     = saved,
            original_name= upload.filename,
            category     = cat_list[idx] if idx < len(cat_list) else "其它",
            uploaded_by  = current_user.id,
        )
        db.add(doc)

    await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id_str}", status_code=303)


# ── 附件預覽（路由必須在 /{form_id} 之前）────────

@router.get("/doc/preview/{doc_id}", response_class=FileResponse)
async def preview_pcn_doc(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(PCNDocument).where(PCNDocument.id == doc_id))
    doc    = result.scalars().first()
    if not doc:
        raise HTTPException(status_code=404)
    filepath = os.path.join(UPLOAD_BASE, f"pcn_{doc.form_id_fk}", doc.filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="檔案不存在")
    return FileResponse(filepath, filename=doc.original_name)


# ── 詳細頁 ───────────────────────────────────────

@router.get("/{form_id}", response_class=HTMLResponse)
async def get_pcn_form(
    form_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await _get_form_or_404(form_id, db)
    docs_by_cat = _docs_by_category(form.documents)

    # 簽核紀錄狀態標籤對照
    transition_combo = {
        "DRAFT→PENDING_QC":              ("送審品保",   "primary"),
        "PENDING_QC→PENDING_PRODUCTION": ("品保完成",   "info"),
        "PENDING_PRODUCTION→PENDING_BU_APPROVAL": ("送 BU 審核", "warning"),
        "PENDING_BU_APPROVAL→APPROVED":  ("BU 核准",   "success"),
        "PENDING_BU_APPROVAL→RETURNED":  ("BU 退回",   "danger"),
        "RETURNED→PENDING_QC":           ("重新送審",   "secondary"),
        "APPROVED→CLOSED":               ("結案",       "dark"),
    }

    return templates.TemplateResponse("pcn_forms/detail.html", {
        "request":          request,
        "user":             current_user,
        "form":             form,
        "docs_by_cat":      docs_by_cat,
        "transition_combo": transition_combo,
        "PCNFormStatus":    PCNFormStatus,
    })


# ── 編輯 GET ─────────────────────────────────────

@router.get("/{form_id}/edit", response_class=HTMLResponse)
async def edit_pcn_form_page(
    form_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await _get_form_or_404(form_id, db)

    if form.status not in (PCNFormStatus.DRAFT, PCNFormStatus.RETURNED):
        raise HTTPException(status_code=403, detail="只有草稿或退回狀態才可編輯")
    if current_user.role != Role.ADMIN and form.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="只有建單者可以編輯")

    qc_result = await db.execute(select(User).where(User.role == Role.QC, User.is_active == True))
    pm_result = await db.execute(select(User).where(User.role == Role.PROD_MGR, User.is_active == True))
    qc_users  = qc_result.scalars().all()
    pm_users  = pm_result.scalars().all()

    docs_by_cat = _docs_by_category(form.documents)

    return templates.TemplateResponse("pcn_forms/edit.html", {
        "request":    request,
        "user":       current_user,
        "form":       form,
        "qc_users":   qc_users,
        "pm_users":   pm_users,
        "docs_by_cat":docs_by_cat,
        "PCNType":    PCNType,
        "BU":         BU,
    })


# ── 編輯 POST ────────────────────────────────────

@router.post("/{form_id}/edit")
async def update_pcn_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    bu:                 str = Form(...),
    product_name:       str = Form(...),
    product_model:      str = Form(""),
    change_description: str = Form(...),
    change_reason:      str = Form(""),
    effective_date:     str = Form(""),
    assigned_qc_id:     str = Form(""),
    assigned_prod_mgr_id: str = Form(""),
    attach_files:       List[UploadFile] = File(default=[]),
    attach_categories:  str = Form(""),
):
    form = await _get_form_or_404(form_id, db)

    if form.status not in (PCNFormStatus.DRAFT, PCNFormStatus.RETURNED):
        raise HTTPException(status_code=403, detail="只有草稿或退回狀態才可編輯")
    if current_user.role != Role.ADMIN and form.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="只有建單者可以編輯")

    form.bu                  = bu
    form.product_name        = product_name
    form.product_model       = product_model or None
    form.change_description  = change_description
    form.change_reason       = change_reason or None
    form.effective_date      = effective_date or None
    form.assigned_qc_id      = int(assigned_qc_id) if assigned_qc_id else None
    form.assigned_prod_mgr_id= int(assigned_prod_mgr_id) if assigned_prod_mgr_id else None
    form.updated_at          = datetime.utcnow()

    # 新增附件
    upload_dir = _pcn_upload_dir(form.id)
    cat_list   = [c.strip() for c in attach_categories.split(",")] if attach_categories else []
    for idx, upload in enumerate(attach_files):
        if not upload.filename:
            continue
        ext      = os.path.splitext(upload.filename)[1]
        saved    = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(upload_dir, saved)
        with open(filepath, "wb") as f:
            f.write(await upload.read())
        doc = PCNDocument(
            form_id_fk   = form.id,
            filename     = saved,
            original_name= upload.filename,
            category     = cat_list[idx] if idx < len(cat_list) else "其它",
            uploaded_by  = current_user.id,
        )
        db.add(doc)

    db.add(form)
    await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── 刪除附件 ─────────────────────────────────────

@router.post("/{form_id}/delete-doc/{doc_id}")
async def delete_pcn_doc(
    form_id: str,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await _get_form_or_404(form_id, db)
    if form.status not in (PCNFormStatus.DRAFT, PCNFormStatus.RETURNED):
        raise HTTPException(status_code=403, detail="此狀態不可刪除附件")
    if current_user.role != Role.ADMIN and form.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="無權限")

    doc_result = await db.execute(select(PCNDocument).where(PCNDocument.id == doc_id))
    doc = doc_result.scalars().first()
    if doc and doc.form_id_fk == form.id:
        filepath = os.path.join(UPLOAD_BASE, f"pcn_{form.id}", doc.filename)
        if os.path.exists(filepath):
            os.remove(filepath)
        await db.delete(doc)
        await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id}/edit", status_code=303)


# ── 送審（工程師 → 品保）────────────────────────

@router.post("/{form_id}/submit")
async def submit_pcn_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)

    if form.status not in (PCNFormStatus.DRAFT, PCNFormStatus.RETURNED):
        raise HTTPException(status_code=400, detail="目前狀態無法送審")
    if current_user.role != Role.ADMIN and form.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="只有建單者可以送審")

    # 驗證必要附件：圖面
    has_drawing = any(d.category == "圖面" for d in form.documents)
    if not has_drawing:
        raise HTTPException(status_code=400, detail="送審前請先上傳【圖面】附件")

    old_status   = form.status
    form.status  = PCNFormStatus.PENDING_QC
    form.updated_at = datetime.utcnow()
    db.add(form)

    approval = PCNApproval(
        form_id_fk  = form.id,
        approver_id = current_user.id,
        action      = "SUBMIT",
        comment     = comment or None,
        from_status = old_status.value,
        to_status   = PCNFormStatus.PENDING_QC.value,
    )
    db.add(approval)
    await db.commit()

    # 通知品保
    await notif.notify_pcn_submitted(db, form)
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── 品保完成（填 SIP 檢表）→ 產線主管 ──────────

@router.post("/{form_id}/qc-done")
async def qc_done(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)

    if form.status != PCNFormStatus.PENDING_QC:
        raise HTTPException(status_code=400, detail="目前狀態不是待品保")
    if current_user.role not in (Role.QC, Role.ADMIN):
        raise HTTPException(status_code=403, detail="只有品保人員可以操作")
    # 指派驗證：只有被指派的品保才能操作（admin 除外）
    if current_user.role == Role.QC and form.assigned_qc_id and form.assigned_qc_id != current_user.id:
        raise HTTPException(status_code=403, detail="您不是此單的指定品保人員")

    # 驗證必要附件：SIP 檢表
    has_sip = any(d.category == "SIP檢表" for d in form.documents)
    if not has_sip:
        raise HTTPException(status_code=400, detail="請先上傳【SIP檢表】附件後再完成")

    old_status   = form.status
    form.status  = PCNFormStatus.PENDING_PRODUCTION
    form.qc_comment = comment or None
    form.updated_at = datetime.utcnow()
    db.add(form)

    approval = PCNApproval(
        form_id_fk  = form.id,
        approver_id = current_user.id,
        action      = "QC_DONE",
        comment     = comment or None,
        from_status = old_status.value,
        to_status   = PCNFormStatus.PENDING_PRODUCTION.value,
    )
    db.add(approval)
    await db.commit()

    # 通知產線主管
    await notif.notify_pcn_qc_done(db, form)
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── 品保上傳 SIP 附件 ────────────────────────────

@router.post("/{form_id}/upload-qc")
async def upload_qc_doc(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    attach_files: List[UploadFile] = File(default=[]),
    attach_categories: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)

    if form.status != PCNFormStatus.PENDING_QC:
        raise HTTPException(status_code=403, detail="目前狀態不可上傳品保附件")
    if current_user.role not in (Role.QC, Role.ADMIN):
        raise HTTPException(status_code=403, detail="無權限")

    upload_dir = _pcn_upload_dir(form.id)
    cat_list   = [c.strip() for c in attach_categories.split(",")] if attach_categories else []
    for idx, upload in enumerate(attach_files):
        if not upload.filename:
            continue
        ext      = os.path.splitext(upload.filename)[1]
        saved    = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(upload_dir, saved)
        with open(filepath, "wb") as f:
            f.write(await upload.read())
        doc = PCNDocument(
            form_id_fk   = form.id,
            filename     = saved,
            original_name= upload.filename,
            category     = cat_list[idx] if idx < len(cat_list) else "SIP檢表",
            uploaded_by  = current_user.id,
        )
        db.add(doc)

    await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── 產線主管完成（填 SOP）→ BU Head ─────────────

@router.post("/{form_id}/prod-done")
async def prod_done(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)

    if form.status != PCNFormStatus.PENDING_PRODUCTION:
        raise HTTPException(status_code=400, detail="目前狀態不是待產線")
    if current_user.role not in (Role.PROD_MGR, Role.ADMIN):
        raise HTTPException(status_code=403, detail="只有產線主管可以操作")
    if current_user.role == Role.PROD_MGR and form.assigned_prod_mgr_id and form.assigned_prod_mgr_id != current_user.id:
        raise HTTPException(status_code=403, detail="您不是此單的指定產線主管")

    # 驗證必要附件：作業SOP + 包裝SOP
    cats = {d.category for d in form.documents}
    missing = []
    if "作業SOP" not in cats:
        missing.append("作業SOP")
    if "包裝SOP" not in cats:
        missing.append("包裝SOP")
    if missing:
        raise HTTPException(status_code=400, detail=f"請先上傳【{'、'.join(missing)}】附件後再完成")

    old_status   = form.status
    form.status  = PCNFormStatus.PENDING_BU_APPROVAL
    form.prod_comment = comment or None
    form.updated_at = datetime.utcnow()
    db.add(form)

    approval = PCNApproval(
        form_id_fk  = form.id,
        approver_id = current_user.id,
        action      = "PROD_DONE",
        comment     = comment or None,
        from_status = old_status.value,
        to_status   = PCNFormStatus.PENDING_BU_APPROVAL.value,
    )
    db.add(approval)
    await db.commit()

    # 通知 BU Head
    await notif.notify_pcn_prod_done(db, form)
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── 產線主管上傳 SOP 附件 ────────────────────────

@router.post("/{form_id}/upload-prod")
async def upload_prod_doc(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    attach_files: List[UploadFile] = File(default=[]),
    attach_categories: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)

    if form.status != PCNFormStatus.PENDING_PRODUCTION:
        raise HTTPException(status_code=403, detail="目前狀態不可上傳產線附件")
    if current_user.role not in (Role.PROD_MGR, Role.ADMIN):
        raise HTTPException(status_code=403, detail="無權限")

    upload_dir = _pcn_upload_dir(form.id)
    cat_list   = [c.strip() for c in attach_categories.split(",")] if attach_categories else []
    for idx, upload in enumerate(attach_files):
        if not upload.filename:
            continue
        ext      = os.path.splitext(upload.filename)[1]
        saved    = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(upload_dir, saved)
        with open(filepath, "wb") as f:
            f.write(await upload.read())
        doc = PCNDocument(
            form_id_fk   = form.id,
            filename     = saved,
            original_name= upload.filename,
            category     = cat_list[idx] if idx < len(cat_list) else "作業SOP",
            uploaded_by  = current_user.id,
        )
        db.add(doc)

    await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── BU Head 核准 ─────────────────────────────────

@router.post("/{form_id}/approve")
async def approve_pcn_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)

    if form.status != PCNFormStatus.PENDING_BU_APPROVAL:
        raise HTTPException(status_code=400, detail="目前狀態不是待 BU 審核")
    if current_user.role not in (Role.BU, Role.ADMIN):
        raise HTTPException(status_code=403, detail="只有 BU Head 可以審核")

    old_status  = form.status
    form.status = PCNFormStatus.APPROVED
    form.updated_at = datetime.utcnow()
    db.add(form)

    approval = PCNApproval(
        form_id_fk  = form.id,
        approver_id = current_user.id,
        action      = "APPROVE",
        comment     = comment or None,
        from_status = old_status.value,
        to_status   = PCNFormStatus.APPROVED.value,
    )
    db.add(approval)
    await db.commit()

    # 通知所有相關人員（LINE 推播取代 Mail）
    await notif.notify_pcn_approved(db, form)
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── BU Head 退回 ─────────────────────────────────

@router.post("/{form_id}/reject")
async def reject_pcn_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)

    if form.status != PCNFormStatus.PENDING_BU_APPROVAL:
        raise HTTPException(status_code=400, detail="目前狀態不是待 BU 審核")
    if current_user.role not in (Role.BU, Role.ADMIN):
        raise HTTPException(status_code=403, detail="只有 BU Head 可以退回")

    old_status  = form.status
    form.status = PCNFormStatus.RETURNED
    form.updated_at = datetime.utcnow()
    db.add(form)

    approval = PCNApproval(
        form_id_fk  = form.id,
        approver_id = current_user.id,
        action      = "REJECT",
        comment     = comment or None,
        from_status = old_status.value,
        to_status   = PCNFormStatus.RETURNED.value,
    )
    db.add(approval)
    await db.commit()

    # 通知工程師（建單者）
    await notif.notify_pcn_rejected(db, form)
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── 結案 ─────────────────────────────────────────

@router.post("/{form_id}/close")
async def close_pcn_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)

    if form.status != PCNFormStatus.APPROVED:
        raise HTTPException(status_code=400, detail="只有核准狀態才可結案")
    if current_user.role not in (Role.ENGINEER, Role.ADMIN):
        raise HTTPException(status_code=403, detail="只有工程師可以結案")
    if current_user.role == Role.ENGINEER and form.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="只有建單者可以結案")

    old_status  = form.status
    form.status = PCNFormStatus.CLOSED
    form.updated_at = datetime.utcnow()
    db.add(form)

    approval = PCNApproval(
        form_id_fk  = form.id,
        approver_id = current_user.id,
        action      = "CLOSE",
        comment     = comment or None,
        from_status = old_status.value,
        to_status   = PCNFormStatus.CLOSED.value,
    )
    db.add(approval)
    await db.commit()

    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)
