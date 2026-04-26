"""QC 異常管理系統（NCR）路由

流程（草案，後續再迭代）：
  DRAFT (品保填寫 IPC)
    → PENDING_DISPOSITION (品保下處理判斷：退貨/實驗/特採)
    → PENDING_RCA (Mail 通知 + 根因分析)
    → PENDING_IMPROVEMENT (制定長期改善方案 — 圖面/SOP/SIP)
    → LINKED_ECN (若需修訂圖面/SOP/SIP，開 ECN 連結進去)
    → CLOSED
"""
import os, uuid, logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.user import User, Role
from app.models.qc_exception import (
    QCException, QCExceptionDocument, QCExceptionApproval,
    QCExceptionStatus, QCDisposition, QCExceptionStage,
    QCDocType, QCEventDateType,
)
from app.services.auth import get_current_user
from app.services import qc_notification as qc_notif

router    = APIRouter(prefix="/qc-exceptions")
templates = Jinja2Templates(directory="app/templates")

UPLOAD_BASE = "uploads"
ATTACH_CATEGORIES = ["異常照片", "實驗報告", "圖面", "其它"]

_QC_ROLES    = (Role.QC, Role.ADMIN)                                     # 處理判斷 / RCA / 改善方案 專屬
_CREATE_ROLES = (Role.QC, Role.PROD_MGR, Role.ASSISTANT, Role.ADMIN)     # 建單權限：品保 + 產線主管 + 業助
_VIEW_ROLES  = (Role.QC, Role.ENGINEER, Role.ENG_MGR, Role.PURCHASE,
                Role.PROD_MGR, Role.ASSISTANT, Role.BU, Role.ADMIN)


# ── 共用 helper ─────────────────────────────────

def _upload_dir(form_pk: int) -> str:
    p = os.path.join(UPLOAD_BASE, f"qc_{form_pk}")
    os.makedirs(p, exist_ok=True)
    return p


async def _next_form_id(db: AsyncSession) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"NCR-{today}-"
    r = await db.execute(select(QCException).where(QCException.form_id.like(f"{prefix}%")))
    rows = list(r.scalars().all())
    seq = len(rows) + 1
    return f"{prefix}{seq:03d}"


async def _get_or_404(form_id: str, db: AsyncSession) -> QCException:
    r = await db.execute(
        select(QCException)
        .options(
            selectinload(QCException.creator),
            selectinload(QCException.assigned_qc),
            selectinload(QCException.dispositioner),
            selectinload(QCException.linked_ecn),
            selectinload(QCException.documents),
            selectinload(QCException.approvals).selectinload(QCExceptionApproval.approver),
        )
        .where(QCException.form_id == form_id)
    )
    f = r.scalars().first()
    if not f:
        raise HTTPException(status_code=404, detail="QC 異常單不存在")
    return f


def _log(form: QCException, user: User, action: str,
         from_s: QCExceptionStatus | None, to_s: QCExceptionStatus | None,
         comment: str = "", reject_target: str | None = None):
    return QCExceptionApproval(
        form_id_fk=form.id, approver_id=user.id, action=action,
        comment=comment or None, reject_target=reject_target,
        from_status=from_s.value if from_s else None,
        to_status=to_s.value if to_s else None,
    )


def _docs_by_cat(docs):
    out = {}
    for d in (docs or []):
        out.setdefault(d.category or "其它", []).append(d)
    return out


async def _save_attachments(db, form_pk, user_id, files, categories):
    if not files:
        return
    upload_dir = _upload_dir(form_pk)
    for i, uf in enumerate(files):
        if not uf or not uf.filename:
            continue
        content = await uf.read()
        if not content:
            continue
        ext = os.path.splitext(uf.filename)[1] or ".bin"
        saved = f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(upload_dir, saved), "wb") as f:
            f.write(content)
        cat = (categories[i] if i < len(categories) else "其它")
        if cat not in ATTACH_CATEGORIES:
            cat = "其它"
        db.add(QCExceptionDocument(
            form_id_fk=form_pk, filename=saved, original_name=uf.filename,
            category=cat, uploaded_by=user_id,
        ))


