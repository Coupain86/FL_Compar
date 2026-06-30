"""
docspec.py — Décompose une image / un PDF en une SPÉCIFICATION COMPLÈTE rejouable,
puis la régénère avec une fidélité mesurée (objectif : >= 99 %, ou exact).

100 % local — fitz + Pillow + numpy, aucun appel réseau.

PRINCIPE (l'architecture décrite : structuré + résidu)
    1. Ingestion      : tout format (PDF page à page, JPG, PNG, …) -> raster RGB canonique.
    2. Couche STRUCTURE: quantification de palette (capture texte / aplats / graphiques).
                         -> phase 2 : OCR + vectorisation Bézier (réservé dans le manifest).
    3. Couche RÉSIDU  : différence (original - reconstruction de la base), quantifiée
                         par un pas `step`. C'EST ELLE QUI GARANTIT LA FIDÉLITÉ.
                            step = 1  -> reconstruction EXACTE (100 %)
                            step > 1  -> plus léger, fidélité réglée sur une cible SSIM.
    4. Conteneur      : un fichier .imgspec (zip) = manifest.json + assets par page.
    5. Régénération   : .imgspec -> image(s) identiques (ou >= cible SSIM).

Le manifest.json EST la spécification extraite, lisible : dimensions, DPI, espace
couleur, couleur de fond, palette, paramètres du résidu, et fidélité mesurée.

Dépendances : pip install pymupdf Pillow numpy

Usage :
    py docspec.py roundtrip image.jpg                 (encode + decode + rapport fidélité)
    py docspec.py roundtrip doc.pdf --target 0.99
    py docspec.py encode  image.png -o sortie.imgspec --lossless
    py docspec.py decode  sortie.imgspec -o dossier_sortie
"""

import argparse
import io
import json
import os
import sys
import zipfile

import numpy as np
from PIL import Image

try:
    import fitz  # PyMuPDF — pour les PDF
except ImportError:
    fitz = None

FORMAT_VERSION = 1
DEFAULT_DPI     = 150     # DPI de rasterisation des PDF
DEFAULT_COLORS  = 64      # couleurs de la palette (couche structure)
DEFAULT_TARGET  = 0.99    # cible SSIM en mode perceptuel
STEP_LADDER     = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32]  # pas de résidu testés


