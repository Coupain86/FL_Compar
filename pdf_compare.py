from dataclasses import dataclass, field
import fitz
from typing import List, Tuple, Optional, Callable
from collections import Counter
import base64
import time
import sys
import os
import argparse
import threading
import concurrent.futures
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
import io

# ─────────────────────────────────────────────
#  CONFIGURATION GLOBALE
# ─────────────────────────────────────────────
RENDER_DPI       = 100   # DPI de rasterisation
PIXEL_TOLERANCE  = 10    # Tolérance colorimétrique par canal (0-255)
MIN_DIFF_AREA_PX = 50    # Surface minimale (px²) d'une zone diff
CLUSTER_PADDING  = 8     # Padding autour des bounding boxes de diff
MERGE_GAP_PX     = 15    # Distance max entre clusters pour fusion (px)
MAX_WORKERS      = max(2, (os.cpu_count() or 2) - 1)  # Cœurs parallèles


# ─────────────────────────────────────────────
#  UTILITAIRES
# ─────────────────────────────────────────────
def fmt_eta(seconds: float) -> str:
    s = int(seconds)
    if s < 60:     return f"{s}s"
    elif s < 3600: return f"{s // 60}m {s % 60:02d}s"
    else:
        h = s // 3600; m = (s % 3600) // 60; sec = s % 60
        return f"{h}h {m:02d}m {sec:02d}s"


class Progress:
    """Barre de progression terminal. Ignorée si progress_callback fourni."""
    def __init__(self, total: int, prefix: str = "", width: int = 30):
        self.total = total; self.prefix = prefix; self.width = width
        self._start = time.time()

    def update(self, current: int, suffix: str = ""):
        elapsed = time.time() - self._start
        eta = (elapsed / current * (self.total - current)) if current > 0 else 0
        eta_str = f" Temps restant : {fmt_eta(eta)}" if eta > 1 else ""
        pct = current / self.total if self.total else 1
        bar = "#" * int(self.width * pct) + "-" * (self.width - int(self.width * pct))
        sys.stdout.write(f"\r{self.prefix} [{bar}] {current}/{self.total}{eta_str}  {suffix}")
        sys.stdout.flush()
        if current >= self.total:
            sys.stdout.write("\n"); sys.stdout.flush()


def ensure_html_extension(path: str) -> str:
    base, ext = os.path.splitext(path)
    return path if ext.lower() == ".html" else base + ".html"


def is_in_excluded_zone(x0, y0, x1, y1, exclude_zones):
    for ex_x0, ex_y0, ex_x1, ex_y1 in exclude_zones:
        if not (x1 < ex_x0 or x0 > ex_x1 or y1 < ex_y0 or y0 > ex_y1):
            return True
    return False


def serialize_anomaly(anomaly_type: str, item: dict) -> str:
    if anomaly_type == "moved":
        return f"moved|{item['text']}|{round(item['ref_x']):.0f}|{round(item['ref_y']):.0f}"
    return f"{anomaly_type}|{item['text']}|{round(item['x0']):.0f}|{round(item['y0']):.0f}"


def get_distance(b1, b2) -> float:
    return abs(b1.x0 - b2.x0) + abs(b1.y0 - b2.y0)


def rect_to_b64(doc: fitz.Document, page_num_1: int, x0, y0, x1, y1, scale=2.0, padding=8) -> str:
    try:
        page = doc[page_num_1 - 1]
        r = fitz.Rect(max(0, x0-padding), max(0, y0-padding),
                      min(page.rect.width, x1+padding), min(page.rect.height, y1+padding))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=r)
        return base64.b64encode(pix.tobytes("png")).decode("utf-8")
    except Exception:
        return ""


# ─────────────────────────────────────────────
#  DATACLASSES
# ─────────────────────────────────────────────
@dataclass
class TextBlock:
    text: str; x0: float; y0: float; x1: float; y1: float; text_b64: str = ""

@dataclass
class ImageDiffRegion:
    x0: float; y0: float; x1: float; y1: float
    diff_score: float
    ref_img_b64: str = ""; new_img_b64: str = ""

@dataclass
class PageComparisonResult:
    ref_page: int; new_page: int
    moved:       List[dict]            = field(default_factory=list)
    missing:     List[dict]            = field(default_factory=list)
    added:       List[dict]            = field(default_factory=list)
    image_diffs: List[ImageDiffRegion] = field(default_factory=list)
    page_count_warning: str            = ""

    def has_anomalies(self) -> bool:
        return bool(self.moved or self.missing or self.added or self.image_diffs)


# ─────────────────────────────────────────────
#  PHASE 1 — EXTRACTION DU TEXTE
# ─────────────────────────────────────────────
def extract_text_blocks(pdf_path: str, exclude_zones: List[Tuple] = None) -> List[Tuple[int, List[TextBlock]]]:
    if exclude_zones is None:
        exclude_zones = []
    doc = fitz.open(pdf_path)
    pages = []
    for idx, page in enumerate(doc):
        lines_dict = {}
        for w in page.get_text("words"):
            x0, y0, x1, y1, text, block_no, line_no, _ = w
            key = (block_no, line_no)
            if key not in lines_dict:
                lines_dict[key] = {"text_parts": [text], "x0": x0, "y0": y0, "x1": x1, "y1": y1}
            else:
                d = lines_dict[key]
                d["text_parts"].append(text)
                d["x0"] = min(d["x0"], x0); d["y0"] = min(d["y0"], y0)
                d["x1"] = max(d["x1"], x1); d["y1"] = max(d["y1"], y1)
        page_blocks = []
        for ld in lines_dict.values():
            text = " ".join(ld["text_parts"]).strip()
            if text and not is_in_excluded_zone(ld["x0"], ld["y0"], ld["x1"], ld["y1"], exclude_zones):
                page_blocks.append(TextBlock(text=text, x0=ld["x0"], y0=ld["y0"], x1=ld["x1"], y1=ld["y1"]))
        pages.append((idx + 1, page_blocks))
    doc.close()
    return pages


def _extract_chunk(args_tuple):
    """
    Extrait le texte d'un sous-ensemble de pages.
    Conçu pour être appelé par ProcessPoolExecutor — ouvre son propre fitz.Document.
    Retourne une liste de (page_num_1based, [TextBlock, ...]).
    """
    pdf_path, page_indices, exclude_zones = args_tuple
    doc = fitz.open(pdf_path)
    results = []
    for idx in page_indices:
        page = doc[idx]
        lines_dict = {}
        for w in page.get_text("words"):
            x0, y0, x1, y1, text, block_no, line_no, _ = w
            key = (block_no, line_no)
            if key not in lines_dict:
                lines_dict[key] = {"text_parts": [text], "x0": x0, "y0": y0, "x1": x1, "y1": y1}
            else:
                d = lines_dict[key]
                d["text_parts"].append(text)
                d["x0"] = min(d["x0"], x0); d["y0"] = min(d["y0"], y0)
                d["x1"] = max(d["x1"], x1); d["y1"] = max(d["y1"], y1)
        page_blocks = []
        for ld in lines_dict.values():
            text = " ".join(ld["text_parts"]).strip()
            if text and not is_in_excluded_zone(ld["x0"], ld["y0"], ld["x1"], ld["y1"], exclude_zones):
                page_blocks.append((text, ld["x0"], ld["y0"], ld["x1"], ld["y1"]))
        results.append((idx + 1, page_blocks))
    doc.close()
    return results