# ── 列表 ────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def list_qc(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in _VIEW_ROLES:
        raise HTTPException(status_code=403, detail="您的角色無權限存取 QC 異常管理")
    q = (select(QCException)
         .options(selectinload(QCException.creator), selectinload(QCException.linked_ecn))
         .order_by(QCException.created_at.desc()))
    r = await db.execute(q)
    forms = list(r.scalars().all())
    return templates.TemplateResponse("qc_exceptions/list.html", {
        "request": request, "user": current_user,
        "forms": forms,
        "QCExceptionStatus": QCExceptionStatus,
        "QCDisposition": QCDisposition,
    })


# ── 新建（顯示 IPC 異常資訊表單） ────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_qc_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in _CREATE_ROLES:
        raise HTTPException(status_code=403, detail="僅品保 / 產線主管 / 業助可新建 QC 異常單")
    return templates.TemplateResponse("qc_exceptions/new.html", {
        "request": request, "user": current_user,
        "QCExceptionStage": QCExceptionStage,
        "QCDocType": QCDocType,
        "QCEventDateType": QCEventDateType,
        "ATTACH_CATEGORIES": ATTACH_CATEGORIES,
    })


@router.post("/new")
async def create_qc(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    part_no:          str = Form(""),
    doc_type:         str = Form("RECEIVE"),
    receive_doc_no:   str = Form(""),
    event_date_type:  str = Form("RECEIVE"),
    receive_date:     str = Form(""),
    stage:            str = Form("IQC"),
    supplier_name:    str = Form(""),
    receive_qty:      str = Form(""),
    defect_cause:     str = Form(""),
    measurement_data: str = Form(""),
    defect_qty:       str = Form(""),
    sample_qty:       str = Form(""),
    attach_files:      List[UploadFile] = File(default=[]),
    attach_categories: List[str] = Form(default=[]),
    submit_action:    str = Form("draft"),  # draft | submit
):
    if current_user.role not in _CREATE_ROLES:
        raise HTTPException(status_code=403)
    if not (part_no.strip() and defect_cause.strip()):
        raise HTTPException(status_code=400, detail="品號與異常原因為必填")

    def _int(s):
        try: return int(s)
        except (TypeError, ValueError): return None

    dq = _int(defect_qty)
    sq = _int(sample_qty)
    rate = (dq / sq) if (dq is not None and sq and sq > 0) else None

    try:
        st_enum = QCExceptionStage(stage)
    except ValueError:
        st_enum = QCExceptionStage.IQC
    try:
        dt_enum = QCDocType(doc_type)
    except ValueError:
        dt_enum = QCDocType.RECEIVE
    try:
        edt_enum = QCEventDateType(event_date_type)
    except ValueError:
        edt_enum = QCEventDateType.RECEIVE

    form_id = await _next_form_id(db)
    initial_status = (QCExceptionStatus.PENDING_DISPOSITION
                      if submit_action == "submit"
                      else QCExceptionStatus.DRAFT)
    qc = QCException(
        form_id=form_id, status=initial_status,
        part_no=part_no.strip(),
        doc_type=dt_enum, receive_doc_no=receive_doc_no.strip() or None,
        event_date_type=edt_enum, receive_date=receive_date.strip() or None,
        stage=st_enum, supplier_name=supplier_name.strip() or None,
        receive_qty=_int(receive_qty), defect_cause=defect_cause.strip(),
        measurement_data=measurement_data.strip() or None,
        defect_qty=dq, sample_qty=sq, defect_rate=rate,
        created_by=current_user.id,
        # 建單者若不是品保，assigned_qc 留空待品保接手
        assigned_qc_id=(current_user.id if current_user.role in _QC_ROLES else None),
    )
    db.add(qc)
    await db.commit()
    await db.refresh(qc)
    if attach_files:
        await _save_attachments(db, qc.id, current_user.id, attach_files, attach_categories)
        await db.commit()
    db.add(_log(qc, current_user,
                "SUBMIT" if initial_status != QCExceptionStatus.DRAFT else "CREATE",
                None, initial_status, "建立 QC 異常單"))
    await db.commit()
    # 送出（非草稿）才通知 LINE 群組 + 相關角色，避免草稿就吵到大家
    if initial_status != QCExceptionStatus.DRAFT:
        try:
            await qc_notif.notify_exception_created(
                db, qc, creator_name=(current_user.display_name or current_user.username))
        except Exception:
            logging.exception("notify_exception_created failed")
    return RedirectResponse(url=f"/qc-exceptions/{form_id}", status_code=303)


