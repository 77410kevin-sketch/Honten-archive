"""NPI 模組路由 — RFQ + NPI 兩階段流程

角色對應：
  sales       → 建單 / 業務補充 / 成本分析 / 發送客戶報價 / 提供最終版
  engineer    → 排製程、派發供應商詢價、回填報價、選供應商、開 ERP 模具請購單
  eng_mgr     → CC 知悉
  bu          → 核准 NPI 成案 / 退回
  purchase    → 採購議價回填
  admin       → 全權限
"""
import os, uuid, json, mimetypes, logging
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
from app.services import rfq_archive

router    = APIRouter(prefix="/npi-forms")
templates = Jinja2Templates(directory="app/templates")

def _fromjson_filter(s):
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}
templates.env.filters["fromjson"] = _fromjson_filter
UPLOAD_BASE = "uploads"

# 可建單角色
_SALES_ROLES = (Role.SALES, Role.ADMIN)
_ENG_ROLES   = (Role.ENGINEER, Role.ADMIN)
_BU_ROLES    = (Role.BU, Role.ADMIN)
_PURCHASE_ROLES = (Role.PURCHASE, Role.ADMIN)
# RFQ 階段：供應商報價回收 / 宣告收齊 — 採購主責，工程也可協助
_RFQ_COLLECT_ROLES = (Role.PURCHASE, Role.ENGINEER, Role.ADMIN)

ATTACH_CATEGORIES = [
    "客戶詢價信", "圖面",
    "供應商報價", "議價後報價單",
    "成本分析表", "客戶報價單",
    "模具請購單", "議價記錄", "其它",
]


# ── helpers ─────────────────────────────────────

def _upload_dir(form_pk: int) -> str:
    path = os.path.join(UPLOAD_BASE, f"npi_{form_pk}")
    os.makedirs(path, exist_ok=True)
    return path


