"""AI 自動解析客戶詢價信 → 結構化 JSON

使用 Claude Haiku 4.5（claude-haiku-4-5）搭配 prompt cache，
對於一般 2-3 頁 email/PDF 詢價信延遲約 2-3 秒、成本極低。
"""
import os
import json
import logging
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

_PARSER_MODEL = "claude-haiku-4-5"

_SYSTEM_PROMPT = """你是鴻騰電子 NPI 模組的客戶詢價信解析助手。

請從使用者貼上的【客戶詢價信 / email / RFQ】中擷取以下欄位，輸出嚴格 JSON（不要 markdown 圍欄、不要解說）：

{
  "customer_name": "客戶公司名稱（繁體中文，必要）",
  "customer_contact": "客戶聯絡窗口姓名（可空）",
  "customer_email": "客戶聯絡 email（可空）",
  "product_name": "產品名稱（繁體中文）",
  "product_model": "型號 / 料號（可空）",
  "spec_summary": "規格摘要（一行簡短條列，可用「、」分隔）",
  "target_price": "客戶期望單價（數字，無幣別，沒講則 null）",
  "annual_qty": "年需求量或總量（整數，沒講則 null）",
  "rfq_due_date": "客戶希望回覆 / 報價截止日（YYYY-MM-DD；沒講則 null）",
  "bu": "若內文可推斷所屬事業部（儲能事業部 / 消費性事業部），否則 null",
  "sales_note": "對工程最有幫助的關鍵資訊摘要（1-3 句，繁體中文）"
}

規則：
- 若某欄位在信中找不到，給 null（而非省略 key）
- customer_name / product_name 為必要，若兩者皆不明確 → customer_name / product_name 填 "UNKNOWN"
- 日期請轉成 YYYY-MM-DD
- 數字請用純數字，不帶單位或逗號
- 輸出只能是一個 JSON 物件，禁止 ```json ``` 圍欄"""


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_IMAGE_MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/png",
}


def _build_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _parse_raw(raw: str) -> dict[str, Any]:
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```"))
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        logger.warning(f"JSON decode failed: {e}\nraw={raw[:500]}")
        raise ValueError(f"AI 回傳非有效 JSON：{raw[:200]}")


def parse_inquiry_image(filename: str, content: bytes) -> dict[str, Any]:
    """用 Claude Vision 直接解析圖片詢價信（PNG/JPG 等）→ 結構化 JSON。"""
    import base64
    ext = os.path.splitext(filename)[1].lower()
    media_type = _IMAGE_MEDIA.get(ext, "image/png")
    client = _build_client()
    resp = client.messages.create(
        model=_PARSER_MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(content).decode("utf-8"),
            }},
            {"type": "text", "text": "以上是客戶詢價信的圖片，請擷取所有可識別的資訊並回 JSON："},
        ]}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return _parse_raw(raw)


def parse_inquiry_letter(text: str) -> dict[str, Any]:
    """擷取詢價信欄位。輸入純文字，回 dict。"""
    if not text or not text.strip():
        raise ValueError("詢價信內容不可為空")

    client = _build_client()
    resp = client.messages.create(
        model=_PARSER_MODEL,
        max_tokens=1024,
        system=[{
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": f"以下是客戶詢價信內容，請擷取並回 JSON：\n\n{text.strip()[:12000]}"
        }],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    logger.info(f"[inquiry_parser] cache tokens: "
                f"read={getattr(resp.usage, 'cache_read_input_tokens', 0)}, "
                f"write={getattr(resp.usage, 'cache_creation_input_tokens', 0)}")
    return _parse_raw(raw)


def extract_text_from_upload(filename: str, content: bytes) -> str:
    """從上傳檔案抽文字。支援 .txt / .pdf / .eml；其他類型嘗試 UTF-8 decode"""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(content))
            return "\n".join(p.extract_text() or "" for p in reader.pages)
        except ImportError:
            raise RuntimeError("PDF 解析需要 pypdf（pip install pypdf），目前未安裝")
        except Exception as e:
            raise RuntimeError(f"PDF 解析失敗：{e}")
    if ext in (".eml", ".msg"):
        try:
            from email import message_from_bytes
            from email.policy import default
            msg = message_from_bytes(content, policy=default)
            body = msg.get_body(preferencelist=("plain", "html"))
            return body.get_content() if body else ""
        except Exception as e:
            raise RuntimeError(f"Email 檔解析失敗：{e}")
    # 預設當 utf-8 文字
    for enc in ("utf-8", "utf-8-sig", "big5", "cp950", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise RuntimeError("檔案無法解碼為文字，請改貼純文字內容")
