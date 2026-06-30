"""
docspec.py — Décompose une image / un PDF en une SPÉCIFICATION COMPLÈTE rejouable,
puis la régénère avec une fidélité mesurée (>= 99 %, ou exacte).  (v2)

100 % local — fitz + Pillow + numpy (+ pytesseract/tesseract optionnel pour l'OCR).

ARCHITECTURE
    1. Ingestion        : tout format (PDF page à page, JPG, PNG, …) -> raster RGB.
    2. Couche BASE      : codec image compact (WebP). Couvre TOUT, photos comprises.
                          - mode --lossless : WebP sans perte -> base exacte.
                          - sinon           : WebP qualité réglable.
    3. Couche RÉSIDU    : correction (original - base), quantifiée par un pas `step`.
                          GARANTIT la fidélité. Ajoutée seulement si la base seule
                          n'atteint pas la cible SSIM (sinon : pas de résidu).
    4. Couche STRUCTURE : extraction descriptive (= la "spécification") :
                          fond, palette, RÉGIONS VECTORIELLES (rectangles d'aplats),
                          TEXTE (OCR : mots, position, taille de police, couleur).
    5. Conteneur        : .imgspec (zip) = manifest.json + assets par page.
    6. Régénération     : .imgspec -> image(s) identiques (ou >= cible SSIM).

Le manifest.json EST la spécification extraite, lisible.

Dépendances : pip install pymupdf Pillow numpy   (OCR : pip install pytesseract + Tesseract)

Usage :
    py docspec.py roundtrip image.jpg                 (encode + decode + rapport fidélité)
    py docspec.py roundtrip doc.pdf --lossless
    py docspec.py encode  image.png -o sortie.imgspec --quality 80
    py docspec.py decode  sortie.imgspec -o dossier_sortie
    py docspec.py svg     sortie.imgspec              (SVG hybride : image fidèle + texte éditable)
    py docspec.py render  sortie.imgspec              (rendu depuis la structure seule = voie éditable)
"""

import argparse
import io
import json
import os
import sys
import zipfile

import base64
import html

import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    import fitz  # PyMuPDF — pour les PDF
except ImportError:
    fitz = None

FORMAT_VERSION  = 2
DEFAULT_DPI     = 150
DEFAULT_QUALITY = 80      # qualité WebP de la couche base (mode perceptuel)
DEFAULT_COLORS  = 64      # couleurs de la palette décrite
DEFAULT_TARGET  = 0.99    # cible SSIM
STEP_LADDER     = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32]
REGION_BLOCK    = 16      # taille de bloc pour la détection de régions vectorielles
OCR_MIN_CONF    = 40      # confiance OCR minimale (%)


# ─────────────────────────────────────────────
#  1. INGESTION
# ─────────────────────────────────────────────
def ingest(path: str, dpi: int = DEFAULT_DPI):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        if fitz is None:
            raise SystemExit("PyMuPDF requis pour les PDF : pip install pymupdf")
        doc = fitz.open(path)
        pages, scale = [], dpi / 72.0
        try:
            for page in doc:
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB)
                arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).copy()
                pages.append(arr)
        finally:
            doc.close()
        return pages, {"source_kind": "pdf", "dpi": dpi}

    img = Image.open(path).convert("RGB")
    info_dpi = img.info.get("dpi", (72, 72))
    return [np.asarray(img, dtype=np.uint8).copy()], {
        "source_kind": "image", "dpi": int(info_dpi[0]) if info_dpi else 72}


# ─────────────────────────────────────────────
#  2. COUCHE BASE (WebP)  +  3. COUCHE RÉSIDU
# ─────────────────────────────────────────────
def webp_base(rgb: np.ndarray, lossless: bool, quality: int):
    buf = io.BytesIO()
    if lossless:
        Image.fromarray(rgb, "RGB").save(buf, format="WEBP", lossless=True, quality=100, method=6)
    else:
        Image.fromarray(rgb, "RGB").save(buf, format="WEBP", quality=quality, method=6)
    data = buf.getvalue()
    base_rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.uint8)
    return data, base_rgb


