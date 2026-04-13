import os, uuid, json, mimetypes
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.user import User, Role
from app.models.pcn_form import PCNForm, PCNDocument, PCNApproval, PCNFormStatus, PCNType
from app.services.auth import get_current_user
import app.services.notification as notif

router    = APIRouter(prefix="/pcn-forms")
templates = Jinja2Templates(directory="app/templates")
UPLOAD_BASE = "uploads"

# 提出部門選項
DEPARTMENTS = ["業務部", "工程部", "資材部", "製造部"]

# PCN 只限工程師/管理員；ECN 所有已登入使用者皆可建立
PCN_ALLOWED_ROLES = (Role.ENGINEER, Role.ADMIN)

# ECN 技術類變更類型（需走工程 + 品保確認）
ECN_TECH_TYPES = {"製程變更", "設計變更", "供應商變更"}


def _pcn_upload_dir(form_id: int) -> str:
    path = os.path.join(UPLOAD_BASE, f"pcn_{form_id}")
    os.makedirs(path, exist_ok=True)
    return path


async def _gen_form_id(db: AsyncSession, pcn_type: str) -> str:
    today  = datetime.utcnow().strftime("%Y%m%d")
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


def _check_create_permission(current_user: User, pcn_type: str):
    """PCN 限工程師；ECN 所有角色均可"""
    if pcn_type == "PCN" and current_user.role not in PCN_ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="PCN（開發轉量產）只有工程師可以建立")


def _parse_change_types(change_types_str: str) -> list:
    if not change_types_str:
        return []
    try:
        result = json.loads(change_types_str)
        return result if isinstance(result, list) else []
    except Exception:
        return []


def _ecn_needs_tech_review(form: PCNForm) -> bool:
    """判斷 ECN 是否含技術類變更（需工程+品保確認）"""
    if form.type != PCNType.ECN:
        return False
    selected = set(_parse_change_types(form.change_types))
    return bool(selected & ECN_TECH_TYPES)


# ── 列表 ─────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def list_pcn_forms(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = (
        select(PCNForm)
        .options(selectinload(PCNForm.creator))
        .order_by(PCNForm.created_at.desc())
    )
    # 各角色篩選
    if current_user.role in (Role.ADMIN, Role.BU):
        pass  # 看全部
    elif current_user.role == Role.ENGINEER:
        # 自建 + 所有待工程確認的 ECN
        q = q.where(or_(
            PCNForm.created_by == current_user.id,
            PCNForm.status == PCNFormStatus.ECN_PENDING_ENG,
        ))
    elif current_user.role == Role.QC:
        # PCN 待品保 + ECN 待品保確認
        q = q.where(or_(
            PCNForm.status == PCNFormStatus.PENDING_QC,
            PCNForm.status == PCNFormStatus.ECN_PENDING_QC,
        ))
    elif current_user.role == Role.PROD_MGR:
        q = q.where(PCNForm.status == PCNFormStatus.PENDING_PRODUCTION)
    else:
        # 其他角色（業務等）只看自建
        q = q.where(PCNForm.created_by == current_user.id)

    result = await db.execute(q)
    forms  = result.scalars().all()
    return templates.TemplateResponse("pcn_forms/list.html", {
        "request": request, "user": current_user, "forms": forms,
        "PCNFormStatus": PCNFormStatus,
    })


# ── 新建 GET ─────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_pcn_form_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return templates.TemplateResponse("pcn_forms/new.html", {
        "request":    request,
        "user":       current_user,
        "PCNType":    PCNType,
        "DEPARTMENTS": DEPARTMENTS,
        "is_engineer": current_user.role in PCN_ALLOWED_ROLES,
    })


# ── 新建 POST ────────────────────────────────────