def extract_text_blocks_parallel(
    pdf_path: str,
    exclude_zones: List[Tuple] = None,
    max_workers: int = MAX_WORKERS,
) -> List[Tuple[int, List[TextBlock]]]:
    """
    Version parallèle de extract_text_blocks.
    Découpe les pages en chunks (un par worker), les distribue via ProcessPoolExecutor,
    réassemble dans l'ordre. Gain ~nb_cœurs× sur les gros documents.
    """
    if exclude_zones is None:
        exclude_zones = []

    doc = fitz.open(pdf_path)
    n_pages = doc.page_count
    doc.close()

    if n_pages == 0:
        return []

    # Découper les indices de pages en chunks équilibrés
    indices = list(range(n_pages))
    chunk_size = max(1, (n_pages + max_workers - 1) // max_workers)
    chunks = [indices[i:i + chunk_size] for i in range(0, n_pages, chunk_size)]

    tasks = [(pdf_path, chunk, exclude_zones) for chunk in chunks]

    all_results: List[Tuple[int, List]] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_extract_chunk, t) for t in tasks]
        for future in concurrent.futures.as_completed(futures):
            all_results.extend(future.result())

    # Réassembler dans l'ordre des pages
    all_results.sort(key=lambda x: x[0])

    # Reconstruire les TextBlock depuis les tuples (sérialisation inter-process)
    pages = []
    for page_num, block_tuples in all_results:
        blocks = [TextBlock(text=t, x0=x0, y0=y0, x1=x1, y1=y1)
                  for t, x0, y0, x1, y1 in block_tuples]
        pages.append((page_num, blocks))

    return pages


def _page_matches_rule(doc: 'fitz.Document', page_idx: int, rule: dict) -> bool:
    """
    Retourne True si la page contient au moins un des mots-clés de la règle
    dans la zone définie (coordonnées en points PDF).
    """
    try:
        x0, y0, x1, y1 = rule["zone"]
        keywords = [k.lower() for k in rule.get("keywords", [])]
        if not keywords:
            return False
        page = doc[page_idx]
        clip = fitz.Rect(x0, y0, x1, y1)
        text = page.get_text("text", clip=clip).lower()
        return any(kw in text for kw in keywords)
    except Exception:
        return False


def trim_pages(pages, skip_start=0, skip_end=0):
    end = len(pages) - skip_end if skip_end > 0 else len(pages)
    return pages[skip_start:end]


# ─────────────────────────────────────────────
#  PHASE 2 — IMAGE DIFF
# ─────────────────────────────────────────────
def page_to_pil(doc: fitz.Document, page_idx: int, dpi: int = RENDER_DPI) -> Image.Image:
    scale = dpi / 72.0
    pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def mask_text_zones(img: Image.Image, text_blocks: List[TextBlock], dpi: int) -> Image.Image:
    draw = ImageDraw.Draw(img)
    scale = dpi / 72.0
    for b in text_blocks:
        draw.rectangle([int(b.x0*scale)-2, int(b.y0*scale)-4,
                        int(b.x1*scale)+2, int(b.y1*scale)+4], fill=(255, 255, 255))
    return img


def cluster_diff_mask(diff_mask: np.ndarray, min_area: int = MIN_DIFF_AREA_PX,
                      merge_gap: int = MERGE_GAP_PX) -> List[Tuple[int,int,int,int]]:
    if not diff_mask.any():
        return []

    def _dilate_axis(arr, gap, axis):
        padded = np.pad(arr.astype(np.int8),
                        [(gap, gap) if i == axis else (0, 0) for i in range(2)],
                        constant_values=0)
        cs = np.cumsum(padded, axis=axis)
        h, w = arr.shape
        return ((cs[:, gap*2:gap*2+w] - cs[:, :w]) > 0) if axis == 1 \
               else ((cs[gap*2:gap*2+h, :] - cs[:h, :]) > 0)

    dilated = _dilate_axis(_dilate_axis(diff_mask, merge_gap, 1), merge_gap, 0)

    row_active  = dilated.any(axis=1)
    row_changes = np.diff(row_active.astype(np.int8), prepend=0, append=0)
    result = []
    for ys, ye in zip(np.where(row_changes == 1)[0], np.where(row_changes == -1)[0]):
        col_active  = dilated[ys:ye].any(axis=0)
        col_changes = np.diff(col_active.astype(np.int8), prepend=0, append=0)
        for xs, xe in zip(np.where(col_changes == 1)[0], np.where(col_changes == -1)[0]):
            if int(diff_mask[ys:ye, xs:xe].sum()) >= min_area:
                result.append((int(xs), int(ys), int(xe-1), int(ye-1)))
    return result


def _min_score_after_shift(
    ref_arr: np.ndarray, new_arr: np.ndarray,
    bx0: int, by0: int, bx1: int, by1: int,
    shift_tolerance: int
) -> float:
    """
    Retourne le score minimum sur tous les décalages (dx,dy) dans [-N,+N].
    Vectorisé via stride_tricks : un seul passage NumPy pour tous les décalages,
    sans double boucle Python.
    """
    if shift_tolerance <= 0:
        return float(np.abs(ref_arr[by0:by1+1, bx0:bx1+1].astype(np.int16)
                            - new_arr[by0:by1+1, bx0:bx1+1].astype(np.int16)).mean())

    N  = shift_tolerance
    h, w = ref_arr.shape[:2]

    # Zone de référence (crop exact)
    ref_zone = ref_arr[by0:by1+1, bx0:bx1+1].astype(np.int16)  # (zh, zw [,3])
    zh, zw   = ref_zone.shape[:2]

    # Fenêtre dans new_arr incluant tous les décalages possibles
    wy0 = max(0,   by0 - N);  wy1 = min(h, by1 + N + 1)
    wx0 = max(0,   bx0 - N);  wx1 = min(w, bx1 + N + 1)
    new_win = new_arr[wy0:wy1, wx0:wx1].astype(np.int16)  # fenêtre élargie

    win_h, win_w = new_win.shape[:2]
    if win_h < zh or win_w < zw:
        # Fenêtre trop petite (bord de page), fallback direct
        return float(np.abs(ref_zone - new_arr[by0:by1+1, bx0:bx1+1].astype(np.int16)).mean())

    # Nombre de décalages valides dans chaque direction
    n_dy = win_h - zh + 1
    n_dx = win_w - zw + 1

    # Construire une vue stride : shape (n_dy, n_dx, zh, zw [,3])
    if new_win.ndim == 3:
        s = new_win.strides
        patches = np.lib.stride_tricks.as_strided(
            new_win,
            shape=(n_dy, n_dx, zh, zw, new_win.shape[2]),
            strides=(s[0], s[1], s[0], s[1], s[2]),
            writeable=False)
        # Diff absolue : (n_dy, n_dx, zh, zw, 3) → mean sur axes 2,3,4
        diff = np.abs(patches - ref_zone).mean(axis=(2, 3, 4))
    else:
        s = new_win.strides
        patches = np.lib.stride_tricks.as_strided(
            new_win,
            shape=(n_dy, n_dx, zh, zw),
            strides=(s[0], s[1], s[0], s[1]),
            writeable=False)
        diff = np.abs(patches - ref_zone).mean(axis=(2, 3))

    return float(diff.min())