def base_from_webp(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.uint8)


def quantize_residual(rgb, base_rgb, step):
    return np.round((rgb.astype(np.int16) - base_rgb.astype(np.int16)) / step).astype(np.int16)


def apply_residual(base_rgb, q, step):
    return np.clip(base_rgb.astype(np.int16) + q.astype(np.int16) * step, 0, 255).astype(np.uint8)


def residual_bytes(q, step):
    buf = io.BytesIO()
    np.savez_compressed(buf, q=q.astype(np.int16), step=np.int32(step))
    return buf.getvalue()


def residual_from_bytes(data):
    with np.load(io.BytesIO(data)) as npz:
        return npz["q"], int(npz["step"])


# ─────────────────────────────────────────────
#  4. COUCHE STRUCTURE — extraction descriptive
# ─────────────────────────────────────────────
def extract_palette(rgb, n_colors):
    pal = Image.fromarray(rgb, "RGB").quantize(colors=n_colors, method=Image.MEDIANCUT, dither=Image.NONE)
    raw = pal.getpalette() or []
    counts = np.bincount(np.asarray(pal).ravel())
    used = np.argsort(counts)[::-1]
    used = [int(i) for i in used if counts[i] > 0]
    palette = [raw[i * 3:i * 3 + 3] for i in used if i * 3 + 2 < len(raw)]
    background = palette[0] if palette else [255, 255, 255]
    return [[int(c) for c in col] for col in palette], [int(c) for c in background]


def extract_vector_regions(rgb, block=REGION_BLOCK, tol=6, min_area_blocks=4):
    """Détecte les aplats rectangulaires (fonds, blocs de couleur, cases de tableau)."""
    h, w = rgb.shape[:2]
    bh, bw = h // block, w // block
    if bh == 0 or bw == 0:
        return []
    color = np.zeros((bh, bw, 3), dtype=np.int16)
    uniform = np.zeros((bh, bw), dtype=bool)
    for by in range(bh):
        ys = by * block
        for bx in range(bw):
            cell = rgb[ys:ys + block, bx * block:(bx + 1) * block].reshape(-1, 3)
            mn, mx = cell.min(0), cell.max(0)
            if int((mx - mn).max()) <= tol:
                uniform[by, bx] = True
                color[by, bx] = cell[0]
    visited = np.zeros((bh, bw), dtype=bool)
    regions = []
    for by in range(bh):
        for bx in range(bw):
            if not uniform[by, bx] or visited[by, bx]:
                continue
            c = color[by, bx]
            x2 = bx
            while x2 + 1 < bw and uniform[by, x2 + 1] and not visited[by, x2 + 1] and (color[by, x2 + 1] == c).all():
                x2 += 1
            y2 = by
            while y2 + 1 < bh and all(
                    uniform[y2 + 1, xx] and not visited[y2 + 1, xx] and (color[y2 + 1, xx] == c).all()
                    for xx in range(bx, x2 + 1)):
                y2 += 1
            visited[by:y2 + 1, bx:x2 + 1] = True
            nb = (x2 - bx + 1) * (y2 - by + 1)
            if nb >= min_area_blocks:
                regions.append({"x": bx * block, "y": by * block,
                                "w": (x2 - bx + 1) * block, "h": (y2 - by + 1) * block,
                                "color": [int(v) for v in c]})
    return regions


def _glyph_color(patch):
    """Couleur des glyphes : moyenne des pixels les plus contrastés vs le fond
    (et non la médiane, qui renvoie la couleur du fond)."""
    px = patch.reshape(-1, 3)
    if px.size == 0:
        return [0, 0, 0]
    lum = px.astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    bg = float(np.median(lum))
    # pixels les plus éloignés du fond en luminance = traits du texte
    far = np.abs(lum - bg)
    thr = np.percentile(far, 75)
    sel = px[far >= thr]
    src = sel if sel.size else px
    return [int(c) for c in src.mean(0)]


