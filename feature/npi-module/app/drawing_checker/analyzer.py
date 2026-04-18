"""
圖面分析模組 — 使用 Claude Vision API 提取尺寸標注
"""

import base64
import json
import re
import anthropic

from app.drawing_checker.preprocess import find_orange_regions

SYSTEM_ORANGE = """你是一位專業的工業製圖量測工程師。
圖面上已標出多個「藍色方框」，每個框內包含一個需要量測的尺寸標注。
你的任務是逐一讀出每個藍色框內的尺寸資訊。

輸出格式為純 JSON（無任何說明文字）：
{
  "part_name": "零件名稱（從標題欄讀取，若無則 Unknown）",
  "drawing_no": "圖號（從標題欄讀取，若無則 N/A）",
  "has_yellow_marks": true,
  "dimensions": [
    {
      "item_no": 1,
      "name": "尺寸描述（位置/特徵）",
      "nominal": "標稱數字",
      "unit": "mm",
      "upper_tol": 正數或null,
      "lower_tol": 負數或null,
      "note": "視圖備註"
    }
  ]
}
注意：dimensions 陣列的順序必須與藍色框的編號順序完全對應（框1→item_no 1，框2→item_no 2…）。
"""

SYSTEM_ALL = """你是一位專業的工業製圖量測工程師。
請從工程圖面中找出所有尺寸標注（長度、寬度、高度、公差、角度、直徑等）。

輸出格式為純 JSON（無任何說明文字）：
{
  "part_name": "零件名稱（從標題欄讀取，若無則 Unknown）",
  "drawing_no": "圖號（從標題欄讀取，若無則 N/A）",
  "has_yellow_marks": false,
  "dimensions": [
    {
      "item_no": 1,
      "name": "尺寸描述",
      "nominal": "標稱數字",
      "unit": "mm",
      "upper_tol": 正數或null,
      "lower_tol": 負數或null,
      "note": "視圖備註",
      "x": 25,
      "y": 40
    }
  ]
}
"""


def encode_image(image_path: str) -> tuple:
    ext = image_path.lower().split(".")[-1]
    media_type_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    }
    media_type = media_type_map.get(ext, "image/png")
    with open(image_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return data, media_type


def _parse_json_response(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        inner = [l for i, l in enumerate(lines)
                 if i > 0 and not (i == len(lines) - 1 and l.strip() == "```")]
        text = "\n".join(inner).strip()

    for candidate in [text,
                      text[text.find("{"):text.rfind("}")+1] if "{" in text else ""]:
        if not candidate:
            continue
        cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
    return None


def _call_claude(client, system: str, image_b64: str, media_type: str,
                 user_text: str, use_thinking: bool = False) -> str:
    kwargs = dict(
        model="claude-opus-4-6",
        max_tokens=16000,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64",
                            "media_type": media_type,
                            "data": image_b64}},
                {"type": "text", "text": user_text},
            ],
        }],
    )
    if use_thinking:
        kwargs["thinking"] = {"type": "adaptive"}

    with client.messages.stream(**kwargs) as stream:
        msg = stream.get_final_message()

    text = next((b.text for b in msg.content if b.type == "text"), "")
    return text


def analyze_drawing_image(image_path: str, api_key: str = None) -> dict:
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    print(f"\n  → 分析：{image_path}")

    regions, annotated_b64 = find_orange_regions(image_path)
    has_orange = len(regions) > 0

    if has_orange:
        print(f"  → 偵測到 {len(regions)} 個橙色標記，使用標記圖分析")
        ann_data = annotated_b64
        user_text = (
            f"此圖面上有 {len(regions)} 個藍色方框（已用藍框標出並標上編號 1~{len(regions)}）。\n"
            f"請按編號順序逐一讀出每個藍框內的尺寸數值與公差。\n"
            f"同時請從標題欄讀取零件名稱與圖號。\n"
            f"輸出純 JSON，dimensions 陣列共 {len(regions)} 筆，順序與框號一致。"
        )
        text_resp = _call_claude(client, SYSTEM_ORANGE, ann_data, "image/jpeg",
                                 user_text, use_thinking=False)
    else:
        print("  → 未偵測到橙色標記，分析全部尺寸")
        orig_data, media_type = encode_image(image_path)
        user_text = (
            "請分析此工程圖面，提取所有尺寸標注。\n"
            "從標題欄讀取零件名稱與圖號。\n"
            "估算每個尺寸的位置（x, y 百分比）。\n"
            "輸出純 JSON。"
        )
        text_resp = _call_claude(client, SYSTEM_ALL, orig_data, media_type,
                                 user_text, use_thinking=True)

    print(f"  → 回應長度：{len(text_resp)} 字元")
    result = _parse_json_response(text_resp)

    if result is None:
        return {
            "part_name": "Unknown", "drawing_no": "N/A",
            "has_yellow_marks": has_orange, "dimensions": [],
            "_parse_error": True, "_raw": text_resp[:300],
        }

    result.setdefault("part_name", "Unknown")
    result.setdefault("drawing_no", "N/A")
    result.setdefault("has_yellow_marks", has_orange)
    result.setdefault("dimensions", [])

    dims = result["dimensions"]
    if has_orange and regions:
        for i, dim in enumerate(dims):
            if i < len(regions):
                r = regions[i]
                dim["x"]    = r["cx"]
                dim["y"]    = r["cy"]
                dim["x1p"]  = r["x1p"]
                dim["y1p"]  = r["y1p"]
                dim["x2p"]  = r["x2p"]
                dim["y2p"]  = r["y2p"]
                dim["orientation"] = r.get("orientation", "h")
            dim.setdefault("item_no", i + 1)
    else:
        for i, dim in enumerate(dims):
            dim.setdefault("item_no", i + 1)

    print(f"  ✅ 完成：{result['part_name']} / {len(dims)} 項")
    return result


def analyze_multiple_images(image_paths: list, api_key: str = None) -> dict:
    if not image_paths:
        return {"part_name": "Unknown", "drawing_no": "N/A",
                "has_yellow_marks": False, "dimensions": []}
    if len(image_paths) == 1:
        return analyze_drawing_image(image_paths[0], api_key)

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    content = []
    for i, path in enumerate(image_paths):
        img_data, mt = encode_image(path)
        content.append({"type": "image",
                        "source": {"type": "base64",
                                   "media_type": mt, "data": img_data}})
        content.append({"type": "text", "text": f"（第 {i+1} 頁）"})

    content.append({"type": "text", "text": (
        "以上為同一零件的多頁圖面。請整合識別所有尺寸標注（優先提取橙/黃色文字尺寸；若無則全部）。\n"
        "估算每個尺寸的 x, y 位置（0-100百分比）。輸出純 JSON。"
    )})

    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM_ALL,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        msg = stream.get_final_message()

    text_resp = next((b.text for b in msg.content if b.type == "text"), "")
    result = _parse_json_response(text_resp)

    if result is None:
        return {"part_name": "Unknown", "drawing_no": "N/A",
                "has_yellow_marks": False, "dimensions": []}

    result.setdefault("part_name", "Unknown")
    result.setdefault("drawing_no", "N/A")
    result.setdefault("has_yellow_marks", False)
    result.setdefault("dimensions", [])
    return result
