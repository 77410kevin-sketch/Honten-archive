"""
圖面辨識量測表 — 路由
僅限 QC / Admin 角色存取（/drawing-checker/）
"""

import os
import base64
import tempfile

import anthropic as _anthropic
from fastapi import APIRouter, Depends, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.services.auth import get_current_user
from app.models.user import User, Role
import app.drawing_checker.db as dc_db
from app.drawing_checker.analyzer import analyze_drawing_image, analyze_multiple_images
from app.drawing_checker.pdf_converter import pdf_to_images, cleanup_temp_images
from app.drawing_checker.preprocess import pdf_first_page_thumbnail

router    = APIRouter(prefix="/drawing-checker")
templates = Jinja2Templates(directory="app/templates")

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf"}
ALLOWED_ROLES      = {Role.QC, Role.ADMIN}


def _require_qc(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="僅限品保或管理員存取")
    return current_user


# ── 初始化 DB ────────────────────────────────────────

def init():
    dc_db.init_db()


# ── 頁面 ────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def drawing_checker_page(
    request: Request,
    current_user: User = Depends(_require_qc),
):
    return templates.TemplateResponse("drawing_checker/index.html", {
        "request": request,
        "user": current_user,
    })


# ── API ─────────────────────────────────────────────

@router.post("/api/thumbnail")
async def get_thumbnail(
    file: UploadFile = File(...),
    _: User = Depends(_require_qc),
):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext != ".pdf":
        return JSONResponse({"thumbnail": None})
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        thumb = pdf_first_page_thumbnail(tmp_path, max_w=800)
        return JSONResponse({"thumbnail": thumb})
    finally:
        os.unlink(tmp_path)


@router.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...),
    all_pages: bool = False,
    page: int = 1,
    _: User = Depends(_require_qc),
):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY 未設定")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支援的格式：{ext}")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    preview_b64 = None
    temp_images = []

    try:
        if ext == ".pdf":
            temp_images = pdf_to_images(tmp_path, dpi=250)
            if not temp_images:
                raise HTTPException(status_code=400, detail="PDF 轉換失敗")

            if all_pages and len(temp_images) > 1:
                data = analyze_multiple_images(temp_images, api_key)
                preview_path = temp_images[0]
            else:
                page_idx = max(0, min(page - 1, len(temp_images) - 1))
                data = analyze_drawing_image(temp_images[page_idx], api_key)
                preview_path = temp_images[page_idx]

            with open(preview_path, "rb") as f:
                preview_b64 = "data:image/png;base64," + base64.b64encode(f.read()).decode()
        else:
            data = analyze_drawing_image(tmp_path, api_key)
            media_type = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
            }.get(ext, "image/png")
            preview_b64 = f"data:{media_type};base64," + base64.b64encode(content).decode()

    except _anthropic.AuthenticationError:
        raise HTTPException(status_code=401, detail="API Key 無效")
    except _anthropic.BadRequestError as e:
        msg = str(e)
        if "credit balance" in msg.lower():
            raise HTTPException(status_code=402, detail="Anthropic API 帳戶餘額不足")
        raise HTTPException(status_code=400, detail=f"API 請求錯誤：{msg[:200]}")
    except _anthropic.RateLimitError:
        raise HTTPException(status_code=429, detail="請求頻率超限，請稍後再試")
    except _anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"AI 服務暫時無法使用：{str(e)[:200]}")
    finally:
        os.unlink(tmp_path)
        if temp_images:
            cleanup_temp_images(temp_images)

    dims = data.get("dimensions", [])
    resp = {
        "success": True,
        "preview": preview_b64,
        "part_name": data.get("part_name", "Unknown"),
        "drawing_no": data.get("drawing_no", "N/A"),
        "has_yellow_marks": data.get("has_yellow_marks", False),
        "dimensions": dims,
    }
    if len(dims) == 0 and data.get("_parse_error"):
        resp["_warn"] = f"AI 回應解析失敗，請重新上傳或確認圖面清晰度。原始片段：{data.get('_raw','')[:100]}"

    return JSONResponse(resp)


# ── 檢表 CRUD ────────────────────────────────────────

class SaveRequest(BaseModel):
    part_name: str
    drawing_no: str = ""
    internal_no: str = ""
    dimensions: list
    tools: dict = {}
    preview: str = ""


@router.post("/api/checklists")
async def save_checklist(req: SaveRequest, _: User = Depends(_require_qc)):
    cid = dc_db.save(req.part_name, req.drawing_no, req.internal_no,
                     req.dimensions, req.tools, req.preview)
    return JSONResponse({"id": cid, "count": dc_db.count()})


@router.get("/api/checklists")
async def list_checklists(_: User = Depends(_require_qc)):
    rows = dc_db.list_all()
    for r in rows:
        if r.get("preview_b64") and len(r["preview_b64"]) > 8000:
            r["preview_b64"] = r["preview_b64"][:8000]
    return JSONResponse({"items": rows, "count": len(rows)})


@router.get("/api/checklists/{cid}")
async def get_checklist(cid: int, _: User = Depends(_require_qc)):
    row = dc_db.get(cid)
    if not row:
        raise HTTPException(status_code=404, detail="找不到該檢表")
    return JSONResponse(row)


@router.delete("/api/checklists/{cid}")
async def delete_checklist(cid: int, _: User = Depends(_require_qc)):
    dc_db.delete(cid)
    return JSONResponse({"count": dc_db.count()})
