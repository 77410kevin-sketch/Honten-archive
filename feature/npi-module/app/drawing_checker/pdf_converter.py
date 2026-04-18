"""
PDF 轉圖片模組 — 使用 PyMuPDF 將 PDF 轉為高解析度圖片
"""
import os
import tempfile
import fitz  # PyMuPDF

def pdf_to_images(pdf_path: str, dpi: int = 200, output_dir: str = None) -> list:
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="drawing_checker_")

    doc = fitz.open(pdf_path)
    image_paths = []
    matrix = fitz.Matrix(dpi / 72, dpi / 72)

    for page_num in range(len(doc)):
        page = doc[page_num]
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        image_path = os.path.join(output_dir, f"page_{page_num + 1:03d}.png")
        pixmap.save(image_path)
        image_paths.append(image_path)

    doc.close()
    return image_paths

def cleanup_temp_images(image_paths: list):
    for path in image_paths:
        try:
            os.remove(path)
        except OSError:
            pass
    if image_paths:
        try:
            os.rmdir(os.path.dirname(image_paths[0]))
        except OSError:
            pass