def crop_b64_from_pil(img: Image.Image, px0, py0, px1, py1, padding=8) -> str:
    w, h = img.size
    box = (max(0, px0-padding), max(0, py0-padding), min(w, px1+padding), min(h, py1+padding))
    crop = img.crop(box)
    # JPEG qualité 80 : ~10× plus léger que PNG, suffisant pour un rapport de diff
    buf = io.BytesIO()
    crop.convert("RGB").save(buf, format="JPEG", quality=80, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ─────────────────────────────────────────────
#  TRAITEMENT D'UNE PAGE (appelable en parallèle)
# ─────────────────────────────────────────────
def _process_page(args_tuple) -> PageComparisonResult:
    """Traite une paire de pages (Phase 1 + Phase 2). Conçu pour ProcessPoolExecutor."""
    (ref_pdf_path, new_pdf_path, ref_page_num, new_page_num,
     ref_blocks_data, new_blocks_data,
     tolerance, dpi, pixel_tolerance, min_diff_area, exclude_zones,
     include_screenshots, shift_tolerance,
     run_phase1, run_phase2) = args_tuple

    # Reconstruire les TextBlock depuis les tuples (nécessaire après sérialisation inter-process)
    ref_blocks = [TextBlock(*b) for b in ref_blocks_data]
    new_blocks  = [TextBlock(*b) for b in new_blocks_data]

    page_res = PageComparisonResult(ref_page=ref_page_num, new_page=new_page_num)
    matched_new = set()

    # ── Phase 1 : texte ──────────────────────
    if run_phase1:
        for rb in ref_blocks:
            candidates = [(i, b) for i, b in enumerate(new_blocks)
                          if i not in matched_new and rb.text == b.text]
            if candidates:
                best_idx, best = min(candidates, key=lambda c: get_distance(rb, c[1]))
                dx, dy = abs(rb.x0 - best.x0), abs(rb.y0 - best.y0)
                matched_new.add(best_idx)
                if dx > tolerance or dy > tolerance:
                    page_res.moved.append({"text": rb.text,
                        "ref_x": rb.x0, "ref_y": rb.y0, "new_x": best.x0, "new_y": best.y0,
                        "dx": dx, "dy": dy,
                        "dx_px": round(dx * dpi / 72, 1), "dy_px": round(dy * dpi / 72, 1),
                        "text_b64": ""})
            else:
                page_res.missing.append({"text": rb.text,
                    "x0": rb.x0, "y0": rb.y0, "x1": rb.x1, "y1": rb.y1, "text_b64": ""})

        for i, b in enumerate(new_blocks):
            if i not in matched_new:
                page_res.added.append({"text": b.text,
                    "x0": b.x0, "y0": b.y0, "x1": b.x1, "y1": b.y1, "text_b64": ""})

    # ── Phase 2 : image diff ─────────────────
    if run_phase2:
        ref_doc = fitz.open(ref_pdf_path)
        new_doc = fitz.open(new_pdf_path)

        ref_img = page_to_pil(ref_doc, ref_page_num - 1, dpi)
        new_img = page_to_pil(new_doc, new_page_num - 1, dpi)

        ref_masked = mask_text_zones(ref_img.copy(), ref_blocks, dpi)
        new_masked = mask_text_zones(new_img.copy(), new_blocks, dpi)

        scale = dpi / 72.0
        if exclude_zones:
            for img in [ref_masked, new_masked]:
                draw = ImageDraw.Draw(img)
                for ex_x0, ex_y0, ex_x1, ex_y1 in exclude_zones:
                    draw.rectangle([int(ex_x0*scale), int(ex_y0*scale),
                                    int(ex_x1*scale), int(ex_y1*scale)], fill=(255,255,255))

        if ref_masked.size != new_masked.size:
            new_masked   = new_masked.resize(ref_masked.size, Image.LANCZOS)
            new_img_res  = new_img.resize(ref_img.size, Image.LANCZOS)
        else:
            new_img_res = new_img

        ref_arr   = np.array(ref_masked, dtype=np.int16)
        new_arr   = np.array(new_masked, dtype=np.int16)
        diff_arr  = np.abs(ref_arr - new_arr)
        diff_mask = diff_arr.max(axis=2) > pixel_tolerance

        for bx0, by0, bx1, by1 in cluster_diff_mask(diff_mask, min_diff_area):
            bx0 = max(0, bx0 - CLUSTER_PADDING); by0 = max(0, by0 - CLUSTER_PADDING)
            bx1 = min(ref_masked.width-1, bx1 + CLUSTER_PADDING)
            by1 = min(ref_masked.height-1, by1 + CLUSTER_PADDING)
            pdf_x0, pdf_y0 = bx0/scale, by0/scale
            pdf_x1, pdf_y1 = bx1/scale, by1/scale
            if is_in_excluded_zone(pdf_x0, pdf_y0, pdf_x1, pdf_y1, exclude_zones):
                continue
            score = _min_score_after_shift(
                ref_arr, new_arr, bx0, by0, bx1, by1, shift_tolerance)
            if score < pixel_tolerance:
                continue
            ref_b64 = crop_b64_from_pil(ref_img,    bx0, by0, bx1, by1) if include_screenshots else ""
            new_b64 = crop_b64_from_pil(new_img_res, bx0, by0, bx1, by1) if include_screenshots else ""
            page_res.image_diffs.append(ImageDiffRegion(
                x0=pdf_x0, y0=pdf_y0, x1=pdf_x1, y1=pdf_y1,
                diff_score=score,
                ref_img_b64=ref_b64, new_img_b64=new_b64))

        ref_doc.close(); new_doc.close()
    return page_res


# ─────────────────────────────────────────────
#  APPARIEMENT ÉLASTIQUE (DTW bidirectionnel)
# ─────────────────────────────────────────────
def _text_similarity(blocks_a: List[TextBlock], blocks_b: List[TextBlock]) -> float:
    """
    Retourne un score de similarité [0.0, 1.0] entre deux listes de blocs texte.
    Basé sur l'intersection des mots (sac de mots normalisé).
    O(|a| + |b|) via Counter — rapide même sur 3000 pages.
    """
    if not blocks_a and not blocks_b:
        return 1.0
    if not blocks_a or not blocks_b:
        return 0.0

    def word_bag(blocks):
        bag = Counter()
        for b in blocks:
            for w in b.text.lower().split():
                bag[w] += 1
        return bag

    bag_a = word_bag(blocks_a)
    bag_b = word_bag(blocks_b)

    # Intersection
    common = sum((bag_a & bag_b).values())
    total  = sum(bag_a.values()) + sum(bag_b.values())
    if total == 0:
        return 1.0
    return 2 * common / total   # Dice coefficient


def _elastic_align(
    ref_pages: List[Tuple[int, List[TextBlock]]],
    new_pages: List[Tuple[int, List[TextBlock]]],
    band_radius: int = 50,
    sim_threshold: float = 0.05,
) -> List[Tuple[int, int]]:
    """
    Appariement élastique bidirectionnel par DTW sur la similarité textuelle.

    Transitions autorisées :
      • (i+1, j+1) : correspondance 1-1 normale
      • (i+1, j)   : REF avance seule  → page CAND déborde sur 2 pages REF
      • (i,   j+1) : CAND avance seule → page REF déborde sur 2 pages CAND

    Optimisations :
      • Bande diagonale ±band_radius — O(N × band) cellules au lieu de O(N×M)
      • Stockage sparse par dict — seules les cellules dans la bande sont allouées
        (évite les matrices N×M qui explosent à 30 000 pages)
      • Cache de similarité par cellule (calcul unique)
    """
    N = len(ref_pages)
    M = len(new_pages)
    if N == 0 or M == 0:
        return []

    INF = float('inf')

    # ── Cache de similarité ───────────────────────────────────────────
    _sim_cache: dict = {}

    def cost(i: int, j: int) -> float:
        if (i, j) not in _sim_cache:
            s = _text_similarity(ref_pages[i][1], new_pages[j][1])
            _sim_cache[(i, j)] = 1.0 - s if s >= sim_threshold else 1.0
        return _sim_cache[(i, j)]

    # ── DP sparse par dicts (une entrée par cellule dans la bande) ────
    # dp[(i,j)]   = coût cumulé minimal
    # prev[(i,j)] = (pi, pj) du prédécesseur optimal
    dp:   dict = {}
    prev: dict = {}

    dp[(0, 0)] = cost(0, 0)
    prev[(0, 0)] = None

    for i in range(N):
        j_lo = max(0, i - band_radius)
        j_hi = min(M - 1, i + band_radius)
        for j in range(j_lo, j_hi + 1):
            if i == 0 and j == 0:
                continue
            best      = INF
            best_prev = None

            # (i-1, j-1) → (i, j)
            if i > 0 and j > 0:
                v = dp.get((i-1, j-1), INF)
                if v < best: best = v; best_prev = (i-1, j-1)
            # (i-1, j) → (i, j)
            if i > 0:
                v = dp.get((i-1, j), INF)
                if v < best: best = v; best_prev = (i-1, j)
            # (i, j-1) → (i, j)
            if j > 0:
                v = dp.get((i, j-1), INF)
                if v < best: best = v; best_prev = (i, j-1)

            if best < INF:
                dp[(i, j)]   = best + cost(i, j)
                prev[(i, j)] = best_prev

    # ── Backtracking ─────────────────────────────────────────────────
    if dp.get((N-1, M-1), INF) == INF:
        print("  ⚠ Alignement élastique : coin inaccessible dans la bande "
              f"(band_radius={band_radius}). Appariement séquentiel de secours.")
        n = min(N, M)
        return [(ref_pages[i][0], new_pages[i][0]) for i in range(n)]

    path = []
    ci, cj = N - 1, M - 1
    while (ci, cj) is not None:
        path.append((ref_pages[ci][0], new_pages[cj][0]))
        nxt = prev.get((ci, cj))
        if nxt is None:
            break
        ci, cj = nxt

    path.reverse()

    seen   = set()
    unique = []
    for pair in path:
        if pair not in seen:
            seen.add(pair)
            unique.append(pair)

    print(f"  Alignement élastique : {len(unique)} paires ({N} pages REF × {M} pages CAND)")
    return unique


# ─────────────────────────────────────────────
#  COMPARAISON PRINCIPALE
# ─────────────────────────────────────────────
def compare_pdfs(
    reference_pdf: str,
    candidate_pdf: str,
    tolerance: float = 2.0,
    ref_skip_start: int = 0, ref_skip_end: int = 0,
    new_skip_start: int = 0, new_skip_end: int = 0,
    exclude_zones: List[Tuple] = None,
    page_exclude_rules: List[dict] = None,
    dpi: int = RENDER_DPI,
    pixel_tolerance: int = PIXEL_TOLERANCE,
    min_diff_area: int = MIN_DIFF_AREA_PX,
    include_screenshots: bool = True,
    shift_tolerance: int = 3,
    elastic_align: bool = False,
    elastic_band: int = 50,
    run_phase1: bool = True,
    run_phase2: bool = True,
    progress_callback: Optional[Callable] = None,
    stop_event: Optional[threading.Event] = None,
    max_workers: int = MAX_WORKERS,
) -> List[PageComparisonResult]:
    """
    Compare deux PDFs page à page (Phase 1 texte + Phase 2 image diff).

    progress_callback(current, total, ref_page_num) — appelé après chaque page.
    stop_event — threading.Event : si set(), arrête proprement après la page en cours.
    max_workers — nombre de processus parallèles (défaut : nb cœurs - 1).
    """
    if exclude_zones is None:
        exclude_zones = []

    # ── Validation des fichiers ───────────────
    for path, label in [(reference_pdf, "REF"), (candidate_pdf, "CANDIDAT")]:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Fichier {label} introuvable : {path}")
        try:
            doc = fitz.open(path)
            if doc.page_count == 0:
                raise ValueError(f"Le fichier {label} est vide ou corrompu : {path}")
            doc.close()
        except Exception as e:
            raise ValueError(f"Impossible d'ouvrir le fichier {label} : {e}")

    # ── Extraction texte parallèle ───────────
    # Un seul ProcessPoolExecutor traite tous les chunks de REF et CAND
    # simultanément. Gain : ~nb_cœurs× vs extraction séquentielle.
    print("  Extraction du texte (REF + CANDIDAT en parallèle)…")

    ref_doc_tmp = fitz.open(reference_pdf)
    new_doc_tmp = fitz.open(candidate_pdf)
    ref_n = ref_doc_tmp.page_count
    new_n = new_doc_tmp.page_count
    ref_doc_tmp.close(); new_doc_tmp.close()

    chunk_size = max(1, max(ref_n, new_n + 1) // max_workers)

    ref_indices = list(range(ref_n))
    new_indices = list(range(new_n))
    ref_chunks  = [ref_indices[i:i+chunk_size] for i in range(0, ref_n, chunk_size)]
    new_chunks  = [new_indices[i:i+chunk_size] for i in range(0, new_n, chunk_size)]

    ref_tasks = [(reference_pdf, chunk, exclude_zones) for chunk in ref_chunks]
    new_tasks = [(candidate_pdf, chunk, exclude_zones) for chunk in new_chunks]

    ref_raw: List[Tuple[int, List]] = []
    new_raw: List[Tuple[int, List]] = []

    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        ref_futures = {executor.submit(_extract_chunk, t): "ref" for t in ref_tasks}
        new_futures = {executor.submit(_extract_chunk, t): "new" for t in new_tasks}
        all_futures = {**ref_futures, **new_futures}
        for future in concurrent.futures.as_completed(all_futures):
            label = all_futures[future]
            if label == "ref":
                ref_raw.extend(future.result())
            else:
                new_raw.extend(future.result())

    ref_raw.sort(key=lambda x: x[0])
    new_raw.sort(key=lambda x: x[0])

    def _rebuild(raw):
        return [(pn, [TextBlock(text=t, x0=x0, y0=y0, x1=x1, y1=y1)
                      for t, x0, y0, x1, y1 in blocks])
                for pn, blocks in raw]

    ref_text_pages = trim_pages(_rebuild(ref_raw), ref_skip_start, ref_skip_end)
    new_text_pages = trim_pages(_rebuild(new_raw), new_skip_start, new_skip_end)
    print(f"  Texte extrait : {len(ref_text_pages)} pages REF, {len(new_text_pages)} pages CAND")

    # ── Exclusion de pages par règles mot-clé ────────────────────────
    # Chaque fichier est filtré INDÉPENDAMMENT :
    # si le mot est trouvé dans une page REF, seule cette page REF est retirée.
    # La page correspondante du candidat n'est PAS retirée (et vice-versa).
    # Les deux listes sont ensuite réappariées séquentiellement.
    if page_exclude_rules:
        ref_doc_tmp = fitz.open(reference_pdf)
        new_doc_tmp = fitz.open(candidate_pdf)

        filtered_ref = []
        for rp, rb in ref_text_pages:
            matched = any(_page_matches_rule(ref_doc_tmp, rp-1, rule)
                          for rule in page_exclude_rules)
            if matched:
                print(f"  REF p.{rp} exclue par règle mot-clé.")
            else:
                filtered_ref.append((rp, rb))

        filtered_new = []
        for np_, nb in new_text_pages:
            matched = any(_page_matches_rule(new_doc_tmp, np_-1, rule)
                          for rule in page_exclude_rules)
            if matched:
                print(f"  CANDIDAT p.{np_} exclue par règle mot-clé.")
            else:
                filtered_new.append((np_, nb))

        ref_doc_tmp.close(); new_doc_tmp.close()

        excl_ref = len(ref_text_pages) - len(filtered_ref)
        excl_new = len(new_text_pages) - len(filtered_new)
        if excl_ref or excl_new:
            print(f"  Exclusions : {excl_ref} page(s) REF, {excl_new} page(s) CANDIDAT.")

        ref_text_pages = filtered_ref
        new_text_pages = filtered_new

    ref_total = len(ref_text_pages)
    new_total = len(new_text_pages)

    # ── Calcul des paires de pages ────────────────────────────────────
    page_count_warning = ""

    if elastic_align:
        # Appariement élastique DTW : gère les débordements dans les deux sens
        print("  Appariement élastique (DTW)…")
        pairs = _elastic_align(ref_text_pages, new_text_pages, band_radius=elastic_band)
        if not pairs:
            raise ValueError("L'alignement élastique n'a produit aucune paire de pages.")
        # Construire des index pour accès rapide aux blocs
        ref_idx = {page_num: blocks for page_num, blocks in ref_text_pages}
        new_idx = {page_num: blocks for page_num, blocks in new_text_pages}
    else:
        # Appariement séquentiel classique (index à index)
        max_pages = min(ref_total, new_total)
        if ref_total != new_total:
            page_count_warning = (
                f"⚠ Nombre de pages différent : REF={ref_total}, CANDIDAT={new_total}. "
                f"Seules les {max_pages} premières pages sont comparées."
            )
            print(f"  {page_count_warning}")
        pairs = [(ref_text_pages[i][0], new_text_pages[i][0]) for i in range(max_pages)]
        ref_idx = {page_num: blocks for page_num, blocks in ref_text_pages}
        new_idx = {page_num: blocks for page_num, blocks in new_text_pages}

    max_pages = len(pairs)

    # Sérialiser les TextBlock en tuples pour passage inter-process
    def blocks_to_tuples(blocks):
        return [(b.text, b.x0, b.y0, b.x1, b.y1, b.text_b64) for b in blocks]

    tasks = [
        (reference_pdf, candidate_pdf,
         ref_pn, new_pn,
         blocks_to_tuples(ref_idx[ref_pn]),
         blocks_to_tuples(new_idx[new_pn]),
         tolerance, dpi, pixel_tolerance, min_diff_area, exclude_zones,
         include_screenshots, shift_tolerance,
         run_phase1, run_phase2)
        for ref_pn, new_pn in pairs
    ]

    page_results: List[Optional[PageComparisonResult]] = [None] * max_pages
    progress = None if progress_callback else Progress(max_pages, prefix="  Pages")
    completed = 0

    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {executor.submit(_process_page, t): i for i, t in enumerate(tasks)}

        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                page_results[idx] = future.result()
            except Exception as e:
                ref_pn, new_pn = pairs[idx]
                print(f"\n  ⚠ Erreur page REF {ref_pn} : {e}")
                page_results[idx] = PageComparisonResult(
                    ref_page=ref_pn, new_page=new_pn)

            completed += 1
            ref_page_num = pairs[idx][0]

            if progress_callback:
                progress_callback(completed, max_pages, ref_page_num)
            else:
                progress.update(completed, suffix=f"REF p.{ref_page_num}")

            # Arrêt propre si demandé
            if stop_event and stop_event.is_set():
                print("\n  Arrêt demandé — annulation des pages restantes…")
                executor.shutdown(wait=False, cancel_futures=True)
                break

    # Ajouter l'avertissement de pages manquantes sur le premier résultat
    if page_count_warning and page_results and page_results[0]:
        page_results[0].page_count_warning = page_count_warning

    # Filtrer les None (pages annulées) et trier par ordre de page
    results = [r for r in page_results if r is not None]
    results.sort(key=lambda r: r.ref_page)

    # Stocker timings (mode profiling minimal pour compatibilité)
    compare_pdfs._last_n_pages = len(results)
    compare_pdfs._last_timings = {}

    return results


# ─────────────────────────────────────────────
#  GÉNÉRATION DU RAPPORT HTML
# ─────────────────────────────────────────────
CSS = """
body{font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;background:#f4f6f9;color:#333;margin:0;padding:20px}
.container{max-width:1200px;margin:0 auto;background:white;padding:30px;border-radius:8px;box-shadow:0 4px 6px rgba(0,0,0,.1)}
h1{color:#2c3e50;border-bottom:2px solid #ecf0f1;padding-bottom:10px}
h2{color:#2980b9;margin-top:30px}
.meta{color:#555;font-size:14px;margin-bottom:20px}
.warning-box{background:#fff3cd;border:1px solid #ffc107;color:#856404;padding:12px 16px;border-radius:6px;margin-bottom:20px;font-weight:500}
.summary-box{display:flex;gap:15px;margin-bottom:20px;background:#eef2f7;padding:15px;border-radius:6px;flex-wrap:wrap}
.summary-item{flex:1;min-width:120px;text-align:center;padding:10px;border-radius:4px;background:white;box-shadow:0 2px 4px rgba(0,0,0,.05)}
.summary-item.moved{border-top:4px solid #f39c12}
.summary-item.missing{border-top:4px solid #e74c3c}
.summary-item.added{border-top:4px solid #2ecc71}
.summary-item.visual{border-top:4px solid #9b59b6}
.summary-number{font-size:24px;font-weight:bold;margin-bottom:5px}
.filter-box{background:#ebf8ff;border:1px solid #bee3f8;color:#2b6cb0;padding:15px;border-radius:6px;margin-bottom:25px}
.filter-box input{padding:8px;width:250px;border:1px solid #cbd5e0;border-radius:4px;margin-right:10px}
.filter-box button{padding:8px 15px;background:#3182ce;color:white;border:none;border-radius:4px;cursor:pointer}
.tag-list{margin-top:10px;display:flex;gap:5px;flex-wrap:wrap}
.tag{background:#cbd5e0;padding:4px 8px;border-radius:12px;font-size:12px;display:inline-flex;align-items:center;gap:5px}
.tag span{cursor:pointer;font-weight:bold;color:#e53e3e}
.collapse-bar{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.collapse-bar button{padding:5px 12px;background:#edf2f7;border:1px solid #cbd5e0;border-radius:4px;cursor:pointer;font-size:12px;color:#4a5568}
.page-section,.common-section{background:#fafafa;border:1px solid #e2e8f0;border-radius:6px;margin-bottom:12px}
.common-section{background:#fffaf0;border-color:#feebc8}
.page-header{font-weight:bold;font-size:15px;color:#4a5568;padding:10px 15px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;user-select:none}
.page-header:hover{background:#f0f4f8;border-radius:6px 6px 0 0}
.page-header .chevron{transition:transform .2s;font-size:12px}
.page-body{padding:0 15px 15px 15px}
.badge{display:inline-block;padding:3px 8px;font-size:11px;font-weight:bold;border-radius:3px;color:white;margin-right:6px}
.badge.moved{background:#f39c12}.badge.missing{background:#e74c3c}
.badge.added{background:#2ecc71}.badge.visual{background:#9b59b6}.badge.occ{background:#555}
.anomaly-item{background:white;padding:15px;border-radius:4px;margin-bottom:12px;border-left:4px solid #ccc;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.anomaly-item.moved{border-left-color:#f39c12}.anomaly-item.missing{border-left-color:#e74c3c}
.anomaly-item.added{border-left-color:#2ecc71}.anomaly-item.visual{border-left-color:#9b59b6}
.anomaly-layout{display:flex;gap:15px;align-items:flex-start;justify-content:space-between}
.anomaly-info{flex:1}
.anomaly-screenshot{border:1px solid #cbd5e0;border-radius:4px;background:#fff;padding:2px;max-width:250px;box-shadow:0 2px 4px rgba(0,0,0,.1)}
.anomaly-screenshot img{display:block;max-width:100%;height:auto;max-height:200px;object-fit:contain}
.img-label{font-size:10px;font-weight:bold;text-align:center;color:#666;padding:2px 0}
.text-content{font-family:monospace;background:#f8fafc;padding:5px;border-radius:3px;display:inline-block;margin-top:5px;color:#1e293b;word-break:break-all}
.details{font-size:13px;color:#64748b;margin-top:8px;display:flex;justify-content:space-between;align-items:center}
.btn-hide-zone{background:#edf2f7;border:1px solid #cbd5e0;padding:2px 6px;font-size:11px;border-radius:3px;cursor:pointer;color:#4a5568}
.hidden-anomaly{display:none!important}
"""

JS = """
let activeFilters=[], activeZoneFilters=[];
function addFilter(){
  const v=document.getElementById('filterInput').value.trim();
  if(v&&!activeFilters.includes(v.toLowerCase())){activeFilters.push(v.toLowerCase());document.getElementById('filterInput').value='';renderTags();applyFilters();}
}
function addZoneFilter(x0,y0,x1,y1){
  if(!activeZoneFilters.some(z=>Math.abs(z.x0-x0)<1&&Math.abs(z.y0-y0)<1)){
    activeZoneFilters.push({x0,y0,x1,y1});renderTags();applyFilters();
  }
}
function removeFilter(i){activeFilters.splice(i,1);renderTags();applyFilters();}
function removeZoneFilter(i){activeZoneFilters.splice(i,1);renderTags();applyFilters();}
function renderTags(){
  document.getElementById('tagList').innerHTML=
    activeFilters.map((f,i)=>`<span class="tag">Texte: "${f}" <span onclick="removeFilter(${i})">&times;</span></span>`).join('')+
    activeZoneFilters.map((z,i)=>`<span class="tag">Zone:[${z.x0},${z.y0}] <span onclick="removeZoneFilter(${i})">&times;</span></span>`).join('');
}
function applyFilters(){
  const items=document.querySelectorAll('.anomaly-item');
  let counts={missing:0,added:0,moved:0,visual:0};
  items.forEach(item=>{
    const text=item.getAttribute('data-text').toLowerCase();
    const type=item.getAttribute('data-type');
    const x0=parseFloat(item.getAttribute('data-x0'));
    const y0=parseFloat(item.getAttribute('data-y0'));
    let excluded=activeFilters.some(f=>text.includes(f));
    if(!excluded)excluded=activeZoneFilters.some(z=>Math.abs(z.x0-x0)<=2&&Math.abs(z.y0-y0)<=2);
    item.classList.toggle('hidden-anomaly',excluded);
    if(!excluded&&counts[type]!==undefined)counts[type]++;
  });
  ['moved','missing','added','visual'].forEach(k=>{
    const el=document.getElementById('count-'+k);if(el)el.innerText=counts[k];
  });
  document.querySelectorAll('.page-section').forEach(s=>{
    s.style.display=s.querySelectorAll('.anomaly-item:not(.hidden-anomaly)').length===0?'none':'block';
  });
  const cs=document.getElementById('common-section');
  if(cs)cs.style.display=cs.querySelectorAll('.anomaly-item:not(.hidden-anomaly)').length===0?'none':'block';
  const total=Object.values(counts).reduce((a,b)=>a+b,0);
  const nm=document.getElementById('no-diff-msg');if(nm)nm.style.display=total===0?'block':'none';
}
function toggleSection(id){
  const body=document.getElementById('body-'+id);
  const chev=document.getElementById('chev-'+id);
  const open=body.getAttribute('data-open')!=='false';
  body.style.display=open?'none':'block';
  body.setAttribute('data-open', open?'false':'true');
  chev.style.transform=open?'rotate(-90deg)':'rotate(0deg)';
}
function collapseAll(){
  document.querySelectorAll('.page-body').forEach(b=>{b.style.display='none';b.setAttribute('data-open','false');});
  document.querySelectorAll('.chevron').forEach(c=>c.style.transform='rotate(-90deg)');
}
function expandAll(){
  document.querySelectorAll('.page-body').forEach(b=>{b.style.display='block';b.setAttribute('data-open','true');});
  document.querySelectorAll('.chevron').forEach(c=>c.style.transform='rotate(0deg)');
}
window.onload=function(){
  // Initialiser explicitement toutes les page-body comme ouvertes
  document.querySelectorAll('.page-body').forEach(b=>{b.style.display='block';b.setAttribute('data-open','true');});
  applyFilters();
};
"""


def _anomaly_html(atype, item, is_common, ref_doc, new_doc, page_num, include_screenshots):
    img_b64 = ""
    if include_screenshots:
        doc_use = ref_doc if atype in ("missing", "moved") else new_doc
        if atype == "moved":
            x0,y0,x1,y1 = item["ref_x"],item["ref_y"],item["ref_x"]+200,item["ref_y"]+20
        else:
            x0,y0,x1,y1 = item["x0"],item["y0"],item["x1"],item["y1"]
        img_b64 = rect_to_b64(doc_use, page_num, x0, y0, x1, y1)
    img_tag = f'<div class="anomaly-screenshot"><img src="data:image/png;base64,{img_b64}" loading="lazy"/></div>' if img_b64 else ""
    occ = item.get('_occ', 0)
    sfx = f" — COMMUN ({occ} pages)" if is_common and occ > 1 else " (COMMUN)" if is_common else ""
    if atype == "missing":
        badge,pos = f"MANQUANT{sfx}", f"Position: ({item['x0']:.1f}, {item['y0']:.1f})"
        click = f"addZoneFilter({item['x0']:.1f},{item['y0']:.1f},{item['x1']:.1f},{item['y1']:.1f})"
        dat = f'data-x0="{item["x0"]:.1f}" data-y0="{item["y0"]:.1f}" data-x1="{item["x1"]:.1f}" data-y1="{item["y1"]:.1f}"'
    elif atype == "added":
        badge,pos = f"NOUVEAU{sfx}", f"Position: ({item['x0']:.1f}, {item['y0']:.1f})"
        click = f"addZoneFilter({item['x0']:.1f},{item['y0']:.1f},{item['x1']:.1f},{item['y1']:.1f})"
        dat = f'data-x0="{item["x0"]:.1f}" data-y0="{item["y0"]:.1f}" data-x1="{item["x1"]:.1f}" data-y1="{item["y1"]:.1f}"'
    else:
        badge = f"DÉPLACÉ{sfx}"
        dx_px = item.get('dx_px', round(item['dx'] * 100 / 72, 1))
        dy_px = item.get('dy_px', round(item['dy'] * 100 / 72, 1))
        pos = f"REF=({item['ref_x']:.1f},{item['ref_y']:.1f}) → NEW=({item['new_x']:.1f},{item['new_y']:.1f}) | Δx={dx_px}px Δy={dy_px}px"
        click = f"addZoneFilter({item['ref_x']:.1f},{item['ref_y']:.1f},{item['new_x']:.1f},{item['new_y']:.1f})"
        dat = f'data-x0="{item["ref_x"]:.1f}" data-y0="{item["ref_y"]:.1f}" data-x1="{item["new_x"]:.1f}" data-y1="{item["new_y"]:.1f}"'
    return f"""<div class="anomaly-item {atype}" data-type="{atype}" data-text="{item['text'].replace('"','&quot;')}" {dat}>
  <div class="anomaly-layout"><div class="anomaly-info">
    <span class="badge {atype}">{badge}</span><br>
    <span class="text-content">{item['text']}</span>
    <div class="details"><div>{pos}</div>
    <button class="btn-hide-zone" onclick="{click}">🚫 Filtrer</button></div>
  </div>{img_tag}</div></div>"""


def _visual_diff_html(region):
    ref_tag = f'<div class="anomaly-screenshot"><div class="img-label">REF</div><img src="data:image/png;base64,{region.ref_img_b64}" loading="lazy"/></div>' if region.ref_img_b64 else ""
    new_tag = f'<div class="anomaly-screenshot"><div class="img-label">NEW</div><img src="data:image/png;base64,{region.new_img_b64}" loading="lazy"/></div>' if region.new_img_b64 else ""
    return f"""<div class="anomaly-item visual" data-type="visual" data-text="[DIFF VISUELLE]"
  data-x0="{region.x0:.1f}" data-y0="{region.y0:.1f}" data-x1="{region.x1:.1f}" data-y1="{region.y1:.1f}">
  <div class="anomaly-layout"><div class="anomaly-info">
    <span class="badge visual">DIFF VISUELLE</span><br>
    <span class="text-content">[Élément graphique / image / code-barres]</span>
    <div class="details">
      <div>Zone: ({region.x0:.1f},{region.y0:.1f}) → ({region.x1:.1f},{region.y1:.1f}) | Score: {region.diff_score:.1f}</div>
      <button class="btn-hide-zone" onclick="addZoneFilter({region.x0:.1f},{region.y0:.1f},{region.x1:.1f},{region.y1:.1f})">🚫 Filtrer</button>
    </div></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">{ref_tag}{new_tag}</div>
  </div></div>"""


def _timing_html(timing_info: dict) -> str:
    """Génère les lignes de temps pour le bloc méta du rapport."""
    if not timing_info:
        return ""
    def _f(s, placeholder): return fmt_eta(s) if s is not None else placeholder
    analyse = timing_info.get('analyse', 0)
    return (
        f'<br><strong>Temps d\'analyse :</strong> {_f(analyse, "—")}'
        f' &nbsp;|&nbsp; <strong>Génération rapport :</strong> __RAPPORT_TIME__'
        f' &nbsp;|&nbsp; <strong>Total :</strong> __TOTAL_TIME__'
    )


def generate_html_report(
    results: List[PageComparisonResult],
    output_path: str,
    ref_info: dict,
    new_info: dict,
    include_screenshots: bool = True,
    timing_info: dict = None,
):
    output_path = ensure_html_extension(output_path)
    ref_doc = fitz.open(ref_info["path"]) if include_screenshots else None
    new_doc = fitz.open(new_info["path"]) if include_screenshots else None

    # ── Détection anomalies communes ─────────
    counter = Counter()
    for r in results:
        for m in r.missing: counter[serialize_anomaly("missing", m)] += 1
        for a in r.added:   counter[serialize_anomaly("added",   a)] += 1
        for md in r.moved:  counter[serialize_anomaly("moved",  md)] += 1

    common_keys = {k for k, c in counter.items() if c > 1}
    common_missing, common_added, common_moved = [], [], []
    common_seen, cleaned = set(), []

    for r in results:
        cr = PageComparisonResult(ref_page=r.ref_page, new_page=r.new_page,
                                  image_diffs=r.image_diffs,
                                  page_count_warning=r.page_count_warning)
        for m in r.missing:
            k = serialize_anomaly("missing", m)
            if k in common_keys:
                if k not in common_seen:
                    common_seen.add(k); m["_page_num"]=r.ref_page; m["_occ"]=counter[k]; common_missing.append(m)
            else: cr.missing.append(m)
        for a in r.added:
            k = serialize_anomaly("added", a)
            if k in common_keys:
                if k not in common_seen:
                    common_seen.add(k); a["_page_num"]=r.new_page; a["_occ"]=counter[k]; common_added.append(a)
            else: cr.added.append(a)
        for md in r.moved:
            k = serialize_anomaly("moved", md)
            if k in common_keys:
                if k not in common_seen:
                    common_seen.add(k); md["_page_num"]=r.ref_page; md["_occ"]=counter[k]; common_moved.append(md)
            else: cr.moved.append(md)
        cleaned.append(cr)

    total_moved   = sum(len(r.moved)        for r in cleaned) + len(common_moved)
    total_missing = sum(len(r.missing)      for r in cleaned) + len(common_missing)
    total_added   = sum(len(r.added)        for r in cleaned) + len(common_added)
    total_visual  = sum(len(r.image_diffs)  for r in cleaned)

    # ── Avertissement pages ───────────────────
    warning_html = ""
    for r in results:
        if r.page_count_warning:
            warning_html = f'<div class="warning-box">⚠ {r.page_count_warning}</div>'
            break

    # ── Sections communes ────────────────────
    common_html = ""
    if common_missing or common_added or common_moved:
        common_html = '<div class="common-section" id="common-section">\n'
        common_html += ('<div class="page-header" style="color:#dd6b20;" '
                        'onclick="toggleSection(\'common\')">'
                        '★ DIFFÉRENCES COMMUNES (présentes sur plusieurs pages) '
                        '<span class="chevron" id="chev-common">▼</span></div>\n')
        common_html += '<div class="page-body" id="body-common">\n'
        for m in common_missing:
            common_html += _anomaly_html("missing", m, True, ref_doc, new_doc, m.get("_page_num",1), include_screenshots)
        for a in common_added:
            common_html += _anomaly_html("added",   a, True, ref_doc, new_doc, a.get("_page_num",1), include_screenshots)
        for md in common_moved:
            common_html += _anomaly_html("moved",  md, True, ref_doc, new_doc, md.get("_page_num",1), include_screenshots)
        common_html += '</div></div>\n'

    # ── Détails par page (sections repliables) ─
    pages_html = "<h2>Détails par Page</h2>\n"
    pages_html += '<div class="collapse-bar"><button onclick="expandAll()">⊞ Tout déplier</button><button onclick="collapseAll()">⊟ Tout replier</button></div>\n'
    pages_html += '<div id="pages-container">\n'

    for r in cleaned:
        if not r.has_anomalies():
            continue
        n_issues = len(r.moved) + len(r.missing) + len(r.added) + len(r.image_diffs)
        uid = f"p{r.ref_page}"
        pages_html += f'<div class="page-section" data-page="{r.ref_page}">\n'
        pages_html += (f'<div class="page-header" onclick="toggleSection(\'{uid}\')">'
                       f'REF page {r.ref_page} ↔ CANDIDAT page {r.new_page}'
                       f'<span style="color:#999;font-size:12px;font-weight:normal">'
                       f'{n_issues} anomalie(s)</span>'
                       f'<span class="chevron" id="chev-{uid}">▼</span></div>\n')
        pages_html += f'<div class="page-body" id="body-{uid}">\n'
        for m  in r.missing:     pages_html += _anomaly_html("missing", m,  False, ref_doc, new_doc, r.ref_page, include_screenshots)
        for a  in r.added:       pages_html += _anomaly_html("added",   a,  False, ref_doc, new_doc, r.new_page, include_screenshots)
        for md in r.moved:       pages_html += _anomaly_html("moved",   md, False, ref_doc, new_doc, r.ref_page, include_screenshots)
        for vd in r.image_diffs: pages_html += _visual_diff_html(vd)
        pages_html += '</div></div>\n'
    pages_html += '</div>'

    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8"><title>Rapport Comparaison PDF</title>
<style>{CSS}</style></head><body><div class="container">
<h1>Rapport de Comparaison PDF</h1>
<div class="meta"><strong>Référence :</strong> {ref_info['path']}<br><strong>Candidat :</strong> {new_info['path']}<br><strong>Pages comparées :</strong> {len(results)} page(s){_timing_html(timing_info)}</div>
{warning_html}
<div class="filter-box"><strong>Filtrage à la volée :</strong><br><br>
  <input type="text" id="filterInput" placeholder="Ex: Version 2.0...">
  <button onclick="addFilter()">Ajouter filtre texte</button>
  <div class="tag-list" id="tagList"></div>
</div>
<div class="summary-box">
  <div class="summary-item moved"><div class="summary-number" id="count-moved" style="color:#f39c12">{total_moved}</div><div>Blocs Déplacés</div></div>
  <div class="summary-item missing"><div class="summary-number" id="count-missing" style="color:#e74c3c">{total_missing}</div><div>Blocs Manquants</div></div>
  <div class="summary-item added"><div class="summary-number" id="count-added" style="color:#2ecc71">{total_added}</div><div>Nouveaux Blocs</div></div>
  <div class="summary-item visual"><div class="summary-number" id="count-visual" style="color:#9b59b6">{total_visual}</div><div>Diffs Visuelles</div></div>
</div>
<p id="no-diff-msg" style="color:#2ecc71;font-weight:bold;display:none">Aucune différence avec les filtres actuels.</p>
{common_html}{pages_html}
</div><script>{JS}</script></body></html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Rapport généré : {output_path}")

    if ref_doc: ref_doc.close()
    if new_doc: new_doc.close()


# ─────────────────────────────────────────────
#  POINT D'ENTRÉE CLI
# ─────────────────────────────────────────────
def _patch_timings(html_path: str, rapport_s: float, total_s: float):
    """Remplace les placeholders de timing dans le HTML après génération."""
    try:
        with open(html_path, encoding='utf-8') as f:
            html = f.read()
        html = html.replace('__RAPPORT_TIME__', fmt_eta(rapport_s))
        html = html.replace('__TOTAL_TIME__',   fmt_eta(total_s))
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html)
    except Exception:
        pass


def parse_args():
    parser = argparse.ArgumentParser(
        description="Comparateur de PDF 100%% local — Phase 1 (texte) + Phase 2 (image diff)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python pdf_compare.py ref.pdf candidat.pdf
  python pdf_compare.py ref.pdf candidat.pdf -o rapport.html --dpi 150
  python pdf_compare.py ref.pdf candidat.pdf --skip-ref-start 1 --skip-new-start 4
  python pdf_compare.py ref.pdf candidat.pdf --no-screenshots --workers 4
  python pdf_compare.py ref.pdf candidat.pdf --profile 10
        """)
    parser.add_argument("reference")
    parser.add_argument("candidat")
    parser.add_argument("-o","--output", default="rapport_comparaison.html")
    parser.add_argument("--tolerance",       type=float, default=2)
    parser.add_argument("--dpi",             type=int,   default=RENDER_DPI)
    parser.add_argument("--pixel-tolerance", type=int,   default=PIXEL_TOLERANCE)
    parser.add_argument("--min-diff-area",   type=int,   default=MIN_DIFF_AREA_PX)
    parser.add_argument("--skip-ref-start",  type=int,   default=0)
    parser.add_argument("--skip-ref-end",    type=int,   default=0)
    parser.add_argument("--skip-new-start",  type=int,   default=0)
    parser.add_argument("--skip-new-end",    type=int,   default=0)
    parser.add_argument("--shift-tolerance", type=int,   default=3,
                        help="Tolérance de décalage de rendu en pixels (défaut : 3). "
                             "Teste ±N px avant de signaler une diff visuelle.")
    parser.add_argument("--elastic",         action="store_true",
                        help="Active l'appariement élastique (DTW) pour gérer les pages "
                             "qui débordent dans les deux sens (REF→CAND ou CAND→REF).")
    parser.add_argument("--elastic-band",    type=int,   default=50,
                        help="Rayon de la bande diagonale DTW (défaut : 50). "
                             "Augmenter si le décalage entre PDF est très grand.")
    parser.add_argument("--no-text",         action="store_true",
                        help="Désactive la Phase 1 (comparaison texte).")
    parser.add_argument("--no-image",        action="store_true",
                        help="Désactive la Phase 2 (comparaison image).")
    parser.add_argument("--no-screenshots",  action="store_true")
    parser.add_argument("--workers",         type=int,   default=MAX_WORKERS,
                        help=f"Processus parallèles (défaut : {MAX_WORKERS})")
    parser.add_argument("--profile",         type=int,   default=0, metavar="N",
                        help="Profiling : traite N pages et affiche les temps")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    output = ensure_html_extension(args.output)
    include_ss = not args.no_screenshots
    t0 = time.time()

    print(f"\n=== Comparaison PDF {'(sans screenshots)' if not include_ss else ''} ===")
    print(f"  REF      : {args.reference}")
    print(f"  CANDIDAT : {args.candidat}")
    print(f"  SORTIE   : {output}")
    print(f"  DPI={args.dpi}  tol.texte={args.tolerance}  tol.pixel={args.pixel_tolerance}  workers={args.workers}\n")

    profile_n = args.profile if args.profile > 0 else None
    ref_skip_end = args.skip_ref_end
    new_skip_end = args.skip_new_end
    if profile_n:
        print(f"  [PROFILING] {profile_n} premières pages — pas de rapport généré\n")
        ref_skip_end = max(0, fitz.open(args.reference).page_count - args.skip_ref_start - profile_n)
        new_skip_end = max(0, fitz.open(args.candidat).page_count  - args.skip_new_start - profile_n)

    resultats = compare_pdfs(
        reference_pdf=args.reference, candidate_pdf=args.candidat,
        tolerance=args.tolerance,
        ref_skip_start=args.skip_ref_start, ref_skip_end=ref_skip_end,
        new_skip_start=args.skip_new_start, new_skip_end=new_skip_end,
        dpi=args.dpi, pixel_tolerance=args.pixel_tolerance,
        min_diff_area=args.min_diff_area,
        include_screenshots=include_ss,
        shift_tolerance=args.shift_tolerance,
        elastic_align=args.elastic,
        elastic_band=args.elastic_band,
        run_phase1=not args.no_text,
        run_phase2=not args.no_image,
        max_workers=args.workers,
    )

    total_s = time.time() - t0
    if profile_n:
        n = compare_pdfs._last_n_pages
        print(f"\n{'─'*40}")
        print(f"  PROFILING — {n} page(s) — {total_s:.2f}s total ({total_s/n*1000:.0f}ms/page)")
        print(f"{'─'*40}\n")
    else:
        t_rapport_start = time.time()
        generate_html_report(resultats, output,
                             {"path": args.reference}, {"path": args.candidat},
                             include_screenshots=include_ss,
                             timing_info={
                                 'analyse': total_s,
                                 'rapport': None,   # inconnu à ce stade
                                 'total':   None,
                             })
        t_rapport_s = time.time() - t_rapport_start
        t_total_s   = time.time() - t0
        # Patch rapide des timings dans le fichier HTML déjà écrit
        _patch_timings(output, t_rapport_s, t_total_s)
        print(f"\nTerminé en {fmt_eta(total_s)} — rapport : {output}")