# ── 詳情 ────────────────────────────────────────

@router.get("/{form_id}", response_class=HTMLResponse)
async def detail_qc(
    form_id: str, request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in _VIEW_ROLES:
        raise HTTPException(status_code=403)
    form = await _get_or_404(form_id, db)
    transition_combo = {
        "DRAFT→PENDING_DISPOSITION":              ("品保送審",       "primary"),
        "PENDING_DISPOSITION→PENDING_RCA":         ("品保下處理判斷", "info"),
        "PENDING_RCA→PENDING_IMPROVEMENT":         ("根因分析完成",   "info"),
        "PENDING_IMPROVEMENT→LINKED_ECN":          ("綁入 ECN",       "warning"),
        "PENDING_IMPROVEMENT→CLOSED":              ("結案",           "dark"),
        "LINKED_ECN→CLOSED":                       ("ECN 已結案 → 結案", "dark"),
    }
    return templates.TemplateResponse("qc_exceptions/detail.html", {
        "request": request, "user": current_user, "form": form,
        "docs_by_cat": _docs_by_cat(form.documents),
        "transition_combo": transition_combo,
        "QCExceptionStatus": QCExceptionStatus,
        "QCDisposition": QCDisposition,
        "QCExceptionStage": QCExceptionStage,
        "ATTACH_CATEGORIES": ATTACH_CATEGORIES,
    })


# ── 狀態流轉 ────────────────────────────────────

@router.post("/{form_id}/disposition")
async def set_disposition(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    disposition: str = Form(...),     # RETURN_TO_SUPPLIER / LAB_TEST / SPECIAL_ACCEPT
    note:        str = Form(""),
):
    """品保下處理判斷 → 進 PENDING_RCA"""
    form = await _get_or_404(form_id, db)
    if current_user.role not in _QC_ROLES:
        raise HTTPException(status_code=403)
    if form.status not in (QCExceptionStatus.PENDING_DISPOSITION, QCExceptionStatus.DRAFT):
        raise HTTPException(status_code=400, detail="目前狀態無法下處理判斷")
    try:
        d = QCDisposition(disposition)
    except ValueError:
        raise HTTPException(status_code=400, detail="無效的處理判斷")
    old = form.status
    form.disposition = d
    form.disposition_note = note.strip() or None
    form.disposition_at = datetime.utcnow()
    form.disposition_by = current_user.id
    form.status = QCExceptionStatus.PENDING_RCA
    form.updated_at = datetime.utcnow()
    db.add(_log(form, current_user, "DISPOSITION", old, form.status,
                f"處理判斷：{d.value}｜{note.strip()[:120]}"))
    await db.commit()
    try:
        await qc_notif.notify_disposition(
            db, form, disposer_name=(current_user.display_name or current_user.username))
    except Exception:
        logging.exception("notify_disposition failed")
    return RedirectResponse(url=f"/qc-exceptions/{form_id}", status_code=303)


@router.post("/{form_id}/save-rca")
async def save_rca(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    notify_mail_to: str = Form(""),
    notify_mail_cc: str = Form(""),
    root_cause:     str = Form(""),
    advance:        str = Form(""),  # "1" → 順便推進到 PENDING_IMPROVEMENT
):
    """填寫 Mail 收件人 / 根因分析；可選擇順便推進"""
    form = await _get_or_404(form_id, db)
    if current_user.role not in _QC_ROLES:
        raise HTTPException(status_code=403)
    if form.status not in (QCExceptionStatus.PENDING_RCA, QCExceptionStatus.PENDING_IMPROVEMENT):
        raise HTTPException(status_code=400, detail="目前狀態無法編輯根因分析")
    form.notify_mail_to = notify_mail_to.strip() or None
    form.notify_mail_cc = notify_mail_cc.strip() or None
    form.root_cause = root_cause.strip() or None
    if advance == "1" and form.status == QCExceptionStatus.PENDING_RCA and root_cause.strip():
        old = form.status
        form.status = QCExceptionStatus.PENDING_IMPROVEMENT
        form.notify_sent_at = datetime.utcnow()
        db.add(_log(form, current_user, "RCA_DONE", old, form.status,
                    "已 Mail 通知 + 完成根因分析"))
    form.updated_at = datetime.utcnow()
    await db.commit()
    return RedirectResponse(url=f"/qc-exceptions/{form_id}", status_code=303)


@router.post("/{form_id}/save-improvement")
async def save_improvement(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    need_drawing_rev: str = Form(""),
    need_sop_rev:     str = Form(""),
    need_sip_rev:     str = Form(""),
    improvement_plan: str = Form(""),
    advance:          str = Form(""),  # "ecn" / "close"
):
    """改善方案：勾選需修訂項目 + 內容；可選擇推進到 LINKED_ECN 或直接結案"""
    form = await _get_or_404(form_id, db)
    if current_user.role not in _QC_ROLES:
        raise HTTPException(status_code=403)
    if form.status not in (QCExceptionStatus.PENDING_IMPROVEMENT, QCExceptionStatus.LINKED_ECN):
        raise HTTPException(status_code=400, detail="目前狀態無法編輯改善方案")
    form.need_drawing_rev = (need_drawing_rev == "1")
    form.need_sop_rev     = (need_sop_rev == "1")
    form.need_sip_rev     = (need_sip_rev == "1")
    form.improvement_plan = improvement_plan.strip() or None
    if advance == "ecn" and form.status == QCExceptionStatus.PENDING_IMPROVEMENT:
        old = form.status
        form.status = QCExceptionStatus.LINKED_ECN
        db.add(_log(form, current_user, "TO_ECN", old, form.status,
                    "需修訂圖面/SOP/SIP，待開 ECN"))
    elif advance == "close":
        old = form.status
        form.status = QCExceptionStatus.CLOSED
        db.add(_log(form, current_user, "CLOSE", old, form.status, "結案"))
    form.updated_at = datetime.utcnow()
    await db.commit()
    return RedirectResponse(url=f"/qc-exceptions/{form_id}", status_code=303)


@router.post("/{form_id}/link-ecn")
async def link_ecn(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ecn_form_id: str = Form(...),  # PCNForm.form_id (e.g. PCN-20260424-001)
):
    """把已建立的 ECN 表單綁進來"""
    form = await _get_or_404(form_id, db)
    if current_user.role not in _QC_ROLES:
        raise HTTPException(status_code=403)
    from app.models.pcn_form import PCNForm
    r = await db.execute(select(PCNForm).where(PCNForm.form_id == ecn_form_id.strip()))
    ecn = r.scalars().first()
    if not ecn:
        raise HTTPException(status_code=404, detail="找不到該 ECN 表單")
    form.linked_ecn_form_id = ecn.id
    if form.status == QCExceptionStatus.PENDING_IMPROVEMENT:
        old = form.status
        form.status = QCExceptionStatus.LINKED_ECN
        db.add(_log(form, current_user, "LINK_ECN", old, form.status,
                    f"綁定 ECN {ecn.form_id}"))
    form.updated_at = datetime.utcnow()
    await db.commit()
    return RedirectResponse(url=f"/qc-exceptions/{form_id}", status_code=303)


@router.post("/{form_id}/close")
async def close_qc(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    comment: str = Form(""),
):
    form = await _get_or_404(form_id, db)
    if current_user.role not in _QC_ROLES:
        raise HTTPException(status_code=403)
    if form.status == QCExceptionStatus.CLOSED:
        raise HTTPException(status_code=400, detail="已結案")
    old = form.status
    form.status = QCExceptionStatus.CLOSED
    form.updated_at = datetime.utcnow()
    db.add(_log(form, current_user, "CLOSE", old, form.status, comment.strip() or "結案"))
    await db.commit()
    return RedirectResponse(url=f"/qc-exceptions/{form_id}", status_code=303)