def extract_text(rgb, dpi):
    """OCR optionnel (Tesseract). Mots + position + taille de police + couleur."""
    try:
        import pytesseract
        data = pytesseract.image_to_data(Image.fromarray(rgb, "RGB"),
                                          output_type=pytesseract.Output.DICT)
    except Exception as e:
        msg = (str(e).splitlines() or ["pytesseract/tesseract indisponible"])[0]
        return {"available": False, "reason": msg}
    words = []
    for i in range(len(data["text"])):
        t = data["text"][i].strip()
        if not t:
            continue
        try:
            conf = float(data["conf"][i])
        except ValueError:
            conf = -1.0
        if conf < OCR_MIN_CONF:
            continue
        x, y, w, h = (int(data[k][i]) for k in ("left", "top", "width", "height"))
        col = _glyph_color(rgb[max(0, y):y + h, max(0, x):x + w])
        words.append({"text": t, "x": x, "y": y, "w": w, "h": h,
                      "font_pt": round(h * 72.0 / max(dpi, 1), 1),
                      "color": col, "conf": round(conf, 1)})
    return {"available": True, "engine": "tesseract", "n_words": len(words), "words": words}


# ─────────────────────────────────────────────
#  MÉTRIQUES (numpy pur)
# ─────────────────────────────────────────────
def psnr(a, b):
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return float("inf") if mse == 0 else float(10.0 * np.log10(255.0 ** 2 / mse))


def _box_blur(img, k):
    pad = k // 2
    p = np.pad(img, ((pad, pad), (pad, pad)), mode="edge")
    out = np.zeros_like(img, dtype=np.float64)
    h, w = img.shape
    for dy in range(k):
        for dx in range(k):
            out += p[dy:dy + h, dx:dx + w]
    return out / (k * k)


def ssim(a, b, k=7):
    xf = a.astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    yf = b.astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mu_x, mu_y = _box_blur(xf, k), _box_blur(yf, k)
    mu_x2, mu_y2, mu_xy = mu_x * mu_x, mu_y * mu_y, mu_x * mu_y
    sx = _box_blur(xf * xf, k) - mu_x2
    sy = _box_blur(yf * yf, k) - mu_y2
    sxy = _box_blur(xf * yf, k) - mu_xy
    smap = ((2 * mu_xy + C1) * (2 * sxy + C2)) / ((mu_x2 + mu_y2 + C1) * (sx + sy + C2))
    return float(np.clip(smap, -1, 1).mean())


# ─────────────────────────────────────────────
#  ENCODAGE
# ─────────────────────────────────────────────
def encode_page(rgb, lossless, quality, target_ssim, n_colors):
    base_data, base_rgb = webp_base(rgb, lossless, quality)

    residual = None  # (q, step) ou None
    if lossless:
        if not np.array_equal(base_rgb, rgb):
            residual = (quantize_residual(rgb, base_rgb, 1), 1)
    else:
        if ssim(rgb, base_rgb) < target_ssim:
            chosen = 1
            for step in STEP_LADDER:
                q = quantize_residual(rgb, base_rgb, step)
                if ssim(rgb, apply_residual(base_rgb, q, step)) >= target_ssim:
                    chosen = step
                else:
                    break
            residual = (quantize_residual(rgb, base_rgb, chosen), chosen)

    recon = base_rgb if residual is None else apply_residual(base_rgb, residual[0], residual[1])

    palette, background = extract_palette(rgb, n_colors)
    regions = extract_vector_regions(rgb)
    text = extract_text(rgb, dpi_for_text)

    h, w = rgb.shape[:2]
    page = {
        "width": int(w), "height": int(h),
        "base_layer": {"type": "webp", "lossless": bool(lossless),
                       "quality": None if lossless else int(quality), "asset": None},
        "residual_layer": (None if residual is None else
                           {"type": "raster_residual", "step": int(residual[1]),
                            "nonzero_fraction": round(float(np.count_nonzero(residual[0]) / residual[0].size), 4),
                            "asset": None}),
        "structure": {
            "background_color": background,
            "palette": palette,
            "vector_regions": regions,
            "n_vector_regions": len(regions),
            "text": text,
        },
        "fidelity": {"ssim": round(ssim(rgb, recon), 5),
                     "psnr_db": (None if psnr(rgb, recon) == float("inf") else round(psnr(rgb, recon), 2)),
                     "exact": bool(psnr(rgb, recon) == float("inf"))},
    }
    return base_data, (residual[0] if residual else None), residual[1] if residual else None, page