# ─────────────────────────────────────────────
#  1. INGESTION — tout format -> liste de pages RGB (numpy uint8)
# ─────────────────────────────────────────────
def ingest(path: str, dpi: int = DEFAULT_DPI):
    """Retourne (pages, meta). pages = liste de tableaux HxWx3 uint8."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        if fitz is None:
            raise SystemExit("PyMuPDF requis pour les PDF : pip install pymupdf")
        doc = fitz.open(path)
        pages = []
        scale = dpi / 72.0
        try:
            for page in doc:
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                                      colorspace=fitz.csRGB)
                arr = np.frombuffer(pix.samples, dtype=np.uint8)
                arr = arr.reshape(pix.height, pix.width, 3).copy()
                pages.append(arr)
        finally:
            doc.close()
        meta = {"source_kind": "pdf", "dpi": dpi}
        return pages, meta

    # Image bitmap (jpg, png, bmp, tiff, webp, …)
    img = Image.open(path).convert("RGB")
    arr = np.asarray(img, dtype=np.uint8).copy()
    info_dpi = img.info.get("dpi", (72, 72))
    meta = {"source_kind": "image", "dpi": int(info_dpi[0]) if info_dpi else 72}
    return [arr], meta


# ─────────────────────────────────────────────
#  2. COUCHE STRUCTURE — quantification de palette
# ─────────────────────────────────────────────
def build_base(rgb: np.ndarray, n_colors: int):
    """
    Reconstruit une approximation 'structurée' (aplats de couleur) via palette.
    Retourne (base_rgb, png_bytes, palette, background_rgb).
    """
    img = Image.fromarray(rgb, "RGB")
    pal_img = img.quantize(colors=n_colors, method=Image.MEDIANCUT, dither=Image.NONE)

    # PNG indexé = asset compact dans le conteneur
    buf = io.BytesIO()
    pal_img.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()

    base_rgb = np.asarray(pal_img.convert("RGB"), dtype=np.uint8)

    # Palette (liste de couleurs) + couleur de fond (index le plus fréquent)
    raw_pal = pal_img.getpalette() or []
    used = sorted({int(i) for i in np.asarray(pal_img).ravel()})
    palette = [raw_pal[i * 3:i * 3 + 3] for i in used if i * 3 + 2 < len(raw_pal)]
    counts = np.bincount(np.asarray(pal_img).ravel())
    bg_idx = int(np.argmax(counts))
    background = raw_pal[bg_idx * 3:bg_idx * 3 + 3] if bg_idx * 3 + 2 < len(raw_pal) else [255, 255, 255]
    return base_rgb, png_bytes, palette, [int(c) for c in background]


def base_from_png(png_bytes: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    return np.asarray(img, dtype=np.uint8)


# ─────────────────────────────────────────────
#  3. COUCHE RÉSIDU — garantit la fidélité
# ─────────────────────────────────────────────
def quantize_residual(rgb: np.ndarray, base_rgb: np.ndarray, step: int):
    """residual = original - base ; q = round(residual/step). Reconstruit exact si step=1."""
    residual = rgb.astype(np.int16) - base_rgb.astype(np.int16)
    q = np.round(residual / step).astype(np.int16)
    return q


def apply_residual(base_rgb: np.ndarray, q: np.ndarray, step: int) -> np.ndarray:
    recon = base_rgb.astype(np.int16) + q.astype(np.int16) * step
    return np.clip(recon, 0, 255).astype(np.uint8)


def residual_bytes(q: np.ndarray, step: int) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, q=q.astype(np.int16), step=np.int32(step))
    return buf.getvalue()


def residual_from_bytes(data: bytes):
    with np.load(io.BytesIO(data)) as npz:
        return npz["q"], int(npz["step"])


# ─────────────────────────────────────────────
#  MÉTRIQUES (numpy pur, sans scipy)
# ─────────────────────────────────────────────
def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    if mse == 0:
        return float("inf")
    return float(10.0 * np.log10(255.0 ** 2 / mse))


def _box_blur(img: np.ndarray, k: int) -> np.ndarray:
    """Moyenne glissante k×k, 'same', bords répliqués (séparable, robuste)."""
    pad = k // 2
    p = np.pad(img, ((pad, pad), (pad, pad)), mode="edge")
    out = np.zeros_like(img, dtype=np.float64)
    h, w = img.shape
    for dy in range(k):
        for dx in range(k):
            out += p[dy:dy + h, dx:dx + w]
    return out / (k * k)


def ssim(a: np.ndarray, b: np.ndarray, k: int = 7) -> float:
    """SSIM sur la luminance (standard). 1.0 = identique."""
    xf = a.astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    yf = b.astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2
    mu_x = _box_blur(xf, k); mu_y = _box_blur(yf, k)
    mu_x2, mu_y2, mu_xy = mu_x * mu_x, mu_y * mu_y, mu_x * mu_y
    sx = _box_blur(xf * xf, k) - mu_x2
    sy = _box_blur(yf * yf, k) - mu_y2
    sxy = _box_blur(xf * yf, k) - mu_xy
    smap = ((2 * mu_xy + C1) * (2 * sxy + C2)) / ((mu_x2 + mu_y2 + C1) * (sx + sy + C2))
    return float(np.clip(smap, -1, 1).mean())


# ─────────────────────────────────────────────
#  ENCODAGE — choisit le pas de résidu pour atteindre la cible
# ─────────────────────────────────────────────
def encode_page(rgb: np.ndarray, n_colors: int, target_ssim, lossless: bool):
    """
    Retourne un dict asset {png, residual, manifest_page}.
    Si lossless : step=1 (exact). Sinon : plus grand step gardant SSIM >= target.
    """
    base_rgb, png_bytes, palette, background = build_base(rgb, n_colors)

    if lossless:
        chosen_step = 1
    else:
        chosen_step = 1
        for step in STEP_LADDER:
            q = quantize_residual(rgb, base_rgb, step)
            recon = apply_residual(base_rgb, q, step)
            if ssim(rgb, recon) >= target_ssim:
                chosen_step = step
            else:
                break

    q = quantize_residual(rgb, base_rgb, chosen_step)
    recon = apply_residual(base_rgb, q, chosen_step)
    res_bytes = residual_bytes(q, chosen_step)

    h, w = rgb.shape[:2]
    page_manifest = {
        "width": int(w), "height": int(h),
        "background_color": background,
        "base_layer": {
            "type": "palette_quantization",
            "n_colors": len(palette),
            "palette": palette,
            "asset": None,           # rempli au packaging
            "note": "phase 2 prévue : OCR (texte+police) + vectorisation Bézier",
        },
        "residual_layer": {
            "type": "raster_residual",
            "step": int(chosen_step),
            "nonzero_fraction": round(float(np.count_nonzero(q) / q.size), 4),
            "asset": None,
        },
        "fidelity": {
            "ssim": round(ssim(rgb, recon), 5),
            "psnr_db": round(psnr(rgb, recon), 2),
            "exact": bool(chosen_step == 1),
        },
    }
    return {"png": png_bytes, "residual": res_bytes, "manifest_page": page_manifest}


def encode(path: str, out_path: str, n_colors: int = DEFAULT_COLORS,
           target_ssim: float = DEFAULT_TARGET, lossless: bool = False,
           dpi: int = DEFAULT_DPI) -> dict:
    pages, meta = ingest(path, dpi=dpi)
    manifest = {
        "format_version": FORMAT_VERSION,
        "source": os.path.basename(path),
        "global": {
            "n_pages": len(pages),
            "colorspace": "sRGB",
            "dpi": meta.get("dpi"),
            "source_kind": meta.get("source_kind"),
        },
        "pages": [],
    }

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, rgb in enumerate(pages):
            asset = encode_page(rgb, n_colors, target_ssim, lossless)
            base_name = f"page_{i:03d}_base.png"
            res_name = f"page_{i:03d}_residual.npz"
            asset["manifest_page"]["index"] = i
            asset["manifest_page"]["base_layer"]["asset"] = base_name
            asset["manifest_page"]["residual_layer"]["asset"] = res_name
            zf.writestr(base_name, asset["png"])
            zf.writestr(res_name, asset["residual"])
            manifest["pages"].append(asset["manifest_page"])
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

    return manifest


# ─────────────────────────────────────────────
#  DÉCODAGE — régénère les pages
# ─────────────────────────────────────────────
def decode(spec_path: str, out_dir: str) -> list:
    os.makedirs(out_dir, exist_ok=True)
    out_files = []
    with zipfile.ZipFile(spec_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        stem = os.path.splitext(os.path.basename(manifest.get("source", "image")))[0]
        for page in manifest["pages"]:
            base_rgb = base_from_png(zf.read(page["base_layer"]["asset"]))
            q, step = residual_from_bytes(zf.read(page["residual_layer"]["asset"]))
            recon = apply_residual(base_rgb, q, step)
            out_path = os.path.join(out_dir, f"{stem}_regen_p{page['index']:03d}.png")
            Image.fromarray(recon, "RGB").save(out_path)
            out_files.append(out_path)
    return out_files


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────
def _human(nbytes: int) -> str:
    for unit in ("o", "Ko", "Mo"):
        if nbytes < 1024:
            return f"{nbytes:.0f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} Go"


def cmd_encode(args):
    out = args.output or (os.path.splitext(args.input)[0] + ".imgspec")
    m = encode(args.input, out, n_colors=args.colors,
               target_ssim=args.target, lossless=args.lossless, dpi=args.dpi)
    print(f"Spécification écrite : {out}  ({_human(os.path.getsize(out))})")
    for p in m["pages"]:
        f = p["fidelity"]
        print(f"  page {p['index']}: SSIM={f['ssim']}  PSNR={f['psnr_db']} dB  "
              f"step={p['residual_layer']['step']}  exact={f['exact']}")


def cmd_decode(args):
    out_dir = args.output or (os.path.splitext(args.input)[0] + "_regen")
    files = decode(args.input, out_dir)
    print(f"{len(files)} page(s) régénérée(s) dans : {out_dir}")
    for f in files:
        print(f"  {f}")


def cmd_roundtrip(args):
    spec = os.path.splitext(args.input)[0] + ".imgspec"
    print(f"== Encodage de {args.input} ==")
    m = encode(args.input, spec, n_colors=args.colors,
               target_ssim=args.target, lossless=args.lossless, dpi=args.dpi)
    src_size = os.path.getsize(args.input)
    spec_size = os.path.getsize(spec)
    print(f"  Fichier source        : {_human(src_size)}")
    print(f"  Spécification .imgspec : {_human(spec_size)}")
    print(f"  Mode : {'LOSSLESS (exact)' if args.lossless else f'perceptuel (cible SSIM {args.target})'}")
    print(f"\n== Régénération + contrôle de fidélité ==")
    out_dir = os.path.splitext(args.input)[0] + "_regen"
    decode(spec, out_dir)

    pages, _ = ingest(args.input, dpi=args.dpi)
    for i, rgb in enumerate(pages):
        recon = np.asarray(Image.open(
            os.path.join(out_dir, f"{os.path.splitext(os.path.basename(args.input))[0]}_regen_p{i:03d}.png")
        ).convert("RGB"), dtype=np.uint8)
        s = ssim(rgb, recon)
        pr = psnr(rgb, recon)
        verdict = "EXACT (100%)" if pr == float("inf") else \
                  f"{'OK' if s >= args.target else 'SOUS LA CIBLE'} (SSIM {s:.4f})"
        print(f"  page {i}: SSIM={s:.5f}  PSNR={'inf' if pr==float('inf') else f'{pr:.2f} dB'}  -> {verdict}")
    print(f"\nRégénérations dans : {out_dir}\nSpécification dans : {spec}")


def main():
    ap = argparse.ArgumentParser(
        description="docspec — décompose une image/PDF en spécification rejouable "
                    "(structure + résidu) et la régénère à fidélité mesurée.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--colors", type=int, default=DEFAULT_COLORS,
                        help=f"Couleurs de palette (défaut : {DEFAULT_COLORS})")
    common.add_argument("--target", type=float, default=DEFAULT_TARGET,
                        help=f"Cible SSIM en mode perceptuel (défaut : {DEFAULT_TARGET})")
    common.add_argument("--lossless", action="store_true",
                        help="Reconstruction exacte (step=1, 100%%)")
    common.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                        help=f"DPI de rasterisation des PDF (défaut : {DEFAULT_DPI})")

    pe = sub.add_parser("encode", parents=[common]); pe.add_argument("input"); pe.add_argument("-o", "--output")
    pe.set_defaults(func=cmd_encode)
    pd = sub.add_parser("decode"); pd.add_argument("input"); pd.add_argument("-o", "--output")
    pd.set_defaults(func=cmd_decode)
    pr = sub.add_parser("roundtrip", parents=[common]); pr.add_argument("input")
    pr.set_defaults(func=cmd_roundtrip)

    args = ap.parse_args()
    if not hasattr(args, "input") or not os.path.isfile(args.input):
        sys.exit(f"Fichier introuvable : {getattr(args, 'input', '?')}")
    args.func(args)


if __name__ == "__main__":
    main()
