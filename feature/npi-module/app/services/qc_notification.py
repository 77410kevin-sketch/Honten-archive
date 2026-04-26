"""QC 異常管理通知服務

通知策略：
1. 若環境變數設定 `LINE_CHANNEL_ACCESS_TOKEN` + `LINE_QC_GROUP_ID`，
   會透過 LINE Messaging API 推到 QC 異常群組
2. 同時對相關角色（品保 / 工程 / 採購 / 產線主管 / 業助 / BU）做角色推播 fallback
3. 兩個都沒設定 → console log（dry-run 模式）

需要 .env：
    LINE_CHANNEL_ACCESS_TOKEN=...
    LINE_QC_GROUP_ID=Cxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
"""
import os
import logging
from typing import Iterable

from app.models.qc_exception import (
    QCException, QCDocType, QCEventDateType, QCExceptionStage,
)
from app.models.user import Role
from app.services import npi_notification as _ntf

logger = logging.getLogger(__name__)

LINE_TOKEN     = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_QC_GROUP  = os.getenv("LINE_QC_GROUP_ID", "").strip()

_DOC_LBL  = {"RECEIVE": "進貨單號", "PROCESS": "製程單號", "SHIP_DC": "出貨 D/C"}
_DATE_LBL = {"RECEIVE": "進貨日期", "PRODUCE": "生產日期",
             "SHIP": "出貨日期", "COMPLAINT": "客訴日期"}
_STAGE_LBL = {"IQC": "IQC", "IPQC": "IPQC", "OQC": "OQC", "INSPECTION": "品檢",
              "LASER": "雷雕", "CNC": "CNC", "ASSEMBLY": "組裝", "OTHER": "其他"}


def _send_line_group(group_id: str, message: str) -> bool:
    """送到 LINE 群組；無 token/group 則 console log（dry-run）。回傳是否真的送出。"""
    if not (LINE_TOKEN and group_id):
        logger.info(f"[LINE GROUP dry-run] target={group_id or '(unset)'}: {message[:100]}")
        print(f"\n📱 [LINE GROUP dry-run] {group_id or '(未設定 LINE_QC_GROUP_ID)'}\n"
              f"───────────────────────\n{message}\n───────────────────────\n")
        return False
    try:
        import json as _json
        from urllib import request as _urlreq, error as _urlerr
        req = _urlreq.Request(
            "https://api.line.me/v2/bot/message/push",
            data=_json.dumps({
                "to": group_id,
                "messages": [{"type": "text", "text": message[:4900]}],
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {LINE_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                logger.info(f"[LINE group push OK] group={group_id}")
                return True
            body = resp.read().decode("utf-8", errors="ignore")[:200]
            logger.error(f"[LINE group push FAIL] {resp.status}: {body}")
            return False
    except _urlerr.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:200] if e.fp else ""
        logger.error(f"[LINE group push HTTPError] {e.code}: {body}")
        return False
    except Exception as e:
        logger.error(f"[LINE group push ERROR] {e}")
        return False


def build_exception_message(form: QCException, creator_name: str = "") -> str:
    """組成 LINE 推播訊息文字（純文字，emoji 加強可讀性）"""
    doc_label = _DOC_LBL.get(form.doc_type.value if form.doc_type else "", "單號")
    date_label = _DATE_LBL.get(form.event_date_type.value if form.event_date_type else "", "日期")
    stage_label = _STAGE_LBL.get(form.stage.value if form.stage else "", form.stage.value if form.stage else "—")

    rate_str = "—"
    if form.defect_rate is not None:
        rate_str = f"{form.defect_qty or 0} / {form.sample_qty or 0} = {form.defect_rate * 100:.1f}%"

    lines = [
        "🚨 【QC 異常通知】",
        f"單號：{form.form_id}",
        f"品號：{form.part_no}",
        f"{doc_label}：{form.receive_doc_no or '—'}",
        f"{date_label}：{form.receive_date or '—'}",
        f"工段／廠商：{stage_label} ／ {form.supplier_name or '—'}",
        f"數量：{form.receive_qty or '—'} pcs",
        "",
        f"❗ 異常原因：{form.defect_cause}",
        f"📐 量測數據：{form.measurement_data or '—'}",
        f"📊 不良率：{rate_str}",
        "",
        f"建立者：{creator_name or '—'}",
        f"請至系統處理：/qc-exceptions/{form.form_id}",
    ]
    return "\n".join(lines)


# 品保 + 工程 + 採購 + 產線主管 + 業助 + BU
_NOTIFY_ROLES = (Role.QC, Role.ENGINEER, Role.ENG_MGR, Role.PURCHASE,
                 Role.PROD_MGR, Role.ASSISTANT, Role.BU)


async def notify_exception_created(db, form: QCException, creator_name: str = ""):
    """異常單建立後通知 — LINE 群組 + 個別角色 fallback"""
    msg = build_exception_message(form, creator_name)
    # 1) LINE 群組推播
    _send_line_group(LINE_QC_GROUP, msg)
    # 2) 個別相關角色推播（即使群組也送）
    try:
        await _ntf._notify_roles(db, _NOTIFY_ROLES, msg)
    except Exception as e:
        logger.error(f"[QC notify roles error] {e}")


async def notify_disposition(db, form: QCException, disposer_name: str = ""):
    """品保下處理判斷後通知"""
    d_lbl = {"RETURN_TO_SUPPLIER": "退貨", "LAB_TEST": "實驗測試",
             "SPECIAL_ACCEPT": "特採允收"}.get(
        form.disposition.value if form.disposition else "", "—")
    msg = (f"✅ 【QC 處理判斷】{form.form_id}\n"
           f"品號：{form.part_no}\n"
           f"判定：{d_lbl}\n"
           f"判定人：{disposer_name or '—'}\n"
           f"備註：{form.disposition_note or '—'}\n"
           f"系統：/qc-exceptions/{form.form_id}")
    _send_line_group(LINE_QC_GROUP, msg)
    try:
        await _ntf._notify_roles(db, _NOTIFY_ROLES, msg)
    except Exception as e:
        logger.error(f"[QC disposition notify error] {e}")