@router.post("/new")
async def create_pcn_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    pcn_type:           str = Form(...),
    department:         str = Form(...),
    product_name:       str = Form(...),
    product_model:      str = Form(""),
    change_description: str = Form(...),
    change_reason:      str = Form(""),
    effective_date:     str = Form(""),
    change_types:       str = Form(""),   # JSON 陣列字串，ECN 用
    attach_files:       List[UploadFile] = File(default=[]),
    attach_categories:  List[str] = Form(default=[]),
):
    _check_create_permission(current_user, pcn_type)

    form_id_str = await _gen_form_id(db, pcn_type)
    form = PCNForm(
        form_id            = form_id_str,
        type               = PCNType(pcn_type),
        status             = PCNFormStatus.DRAFT,
        department         = department,
        product_name       = product_name,
        product_model      = product_model or None,
        change_description = change_description,
        change_reason      = change_reason or None,
        effective_date     = effective_date or None,
        change_types       = change_types if pcn_type == "ECN" else None,
        created_by         = current_user.id,
    )
    db.add(form)
    await db.flush()

    await _save_attachments(db, form.id, current_user.id, attach_files, attach_categories)
    await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id_str}", status_code=303)


async def _save_attachments(db, form_id, user_id, attach_files, attach_categories):
    upload_dir = _pcn_upload_dir(form_id)
    # attach_categories 可為 List[str] 或單一 str（向下相容）
    if isinstance(attach_categories, str):
        cat_list = [c.strip() for c in attach_categories.split(",")] if attach_categories else []
    else:
        cat_list = list(attach_categories)
    for idx, upload in enumerate(attach_files):
        if not upload.filename:
            continue
        content = await upload.read()
        if not content:
            continue
        ext   = os.path.splitext(upload.filename)[1] or ".bin"
        saved = f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(upload_dir, saved), "wb") as f:
            f.write(content)
        db.add(PCNDocument(
            form_id_fk    = form_id,
            filename      = saved,
            original_name = upload.filename,
            category      = cat_list[idx] if idx < len(cat_list) else "其它",
            uploaded_by   = user_id,
        ))


# ── 附件預覽（必須在 /{form_id} 之前）───────────

@router.get("/doc/preview/{doc_id}")
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
    mime, _ = mimetypes.guess_type(doc.original_name)
    mime = mime or "application/octet-stream"
    # 圖片與 PDF → 瀏覽器直接預覽；其餘 → 下載
    if mime.startswith("image/") or mime == "application/pdf":
        return FileResponse(
            filepath, media_type=mime,
            headers={"Content-Disposition": f'inline; filename="{doc.original_name}"'},
        )
    return FileResponse(filepath, filename=doc.original_name)


# ── 詳細頁 ───────────────────────────────────────

