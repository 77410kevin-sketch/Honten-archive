"""
Demo 版通知服務 — 印 console log 取代實際 LINE 推播
正式環境請替換為真實的 LINE Messaging API 呼叫
"""
import logging
logger = logging.getLogger(__name__)


async def notify_pcn_submitted(db, form):
    logger.info(f"[LINE通知-Demo] 送審品保 | {form.form_id} | {form.product_name}")
    print(f"\n📱 [LINE通知] 【待品保填寫SIP】{form.form_id} - {form.product_name}\n")


async def notify_pcn_qc_done(db, form):
    logger.info(f"[LINE通知-Demo] 品保完成 | {form.form_id}")
    print(f"\n📱 [LINE通知] 【待產線主管填SOP】{form.form_id} - {form.product_name}\n")


async def notify_pcn_prod_done(db, form):
    logger.info(f"[LINE通知-Demo] 產線完成 | {form.form_id}")
    print(f"\n📱 [LINE通知] 【待BU審核】{form.form_id} - {form.product_name}\n")


async def notify_pcn_approved(db, form):
    from app.services.pdf_export import save_cc_pdf
    pdf_path = save_cc_pdf(form)
    logger.info(f"[LINE通知-Demo] BU核准 | {form.form_id} | PDF={pdf_path}")
    print(
        f"\n📱 [LINE通知] 【✅ PCN/ECN已核准】{form.form_id} - {form.product_name} 通知所有相關人員\n"
        f"   📄 CC PDF 已生成：{pdf_path}\n"
    )


async def notify_pcn_rejected(db, form, reject_target: str = "提案單位"):
    logger.info(f"[LINE通知-Demo] BU退回 | {form.form_id} | 退回對象：{reject_target}")
    print(f"\n📱 [LINE通知] 【⚠️ PCN/ECN退回】{form.form_id} 退回給【{reject_target}】，請修改後重新送審\n")


# ── ECN 核准 CC 通知 ─────────────────────────────

async def notify_ecn_approved_tech(db, form):
    """ECN 技術類（製程/設計/供應商）核准 → CC：工程 + 品保 + 資材 + 提出單位 + PDF"""
    import json
    from app.services.pdf_export import save_cc_pdf
    inv_rows = []
    if form.inventory_data:
        try:
            inv_rows = json.loads(form.inventory_data)
        except Exception:
            pass
    pdf_path = save_cc_pdf(form, inv_rows or None)
    logger.info(f"[LINE通知-Demo] ECN技術類核准 | {form.form_id} | CC→工程、品保、資材、提出單位 | PDF={pdf_path}")
    print(
        f"\n📱 [LINE通知-CC] 【✅ ECN技術類核准】{form.form_id} - {form.product_name}\n"
        f"   CC 通知：工程部門、品保部門、資材部門（採購/倉管）、提出單位（{form.department or '—'}）\n"
        f"   📄 CC PDF 已生成：{pdf_path}\n"
    )


async def notify_ecn_approved_price(db, form):
    """ECN 售價變更核准 → CC：提出單位 + 業助 + 人事 + PDF"""
    from app.services.pdf_export import save_cc_pdf
    pdf_path = save_cc_pdf(form)
    logger.info(f"[LINE通知-Demo] ECN售價變更核准 | {form.form_id} | CC→提出單位、業助、人事 | PDF={pdf_path}")
    print(
        f"\n📱 [LINE通知-CC] 【✅ ECN售價變更核准】{form.form_id} - {form.product_name}\n"
        f"   CC 通知：提出單位（{form.department or '—'}）、業務助理、人事\n"
        f"   📄 CC PDF 已生成：{pdf_path}\n"
    )


async def notify_ecn_approved_cost(db, form):
    """ECN 成本變更核准 → CC：提出單位 + 採購 + 人事 + 業務 + PDF"""
    from app.services.pdf_export import save_cc_pdf
    pdf_path = save_cc_pdf(form)
    logger.info(f"[LINE通知-Demo] ECN成本變更核准 | {form.form_id} | CC→提出單位、採購、人事、業務 | PDF={pdf_path}")
    print(
        f"\n📱 [LINE通知-CC] 【✅ ECN成本變更核准】{form.form_id} - {form.product_name}\n"
        f"   CC 通知：提出單位（{form.department or '—'}）、採購、人事、業務\n"
        f"   📄 CC PDF 已生成：{pdf_path}\n"
    )


async def notify_ecn_warehouse_done(db, form):
    """ECN 設計變更：倉管盤點完成 → 通知工程師"""
    logger.info(f"[LINE通知-Demo] ECN倉管盤點完成 | {form.form_id} | 待工程確認")
    print(f"\n📱 [LINE通知] 【📦 庫存盤點完成】{form.form_id} - {form.product_name}，請工程師確認並判定處理方式\n")
