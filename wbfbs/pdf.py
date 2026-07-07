"""Сборка PNG-стикеров WB в один PDF на печать.

WB отдаёт этикетки/стикеры как base64 PNG. Печатают их либо на термопринтере
(одна этикетка = одна страница нужного физического размера), либо на A4
сеткой. Оба режима здесь.
"""

from __future__ import annotations

import io

from PIL import Image

MM_PER_INCH = 25.4
DPI = 300

# типовые размеры этикеток WB, мм
LABEL_58x40 = (58, 40)
LABEL_40x30 = (40, 30)
A4_MM = (210, 297)


def _mm_to_px(mm: float) -> int:
    return round(mm / MM_PER_INCH * DPI)


def _load(png_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(png_bytes))
    return img.convert("RGB")


def stickers_to_pdf(
    pngs: list[bytes],
    out_path: str,
    label_mm: tuple[float, float] = LABEL_58x40,
) -> str:
    """Одна этикетка = одна страница физического размера label_mm (термопринтер)."""
    if not pngs:
        raise ValueError("Пустой список стикеров")
    size = (_mm_to_px(label_mm[0]), _mm_to_px(label_mm[1]))
    pages = [_load(p).resize(size, Image.LANCZOS) for p in pngs]
    pages[0].save(
        out_path, "PDF", resolution=DPI, save_all=True, append_images=pages[1:]
    )
    return out_path


def stickers_to_a4_pdf(
    pngs: list[bytes],
    out_path: str,
    label_mm: tuple[float, float] = LABEL_58x40,
    margin_mm: float = 8.0,
    gap_mm: float = 3.0,
) -> str:
    """Сетка этикеток на A4 для печати на обычном принтере и нарезки."""
    if not pngs:
        raise ValueError("Пустой список стикеров")
    page_w, page_h = _mm_to_px(A4_MM[0]), _mm_to_px(A4_MM[1])
    lw, lh = _mm_to_px(label_mm[0]), _mm_to_px(label_mm[1])
    margin, gap = _mm_to_px(margin_mm), _mm_to_px(gap_mm)

    cols = max(1, (page_w - 2 * margin + gap) // (lw + gap))
    rows = max(1, (page_h - 2 * margin + gap) // (lh + gap))
    per_page = cols * rows

    pages: list[Image.Image] = []
    for start in range(0, len(pngs), per_page):
        page = Image.new("RGB", (page_w, page_h), "white")
        for i, png in enumerate(pngs[start : start + per_page]):
            r, c = divmod(i, cols)
            img = _load(png).resize((lw, lh), Image.LANCZOS)
            page.paste(img, (margin + c * (lw + gap), margin + r * (lh + gap)))
        pages.append(page)

    pages[0].save(
        out_path, "PDF", resolution=DPI, save_all=True, append_images=pages[1:]
    )
    return out_path