@router.get("/{form_id}", response_class=HTMLResponse)
async def get_pcn_form(
    form_id: str, request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await _get_form_or_404(form_id, db)

    # 解析 ECN 變更類型
    change_types_list = _parse_change_types(form.change_types)
    ecn_tech_review   = _ecn_needs_tech_review(form)

    transition_combo = {
        "DRAFT→PENDING_QC":                      ("送審品保",    "primary"),
        "DRAFT→ECN_PENDING_ENG":                 ("ECN送審",     "primary"),
        "DRAFT→PENDING_BU_APPROVAL":             ("ECN直送BU",   "warning"),
        "ECN_PENDING_ENG→ECN_PENDING_QC":        ("工程確認",    "info"),
        "ECN_PENDING_QC→PENDING_BU_APPROVAL":    ("品保確認",    "success"),
        "PENDING_QC→PENDING_PRODUCTION":         ("品保完成",    "info"),
        "PENDING_PRODUCTION→PENDING_BU_APPROVAL":("送BU審核",    "warning"),
        "PENDING_BU_APPROVAL→APPROVED":          ("BU核准",      "success"),
        "PENDING_BU_APPROVAL→RETURNED":          ("BU退回",      "danger"),
        "APPROVED→CLOSED":                       ("結案",        "dark"),
    }
    return templates.TemplateResponse("pcn_forms/detail.html", {
        "request": request, "user": current_user, "form": form,
        "docs_by_cat": _docs_by_category(form.documents),
        "transition_combo": transition_combo,
        "PCNFormStatus": PCNFormStatus,
        "change_types_list": change_types_list,
        "ecn_tech_review": ecn_tech_review,
    })


# ── 編輯 GET ─────────────────────────────────────

@router.get("/{form_id}/edit", response_class=HTMLResponse)
async def edit_pcn_form_page(
    form_id: str, request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await _get_form_or_404(form_id, db)
    if form.status not in (PCNFormStatus.DRAFT, PCNFormStatus.RETURNED):
        raise HTTPException(status_code=403, detail="只有草稿或退回可編輯")
    if current_user.role != Role.ADMIN and form.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="只有建單者可編輯")

    change_types_list = _parse_change_types(form.change_types)
    return templates.TemplateResponse("pcn_forms/edit.html", {
        "request":          request,
        "user":             current_user,
        "form":             form,
        "docs_by_cat":      _docs_by_category(form.documents),
        "PCNType":          PCNType,
        "DEPARTMENTS":      DEPARTMENTS,
        "change_types_list": change_types_list,
        "change_types_json": form.change_types or "[]",
    })


# ── 編輯 POST ────────────────────────────────────

@router.post("/{form_id}/edit")
async def update_pcn_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    department:         str = Form(...),
    product_name:       str = Form(...),
    product_model:      str = Form(""),
    change_description: str = Form(...),
    change_reason:      str = Form(""),
    effective_date:     str = Form(""),
    change_types:       str = Form(""),
    attach_files:       List[UploadFile] = File(default=[]),
    attach_categories:  List[str] = Form(default=[]),
):
    form = await _get_form_or_404(form_id, db)
    if form.status not in (PCNFormStatus.DRAFT, PCNFormStatus.RETURNED):
        raise HTTPException(status_code=403)
    if current_user.role != Role.ADMIN and form.created_by != current_user.id:
        raise HTTPException(status_code=403)

    form.department         = department
    form.product_name       = product_name
    form.product_model      = product_model or None
    form.change_description = change_description
    form.change_reason      = change_reason or None
    form.effective_date     = effective_date or None
    form.updated_at         = datetime.utcnow()
    if form.type == PCNType.ECN:
        form.change_types = change_types or None

    await _save_attachments(db, form.id, current_user.id, attach_files, attach_categories)
    db.add(form)
    await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── 刪除附件 ─────────────────────────────────────

@router.post("/{form_id}/delete-doc/{doc_id}")
async def delete_pcn_doc(
    form_id: str, doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await _get_form_or_404(form_id, db)
    if form.status not in (PCNFormStatus.DRAFT, PCNFormStatus.RETURNED):
        raise HTTPException(status_code=403)
    doc_r = await db.execute(select(PCNDocument).where(PCNDocument.id == doc_id))
    doc   = doc_r.scalars().first()
    if doc and doc.form_id_fk == form.id:
        fp = os.path.join(UPLOAD_BASE, f"pcn_{form.id}", doc.filename)
        if os.path.exists(fp):
            os.remove(fp)
        await db.delete(doc)
        await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id}/edit", status_code=303)


# ── 送審 ─────────────────────────────────────────

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
        raise HTTPException(status_code=403)
    if not form.documents:
        raise HTTPException(status_code=400, detail="送審前請先上傳附件")

    old = form.status

    # 依表單類型與變更類型決定下一站
    if form.type == PCNType.ECN:
        if _ecn_needs_tech_review(form):
            next_status = PCNFormStatus.ECN_PENDING_ENG   # 技術類 → 工程確認
        else:
            next_status = PCNFormStatus.PENDING_BU_APPROVAL  # 商業類 → 直接 BU
    else:
        next_status = PCNFormStatus.PENDING_QC  # PCN → 品保 SIP

    form.status     = next_status
    form.updated_at = datetime.utcnow()
    db.add(form)
    db.add(PCNApproval(
        form_id_fk=form.id, approver_id=current_user.id, action="SUBMIT",
        comment=comment or None, from_status=old.value,
        to_status=next_status.value,
    ))
    await db.commit()
    await notif.notify_pcn_submitted(db, form)
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── ECN 工程確認 ─────────────────────────────────

