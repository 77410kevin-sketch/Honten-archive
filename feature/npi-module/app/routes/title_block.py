"""
圖框轉換工具 — 辨識客戶圖框並更換為鴻騰電子圖框
僅限 ENGINEER / ADMIN 角色存取（/title-block/）
"""

import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request, UploadFile, File, HTTPException, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.services.auth import get_current_user
from app.models.user import User, Role


router    = APIRouter(prefix="/title-block")
templates = Jinja2Templates(directory="app/templates")

ALLOWED_EXTENSIONS = {".pdf", ".dwg", ".dxf"}
ALLOWED_ROLES      = {Role.ENGINEER, Role.ADMIN}

OUTPUT_DIR = Path("uploads/title_block")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _require_engineer(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="僅限工程師或管理員存取")
    return current_user


# ── 頁面 ────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    current_user: User = Depends(_require_engineer),
):
    return templates.TemplateResponse("title_block/index.html", {
        "request": request,
        "user": current_user,
    })


# ── 鴻騰圖框覆蓋（PDF）──────────────────────────────

CJK_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]


def _find_cjk_font() -> str | None:
    for p in CJK_FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _detect_customer_frame(page):
    """
    推論客戶原始外框位置：取所有繪圖物件的整體 bounding box。
    這假設客戶外框即所有可見內容的最外圈（絕大多數工程圖都符合）。
    """
    import fitz
    try:
        drawings = page.get_drawings()
    except Exception:
        return None

    xs, ys = [], []
    for d in drawings:
        rect = d.get("rect")
        if rect is None:
            continue
        xs.extend([rect.x0, rect.x1])
        ys.extend([rect.y0, rect.y1])
    if not xs:
        return None

    page_rect = page.rect
    frame = fitz.Rect(min(xs), min(ys), max(xs), max(ys))
    # 至少覆蓋頁面 25% 面積才視為外框
    if (frame.width * frame.height) < (page_rect.width * page_rect.height * 0.25):
        return None
    # 向內縮 0.5pt 避免蓋到自身
    return frame


def _analyze_content_bbox(page, inside_rect):
    """
    分析 inside_rect 框內繪圖物件的實際分佈邊界（排除外框本身）。
    """
    import fitz
    try:
        drawings = page.get_drawings()
    except Exception:
        return None

    min_x, min_y = inside_rect.x1, inside_rect.y1
    max_x, max_y = inside_rect.x0, inside_rect.y0
    found = False
    for d in drawings:
        rect = d.get("rect")
        if rect is None:
            continue
        if (abs(rect.width - inside_rect.width) < 3
                and abs(rect.height - inside_rect.height) < 3):
            continue
        if rect.x0 < inside_rect.x0 - 2 or rect.x1 > inside_rect.x1 + 2:
            continue
        if rect.y0 < inside_rect.y0 - 2 or rect.y1 > inside_rect.y1 + 2:
            continue
        found = True
        min_x = min(min_x, rect.x0)
        min_y = min(min_y, rect.y0)
        max_x = max(max_x, rect.x1)
        max_y = max(max_y, rect.y1)
    if not found:
        return None
    return fitz.Rect(min_x, min_y, max_x, max_y)


# ── 客戶標題欄/LOGO 關鍵字 ────────────────────────────────
# 這些字出現的地方，十之八九是標題欄；用來定位客戶原始 LOGO 區
_TITLE_KEYWORDS = [
    # 英文標頭
    "TITLE", "DRAWING NO", "DRAWING", "DWG", "DWG NO", "DWG.NO",
    "SCALE", "CHECKED", "APPROVED", "DRAWN BY", "DRAWN",
    "MATERIAL", "FINISH", "UNIT", "UNITS", "SHEET",
    "PROJECT", "PROJECTION", "3RD PROJECTION", "REV", "REVISION",
    "TOLERANCE", "TOL", "ANGLE", "DATE",
    # 中文
    "圖號", "品名", "材質", "比例", "日期", "檢驗", "核准", "繪圖",
    "公差", "單位", "尺寸", "規格", "製造公差",
    # 公司稱呼（常出現於 LOGO 附近）
    "有限公司", "股份", "股份有限", "企業", "科技", "電子",
    "製造", "工業", "實業", "國際",
]


