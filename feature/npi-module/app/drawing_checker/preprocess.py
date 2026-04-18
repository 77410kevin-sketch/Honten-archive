"""
圖面預處理模組 — HSV 色彩空間偵測橙色/金色標記尺寸
"""

import io, base64
from PIL import Image, ImageDraw

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


def _split_at_gaps(boxes: list, orange_mask, min_gap: int = 2) -> list:
    result = []
    for box in boxes:
        bh = box["y2"] - box["y1"]
        if bh < 18:
            result.append(box)
            continue

        sub = orange_mask[box["y1"]:box["y2"], box["x1"]:box["x2"]]
        row_has = sub.any(axis=1)

        orange_rows = [i for i, h in enumerate(row_has) if h]
        if not orange_rows:
            continue
        first_row, last_row = orange_rows[0], orange_rows[-1]
        effective_height = last_row - first_row + 1
        if effective_height < 18:
            result.append(box)
            continue

        EDGE = 3
        gaps = []
        empty_run = 0
        empty_start = -1
        for i in range(first_row + EDGE, last_row - EDGE + 1):
            if not row_has[i]:
                if empty_run == 0:
                    empty_start = i
                empty_run += 1
            else:
                if empty_run >= min_gap:
                    gaps.append((empty_start, i))
                empty_run = 0

        if not gaps:
            result.append(box)
            continue

        cut_points = [first_row]
        for g_s, g_e in gaps:
            cut_points.append((g_s + g_e) // 2)
        cut_points.append(last_row + 1)

        for ci in range(len(cut_points) - 1):
            seg_s, seg_e = cut_points[ci], cut_points[ci+1]
            seg_rows = [i for i in range(seg_s, seg_e) if row_has[i]]
            if not seg_rows:
                continue
            y1_new = box["y1"] + seg_rows[0]
            y2_new = box["y1"] + seg_rows[-1] + 1
            if y2_new - y1_new < 4:
                continue
            result.append({
                "x1": box["x1"], "y1": y1_new,
                "x2": box["x2"], "y2": y2_new,
            })

    return result


def _rgb_to_hsv(R, G, B):
    r = R.astype(np.float32) / 255.0
    g = G.astype(np.float32) / 255.0
    b = B.astype(np.float32) / 255.0

    Cmax  = np.maximum(np.maximum(r, g), b)
    Cmin  = np.minimum(np.minimum(r, g), b)
    delta = Cmax - Cmin
    eps   = 1e-8

    H = np.zeros(r.shape, dtype=np.float32)
    m_r = (Cmax == r) & (delta > eps)
    m_g = (Cmax == g) & (delta > eps)
    m_b = (Cmax == b) & (delta > eps)
    H[m_r] = (60 * ((g[m_r] - b[m_r]) / delta[m_r])) % 360
    H[m_g] =  60 * ((b[m_g] - r[m_g]) / delta[m_g]) + 120
    H[m_b] =  60 * ((r[m_b] - g[m_b]) / delta[m_b]) + 240

    S = np.where(Cmax > eps, delta / Cmax, 0.0).astype(np.float32)
    V = Cmax
    return H, S, V


def find_orange_regions(image_path: str) -> tuple:
    if not _HAS_NUMPY:
        return [], None

    img = Image.open(image_path).convert('RGB')
    W, H = img.size

    arr = np.array(img, dtype=np.uint8)
    hue, sat, val = _rgb_to_hsv(arr[:,:,0], arr[:,:,1], arr[:,:,2])

    title_cutoff = int(H * 0.88)
    orange_mask = (
        (hue >= 10) & (hue <= 65) &
        (sat > 0.45) &
        (val > 0.42)
    )
    orange_mask[title_cutoff:, :] = False

    total = int(orange_mask.sum())
    print(f"  [preprocess] 橙色像素：{total}")
    if total < 15:
        return [], None

    CELL = 14
    ys_px, xs_px = np.where(orange_mask)
    gx_arr = (xs_px // CELL).astype(np.int32)
    gy_arr = (ys_px // CELL).astype(np.int32)
    grid = set(zip(gx_arr.tolist(), gy_arr.tolist()))

    visited = set()
    components = []
    for key in grid:
        if key in visited:
            continue
        comp = []
        queue = [key]
        while queue:
            k = queue.pop()
            if k in visited or k not in grid:
                continue
            visited.add(k)
            comp.append(k)
            kx, ky = k
            for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)):
                nk = (kx+dx, ky+dy)
                if nk not in visited:
                    queue.append(nk)
        if len(comp) >= 2:
            components.append(comp)

    print(f"  [preprocess] grid 群組：{len(components)}")

    PAD = 4
    raw = []
    for comp in components:
        cx_c = [c[0] for c in comp]
        cy_c = [c[1] for c in comp]
        x1 = max(0,  min(cx_c)*CELL - PAD)
        y1 = max(0,  min(cy_c)*CELL - PAD)
        x2 = min(W, (max(cx_c)+1)*CELL + PAD)
        y2 = min(H, (max(cy_c)+1)*CELL + PAD)
        bw, bh = x2-x1, y2-y1
        area = bw * bh
        if area < 80:
            continue
        if area > W * H * 0.06:
            continue
        ratio = max(bw, bh) / max(min(bw, bh), 1)
        if ratio > 18:
            continue
        raw.append({"x1":x1,"y1":y1,"x2":x2,"y2":y2})

    if not raw:
        return [], None

    raw = _split_at_gaps(raw, orange_mask, min_gap=2)

    raw.sort(key=lambda r: ((r["y1"]+r["y2"])//2, (r["x1"]+r["x2"])//2))
    used = [False]*len(raw)
    line_merged = []
    for i, ri in enumerate(raw):
        if used[i]: continue
        group = [ri]
        used[i] = True
        for j in range(i+1, len(raw)):
            if used[j]: continue
            rj = raw[j]
            y_overlap = (ri["y1"] <= rj["y2"] + 2) and (rj["y1"] <= ri["y2"] + 2)
            cx_i = (ri["x1"]+ri["x2"])/2
            cx_j = (rj["x1"]+rj["x2"])/2
            x_close = abs(cx_i-cx_j) < 70
            if y_overlap and x_close:
                group.append(rj)
                used[j] = True
        line_merged.append({
            "x1": min(g["x1"] for g in group),
            "y1": min(g["y1"] for g in group),
            "x2": max(g["x2"] for g in group),
            "y2": max(g["y2"] for g in group),
        })

    filtered = []
    for r in line_merged:
        bw, bh = r["x2"]-r["x1"], r["y2"]-r["y1"]
        area = bw * bh
        if area > W * H * 0.04:
            continue
        if bw > W * 0.35:
            continue
        filtered.append(r)

    if not filtered:
        return [], None

    filtered.sort(key=lambda r: (
        round((r["y1"]+r["y2"])/2 / 20) * 20,
        (r["x1"]+r["x2"])/2
    ))

    regions = []
    for idx, r in enumerate(filtered):
        bw, bh = r["x2"]-r["x1"], r["y2"]-r["y1"]
        orient = "h" if bw >= bh else "v"
        regions.append({
            "idx": idx+1,
            "x1": r["x1"], "y1": r["y1"],
            "x2": r["x2"], "y2": r["y2"],
            "x1p": round(r["x1"]/W*100, 2),
            "y1p": round(r["y1"]/H*100, 2),
            "x2p": round(r["x2"]/W*100, 2),
            "y2p": round(r["y2"]/H*100, 2),
            "cx":  round((r["x1"]+r["x2"])/2/W*100, 2),
            "cy":  round((r["y1"]+r["y2"])/2/H*100, 2),
            "orientation": orient,
        })

    print(f"  [preprocess] 最終 {len(regions)} 個標記框")

    MAX_W = 1600
    ann   = img.copy()
    scale = 1.0
    if W > MAX_W:
        scale = MAX_W / W
        ann = ann.resize((MAX_W, int(H*scale)), Image.LANCZOS)

    draw = ImageDraw.Draw(ann)
    for r in regions:
        def s(v): return int(v * scale)
        x1s, y1s = s(r["x1"]), s(r["y1"])
        x2s, y2s = s(r["x2"]), s(r["y2"])
        draw.rectangle([x1s-2, y1s-2, x2s+2, y2s+2], outline=(255,255,255), width=3)
        draw.rectangle([x1s, y1s, x2s, y2s], outline=(0,140,255), width=2)
        lx, ly = x1s, max(0, y1s-16)
        draw.rectangle([lx, ly, lx+18, ly+15], fill=(0,140,255))
        draw.text((lx+2, ly+1), str(r["idx"]), fill=(255,255,255))

    buf = io.BytesIO()
    ann.convert('RGB').save(buf, format='JPEG', quality=88, optimize=True)
    ann_b64 = base64.standard_b64encode(buf.getvalue()).decode()

    return regions, ann_b64


def pdf_first_page_thumbnail(pdf_path: str, max_w: int = 800) -> str | None:
    try:
        import fitz
        doc  = fitz.open(pdf_path)
        page = doc[0]
        mat  = fitz.Matrix(100/72, 100/72)
        pix  = page.get_pixmap(matrix=mat, alpha=False)
        img  = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        doc.close()
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height*ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=82)
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
        return "data:image/jpeg;base64," + b64
    except Exception as e:
        print(f"  [thumbnail] 失敗：{e}")
        return None
