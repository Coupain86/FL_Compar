"""
docspec_ui.py — Interface graphique simple pour docspec.

Flux en deux temps :
    ① DÉCRIRE   : tu choisis un fichier X (image ou PDF) -> l'outil génère sa
                  description technique complète (un fichier .imgspec).
    ② RÉGÉNÉRER : tu redonnes ce .imgspec à l'outil -> il régénère l'image de X,
                  identique (mode Exact) ou à ~99 % (mode Léger).

100 % local. Dépendances : pip install pymupdf Pillow numpy
Lancement : py docspec_ui.py
"""

import json
import os
import sys
import threading
import zipfile

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk
    import docspec
    DEPS_OK = True
except Exception as _e:
    DEPS_OK = False
    _IMPORT_ERR = _e


def _open_path(path):
    """Ouvre un fichier/dossier dans l'explorateur du système."""
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            import subprocess; subprocess.run(["open", path])
        else:
            import subprocess; subprocess.run(["xdg-open", path])
    except Exception:
        pass


def _human(n):
    for u in ("o", "Ko", "Mo"):
        if n < 1024:
            return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} Go"


class DocspecUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("docspec — Description & Régénération")
        self.minsize(940, 720)

        self._src_var = tk.StringVar()        # fichier à décrire
        self._spec_out_var = tk.StringVar()   # .imgspec produit / à régénérer
        self._mode_var = tk.StringVar(value="exact")
        self._dpi_var = tk.IntVar(value=150)
        self._lang_var = tk.StringVar(value="fra+eng")
        self._status = tk.StringVar(value="Prêt.")
        self._last_spec = None
        self._thumbs = {}  # anti-GC des images

        self._build()

    # ── UI ────────────────────────────────────
    def _build(self):
        pad = {"padx": 10, "pady": 6}

        # ① DÉCRIRE
        f1 = ttk.LabelFrame(self, text="①  Décrire un fichier  →  description complète (.imgspec)", padding=10)
        f1.pack(fill="x", **pad)
        f1.columnconfigure(1, weight=1)

        ttk.Label(f1, text="Fichier (image ou PDF) :").grid(row=0, column=0, sticky="w")
        ttk.Entry(f1, textvariable=self._src_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f1, text="Parcourir…", command=self._browse_src).grid(row=0, column=2)

        opt = ttk.Frame(f1); opt.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(opt, text="Mode :").pack(side="left")
        ttk.Radiobutton(opt, text="Exact (100 % identique)", variable=self._mode_var,
                        value="exact").pack(side="left", padx=6)
        ttk.Radiobutton(opt, text="Léger (≈99 %, fichier plus petit)", variable=self._mode_var,
                        value="leger").pack(side="left", padx=6)
        ttk.Label(opt, text="    DPI (PDF) :").pack(side="left")
        ttk.Spinbox(opt, from_=72, to=300, textvariable=self._dpi_var, width=6).pack(side="left")
        ttk.Label(opt, text="    Langue OCR :").pack(side="left")
        ttk.Entry(opt, textvariable=self._lang_var, width=9).pack(side="left")

        b1 = ttk.Frame(f1); b1.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self._btn_describe = ttk.Button(b1, text="📝  Générer la description", command=self._describe)
        self._btn_describe.pack(side="left")
        self._btn_svg = ttk.Button(b1, text="↗ Export SVG", command=self._export_svg, state="disabled")
        self._btn_svg.pack(side="left", padx=6)
        self._btn_pdf = ttk.Button(b1, text="↗ PDF cherchable", command=self._export_pdf, state="disabled")
        self._btn_pdf.pack(side="left")

        # ② RÉGÉNÉRER
        f2 = ttk.LabelFrame(self, text="②  Régénérer l'image depuis une description (.imgspec)", padding=10)
        f2.pack(fill="x", **pad)
        f2.columnconfigure(1, weight=1)
        ttk.Label(f2, text="Description (.imgspec) :").grid(row=0, column=0, sticky="w")
        ttk.Entry(f2, textvariable=self._spec_out_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f2, text="Parcourir…", command=self._browse_spec).grid(row=0, column=2)
        ttk.Button(f2, text="🔄  Régénérer l'image", command=self._regenerate).grid(
            row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Button(f2, text="📄  Extraire le texte (.txt)", command=self._extract_text).grid(
            row=1, column=1, sticky="w", padx=6, pady=(10, 0))

        # Aperçus
        prev = ttk.LabelFrame(self, text="Aperçu", padding=10)
        prev.pack(fill="both", expand=True, **pad)
        for c in range(3):
            prev.columnconfigure(c, weight=1)
        self._lbl_a = self._make_preview(prev, 0, "Original")
        self._lbl_b = self._make_preview(prev, 1, "Régénéré")
        self._lbl_c = self._make_preview(prev, 2, "Différence ×10")

        # Statut
        bar = ttk.Frame(self); bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self._status, foreground="#333",
                  font=("Segoe UI", 9)).pack(side="left", padx=10, pady=6)
        self._progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
        self._progress.pack(side="right", padx=10)

        if not DEPS_OK:
            self._status.set("Dépendances manquantes — voir message.")
            self.after(200, lambda: messagebox.showerror(
                "Dépendances manquantes",
                f"Installe d'abord :\n  pip install pymupdf Pillow numpy\n\nDétail : {_IMPORT_ERR}"))

    def _make_preview(self, parent, col, title):
        box = ttk.Frame(parent); box.grid(row=0, column=col, sticky="nsew", padx=4)
        ttk.Label(box, text=title, font=("Segoe UI", 9, "bold")).pack()
        lbl = tk.Label(box, bg="#ddd", width=38, height=18, relief="solid", bd=1)
        lbl.pack(fill="both", expand=True, pady=4)
        return lbl

    # ── Helpers ───────────────────────────────
    def _set_busy(self, busy, msg=None):
        if msg:
            self._status.set(msg)
        if busy:
            self._progress.start(12)
            self._btn_describe.configure(state="disabled")
        else:
            self._progress.stop()
            self._btn_describe.configure(state="normal")

    def _show(self, lbl, arr, key):
        """Affiche un tableau numpy RGB redimensionné dans un Label."""
        img = Image.fromarray(arr, "RGB")
        img.thumbnail((360, 460), Image.LANCZOS)
        tkimg = ImageTk.PhotoImage(img)
        self._thumbs[key] = tkimg
        lbl.configure(image=tkimg, width=img.width, height=img.height)

    def _browse_src(self):
        p = filedialog.askopenfilename(
            title="Fichier à décrire",
            filetypes=[("Images / PDF", "*.pdf *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp"),
                       ("Tous", "*.*")])
        if p:
            self._src_var.set(p)

    def _browse_spec(self):
        p = filedialog.askopenfilename(title="Description .imgspec",
                                       filetypes=[("Description docspec", "*.imgspec"), ("Tous", "*.*")])
        if p:
            self._spec_out_var.set(p)

    # ── ① Décrire ─────────────────────────────
    def _describe(self):
        if not DEPS_OK:
            return
        src = self._src_var.get()
        if not src or not os.path.isfile(src):
            messagebox.showwarning("Fichier manquant", "Choisis d'abord un fichier à décrire.")
            return
        lossless = (self._mode_var.get() == "exact")
        dpi = self._dpi_var.get()
        out = os.path.splitext(src)[0] + ".imgspec"
        self._set_busy(True, "Génération de la description…")

        def worker():
            try:
                m = docspec.encode(src, out, lossless=lossless, target_ssim=0.99, dpi=dpi,
                                   lang=self._lang_var.get().strip() or None)
                # aperçus : original + régénéré + diff (page 0)
                pages, _ = docspec.ingest(src, dpi=dpi)
                orig = pages[0]
                with zipfile.ZipFile(out) as zf:
                    man = json.loads(zf.read("manifest.json").decode("utf-8"))
                    recon = docspec._recon_page(zf, man["pages"][0])
                import numpy as np
                diff = np.clip(np.abs(orig.astype(np.int16) - recon.astype(np.int16)) * 10, 0, 255).astype("uint8")
                ss = docspec.ssim(orig, recon)
                pr = docspec.psnr(orig, recon)
                p0 = m["pages"][0]["structure"]
                nshapes = p0.get("n_vector_shapes", 0)
                txt = p0.get("text", {})
                nwords = txt.get("n_words", 0) if txt.get("available") else 0
                size = os.path.getsize(out)

                def done():
                    self._show(self._lbl_a, orig, "a")
                    self._show(self._lbl_b, recon, "b")
                    self._show(self._lbl_c, diff, "c")
                    self._last_spec = out
                    self._spec_out_var.set(out)
                    self._btn_svg.configure(state="normal")
                    self._btn_pdf.configure(state="normal")
                    verdict = "EXACT (100 %)" if pr == float("inf") else f"SSIM {ss:.4f}"
                    self._set_busy(False,
                        f"Description écrite : {os.path.basename(out)} ({_human(size)}) — "
                        f"{len(m['pages'])} page(s), fidélité {verdict} — "
                        f"{nshapes} formes, {nwords} mots OCR.")
                self.after(0, done)
            except Exception:
                import traceback
                tb = traceback.format_exc()
                self.after(0, lambda: (self._set_busy(False, "Échec."),
                                       messagebox.showerror("Erreur", tb.splitlines()[-1])))

        threading.Thread(target=worker, daemon=True).start()

    def _export_svg(self):
        if not self._last_spec:
            return
        out_dir = os.path.splitext(self._last_spec)[0] + "_svg"
        try:
            files = docspec.export_svg(self._last_spec, out_dir)
            self._status.set(f"SVG écrit ({len(files)}) : {out_dir}")
            _open_path(out_dir)
        except Exception as e:
            messagebox.showerror("Erreur SVG", str(e))

    def _export_pdf(self):
        if not self._last_spec:
            return
        out = os.path.splitext(self._last_spec)[0] + "_cherchable.pdf"
        try:
            docspec.export_searchable_pdf(self._last_spec, out)
            self._status.set(f"PDF cherchable écrit : {out}")
            _open_path(os.path.dirname(out) or ".")
        except Exception as e:
            messagebox.showerror("Erreur PDF", str(e))

    def _extract_text(self):
        if not DEPS_OK:
            return
        spec = self._spec_out_var.get()
        if not spec or not os.path.isfile(spec):
            messagebox.showwarning("Fichier manquant", "Choisis une description .imgspec.")
            return
        out = os.path.splitext(spec)[0] + ".txt"
        self._set_busy(True, "Extraction du texte…")

        def worker():
            try:
                info = docspec.extract_text_file(spec, out)

                def done():
                    self._set_busy(False)
                    if not info["available"]:
                        reason = info.get("ocr_error") or "raison inconnue"
                        messagebox.showwarning(
                            "OCR indisponible",
                            "Impossible d'extraire du texte.\n\n"
                            f"Détail : {reason}\n\n"
                            "→ Installe Tesseract OCR (+ le pack français), puis reclique "
                            "« Extraire le texte ». Pas besoin de refaire la description.\n\n"
                            "Windows : installe Tesseract depuis "
                            "github.com/UB-Mannheim/tesseract/wiki\n"
                            "(coche « French » à l'installation), puis "
                            "py -m pip install pytesseract")
                        return
                    self._status.set(f"Texte extrait : {os.path.basename(out)} "
                                     f"({info['n_words']} mots, {info['chars']} car.)")
                    _open_path(out)
                self.after(0, done)
            except Exception:
                import traceback
                tb = traceback.format_exc()
                self.after(0, lambda: (self._set_busy(False, "Échec."),
                                       messagebox.showerror("Erreur", tb.splitlines()[-1])))

        threading.Thread(target=worker, daemon=True).start()

    # ── ② Régénérer ───────────────────────────
    def _regenerate(self):
        if not DEPS_OK:
            return
        spec = self._spec_out_var.get()
        if not spec or not os.path.isfile(spec):
            messagebox.showwarning("Fichier manquant", "Choisis une description .imgspec à régénérer.")
            return
        out_dir = os.path.splitext(spec)[0] + "_regen"
        self._set_busy(True, "Régénération de l'image…")

        def worker():
            try:
                files = docspec.decode(spec, out_dir)
                first = files[0] if files else None
                arr = None
                if first:
                    import numpy as np
                    arr = np.asarray(Image.open(first).convert("RGB"), dtype="uint8")

                def done():
                    if arr is not None:
                        self._show(self._lbl_b, arr, "b")
                    self._set_busy(False, f"{len(files)} page(s) régénérée(s) : {out_dir}")
                    _open_path(out_dir)
                self.after(0, done)
            except Exception:
                import traceback
                tb = traceback.format_exc()
                self.after(0, lambda: (self._set_busy(False, "Échec."),
                                       messagebox.showerror("Erreur", tb.splitlines()[-1])))

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    app = DocspecUI()
    try:
        style = ttk.Style(app); style.theme_use("clam")
    except Exception:
        pass
    app.mainloop()