def _detect_customer_logo_region(page):
    """
    偵測客戶 LOGO / 標題欄所在區域。
    策略：
      1. 抽取所有 text block
      2. 對含有標題欄關鍵字的文字 block 收集 bbox
      3. 以「密度群聚」合併 bbox，得出最大群聚作為 LOGO 區
      4. 判斷方向：直式（vertical）、橫式（horizontal）、角落（corner）
    回傳 (bbox: fitz.Rect | None, orientation: str | None)。
    """
    import fitz
    try:
        blocks = page.get_text("blocks")
    except Exception:
        return None, None

    page_rect = page.rect
    matched = []
    for b in blocks:
        if len(b) < 5:
            continue
        x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
        if not text:
            continue
        txt_norm = text.replace("\n", " ").strip()
        if not txt_norm:
            continue
        txt_up = txt_norm.upper()
        hit = False
        for kw in _TITLE_KEYWORDS:
            if kw.upper() in txt_up:
                hit = True
                break
        if hit:
            matched.append(fitz.Rect(x0, y0, x1, y1))

    if not matched:
        return None, None

    # 以最大連通群聚合併 bbox：相距 <60pt 的視為同一群
    def _close(a, b, gap=60):
        dx = max(0, max(a.x0, b.x0) - min(a.x1, b.x1))
        dy = max(0, max(a.y0, b.y0) - min(a.y1, b.y1))
        return dx <= gap and dy <= gap

    clusters = []
    for r in matched:
        placed = False
        for c in clusters:
            if any(_close(r, x) for x in c):
                c.append(r)
                placed = True
                break
        if not placed:
            clusters.append([r])

    # 選擇「成員最多 + 面積最大」的群聚
    def cluster_bbox(c):
        bb = c[0]
        for x in c[1:]:
            bb = bb | x
        return bb

    best = max(clusters, key=lambda c: (len(c),
                                         cluster_bbox(c).width * cluster_bbox(c).height))
    bbox = cluster_bbox(best)

    # 判斷方向
    w, h = bbox.width, bbox.height
    if h >= w * 1.8:
        orientation = "vertical"
    elif w >= h * 1.8:
        orientation = "horizontal"
    else:
        orientation = "corner"

    return bbox, orientation


def _draw_honten_title_block(page, width: float, height: float,
                             drawing_no: str = "", title: str = "",
                             material: str = "", scale: str = "",
                             drawer: str = "",
                             add_ht_block: bool = True):
    """
    處理流程：
      1. 偵測客戶 LOGO / 標題欄位置（直式 / 橫式 / 角落）
      2. 精準清除該區域（只清文字群聚 bbox，不動其他圖面）
      3. 若 add_ht_block=True，於同一區域放鴻騰標題欄（依方向調整）
      4. 否則只清除不加，維持原圖格式
    回傳 dict: {detected, bbox, orientation} 供上層記錄。
    """
    import fitz
    cjk_path = _find_cjk_font()
    cjk_name = "cjk"
    if cjk_path:
        try:
            page.insert_font(fontname=cjk_name, fontfile=cjk_path)
        except Exception:
            cjk_path = None
    font_for = (lambda _txt: cjk_name) if cjk_path else (lambda _txt: "helv")

    HT_BLUE = (0.05, 0.18, 0.43)
    HT_FILL = (0.05, 0.16, 0.38)

    # ── Step 1：偵測客戶 LOGO 區 ─────────────────────────────
    logo_bbox, orientation = _detect_customer_logo_region(page)
    detected = logo_bbox is not None

    info = {
        "detected": detected,
        "bbox": list(logo_bbox) if logo_bbox else None,
        "orientation": orientation,
    }

    if not detected:
        # 無法偵測：保守退回右下角 30% × 20%
        logo_bbox = fitz.Rect(width * 0.60, height * 0.78, width - 20, height - 20)
        orientation = "corner"

    # ── Step 2：精準清除客戶標題欄區域（含 12pt 膨脹以涵蓋框線）──
    pad = 12
    clear_rect = fitz.Rect(
        max(0, logo_bbox.x0 - pad),
        max(0, logo_bbox.y0 - pad),
        min(width, logo_bbox.x1 + pad),
        min(height, logo_bbox.y1 + pad),
    )
    page.draw_rect(clear_rect, color=(1, 1, 1), fill=(1, 1, 1), width=0)

    if not add_ht_block:
        # 使用者選擇不加鴻騰標題欄：僅清除客戶 LOGO，結束
        return info

    # ── Step 3：依方向決定鴻騰標題欄的位置與尺寸 ─────────────
    #   直式（vertical）：把標題欄轉 90° 放在原位，維持原圖版型
    #   橫式（horizontal） / 角落（corner）：標準橫向
    rotate = 0
    if orientation == "vertical":
        # 直式標題欄：寬 = clear 寬、高 = clear 高；內部文字旋轉 90°
        tb_rect = fitz.Rect(clear_rect)
        rotate = 90
    else:
        # 橫式 / 角落：若 clear 區太寬或太窄，調整成合理比例
        desired_w = min(max(clear_rect.width, 260), 420)
        desired_h = min(max(clear_rect.height, 110), 170)
        # 對齊到 clear 區右下角
        tb_x1 = clear_rect.x1
        tb_y1 = clear_rect.y1
        tb_x0 = tb_x1 - desired_w
        tb_y0 = tb_y1 - desired_h
        # 不得超出頁面
        tb_x0 = max(10, tb_x0)
        tb_y0 = max(10, tb_y0)
        tb_rect = fitz.Rect(tb_x0, tb_y0, tb_x1, tb_y1)

    # ── Step 4：繪製鴻騰標題欄 ────────────────────────────
    _draw_ht_block(page, tb_rect, rotate, HT_BLUE, HT_FILL, font_for,
                   drawing_no, title, material, scale, drawer)

    info["tb_rect"] = list(tb_rect)
    info["rotate"] = rotate
    return info


