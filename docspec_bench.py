"""
docspec_bench.py — Banc d'essai + rapport de preuve HTML pour docspec.

Pour chaque image / PDF d'un dossier, encode (mode EXACT et mode PERCEPTUEL),
régénère, mesure la fidélité (SSIM/PSNR) et la taille, puis produit un rapport
HTML : original | régénéré | diff amplifiée (×10), avec un tableau récapitulatif.

100 % local. Réutilise docspec.py.

Usage :
    py docspec_bench.py                       (corpus par défaut : test_pdfs/)
    py docspec_bench.py mon_dossier -o rapport.html --target 0.99 --dpi 120
"""

import argparse
import base64
import io
import os
import glob

import numpy as np
from PIL import Image

import docspec as ds

IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")


def recon(rgb, lossless, quality, target):
    """Reconstruit (base WebP + résidu si besoin) et retourne (recon, octets, step)."""
    base_data, base_rgb = ds.webp_base(rgb, lossless, quality)
    size = len(base_data)
    step = 0
    if lossless:
        if not np.array_equal(base_rgb, rgb):
            q = ds.quantize_residual(rgb, base_rgb, 1)
            size += len(ds.residual_bytes(q, 1))
            return ds.apply_residual(base_rgb, q, 1), size, 1
        return base_rgb, size, 0
    if ds.ssim(rgb, base_rgb) < target:
        chosen = 1
        for st in ds.STEP_LADDER:
            q = ds.quantize_residual(rgb, base_rgb, st)
            if ds.ssim(rgb, ds.apply_residual(base_rgb, q, st)) >= target:
                chosen = st
            else:
                break
        q = ds.quantize_residual(rgb, base_rgb, chosen)
        size += len(ds.residual_bytes(q, chosen))
        return ds.apply_residual(base_rgb, q, chosen), size, chosen
    return base_rgb, size, 0


def thumb_b64(rgb, max_w=300):
    img = Image.fromarray(rgb, "RGB")
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def diff_amplified(a, b, factor=10):
    d = np.clip(np.abs(a.astype(np.int16) - b.astype(np.int16)) * factor, 0, 255).astype(np.uint8)
    return d


def collect(folder):
    files = sorted(glob.glob(os.path.join(folder, "*")))
    out = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext == ".pdf" and not os.path.basename(f).startswith("perf_"):
            out.append(f)
        elif ext in IMG_EXT:
            out.append(f)
    return out


def run(folder, out_html, target, dpi, quality):
    files = collect(folder)
    if not files:
        raise SystemExit(f"Aucun fichier image/PDF dans : {folder}")

    rows_html = []
    summary = []
    for path in files:
        name = os.path.basename(path)
        try:
            pages, _ = ds.ingest(path, dpi=dpi)
        except Exception as e:
            print(f"  ! {name} : {e}")
            continue
        rgb = pages[0]  # page 0 (corpus de test = 1-2 pages)

        rec_x, size_x, _ = recon(rgb, True, quality, target)            # EXACT
        rec_p, size_p, step_p = recon(rgb, False, quality, target)      # PERCEPTUEL

        ssim_x, psnr_x = ds.ssim(rgb, rec_x), ds.psnr(rgb, rec_x)
        ssim_p, psnr_p = ds.ssim(rgb, rec_p), ds.psnr(rgb, rec_p)
        src_size = os.path.getsize(path)

        print(f"  {name:42s} exact SSIM={ssim_x:.4f} | perceptuel SSIM={ssim_p:.4f} "
              f"({ds._human(size_p)}, step={step_p})")
        summary.append((name, ssim_x, psnr_x, ssim_p, psnr_p, src_size, size_x, size_p))

        diff = diff_amplified(rgb, rec_p)
        rows_html.append(f"""
        <div class="card">
          <h3>{name}</h3>
          <div class="trio">
            <figure><img src="data:image/jpeg;base64,{thumb_b64(rgb)}"/><figcaption>Original</figcaption></figure>
            <figure><img src="data:image/jpeg;base64,{thumb_b64(rec_p)}"/><figcaption>Régénéré (perceptuel)</figcaption></figure>
            <figure><img src="data:image/jpeg;base64,{thumb_b64(diff)}"/><figcaption>Diff ×10</figcaption></figure>
          </div>
          <table class="m">
            <tr><th></th><th>SSIM</th><th>PSNR</th><th>Taille spéc.</th></tr>
            <tr><td>Exact</td><td>{ssim_x:.5f}</td><td>{'inf' if psnr_x==float('inf') else f'{psnr_x:.1f} dB'}</td><td>{ds._human(size_x)}</td></tr>
            <tr><td>Perceptuel (cible {target})</td><td>{ssim_p:.5f}</td><td>{psnr_p:.1f} dB</td><td>{ds._human(size_p)}</td></tr>
            <tr><td>Source</td><td colspan="2">—</td><td>{ds._human(src_size)}</td></tr>
          </table>
        </div>""")

    # Agrégats
    n = len(summary)
    avg_sx = sum(r[1] for r in summary) / n
    avg_sp = sum(r[3] for r in summary) / n
    n_exact = sum(1 for r in summary if r[2] == float("inf"))

    html = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>docspec — rapport de preuve</title><style>