# variable globale simple pour passer le dpi à l'OCR (évite de tout re-câbler)
dpi_for_text = DEFAULT_DPI


def encode(path, out_path, lossless=False, quality=DEFAULT_QUALITY,
           target_ssim=DEFAULT_TARGET, n_colors=DEFAULT_COLORS, dpi=DEFAULT_DPI):
    global dpi_for_text
    dpi_for_text = dpi
    pages, meta = ingest(path, dpi=dpi)
    manifest = {
        "format_version": FORMAT_VERSION,
        "source": os.path.basename(path),
        "global": {"n_pages": len(pages), "colorspace": "sRGB",
                   "dpi": meta.get("dpi"), "source_kind": meta.get("source_kind")},
        "pages": [],
    }
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, rgb in enumerate(pages):
            base_data, q, step, page = encode_page(rgb, lossless, quality, target_ssim, n_colors)
            base_name = f"page_{i:03d}_base.webp"
            page["index"] = i
            page["base_layer"]["asset"] = base_name
            zf.writestr(base_name, base_data)
            if q is not None:
                res_name = f"page_{i:03d}_residual.npz"
                page["residual_layer"]["asset"] = res_name
                zf.writestr(res_name, residual_bytes(q, step))
            manifest["pages"].append(page)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest


# ─────────────────────────────────────────────
#  DÉCODAGE
# ─────────────────────────────────────────────
def _recon_page(zf, page):
    """Reconstruit le raster fidèle d'une page (base + résidu)."""
    recon = base_from_webp(zf.read(page["base_layer"]["asset"]))
    res = page.get("residual_layer")
    if res and res.get("asset"):
        q, step = residual_from_bytes(zf.read(res["asset"]))
        recon = apply_residual(recon, q, step)
    return recon