def _draw_ht_block(page, rect, rotate, HT_BLUE, HT_FILL, font_for,
                   drawing_no, title, material, scale, drawer):
    """
    於指定 rect 繪製鴻騰標題欄（含外框 / 標題列 / 6 格資訊）。
    rotate: 0（橫式）或 90（直式，內容旋轉 90°）。
    """
    import fitz

    # 標題欄外框
    page.draw_rect(rect, color=HT_BLUE, fill=(1, 1, 1), width=1.3)

    labels = [
        ("DRAWING NO 圖號",   drawing_no),
        ("TITLE       品名",  title),
        ("MATERIAL    材質",  material),
        ("SCALE       比例",  scale or "N.T.S."),
        ("DATE        日期",  datetime.now().strftime("%Y-%m-%d")),
        ("DRAWN BY    繪圖者",drawer),
    ]

    if rotate == 90:
        # 直式：標題列在左，文字旋轉 90°
        bar_w = 24
        title_bar = fitz.Rect(rect.x0, rect.y0, rect.x0 + bar_w, rect.y1)
        page.draw_rect(title_bar, color=HT_BLUE, fill=HT_FILL, width=0)
        page.insert_textbox(
            fitz.Rect(title_bar.x0 + 4, title_bar.y0 + 8, title_bar.x1 - 4, title_bar.y1 - 8),
            "HonTen Electronic   鴻騰電子股份有限公司",
            fontsize=10, fontname=font_for("header"),
            color=(1, 1, 1), align=1, rotate=90,
        )
        # 資訊格：6 格縱向排列（1 欄 × 6 列）於標題列右方
        area_x0 = title_bar.x1
        area = fitz.Rect(area_x0, rect.y0, rect.x1, rect.y1)
        cell_w = area.width
        cell_h = area.height / 6
        for idx, (lbl, val) in enumerate(labels):
            cy0 = area.y0 + idx * cell_h
            cell = fitz.Rect(area.x0, cy0, area.x0 + cell_w, cy0 + cell_h)
            page.draw_rect(cell, color=HT_BLUE, width=0.5)
            page.insert_textbox(
                fitz.Rect(cell.x0 + 4, cell.y0 + 3, cell.x1 - 4, cell.y0 + 14),
                lbl, fontsize=6.5, fontname=font_for(lbl),
                color=(0.35, 0.35, 0.35), align=0,
            )
            page.insert_textbox(
                fitz.Rect(cell.x0 + 4, cell.y0 + 14, cell.x1 - 4, cell.y1 - 3),
                val or "—", fontsize=9, fontname=font_for(val or ""),
                color=(0, 0, 0), align=0,
            )
    else:
        # 橫式：標題列在上，3×2 資訊格
        title_bar = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + 24)
        page.draw_rect(title_bar, color=HT_BLUE, fill=HT_FILL, width=0)
        page.insert_textbox(
            fitz.Rect(title_bar.x0 + 8, title_bar.y0 + 5, title_bar.x1 - 8, title_bar.y1 - 5),
            "HonTen Electronic   鴻騰電子股份有限公司",
            fontsize=10, fontname=font_for("header"),
            color=(1, 1, 1), align=0,
        )
        area = fitz.Rect(rect.x0, title_bar.y1, rect.x1, rect.y1)
        cell_w = area.width / 2
        cell_h = area.height / 3
        for idx, (lbl, val) in enumerate(labels):
            r, c = divmod(idx, 2)
            cx0 = area.x0 + c * cell_w
            cy0 = area.y0 + r * cell_h
            cell = fitz.Rect(cx0, cy0, cx0 + cell_w, cy0 + cell_h)
            page.draw_rect(cell, color=HT_BLUE, width=0.5)
            page.insert_textbox(
                fitz.Rect(cell.x0 + 5, cell.y0 + 3, cell.x1 - 5, cell.y0 + 15),
                lbl, fontsize=6.5, fontname=font_for(lbl),
                color=(0.35, 0.35, 0.35), align=0,
            )
            page.insert_textbox(
                fitz.Rect(cell.x0 + 5, cell.y0 + 15, cell.x1 - 5, cell.y1 - 3),
                val or "—", fontsize=10, fontname=font_for(val or ""),
                color=(0, 0, 0), align=0,
            )