@router.post("/{form_id}/ecn-eng-confirm")
async def ecn_eng_confirm(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != PCNFormStatus.ECN_PENDING_ENG:
        raise HTTPException(status_code=400, detail="目前狀態非 ECN 待工程確認")
    if current_user.role not in (Role.ENGINEER, Role.ADMIN):
        raise HTTPException(status_code=403, detail="只有工程師可執行此步驟")

    old = form.status
    form.status     = PCNFormStatus.ECN_PENDING_QC
    form.updated_at = datetime.utcnow()
    db.add(form)
    db.add(PCNApproval(
        form_id_fk=form.id, approver_id=current_user.id, action="ENG_CONFIRM",
        comment=comment or None, from_status=old.value,
        to_status=PCNFormStatus.ECN_PENDING_QC.value,
    ))
    await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── ECN 品保確認 ─────────────────────────────────

@router.post("/{form_id}/ecn-qc-confirm")
async def ecn_qc_confirm(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != PCNFormStatus.ECN_PENDING_QC:
        raise HTTPException(status_code=400, detail="目前狀態非 ECN 待品保確認")
    if current_user.role not in (Role.QC, Role.ADMIN):
        raise HTTPException(status_code=403, detail="只有品保可執行此步驟")

    old = form.status
    form.status     = PCNFormStatus.PENDING_BU_APPROVAL
    form.qc_comment = comment or None
    form.updated_at = datetime.utcnow()
    db.add(form)
    db.add(PCNApproval(
        form_id_fk=form.id, approver_id=current_user.id, action="ECN_QC_CONFIRM",
        comment=comment or None, from_status=old.value,
        to_status=PCNFormStatus.PENDING_BU_APPROVAL.value,
    ))
    await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── PCN 品保上傳 ─────────────────────────────────

@router.post("/{form_id}/upload-qc")
async def upload_qc_doc(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    attach_files: List[UploadFile] = File(default=[]),
    attach_categories: List[str] = Form(default=[]),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != PCNFormStatus.PENDING_QC:
        raise HTTPException(status_code=403)
    if current_user.role not in (Role.QC, Role.ADMIN):
        raise HTTPException(status_code=403)
    await _save_attachments(db, form.id, current_user.id, attach_files, attach_categories)
    await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── PCN 品保完成 ─────────────────────────────────

@router.post("/{form_id}/qc-done")
async def qc_done(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != PCNFormStatus.PENDING_QC:
        raise HTTPException(status_code=400)
    if current_user.role not in (Role.QC, Role.ADMIN):
        raise HTTPException(status_code=403)
    if not any(d.category == "SIP檢表" for d in form.documents):
        raise HTTPException(status_code=400, detail="請先上傳【SIP檢表】附件")

    old = form.status
    form.status     = PCNFormStatus.PENDING_PRODUCTION
    form.qc_comment = comment or None
    form.updated_at = datetime.utcnow()
    db.add(form)
    db.add(PCNApproval(
        form_id_fk=form.id, approver_id=current_user.id, action="QC_DONE",
        comment=comment or None, from_status=old.value,
        to_status=PCNFormStatus.PENDING_PRODUCTION.value,
    ))
    await db.commit()
    await notif.notify_pcn_qc_done(db, form)
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── 產線上傳 ─────────────────────────────────────

@router.post("/{form_id}/upload-prod")
async def upload_prod_doc(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    attach_files: List[UploadFile] = File(default=[]),
    attach_categories: List[str] = Form(default=[]),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != PCNFormStatus.PENDING_PRODUCTION:
        raise HTTPException(status_code=403)
    if current_user.role not in (Role.PROD_MGR, Role.ADMIN):
        raise HTTPException(status_code=403)
    await _save_attachments(db, form.id, current_user.id, attach_files, attach_categories)
    await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── 產線完成 ─────────────────────────────────────

@router.post("/{form_id}/prod-done")
async def prod_done(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != PCNFormStatus.PENDING_PRODUCTION:
        raise HTTPException(status_code=400)
    if current_user.role not in (Role.PROD_MGR, Role.ADMIN):
        raise HTTPException(status_code=403)
    cats    = {d.category for d in form.documents}
    missing = [c for c in ["作業SOP", "包裝SOP"] if c not in cats]
    if missing:
        raise HTTPException(status_code=400, detail=f"請先上傳【{'、'.join(missing)}】")

    old = form.status
    form.status      = PCNFormStatus.PENDING_BU_APPROVAL
    form.prod_comment= comment or None
    form.updated_at  = datetime.utcnow()
    db.add(form)
    db.add(PCNApproval(
        form_id_fk=form.id, approver_id=current_user.id, action="PROD_DONE",
        comment=comment or None, from_status=old.value,
        to_status=PCNFormStatus.PENDING_BU_APPROVAL.value,
    ))
    await db.commit()
    await notif.notify_pcn_prod_done(db, form)
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── BU 核准 ──────────────────────────────────────

@router.post("/{form_id}/approve")
async def approve_pcn_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != PCNFormStatus.PENDING_BU_APPROVAL:
        raise HTTPException(status_code=400)
    if current_user.role not in (Role.BU, Role.ADMIN):
        raise HTTPException(status_code=403)
    old = form.status
    form.status     = PCNFormStatus.APPROVED
    form.updated_at = datetime.utcnow()
    db.add(form)
    db.add(PCNApproval(
        form_id_fk=form.id, approver_id=current_user.id, action="APPROVE",
        comment=comment or None, from_status=old.value,
        to_status=PCNFormStatus.APPROVED.value,
    ))
    await db.commit()

    # 依變更類型決定 CC 通知對象
    if form.type == PCNType.ECN:
        selected = set(_parse_change_types(form.change_types))
        if selected & ECN_TECH_TYPES:
            await notif.notify_ecn_approved_tech(db, form)
        if "售價變更" in selected:
            await notif.notify_ecn_approved_price(db, form)
        if "成本變更" in selected:
            await notif.notify_ecn_approved_cost(db, form)
        if not selected:
            await notif.notify_pcn_approved(db, form)
    else:
        await notif.notify_pcn_approved(db, form)

    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)


# ── BU 退回 ──────────────────────────────────────

@router.post("/{form_id}/reject")
async def reject_pcn_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(...),
    reject_target: str = Form("提案單位"),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != PCNFormStatus.PENDING_BU_APPROVAL:
        raise HTTPException(status_code=400)
    if current_user.role not in (Role.BU, Role.ADMIN):
        raise HTTPException(status_code=403)
    if not comment.strip():
        raise HTTPException(status_code=400, detail="退回原因不得為空")
    old = form.status
    form.status     = PCNFormStatus.RETURNED
    form.updated_at = datetime.utcnow()
    db.add(form)
    db.add(PCNApproval(
        form_id_fk=form.id, approver_id=current_user.id, action="REJECT",
        comment=comment.strip(), from_status=old.value,
        to_status=PCNFormStatus.RETURNED.value,
        reject_target=reject_target,
    ))
    await db.commit()
    await notif.notify_pcn_rejected(db, form, reject_target)
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
        raise HTTPException(status_code=400)
    if current_user.role not in (Role.ENGINEER, Role.ADMIN):
        raise HTTPException(status_code=403)
    old = form.status
    form.status     = PCNFormStatus.CLOSED
    form.updated_at = datetime.utcnow()
    db.add(form)
    db.add(PCNApproval(
        form_id_fk=form.id, approver_id=current_user.id, action="CLOSE",
        comment=comment or None, from_status=old.value,
        to_status=PCNFormStatus.CLOSED.value,
    ))
    await db.commit()
    return RedirectResponse(url=f"/pcn-forms/{form_id}", status_code=303)