async def _gen_form_id(db: AsyncSession) -> str:
    """新建單一律 RFQ- 開頭；若轉入 NPI 階段，ID 仍沿用（便於追溯）。"""
    today  = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"RFQ-{today}-"
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
            selectinload(NPIForm.invites).selectinload(NPISupplierInvite.drawing),
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
    stage: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列表 — 可用 ?stage=RFQ / ?stage=NPI 將 RFQ 詢價與 NPI 開發流程切開顯示。"""
    q = (
        select(NPIForm)
        .options(selectinload(NPIForm.creator))
        .order_by(NPIForm.created_at.desc())
    )
    if stage == "RFQ":
        q = q.where(NPIForm.stage == NPIStage.RFQ)
    elif stage == "NPI":
        q = q.where(NPIForm.stage == NPIStage.NPI)
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
        q = q.where(or_(
            NPIForm.status.in_([
                NPIFormStatus.QUOTING,            # RFQ 階段收集供應商報價
                NPIFormStatus.QUOTES_COLLECTED,
                NPIFormStatus.NPI_PENDING_PURCHASE, # NPI 階段模具議價
            ]),
            # BU 退回採購議價調整
            and_(NPIForm.status == NPIFormStatus.RETURNED,
                 NPIForm.reject_to == "採購"),
        ))
    else:
        q = q.where(NPIForm.created_by == u.id)
    r = await db.execute(q)
    forms = r.scalars().all()
    return templates.TemplateResponse("npi_forms/list.html", {
        "request": request, "user": current_user,
        "forms": forms, "NPIFormStatus": NPIFormStatus, "NPIStage": NPIStage,
        "stage_filter": stage or "",
    })


# ── 新建 GET / POST（業務建立） ─────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_npi_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in _SALES_ROLES:
        raise HTTPException(status_code=403, detail="只有業務可以建立 NPI 單")
    # 取已啟用的客戶作為 datalist 來源（手動建立或從 ERP 同步的都列）
    from app.models.customer import Customer
    r_cust = await db.execute(select(Customer).where(Customer.is_active == True).order_by(Customer.name))
    customers = list(r_cust.scalars().all())
    return templates.TemplateResponse("npi_forms/new.html", {
        "request": request, "user": current_user,
        "ATTACH_CATEGORIES": ATTACH_CATEGORIES,
        "customers": customers,
    })


# ── AI 智慧解析詢價信 → 回 JSON（前端用以預填欄位）─

@router.post("/_parse-inquiry")
async def parse_inquiry(
    current_user: User = Depends(get_current_user),
    inquiry_file: UploadFile = File(...),
):
    if current_user.role not in _SALES_ROLES:
        raise HTTPException(status_code=403, detail="只有業務可以使用此功能")
    from app.services.inquiry_parser import (
        parse_inquiry_letter, parse_inquiry_image,
        extract_text_from_upload, IMAGE_EXTS,
    )
    content = await inquiry_file.read()
    if not content:
        raise HTTPException(status_code=400, detail="檔案為空")
    filename = inquiry_file.filename or "upload.txt"
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext in IMAGE_EXTS:
            data = parse_inquiry_image(content, filename)
        else:
            text = extract_text_from_upload(filename, content)
            data = parse_inquiry_letter(text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析失敗：{e}")
    return {"ok": True, "data": data}


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
    # 送審不強制附件；由業務自行判斷（可空單送審）
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
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    eng_process_note: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.ENG_DISPATCH:
        raise HTTPException(status_code=400, detail="目前狀態非待工程派發")
    if current_user.role not in _ENG_ROLES:
        raise HTTPException(status_code=403)

    # 讀每張圖獨立的材質 / MOQ（drawing_meta_id + drawing_material + drawing_qty 三個同長度陣列）
    # 無圖時退回整張單共用：dispatch_material / dispatch_qty
    fd = await request.form()
    draw_meta_ids = fd.getlist("drawing_meta_id")
    draw_materials = fd.getlist("drawing_material")
    draw_qtys = fd.getlist("drawing_qty")
    per_drawing: dict[int, dict] = {}
    for i, raw_id in enumerate(draw_meta_ids):
        if not raw_id or not raw_id.isdigit():
            continue
        did = int(raw_id)
        mat = (draw_materials[i] if i < len(draw_materials) else "").strip() or None
        qty_raw = (draw_qtys[i] if i < len(draw_qtys) else "").strip()
        qty = int(qty_raw) if qty_raw.isdigit() else None
        per_drawing[did] = {"material": mat, "qty": qty}

    # 無圖 fallback（單一共用）
    fallback_material = (fd.get("dispatch_material") or "").strip() or None
    fallback_qty_raw = (fd.get("dispatch_qty") or "").strip()
    fallback_qty = int(fallback_qty_raw) if fallback_qty_raw.isdigit() else None

    supplier_ids = fd.getlist("row_supplier_id")
    process_names = fd.getlist("row_process")
    drawing_ids = fd.getlist("row_drawing_id")

    # 過濾無效列（未選供應商者略過）
    rows = []
    for i in range(len(supplier_ids)):
        sid = supplier_ids[i].strip() if i < len(supplier_ids) else ""
        if not sid or not sid.isdigit():
            continue
        draw_raw = drawing_ids[i].strip() if i < len(drawing_ids) else ""
        draw_id = int(draw_raw) if draw_raw.isdigit() else None
        rows.append({
            "supplier_id": int(sid),
            "process_name": (process_names[i] if i < len(process_names) else "").strip() or None,
            "drawing_doc_id": draw_id,
        })
    if not rows:
        raise HTTPException(status_code=400, detail="請至少新增一列並指定供應商")

    valid_sids = {r["supplier_id"] for r in rows}
    r_sup = await db.execute(select(Supplier).where(Supplier.id.in_(valid_sids), Supplier.is_active == True))
    sup_map = {s.id: s for s in r_sup.scalars().all()}
    rows = [r for r in rows if r["supplier_id"] in sup_map]
    if not rows:
        raise HTTPException(status_code=400, detail="所選供應商無效或已停用")

    now = datetime.utcnow()
    # 依 drawing_id 套用材質/MOQ；無圖紙的列走 fallback。為方便前端顯示，只在該 drawing 的第一筆 invite 寫 material/qty。
    seen_drawing_marked: set = set()
    fallback_marked = False
    for r in rows:
        did = r["drawing_doc_id"]
        if did is not None:
            meta = per_drawing.get(did, {})
            mat, qty = meta.get("material"), meta.get("qty")
            mark_here = did not in seen_drawing_marked
            seen_drawing_marked.add(did)
        else:
            mat, qty = fallback_material, fallback_qty
            mark_here = not fallback_marked
            fallback_marked = True
        db.add(NPISupplierInvite(
            form_id_fk=form.id,
            supplier_id=r["supplier_id"],
            process_name=r["process_name"],
            material=(mat if mark_here else None),
            qty=(qty if mark_here else None),
            drawing_doc_id=did,
            invited_at=now,
        ))

    old = form.status
    form.eng_process_note = eng_process_note or None
    form.assigned_eng_id = current_user.id
    form.status = NPIFormStatus.QUOTING
    form.updated_at = now
    summary = "、".join(f"{sup_map[r['supplier_id']].name}({r['process_name'] or '—'})" for r in rows)
    db.add(_log_approval(form, current_user, "DISPATCH", old, form.status,
                         f"派發 {len(rows)} 列：{summary}"))
    await db.commit()

    # 合併寄出 flag（UI checkbox 勾選 → 同一供應商多筆合併一封信）
    merge_mail = (fd.get("merge_mail") == "1")

    # 清空 session identity map 再重讀，確保 selectinload 拿到剛寫入的 invites
    db.expire_all()
    form = await _get_form_or_404(form_id, db)
    await notif.notify_quotes_dispatched(db, form, list(form.invites), merge=merge_mail)
    await db.commit()
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 工程回填供應商報價（代填）──────────────────

@router.post("/{form_id}/invite/{invite_id}/reply")
async def fill_invite_reply(
    form_id: str, invite_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    quote_amount:   str  = Form(""),
    tooling_cost:   str  = Form(""),
    lead_time_days: str  = Form(""),
    quote_comment:  str  = Form(""),
    attach_files:   List[UploadFile] = File(default=[]),
):
    form = await _get_form_or_404(form_id, db)
    # 允許狀態：QUOTING（首輪報價收集）；或 RETURNED 且退回對象=採購（議價調整）
    allow = (
        form.status == NPIFormStatus.QUOTING
        or (form.status == NPIFormStatus.RETURNED and form.reject_to == "採購")
    )
    if not allow:
        raise HTTPException(status_code=400, detail="目前狀態非報價中")
    if current_user.role not in _RFQ_COLLECT_ROLES:
        raise HTTPException(status_code=403, detail="只有採購 / 工程可以回填報價")
    inv = next((i for i in form.invites if i.id == invite_id), None)
    if not inv:
        raise HTTPException(status_code=404, detail="找不到派發紀錄")
    inv.quote_amount   = float(quote_amount) if quote_amount else None
    inv.tooling_cost   = float(tooling_cost) if tooling_cost else None
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
    if current_user.role not in _RFQ_COLLECT_ROLES:
        raise HTTPException(status_code=403, detail="只有採購 / 工程可宣告報價收齊")
    old = form.status
    form.status = NPIFormStatus.QUOTES_COLLECTED
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "FINISH_QUOTES", old, form.status, comment))
    await db.commit()
    await notif._notify_roles(db, [Role.SALES],
                             f"【RFQ 報價已收齊】{form.form_id} - 請上線試算成本並送 BU 審核")
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 業務試算完成 → 送 BU 審核報價 ─────────

@router.post("/{form_id}/submit-quote-bu")
async def submit_quote_bu(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    quote_cost_data:    str = Form(""),   # JSON 試算表
    cost_analysis_note: str = Form(""),
    quoted_unit_price:  str = Form(""),   # 業務最終決定的報價單價
    comment:            str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status not in (NPIFormStatus.QUOTES_COLLECTED, NPIFormStatus.RETURNED):
        raise HTTPException(status_code=400, detail="目前狀態不允許送 BU 審核報價")
    if current_user.role not in _SALES_ROLES:
        raise HTTPException(status_code=403)
    if form.status == NPIFormStatus.RETURNED and form.reject_to != "業務":
        raise HTTPException(status_code=400, detail="此退回單非給業務")
    old = form.status
    form.quote_cost_data    = quote_cost_data or None
    form.cost_analysis_note = cost_analysis_note or None
    form.quoted_unit_price  = float(quoted_unit_price) if quoted_unit_price else None
    form.status = NPIFormStatus.PENDING_QUOTE_BU
    form.reject_to = None
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "SUBMIT_QUOTE_BU", old, form.status, comment))
    await db.commit()
    await notif._notify_roles(
        db, [Role.BU],
        f"【RFQ 待 BU 審核報價】{form.form_id} - 建議報價 {form.quoted_unit_price or '—'}，請審核利潤",
    )
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── BU 核准 / 退回 業務報價 ─────────────

@router.post("/{form_id}/purchase-resubmit-bu")
async def purchase_resubmit_bu(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    """採購議價完成後，將調整後的成本重送 BU 審核。"""
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.RETURNED or form.reject_to != "採購":
        raise HTTPException(status_code=400, detail="目前狀態非待採購議價調整")
    if current_user.role not in _PURCHASE_ROLES:
        raise HTTPException(status_code=403, detail="只有採購可重送")
    old = form.status
    form.status = NPIFormStatus.PENDING_QUOTE_BU
    form.reject_to = None
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "PURCHASE_RESUBMIT_BU", old, form.status,
                         comment or "採購議價調整完成，重送 BU 審核"))
    await db.commit()
    await notif._notify_roles(
        db, [Role.BU, Role.SALES],
        f"【採購議價已重送 BU】{form.form_id} - 採購已調整供應商成本，請 BU 重新審核報價",
    )
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


@router.post("/{form_id}/approve-quote-bu")
async def approve_quote_bu(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.PENDING_QUOTE_BU:
        raise HTTPException(status_code=400)
    if current_user.role not in _BU_ROLES:
        raise HTTPException(status_code=403)
    old = form.status
    form.bu_quote_note = comment or None
    form.status = NPIFormStatus.QUOTE_APPROVED
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "APPROVE_QUOTE_BU", old, form.status, comment))
    await db.commit()
    # 重新載入以便 NAS 歸檔存取 documents 關聯
    db.expire_all()
    form = await _get_form_or_404(form_id, db)
    await notif.notify_quote_approved(db, form)
    await notif._notify_roles(
        db, [Role.SALES],
        f"【報價已核准】{form.form_id} - BU 已核准，請發送客戶報價",
    )
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


@router.post("/{form_id}/reject-quote-bu")
async def reject_quote_bu(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment:       str = Form(...),
    reject_target: str = Form("業務"),
):
    form = await _get_form_or_404(form_id, db)
    if form.status != NPIFormStatus.PENDING_QUOTE_BU:
        raise HTTPException(status_code=400)
    if current_user.role not in _BU_ROLES:
        raise HTTPException(status_code=403)
    if not comment.strip():
        raise HTTPException(status_code=400, detail="退回原因不得為空")
    target = reject_target.strip() if reject_target else "業務"
    if target not in ("業務", "採購"):
        target = "業務"
    old = form.status
    form.bu_quote_note = comment.strip()
    form.status = NPIFormStatus.RETURNED
    form.reject_to = target
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "REJECT_QUOTE_BU", old, form.status,
                         comment.strip(), reject_target=target))
    await db.commit()
    notify_role = Role.PURCHASE if target == "採購" else Role.SALES
    notify_msg = (
        f"【報價被 BU 退回】{form.form_id} - 請{'與供應商議價調整成本後重送' if target == '採購' else '調整試算後重送'}。"
        f"原因：{comment.strip()[:80]}"
    )
    await notif._notify_roles(db, [notify_role], notify_msg)
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


# ── 客戶報價單（對外版本，不含成本/供應商資訊）──────────

@router.get("/{form_id}/customer-quote", response_class=HTMLResponse)
async def customer_quote_view(
    form_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """產生對外的客戶報價單（乾淨版面、可列印 / 存 PDF）。
    僅顯示：客戶資訊、機種、材質、數量、單價、交期、付款條件 — 不含成本分析、利潤率、供應商。
    """
    form = await _get_form_or_404(form_id, db)
    if current_user.role not in (*_SALES_ROLES, *_BU_ROLES, Role.ADMIN):
        raise HTTPException(status_code=403)
    quote_data = {}
    if form.quote_cost_data:
        try:
            quote_data = json.loads(form.quote_cost_data)
        except Exception:
            pass
    # 查詢對應 BU 的 BU Head（若無指定 bu 則取任一位 BU）
    bu_head_q = select(User).where(User.role == Role.BU, User.is_active == True)
    if form.bu is not None:
        bu_head_q = bu_head_q.where(User.bu == form.bu)
    bu_head = (await db.execute(bu_head_q)).scalars().first()
    return templates.TemplateResponse("npi_forms/customer_quote.html", {
        "request": request, "user": current_user,
        "form": form, "quote_data": quote_data,
        "bu_head": bu_head,
        "now": datetime.utcnow(),
    })


# ── 業務完成成本分析 → 發客戶報價 ──────────

async def _build_rfq_archive_pdf(form: NPIForm, db: AsyncSession, out_path: str) -> str:
    """把 form + invites + quote_data + BU Head 組成 dict 餵給 PDF 產生器。"""
    creator_name = form.creator.display_name if form.creator else None
    bu_head_name = None
    if form.bu:
        q = select(User).where(User.role == Role.BU, User.bu == form.bu, User.is_active == True)
        bu_head = (await db.execute(q)).scalars().first()
        if bu_head:
            bu_head_name = bu_head.display_name
    quote_data = {}
    if form.quote_cost_data:
        try:
            quote_data = json.loads(form.quote_cost_data)
        except Exception:
            pass
    shared_mat = ""
    shared_qty = ""
    for inv in form.invites or []:
        if not shared_mat and inv.material:
            shared_mat = inv.material
        if not shared_qty and inv.qty:
            shared_qty = inv.qty
    form_dict = {
        "form_id": form.form_id,
        "customer_name": form.customer_name,
        "customer_contact": form.customer_contact,
        "customer_email": form.customer_email,
        "product_name": form.product_name,
        "product_model": form.product_model,
        "spec_summary": form.spec_summary,
        "bu": form.bu.value if form.bu else None,
        "sales_note": form.sales_note,
        "_shared_mat": shared_mat,
        "_shared_qty": shared_qty,
    }
    invites_list = []
    for inv in form.invites or []:
        invites_list.append({
            "supplier_name": inv.supplier.name if inv.supplier else None,
            "process_name": inv.process_name,
            "material": inv.material,
            "qty": inv.qty,
            "quote_amount": inv.quote_amount,
            "tooling_cost": inv.tooling_cost,
            "lead_time_days": inv.lead_time_days,
            "is_selected": inv.is_selected,
        })
    return rfq_archive.build_archive_pdf(
        form_dict, invites_list, quote_data, creator_name, bu_head_name, out_path,
    )


def _rfq_archive_path(form: NPIForm, quote_data: dict) -> str:
    fname = rfq_archive.archive_filename(form.form_id, quote_data)
    return os.path.join(notif.NAS_ROOT, form.form_id, "RFQ_Archive", fname)


async def _build_sale_cost_analysis_pdf(form: NPIForm, db: AsyncSession, out_path: str) -> str:
    """售價成本分析表 PDF = 議價後成本 + 原報價利潤 → 售價。"""
    creator_name = form.creator.display_name if form.creator else None
    bu_head_name = None
    if form.bu:
        try:
            q = select(User).where(User.role == Role.BU, User.bu == form.bu, User.is_active == True)
            bu_head = (await db.execute(q)).scalars().first()
            if bu_head:
                bu_head_name = bu_head.display_name
        except Exception:
            bu_head_name = None
    quote_data = {}
    if form.quote_cost_data:
        try: quote_data = json.loads(form.quote_cost_data)
        except Exception: pass
    bargain_data = {}
    if form.bargain_data:
        try: bargain_data = json.loads(form.bargain_data)
        except Exception: pass
    form_dict = {
        "form_id": form.form_id,
        "customer_name": form.customer_name,
        "customer_contact": form.customer_contact,
        "customer_email": form.customer_email,
        "product_name": form.product_name,
        "product_model": form.product_model,
        "spec_summary": form.spec_summary,
        "bu": (form.bu.value if hasattr(form.bu, "value") else form.bu) if form.bu else None,
        "sales_note": form.sales_note,
    }
    invites_list = [{
        "supplier_name": inv.supplier.name if inv.supplier else None,
        "process_name": inv.process_name,
    } for inv in (form.invites or [])]
    # T1 計畫：對應每張圖面，含客戶需求 + 實際開模
    t1_map = {}
    if form.t1_plan_data:
        try: t1_map = json.loads(form.t1_plan_data) or {}
        except Exception: t1_map = {}
    t1_plan = []
    for d in (form.documents or []):
        if d.category == "圖面":
            row = t1_map.get(str(d.id)) or t1_map.get(d.id) or {}
            t1_plan.append({
                "drawing_name": d.original_name,
                "t1_date":      row.get("t1_date") or "",
                "actual_t1_date": row.get("actual_t1_date") or "",
            })
    return rfq_archive.build_sale_cost_analysis_pdf(
        form_dict, invites_list, quote_data, bargain_data,
        creator_name, bu_head_name, out_path,
        t1_plan=t1_plan,
    )


def _sale_cost_analysis_path(form: NPIForm) -> str:
    return os.path.join(notif.NAS_ROOT, form.form_id, "NPI_Closure", "售價成本分析表.pdf")


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
    if form.status != NPIFormStatus.QUOTE_APPROVED:
        raise HTTPException(status_code=400, detail="需先由 BU 核准報價才能發送客戶")
    if current_user.role not in _SALES_ROLES:
        raise HTTPException(status_code=403)
    if attach_files:
        await _save_attachments(db, form.id, current_user.id, attach_files, attach_categories)
        await db.commit()
        db.expire_all()
        form = await _get_form_or_404(form_id, db)
    # 發送客戶時不強制驗證附件（可已於試算階段上傳）
    old = form.status
    form.cost_analysis_note = cost_analysis_note or None
    form.status = NPIFormStatus.RFQ_DONE
    form.updated_at = datetime.utcnow()

    # 產出結案歸檔 PDF → NAS
    quote_data = {}
    if form.quote_cost_data:
        try:
            quote_data = json.loads(form.quote_cost_data)
        except Exception:
            pass
    archive_path = _rfq_archive_path(form, quote_data)
    try:
        await _build_rfq_archive_pdf(form, db, archive_path)
        form.nas_folder = os.path.dirname(archive_path)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"RFQ archive PDF 生成失敗：{e}")

    db.add(_log_approval(form, current_user, "SEND_CUSTOMER_QUOTE", old, form.status,
                         comment or f"結案：報價歸檔 PDF → {archive_path}"))
    await db.commit()
    await notif.notify_sales_cost_analysis_done(db, form)
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


@router.post("/{form_id}/save-t1-plan")
async def save_t1_plan(
    form_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """儲存每張圖的 T1 計畫：
      • 業務 → 更新 `t1_date`（客戶需求 T1 與樣品提供時間）
      • 採購 → 更新 `actual_t1_date`（實際開模 T1 時間）
      同一張 JSON，按角色只覆寫該角色允許的欄位，其他欄位保留。
    """
    form = await _get_form_or_404(form_id, db)
    is_sales = current_user.role in (*_SALES_ROLES, Role.ADMIN)
    is_purch = current_user.role in (Role.PURCHASE, Role.ADMIN)
    if not (is_sales or is_purch):
        raise HTTPException(status_code=403, detail="僅業務 / 採購可維護 T1 計畫")
    form_data = await request.form()
    ids = form_data.getlist("drawing_id")
    t1_dates = form_data.getlist("t1_date")
    actual_dates = form_data.getlist("actual_t1_date")

    # 先載入現有計畫，逐欄位覆寫以保留他角色的資料
    try:
        existing = json.loads(form.t1_plan_data) if form.t1_plan_data else {}
    except Exception:
        existing = {}

    for i, did in enumerate(ids):
        if not did:
            continue
        key = str(did)
        row = dict(existing.get(key) or {})
        if is_sales and i < len(t1_dates):
            row["t1_date"] = (t1_dates[i] or "").strip()
        if is_purch and i < len(actual_dates):
            row["actual_t1_date"] = (actual_dates[i] or "").strip()
        existing[key] = row

    form.t1_plan_data = json.dumps(existing, ensure_ascii=False) if existing else None
    form.updated_at = datetime.utcnow()
    await db.commit()

    # 只有業務儲存時才通知工程接手；採購的實際日期屬後段資訊
    if is_sales and not is_purch:
        try:
            await notif._notify_roles(
                db, [Role.ENGINEER],
                f"【NPI 業務已提供 T1 計畫】{form.form_id} - 客戶需求 T1 / 樣品時間已填寫，請工程接手推進",
            )
        except Exception:
            pass
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


@router.post("/{form_id}/submit-mould-requisition")
async def submit_mould_requisition(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    erp_req_no: str = Form(""),
    attach_files: List[UploadFile] = File(default=[]),
    attach_categories: List[str] = Form(default=[]),
):
    """工程送出模治具請購單：ERP 單號 + 附件；
    送出前檢查所有有 tooling_cost 的站都已填 tool_part_no。
    """
    form = await _get_form_or_404(form_id, db)
    if current_user.role not in (*_ENG_ROLES, Role.ADMIN):
        raise HTTPException(status_code=403, detail="僅工程可送出模治具請購單")
    if form.stage != NPIStage.NPI:
        raise HTTPException(status_code=400, detail="需進入 NPI 階段")
    if form.erp_req_no:
        raise HTTPException(status_code=400, detail="已送出過，若需修改請聯絡管理員")
    erp_req_no = (erp_req_no or "").strip()
    if not erp_req_no:
        raise HTTPException(status_code=400, detail="請填寫 ERP 模具請購單號")
    # 檢查：每個有 tooling_cost 的站，eng_process_data 必須有 tool_part_no
    try:
        eng_map = json.loads(form.eng_process_data) if form.eng_process_data else {}
    except Exception:
        eng_map = {}
    missing = []
    for inv in form.invites or []:
        if inv.tooling_cost and inv.tooling_cost > 0 and inv.process_name:
            saved = eng_map.get(inv.process_name) or {}
            if not saved.get("tool_part_no"):
                if inv.process_name not in missing:
                    missing.append(inv.process_name)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"以下站別尚未填寫模具料號：{', '.join(missing)}",
        )
    # 儲存 ERP 單號 + 附件
    form.erp_req_no = erp_req_no
    if attach_files and any(f.filename for f in attach_files):
        await _save_attachments(db, form.id, current_user.id, attach_files, attach_categories)
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "MOULD_REQ_SUBMIT",
                         form.status, form.status,
                         f"送出模治具請購單 ERP {erp_req_no}"))
    await db.commit()
    try:
        await notif._notify_roles(
            db, [Role.PURCHASE, Role.SALES],
            f"【模治具請購單已送出】{form.form_id} - ERP {erp_req_no}，請採購接手議價",
        )
    except Exception:
        pass
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


@router.post("/{form_id}/save-eng-process")
async def save_eng_process(
    form_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """工程於 NPI 階段填寫：每站廠內料號 + 是否建途程（SFT）。

    接收 process_name[]/part_no[]/need_routing[] 平行陣列。
    """
    form = await _get_form_or_404(form_id, db)
    if current_user.role not in (*_ENG_ROLES, Role.ADMIN):
        raise HTTPException(status_code=403, detail="僅工程可維護廠內料號 / 途程")
    form_data = await request.form()
    procs = form_data.getlist("process_name")
    parts = form_data.getlist("part_no")
    tool_parts = form_data.getlist("tool_part_no")
    routings_raw = set(form_data.getlist("need_routing"))  # 只勾選才會出現
    data = {}
    for i, pn in enumerate(procs):
        if not pn:
            continue
        data[pn] = {
            "part_no":      (parts[i] if i < len(parts) else "") or "",
            "tool_part_no": (tool_parts[i] if i < len(tool_parts) else "") or "",
            "need_routing": pn in routings_raw,
        }
    form.eng_process_data = json.dumps(data, ensure_ascii=False) if data else None
    form.updated_at = datetime.utcnow()
    await db.commit()
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


@router.post("/{form_id}/save-bargain")
async def save_bargain(
    form_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """採購議價：回填各製程單價 / 站別模治具 + 狀態 flag + 確定議價的新報價單。

    欄位命名：
      price_r{i}_c{j}        採購議價後單價
      tooling_{proc_key}     站別模治具議價後金額
      flag_r{i}_c{j}         議價狀態：no_bargain / no_room / confirmed / 空
      flag_t_{proc_key}      模治具欄位議價狀態
      confirm_file_r{i}_c{j} 確定議價時的新報價單 PDF（會取代對應 invite 的供應商報價）
      note                   議價備註
    """
    form = await _get_form_or_404(form_id, db)
    if current_user.role not in (Role.PURCHASE, Role.ADMIN):
        raise HTTPException(status_code=403, detail="僅採購可議價")
    if form.stage != NPIStage.NPI:
        raise HTTPException(status_code=400, detail="需進入 NPI 階段")
    form_data = await request.form()
    prices: dict[str, float] = {}
    tooling: dict[str, float] = {}
    flags: dict[str, str] = {}
    files: dict[str, UploadFile] = {}
    for key, val in form_data.multi_items():
        if key.startswith("flag_"):
            s = str(val or "").strip()
            if s in ("no_bargain", "no_room", "confirmed"):
                flags[key[5:]] = s
            continue
        if key.startswith("confirm_file_") and hasattr(val, "filename") and val.filename:
            files[key[len("confirm_file_"):]] = val
            continue
        if not val or str(val).strip() == "":
            continue
        if key.startswith("price_"):
            try: prices[key[6:]] = float(val)
            except (TypeError, ValueError): pass
        elif key.startswith("tooling_"):
            try: tooling[key[8:]] = float(val)
            except (TypeError, ValueError): pass
    note = (form_data.get("note") or "").strip() or None
    erp_po_no = (form_data.get("erp_po_no") or "").strip() or None
    erp_keyin_all = bool(form_data.get("erp_keyin_all"))
    form.bargain_data = json.dumps(
        {"prices": prices, "tooling": tooling, "flags": flags,
         "note": note, "erp_po_no": erp_po_no,
         "erp_keyin_all": erp_keyin_all},
        ensure_ascii=False,
    )

    # 議價後報價單：依 (row.process, col.label) 對應 invite，存為「議價後報價單」類別顯示於附件區
    if files and form.quote_cost_data:
        try:
            qd = json.loads(form.quote_cost_data)
            qd_rows = qd.get("rows") or []
            qd_cols = qd.get("columns") or []
        except Exception:
            qd_rows, qd_cols = [], []
        upload_dir = _upload_dir(form.id)
        for key, up in files.items():
            try:
                rpart, cpart = key.split("_")
                ri = int(rpart[1:]); ci = int(cpart[1:])
            except Exception:
                continue
            if ri >= len(qd_rows) or ci >= len(qd_cols):
                continue
            proc_name = (qd_rows[ri].get("process") or "").strip()
            col_label = (qd_cols[ci].get("label") or "").strip()
            target_inv = None
            for inv in form.invites or []:
                if inv.process_name and inv.supplier and inv.process_name == proc_name and inv.supplier.name == col_label:
                    target_inv = inv
                    break
            content = await up.read()
            if not content:
                continue
            ext = os.path.splitext(up.filename)[1] or ".bin"
            saved = f"{uuid.uuid4().hex}{ext}"
            with open(os.path.join(upload_dir, saved), "wb") as fh:
                fh.write(content)
            db.add(NPIDocument(
                form_id_fk    = form.id,
                invite_id_fk  = target_inv.id if target_inv else None,
                filename      = saved,
                original_name = f"[{proc_name}/{col_label}] {up.filename}",
                category      = "議價後報價單",
                uploaded_by   = current_user.id,
            ))

    form.updated_at = datetime.utcnow()
    await db.commit()

    # 自動將「售價成本分析表」PDF 輸出到 NAS 結案資料夾（不開窗，後台執行）
    try:
        await db.refresh(form)
        out_path = _sale_cost_analysis_path(form)
        await _build_sale_cost_analysis_pdf(form, db, out_path)
        form.nas_folder = os.path.dirname(out_path)
        await db.commit()
    except Exception as e:
        logging.getLogger(__name__).warning("售價成本分析表 PDF 產出失敗：%s", e)

    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


@router.post("/{form_id}/close-npi")
async def close_npi(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """採購結案 — 產出售價成本分析表 PDF 到 NAS + 狀態切到 CLOSED + 通知業務 / BU。"""
    form = await _get_form_or_404(form_id, db)
    if current_user.role not in (Role.PURCHASE, Role.ADMIN):
        raise HTTPException(status_code=403, detail="僅採購可結案")
    if form.stage != NPIStage.NPI:
        raise HTTPException(status_code=400, detail="需進入 NPI 階段")
    if form.status not in (NPIFormStatus.NPI_PENDING_PURCHASE,):
        raise HTTPException(status_code=400, detail="目前狀態不允許結案")

    out_path = _sale_cost_analysis_path(form)
    try:
        await _build_sale_cost_analysis_pdf(form, db, out_path)
    except Exception as e:
        logging.getLogger(__name__).exception("結案 PDF 產出失敗")
        raise HTTPException(status_code=500, detail=f"PDF 產出失敗：{e}")

    form.nas_folder = os.path.dirname(out_path)
    old = form.status
    form.status = NPIFormStatus.CLOSED
    form.updated_at = datetime.utcnow()
    db.add(_log_approval(form, current_user, "CLOSE_NPI",
                         old, NPIFormStatus.CLOSED,
                         f"議價結案，PDF → {out_path}"))
    await db.commit()
    try:
        await notif._notify_roles(
            db, [Role.SALES, Role.BU],
            f"【NPI 結案】{form.form_id} - 已產出售價成本分析表，請至 NAS 查看。",
        )
    except Exception:
        pass
    return RedirectResponse(url=f"/npi-forms/{form_id}", status_code=303)


@router.get("/{form_id}/cost-analysis.pdf")
async def download_sale_cost_analysis(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """售價成本分析表 PDF — 採購/BU/管理員。"""
    form = await _get_form_or_404(form_id, db)
    if current_user.role not in (Role.PURCHASE, *_BU_ROLES, Role.ADMIN):
        raise HTTPException(status_code=403)
    out_path = _sale_cost_analysis_path(form)
    try:
        await _build_sale_cost_analysis_pdf(form, db, out_path)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logging.getLogger(__name__).exception("售價成本分析表 PDF 產出失敗")
        return HTMLResponse(
            content=(
                f"<html><body style='font-family:monospace;padding:20px;'>"
                f"<h3>PDF 產出失敗</h3><pre>{tb}</pre></body></html>"
            ),
            status_code=500,
        )
    fname = os.path.basename(out_path)
    return FileResponse(
        out_path, media_type="application/pdf",
        filename=fname,
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{urlquote(fname)}"},
    )


@router.get("/{form_id}/archive.pdf")
async def download_rfq_archive(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """結案歸檔 PDF 下載 — 僅 BU / admin（業務不提供下載，歸檔僅存於 NAS）。"""
    form = await _get_form_or_404(form_id, db)
    if current_user.role not in (*_BU_ROLES, Role.ADMIN):
        raise HTTPException(status_code=403)
    if form.status not in (NPIFormStatus.QUOTE_APPROVED, NPIFormStatus.RFQ_DONE,
                           NPIFormStatus.NPI_STARTED, NPIFormStatus.CLOSED):
        raise HTTPException(status_code=400, detail="需先 BU 核准報價後才能下載歸檔 PDF")
    quote_data = {}
    if form.quote_cost_data:
        try:
            quote_data = json.loads(form.quote_cost_data)
        except Exception:
            pass
    archive_path = _rfq_archive_path(form, quote_data)
    if not os.path.exists(archive_path):
        await _build_rfq_archive_pdf(form, db, archive_path)
    fname = os.path.basename(archive_path)
    return FileResponse(
        archive_path, media_type="application/pdf",
        filename=fname,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urlquote(fname)}"},
    )


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
    # 可選製程（ERP 連接口 — stub 模式回固定清單；未來從 ERP 讀）
    from app.services import erp_client as _erp
    erp_processes = _erp.fetch_processes_from_erp()
    # 業務上傳的圖面附件（派發 modal 的「對應圖面」下拉來源）
    drawings = [d for d in form.documents if (d.category or "") == "圖面"]
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
        "erp_processes": erp_processes,
        "drawings": drawings,
    })


# ── ERP 請購單即時查詢 ───────────────────────

@router.get("/erp-req-lookup")
async def erp_req_lookup(
    req_no: str,
    current_user: User = Depends(get_current_user),
):
    """依請購單號即時查詢 ERP 明細，回 JSON 供前端渲染。"""
    from app.services.erp_client import erp_query_purchase_requisition
    if not req_no or not req_no.strip():
        raise HTTPException(status_code=400, detail="請填寫請購單號")
    rows = erp_query_purchase_requisition(req_no.strip())
    if not rows:
        raise HTTPException(status_code=404, detail=f"ERP 找不到請購單：{req_no}")
    return {"ok": True, "req_no": req_no.strip(), "rows": rows}


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