# ── 處理上傳 & 轉換 ─────────────────────────────────

@router.post("/convert")
async def convert(
    request: Request,
    file: UploadFile = File(...),
    drawing_no: str = Form(""),
    title: str = Form(""),
    material: str = Form(""),
    scale: str = Form(""),
    drawer: str = Form(""),
    current_user: User = Depends(_require_engineer),
):
    filename = file.filename or "upload"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return JSONResponse({"ok": False, "error": f"不支援的檔案格式：{ext}（僅接受 PDF/DWG/DXF）"}, status_code=400)

    # 儲存上傳檔
    job_id = uuid.uuid4().hex[:12]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    src_path = job_dir / f"original{ext}"
    content = await file.read()
    src_path.write_bytes(content)

    # PDF → 用 PyMuPDF 做圖框覆蓋
    if ext == ".pdf":
        try:
            import fitz
        except ImportError:
            return JSONResponse({"ok": False, "error": "伺服器缺少 PyMuPDF，請安裝 pymupdf"}, status_code=500)

        try:
            doc = fitz.open(str(src_path))
            for page in doc:
                rect = page.rect
                _draw_honten_title_block(
                    page, rect.width, rect.height,
                    drawing_no=drawing_no, title=title,
                    material=material, scale=scale,
                    drawer=drawer or current_user.display_name,
                )
            out_name = f"HT_{os.path.splitext(filename)[0]}.pdf"
            out_path = job_dir / out_name
            # 保留原始品質：不壓縮、不刪除未引用物件、不重寫內容
            doc.save(str(out_path), garbage=0, deflate=False, clean=False)
            total_pages = len(doc)
            doc.close()

            # 產生高解析度預覽圖（每頁一張 PNG，用於網頁預覽；下載仍是向量 PDF）
            preview_urls = []
            try:
                pv_doc = fitz.open(str(out_path))
                zoom = 200 / 72  # 200 DPI 預覽
                mat = fitz.Matrix(zoom, zoom)
                for i, p in enumerate(pv_doc):
                    pix = p.get_pixmap(matrix=mat, alpha=False)
                    png_path = job_dir / f"preview_{i+1:03d}.png"
                    pix.save(str(png_path))
                    preview_urls.append(f"/title-block/download/{job_id}/{png_path.name}")
                pv_doc.close()
            except Exception as _e:
                preview_urls = []

            return JSONResponse({
                "ok": True,
                "type": "pdf",
                "job_id": job_id,
                "filename": out_name,
                "download_url": f"/title-block/download/{job_id}/{out_name}",
                "preview_urls": preview_urls,
                "pages": total_pages,
                "message": "已套用鴻騰電子圖框（預設版本）— 預覽為 PNG 圖片，下載檔為高解析度向量 PDF",
            })
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"PDF 處理失敗：{e}"}, status_code=500)

    # DWG / DXF → 暫存，待 CAD 轉換套件整合
    return JSONResponse({
        "ok": True,
        "type": "cad",
        "job_id": job_id,
        "filename": filename,
        "download_url": f"/title-block/download/{job_id}/original{ext}",
        "message": (
            f"已接收 {ext.upper()} 檔案。"
            f"CAD 圖框替換需整合 AutoCAD / ODA File Converter / ezdxf，"
            f"目前保留原檔供下載；實際轉換管線將在確定鴻騰標準圖框格式後建置。"
        ),
    })


# ── 下載 ────────────────────────────────────────────

@router.get("/download/{job_id}/{filename}")
async def download(
    job_id: str,
    filename: str,
    current_user: User = Depends(_require_engineer),
):
    # 安全檢查：路徑不得跳脫
    safe_job = Path(job_id).name
    safe_name = Path(filename).name
    path = OUTPUT_DIR / safe_job / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="檔案不存在")

    lower = safe_name.lower()
    if lower.endswith(".pdf"):
        media_type = "application/pdf"
    elif lower.endswith(".png"):
        media_type = "image/png"
    elif lower.endswith(".jpg") or lower.endswith(".jpeg"):
        media_type = "image/jpeg"
    else:
        media_type = "application/octet-stream"
    return FileResponse(str(path), media_type=media_type, filename=safe_name)