body{{font-family:'Segoe UI',sans-serif;background:#f4f6f9;color:#222;margin:0;padding:24px}}
h1{{color:#2c3e50}} .sum{{background:#fff;border-radius:8px;padding:16px;margin-bottom:20px;box-shadow:0 2px 6px rgba(0,0,0,.08)}}
.card{{background:#fff;border-radius:8px;padding:16px;margin-bottom:18px;box-shadow:0 2px 6px rgba(0,0,0,.08)}}
.card h3{{margin:0 0 10px;color:#2980b9}}
.trio{{display:flex;gap:14px;flex-wrap:wrap}} figure{{margin:0;text-align:center}}
figure img{{max-width:300px;border:1px solid #ddd;border-radius:4px;background:#fff}}
figcaption{{font-size:12px;color:#666;margin-top:4px}}
table.m{{border-collapse:collapse;margin-top:12px;font-size:13px}}
table.m th,table.m td{{border:1px solid #e2e8f0;padding:4px 10px;text-align:center}}
.big{{font-size:26px;font-weight:bold;color:#2ecc71}}
</style></head><body>
<h1>docspec — rapport de preuve</h1>
<div class="sum">
  <div>Fichiers testés : <b>{n}</b> &nbsp;|&nbsp; reconstructions <b>exactes (PSNR ∞)</b> : <b>{n_exact}/{n}</b></div>
  <div>SSIM moyen — mode exact : <span class="big">{avg_sx:.5f}</span>
       &nbsp;|&nbsp; mode perceptuel : <span class="big">{avg_sp:.5f}</span></div>
  <div style="color:#666;font-size:13px;margin-top:6px">Diff ×10 : noir = identique ; toute couleur = écart amplifié 10×.</div>
</div>
{''.join(rows_html)}
</body></html>"""

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nRapport écrit : {out_html}")
    print(f"Résumé : {n} fichiers, {n_exact} exacts, SSIM moyen exact={avg_sx:.5f} perceptuel={avg_sp:.5f}")


def main():
    ap = argparse.ArgumentParser(description="Banc d'essai + rapport de preuve HTML pour docspec.")
    ap.add_argument("folder", nargs="?", default="test_pdfs", help="Dossier (défaut : test_pdfs)")
    ap.add_argument("-o", "--output", default="docspec_rapport.html")
    ap.add_argument("--target", type=float, default=0.99)
    ap.add_argument("--dpi", type=int, default=120)
    ap.add_argument("--quality", type=int, default=80)
    args = ap.parse_args()
    run(args.folder, args.output, args.target, args.dpi, args.quality)


if __name__ == "__main__":
    main()