def decode(spec_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    out_files = []
    with zipfile.ZipFile(spec_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        stem = os.path.splitext(os.path.basename(manifest.get("source", "image")))[0]
        for page in manifest["pages"]:
            recon = _recon_page(zf, page)
            out_path = os.path.join(out_dir, f"{stem}_regen_p{page['index']:03d}.png")
            Image.fromarray(recon, "RGB").save(out_path)
            out_files.append(out_path)
    return out_files


# ─────────────────────────────────────────────
#  PHASE 3 — EXPORT SVG HYBRIDE (image fidèle + couche texte éditable)
# ─────────────────────────────────────────────
def _hex(c):
    return "#{:02x}{:02x}{:02x}".format(*(int(max(0, min(255, v))) for v in c))


def build_svg(recon_rgb, page) -> str:
    """
    SVG hybride : raster fidèle au fond (garantit l'identique) + couche TEXTE
    OCR éditable/sélectionnable par-dessus (invisible, comme un PDF cherchable)
    + couche RÉGIONS VECTORIELLES masquée (éditable). Ouvrable dans tout navigateur.
    """
    h, w = recon_rgb.shape[:2]
    buf = io.BytesIO()
    Image.fromarray(recon_rgb, "RGB").save(buf, format="PNG", optimize=True)
    png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    st = page.get("structure", {})
    # Régions vectorielles (couche éditable, masquée par défaut)
    rects = "".join(
        f'<rect x="{r["x"]}" y="{r["y"]}" width="{r["w"]}" height="{r["h"]}" '
        f'fill="{_hex(r["color"])}"/>'
        for r in st.get("vector_regions", []))
    # Texte OCR : sélectionnable/éditable, superposé et invisible (fill-opacity:0)
    words = ""
    txt = st.get("text", {})
    if txt.get("available"):
        for word_ in txt.get("words", []):
            fs = max(1, int(word_["h"]))
            ty = word_["y"] + int(word_["h"] * 0.8)
            words += (f'<text x="{word_["x"]}" y="{ty}" font-size="{fs}" '
                      f'fill="{_hex(word_["color"])}" fill-opacity="0" '
                      f'font-family="sans-serif">{html.escape(word_["text"])}</text>')

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">\n'
        f'  <image x="0" y="0" width="{w}" height="{h}" '
        f'href="data:image/png;base64,{png_b64}"/>\n'
        f'  <g id="vector_regions" display="none">{rects}</g>\n'
        f'  <g id="text_layer">{words}</g>\n'
        f'</svg>\n')


def export_svg(spec_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    files = []
    with zipfile.ZipFile(spec_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        stem = os.path.splitext(os.path.basename(manifest.get("source", "image")))[0]
        for page in manifest["pages"]:
            recon = _recon_page(zf, page)
            svg = build_svg(recon, page)
            out_path = os.path.join(out_dir, f"{stem}_p{page['index']:03d}.svg")
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(svg)
            files.append(out_path)
    return files


# ─────────────────────────────────────────────
#  PHASE 3 — RENDU DEPUIS LA STRUCTURE SEULE (voie vectorielle/éditable)
# ─────────────────────────────────────────────
def _load_font(px):
    for name in ("DejaVuSans.ttf", "Arial.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, max(1, px))
        except Exception:
            continue
    return ImageFont.load_default()


def render_structure(page) -> np.ndarray:
    """Redessine UNIQUEMENT depuis la structure (fond + régions + texte).
    C'est la régénération 'éditable' approximative (sans base ni résidu)."""
    w, h = page["width"], page["height"]
    st = page.get("structure", {})
    bg = st.get("background_color", [255, 255, 255])
    img = Image.new("RGB", (w, h), tuple(bg))
    draw = ImageDraw.Draw(img)
    for r in st.get("vector_regions", []):
        draw.rectangle([r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"]], fill=tuple(r["color"]))
    txt = st.get("text", {})
    if txt.get("available"):
        for word_ in txt.get("words", []):
            font = _load_font(int(word_["h"]))
            draw.text((word_["x"], word_["y"]), word_["text"],
                      fill=tuple(word_["color"]), font=font)
    return np.asarray(img, dtype=np.uint8)


def render_from_spec(spec_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    results = []
    with zipfile.ZipFile(spec_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        stem = os.path.splitext(os.path.basename(manifest.get("source", "image")))[0]
        for page in manifest["pages"]:
            rendered = render_structure(page)
            faithful = _recon_page(zf, page)
            out_path = os.path.join(out_dir, f"{stem}_structonly_p{page['index']:03d}.png")
            Image.fromarray(rendered, "RGB").save(out_path)
            results.append((out_path, ssim(faithful, rendered)))
    return results


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────
def _human(n):
    for u in ("o", "Ko", "Mo"):
        if n < 1024:
            return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} Go"


def _report_page(p):
    f = p["fidelity"]
    s = p["structure"]
    txt = s["text"]
    nt = txt.get("n_words", 0) if txt.get("available") else "OCR indispo"
    res = p["residual_layer"]
    res_str = "aucun" if res is None else f"step={res['step']}"
    print(f"  page {p['index']}: SSIM={f['ssim']}  PSNR={'inf' if f['exact'] else str(f['psnr_db'])+' dB'}  "
          f"| résidu {res_str} | régions vect.={s['n_vector_regions']} | mots OCR={nt}")


def cmd_encode(args):
    out = args.output or (os.path.splitext(args.input)[0] + ".imgspec")
    m = encode(args.input, out, lossless=args.lossless, quality=args.quality,
               target_ssim=args.target, n_colors=args.colors, dpi=args.dpi)
    print(f"Spécification écrite : {out}  ({_human(os.path.getsize(out))})")
    for p in m["pages"]:
        _report_page(p)


def cmd_decode(args):
    out_dir = args.output or (os.path.splitext(args.input)[0] + "_regen")
    files = decode(args.input, out_dir)
    print(f"{len(files)} page(s) régénérée(s) dans : {out_dir}")
    for f in files:
        print(f"  {f}")


def cmd_roundtrip(args):
    spec = os.path.splitext(args.input)[0] + ".imgspec"
    print(f"== Encodage de {args.input} ==")
    m = encode(args.input, spec, lossless=args.lossless, quality=args.quality,
               target_ssim=args.target, n_colors=args.colors, dpi=args.dpi)
    print(f"  Source : {_human(os.path.getsize(args.input))}   "
          f"Spécification : {_human(os.path.getsize(spec))}   "
          f"Mode : {'LOSSLESS' if args.lossless else f'perceptuel (cible {args.target})'}")
    for p in m["pages"]:
        _report_page(p)
    out_dir = os.path.splitext(args.input)[0] + "_regen"
    decode(spec, out_dir)
    print(f"\nRégénérations : {out_dir}\nSpécification : {spec}")


def cmd_svg(args):
    out_dir = args.output or (os.path.splitext(args.input)[0] + "_svg")
    files = export_svg(args.input, out_dir)
    print(f"{len(files)} SVG hybride(s) écrit(s) dans : {out_dir}")
    for f in files:
        print(f"  {f}  ({_human(os.path.getsize(f))})")


def cmd_render(args):
    out_dir = args.output or (os.path.splitext(args.input)[0] + "_structonly")
    results = render_from_spec(args.input, out_dir)
    print(f"Rendu structure seule (vs raster fidèle) dans : {out_dir}")
    for path, s in results:
        print(f"  {path}  SSIM_vs_fidèle={s:.4f}")


def main():
    ap = argparse.ArgumentParser(
        description="docspec — image/PDF -> spécification rejouable (base WebP + résidu "
                    "+ structure vecteur/OCR) -> régénération à fidélité mesurée.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--lossless", action="store_true", help="Reconstruction exacte (100%%)")
    common.add_argument("--quality", type=int, default=DEFAULT_QUALITY,
                        help=f"Qualité WebP de la base (défaut : {DEFAULT_QUALITY})")
    common.add_argument("--target", type=float, default=DEFAULT_TARGET,
                        help=f"Cible SSIM (défaut : {DEFAULT_TARGET})")
    common.add_argument("--colors", type=int, default=DEFAULT_COLORS,
                        help=f"Couleurs de la palette décrite (défaut : {DEFAULT_COLORS})")
    common.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                        help=f"DPI de rasterisation des PDF (défaut : {DEFAULT_DPI})")

    pe = sub.add_parser("encode", parents=[common]); pe.add_argument("input"); pe.add_argument("-o", "--output")
    pe.set_defaults(func=cmd_encode)
    pdc = sub.add_parser("decode"); pdc.add_argument("input"); pdc.add_argument("-o", "--output")
    pdc.set_defaults(func=cmd_decode)
    prt = sub.add_parser("roundtrip", parents=[common]); prt.add_argument("input")
    prt.set_defaults(func=cmd_roundtrip)
    psvg = sub.add_parser("svg", help="Exporte un SVG hybride (image fidèle + texte éditable)")
    psvg.add_argument("input", help="fichier .imgspec"); psvg.add_argument("-o", "--output")
    psvg.set_defaults(func=cmd_svg)
    prn = sub.add_parser("render", help="Rendu depuis la structure seule (voie éditable)")
    prn.add_argument("input", help="fichier .imgspec"); prn.add_argument("-o", "--output")
    prn.set_defaults(func=cmd_render)

    args = ap.parse_args()
    if not hasattr(args, "input") or not os.path.isfile(args.input):
        sys.exit(f"Fichier introuvable : {getattr(args, 'input', '?')}")
    args.func(args)


if __name__ == "__main__":
    main()
