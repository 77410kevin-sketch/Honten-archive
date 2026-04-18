"""
CC Package PDF 生成服務
BU 核准後將表單資料 + 附件整合成單一 PDF
"""
import io, os, json, logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

UPLOAD_BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")

WINDOWS_FONTS = [
    "C:/Windows/Fonts/msjh.ttc",
    "C:/Windows/Fonts/ARIALUNI.ttf",
    "C:/Windows/Fonts/mingliu.ttc",
    "C:/Windows/Fonts/kaiu.ttf",
]
MAC_FONTS = [
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc",
]


def _register_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for path in WINDOWS_FONTS + MAC_FONTS:
        if not os.path.exists(path):
            continue
        try:
            font_name = "CJKFont"
            pdfmetrics.registerFont(TTFont(font_name, path, subfontIndex=0))
            return font_name
        except Exception:
            try:
                pdfmetrics.registerFont(TTFont(font_name, path))
                return font_name
            except Exception:
                continue
    return "Helvetica"


def generate_cc_pdf(form, inventory_rows=None) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, Image as RLImage,
                                    HRFlowable, PageBreak)
    from reportlab.lib.enums import TA_LEFT, TA_CENTER

    font_name = _register_font()

    buf = io.BytesIO()
    pdf_doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
        title=f"PCN/ECN CC Package — {form.form_id}",
    )

    # ── 樣式 ──────────────────────────────────────
    normal = ParagraphStyle("normal", fontName=font_name, fontSize=9, leading=14, spaceAfter=4)
    title_style = ParagraphStyle("title", fontName=font_name, fontSize=16, leading=20,
                                  textColor=colors.HexColor("#1a56db"), spaceAfter=6)
    h2 = ParagraphStyle("h2", fontName=font_name, fontSize=11, leading=15,
                          textColor=colors.HexColor("#374151"), spaceBefore=12, spaceAfter=6)
    small = ParagraphStyle("small", fontName=font_name, fontSize=8, leading=12, textColor=colors.grey)
    cell_style = ParagraphStyle("cell", fontName=font_name, fontSize=8, leading=12)
    cell_bold = ParagraphStyle("cell_bold", fontName=font_name, fontSize=8, leading=12,
                                textColor=colors.HexColor("#1a56db"))

    def P(text, style=normal):
        safe = str(text or "").replace("📎", "[附件]").replace("✅", "[OK]").replace("⚠", "[!]")
        return Paragraph(safe or "—", style)

    def section_table(rows, col_widths, header_color="#1a56db"):
        t = Table([[P(c, cell_style) for c in r] for r in rows], colWidths=col_widths)
        t.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor(header_color)),
            ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
            ("FONTNAME",     (0,0), (-1,-1), font_name),
            ("FONTSIZE",     (0,0), (-1,-1), 8),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [colors.white, colors.HexColor("#f3f4f6")]),
            ("GRID",         (0,0), (-1,-1), 0.4, colors.HexColor("#d1d5db")),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING",  (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING",   (0,0), (-1,-1), 4),
            ("BOTTOMPADDING",(0,0), (-1,-1), 4),
        ]))
        return t

    story = []

    # ── 封面標題 ──────────────────────────────────
    story.append(P(f"鴻騰電子 PCN/ECN CC Package", title_style))
    story.append(P(f"單號：{form.form_id}   |   產出日期：{datetime.now().strftime('%Y-%m-%d %H:%M')}", small))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#1a56db"), spaceAfter=10))

    # ── 基本資訊 ──────────────────────────────────
    story.append(P("1. 基本資訊", h2))
    status_map = {
        "APPROVED": "已核准", "CLOSED": "已結案", "RETURNED": "已退回",
        "DRAFT": "草稿", "ECN_PENDING_ENG": "待工程確認",
        "ECN_PENDING_QC": "待品保確認", "ECN_PENDING_WAREHOUSE": "待倉管盤點",
        "PENDING_QC": "待品保", "PENDING_PRODUCTION": "待產線主管",
        "PENDING_WAREHOUSE_SOP": "待倉管包裝SOP", "PENDING_BU_APPROVAL": "待BU審核",
    }
    info_rows = [
        ["欄位", "內容"],
        ["單號",         form.form_id],
        ["類型",         "PCN 開發轉量產" if form.type.value == "PCN" else "ECN 產品工程變更"],
        ["狀態",         status_map.get(form.status.value, form.status.value)],
        ["廠內產品料號", form.product_name],
        ["機種名稱",     form.product_model or "—"],
        ["提出人/部門",  form.department or "—"],
        ["預計生效日期", form.effective_date or "—"],
        ["建單者",       form.creator.display_name if form.creator else "—"],
        ["建立時間",     form.created_at.strftime("%Y-%m-%d %H:%M") if form.created_at else "—"],
        ["最後更新",     form.updated_at.strftime("%Y-%m-%d %H:%M") if form.updated_at else "—"],
    ]
    if form.change_types:
        try:
            ct = "、".join(json.loads(form.change_types))
        except Exception:
            ct = form.change_types
        info_rows.append(["ECN 變更類型", ct])
    story.append(section_table(info_rows, [4*cm, 12*cm]))

    # ── 變更說明 ──────────────────────────────────
    story.append(P("2. 變更說明", h2))
    story.append(P(form.change_description or "—", normal))
    if form.change_reason:
        story.append(Spacer(1, 0.2*cm))
        story.append(P("變更原因 / 包裝說明：", cell_bold))
        story.append(P(form.change_reason, normal))

    # ── 各階段確認意見 ──────────────────────────────
    has_opinions = (form.qc_comment or form.prod_comment or
                    any(a.action in ("ENG_CONFIRM","ECN_QC_CONFIRM") for a in (form.approvals or [])))
    if has_opinions:
        story.append(P("3. 各階段確認意見", h2))
        opinion_rows = [["角色", "人員", "意見"]]

        # 工程確認
        for apv in (form.approvals or []):
            if apv.action == "ENG_CONFIRM":
                opinion_rows.append([
                    "工程確認",
                    apv.approver.display_name if apv.approver else "—",
                    apv.comment or "（無意見）",
                ])

        # 品保確認（ECN）
        for apv in (form.approvals or []):
            if apv.action == "ECN_QC_CONFIRM":
                opinion_rows.append([
                    "品保確認(ECN)",
                    apv.approver.display_name if apv.approver else "—",
                    apv.comment or "（無意見）",
                ])

        # 品保意見（PCN）
        if form.qc_comment:
            opinion_rows.append(["品保(PCN)", "—", form.qc_comment])

        # 產線意見
        if form.prod_comment:
            opinion_rows.append(["產線主管", "—", form.prod_comment])

        if len(opinion_rows) > 1:
            story.append(section_table(opinion_rows, [3*cm, 3*cm, 10*cm], "#6f42c1"))

    # ── 庫存盤點 ──────────────────────────────────
    if inventory_rows:
        story.append(P("4. 庫存盤點結果", h2))
        inv_rows_data = [["#", "舊料號", "站別/工序", "庫存量", "處理方式", "備註"]]
        for i, row in enumerate(inventory_rows, 1):
            inv_rows_data.append([
                str(i),
                row.get("old_pn", "—"),
                row.get("station", "—"),
                str(row.get("qty", "—")),
                row.get("action", "—"),
                row.get("remark", ""),
            ])
        story.append(section_table(inv_rows_data, [0.8*cm, 3.5*cm, 3*cm, 2*cm, 3*cm, 3.7*cm], "#198754"))

    # ── 審核記錄 ──────────────────────────────────
    story.append(P("5. 審核記錄", h2))
    action_map = {
        "SUBMIT": "送審", "APPROVE": "核准", "REJECT": "退回",
        "ENG_CONFIRM": "工程確認", "ECN_QC_CONFIRM": "品保確認(ECN)",
        "WH_CONFIRM": "倉管盤點完成", "QC_DONE": "品保完成(PCN)",
        "PROD_DONE": "產線完成", "WH_SOP_DONE": "倉管SOP完成",
        "QC_RESUBMIT": "品保重送", "ENG_RESUBMIT": "工程重送",
        "PROD_RESUBMIT": "產線重送", "WH_RESUBMIT": "倉管重送",
        "QC_REJECT": "品保退回", "CLOSE": "結案",
    }
    apv_data = [["時間", "人員", "動作", "意見"]]
    for apv in (form.approvals or []):
        apv_data.append([
            apv.created_at.strftime("%Y-%m-%d %H:%M") if apv.created_at else "—",
            apv.approver.display_name if apv.approver else "—",
            action_map.get(apv.action, apv.action),
            apv.comment or "",
        ])
    if len(apv_data) == 1:
        apv_data.append(["—", "—", "—", "尚無記錄"])
    story.append(section_table(apv_data, [3*cm, 3*cm, 3*cm, 7*cm], "#374151"))

    # ── 附件 ──────────────────────────────────────
    if form.documents:
        story.append(P("6. 附件清單", h2))
        img_exts = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

        for doc in form.documents:
            doc_path = Path(UPLOAD_BASE) / f"pcn_{doc.form_id_fk}" / doc.filename
            ext = Path(doc.original_name).suffix.lower()
            cat = doc.category or "附件"
            uploader = doc.uploader.display_name if doc.uploader else "—"
            uploaded_at = doc.uploaded_at.strftime("%Y-%m-%d") if doc.uploaded_at else ""
            story.append(P(f"[{cat}]  {doc.original_name}  —  上傳者：{uploader}  {uploaded_at}", normal))
            if ext in img_exts and doc_path.exists():
                try:
                    from PIL import Image as PILImage
                    with PILImage.open(doc_path) as pimg:
                        w, h = pimg.size
                    max_w = 14 * cm
                    scale = min(max_w / w, (12*cm) / h, 1.0)
                    story.append(RLImage(str(doc_path), width=w*scale, height=h*scale))
                except Exception as e:
                    story.append(P(f"  (圖片無法嵌入：{e})", small))
            elif ext == ".pdf" and doc_path.exists():
                story.append(P(f"  (PDF 附件請參閱原始檔案：{doc.original_name})", small))
            elif not doc_path.exists():
                story.append(P(f"  (檔案不存在於伺服器)", small))
            story.append(Spacer(1, 0.2*cm))

    # ── 頁腳 ──────────────────────────────────────
    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(P(
        f"本文件由鴻騰電子 PCN/ECN 系統自動產生  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        small
    ))

    pdf_doc.build(story)
    return buf.getvalue()


def save_cc_pdf(form, inventory_rows=None) -> str:
    try:
        pdf_bytes = generate_cc_pdf(form, inventory_rows)
        out_dir = Path(UPLOAD_BASE) / form.form_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"cc_package_{form.form_id}.pdf"
        out_path.write_bytes(pdf_bytes)
        logger.info(f"[PDF] CC Package 已生成：{out_path}")
        return str(out_path)
    except Exception as e:
        logger.error(f"[PDF] 生成失敗：{e}")
        return ""
