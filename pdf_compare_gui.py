"""
pdf_compare_gui.py — Interface graphique tkinter pour pdf_compare.py
Fenêtre unique avec 3 onglets :
  1. Configuration  — paramètres, fichiers, zones à exclure, lancement
  2. Aperçu diff    — REF | DIFF colorée | CAND sur la page courante (ajustement des seuils)
  3. Zones          — sélecteur visuel de zones à exclure (intégré)
100 % local — aucun appel réseau.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import os, sys, io, time, json, webbrowser

try:
    from PIL import Image, ImageTk, ImageDraw
    import fitz
    import numpy as np
    PIL_OK = True
except ImportError:
    PIL_OK = False

PREFS_FILE = os.path.join(os.path.expanduser("~"), ".pdf_compare_prefs.json")
PREVIEW_DPI = 100   # DPI de rendu dans l'onglet aperçu

# ── Couleurs diff aperçu ──────────────────────
DIFF_REF_FILL   = (255, 230,  60, 140)   # jaune  — présent dans REF, absent du CAND
DIFF_NEW_FILL   = (220,  50,  50, 140)   # rouge  — présent dans CAND, absent du REF
DIFF_VIS_FILL   = (100,  80, 220, 100)   # violet — diff visuelle image
ZONE_COLOR      = "#e74c3c"
PAGE_EX_COLOR   = "#f39c12"
RENDER_DPI_SEL  = 120


# ─────────────────────────────────────────────
#  UTILITAIRES PARTAGÉS
# ─────────────────────────────────────────────
class _StreamRedirect(io.TextIOBase):
    def __init__(self, cb): self._cb = cb
    def write(self, text):
        if text: self._cb(text)
        return len(text)
    def flush(self): pass


def _fmt_eta(s):
    s = int(s)
    if s < 60:     return f"{s}s"
    elif s < 3600: return f"{s//60}m {s%60:02d}s"
    else:          return f"{s//3600}h {(s%3600)//60:02d}m {s%60:02d}s"


def _pdf_page_count(path: str) -> int:
    try:
        doc = fitz.open(path); n = doc.page_count; doc.close(); return n
    except Exception:
        return 0


def _make_report_name(ref_path: str, new_path: str) -> str:
    ref_stem = os.path.splitext(os.path.basename(ref_path))[0]
    new_stem  = os.path.splitext(os.path.basename(new_path))[0]
    out_dir   = os.path.dirname(os.path.abspath(ref_path))
    return os.path.join(out_dir, f"{ref_stem} vs {new_stem}.html")


def _add_tooltip(widget, text: str):
    tip      = None
    after_id = None

    def show(e):
        nonlocal tip, after_id
        # Annuler un éventuel hide planifié
        if after_id:
            widget.after_cancel(after_id)
        # Ne pas créer de doublon
        if tip:
            return
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{e.x_root+12}+{e.y_root+6}")
        tk.Label(tip, text=text, background="#ffffe0", relief="solid",
                 borderwidth=1, font=("Segoe UI", 9), wraplength=280,
                 justify="left", padx=4, pady=2).pack()

    def schedule_hide(e):
        nonlocal after_id
        # Petit délai pour absorber les Leave/Enter entre widget parent et enfant
        after_id = widget.after(80, do_hide)

    def do_hide():
        nonlocal tip, after_id
        after_id = None
        if tip:
            tip.destroy()
            tip = None

    widget.bind("<Enter>", show,          add="+")
    widget.bind("<Leave>", schedule_hide, add="+")


# ─────────────────────────────────────────────
#  ONGLET 1 — CONFIGURATION
# ─────────────────────────────────────────────
class TabConfig(ttk.Frame):
    """Onglet principal : fichiers, paramètres, lancement."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self._app = app
        self._build()

    def _build(self):
        pad = {"padx": 10, "pady": 4}

        # ── Fichiers ──────────────────────────
        frm = ttk.LabelFrame(self, text="Fichiers PDF", padding=8)
        frm.pack(fill="x", **pad)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Référence :").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self._app._ref_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="…", width=3, command=self._app._browse_ref).grid(row=0, column=2)
        ttk.Label(frm, textvariable=self._app._ref_pages,
                  foreground="#666", font=("Segoe UI", 8)).grid(row=0, column=3, sticky="w", padx=(6,0))

        ttk.Label(frm, text="Candidat  :").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self._app._new_var).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="…", width=3, command=self._app._browse_new).grid(row=1, column=2)
        ttk.Label(frm, textvariable=self._app._new_pages,
                  foreground="#666", font=("Segoe UI", 8)).grid(row=1, column=3, sticky="w", padx=(6,0))

        ttk.Label(frm, text="Rapport   :").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self._app._out_var).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="…", width=3, command=self._app._browse_out).grid(row=2, column=2)

        # ── Pages à ignorer ───────────────────
        frm_skip = ttk.LabelFrame(self, text="Pages à ignorer", padding=8)
        frm_skip.pack(fill="x", **pad)
        skip_fields = [
            ("REF — début :", self._app._ref_skip_start, "Pages à ignorer au début du PDF référence"),
            ("REF — fin :",   self._app._ref_skip_end,   "Pages à ignorer à la fin du PDF référence"),
            ("NEW — début :", self._app._new_skip_start, "Pages à ignorer au début du PDF candidat"),
            ("NEW — fin :",   self._app._new_skip_end,   "Pages à ignorer à la fin du PDF candidat"),
        ]
        for col, (lbl, var, tip) in enumerate(skip_fields):
            l = ttk.Label(frm_skip, text=lbl)
            l.grid(row=0, column=col*2, sticky="w", padx=(8 if col else 0, 2))
            s = ttk.Spinbox(frm_skip, from_=0, to=9999, textvariable=var, width=6)
            s.grid(row=0, column=col*2+1, sticky="w")
            _add_tooltip(l, tip); _add_tooltip(s, tip)

        # ── Modes de comparaison ──────────────
        frm_modes = ttk.Frame(self, padding=(10, 4))
        frm_modes.pack(fill="x")
        frm_modes.columnconfigure(0, weight=1)
        frm_modes.columnconfigure(1, weight=1)

        def _make_spinrow(parent, lbl, var, vmin, vmax, tip, row):
            l = ttk.Label(parent, text=lbl)
            l.grid(row=row, column=0, sticky="w", padx=(4, 2), pady=2)
            s = ttk.Spinbox(parent, from_=vmin, to=vmax, textvariable=var, width=7)
            s.grid(row=row, column=1, sticky="w", pady=2)
            _add_tooltip(l, tip); _add_tooltip(s, tip)
            return l, s

        def _set_children_state(widgets, enabled):
            st = "normal" if enabled else "disabled"
            for w in widgets:
                try: w.configure(state=st)
                except Exception: pass

        # ── Bloc TEXTE ────────────────────────
        frm_txt = ttk.LabelFrame(frm_modes, padding=8)
        frm_txt.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        cb_txt = ttk.Checkbutton(frm_txt, text="Phase 1 — Comparaison TEXTE",
                                  variable=self._app._mode_text_var,
                                  style="Bold.TCheckbutton")
        cb_txt.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        _add_tooltip(cb_txt, "Active la comparaison textuelle (Phase 1) :\ndétecte les blocs manquants, ajoutés et déplacés.")

        txt_widgets = []
        for row_i, (lbl, var, vmin, vmax, tip) in enumerate([
            ("Tol. position (px) :", self._app._tol_txt_var, 0, 500,
             "Décalage maximum (en pixels) toléré pour un bloc texte avant\n"
             "qu'il soit considéré comme 'déplacé'. Défaut : 3 px."),
        ], start=1):
            l, s = _make_spinrow(frm_txt, lbl, var, vmin, vmax, tip, row_i)
            txt_widgets += [l, s]

        cb_elastic = ttk.Checkbutton(frm_txt, text="Appariement élastique (DTW)",
                                      variable=self._app._elastic_var)
        cb_elastic.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        _add_tooltip(cb_elastic,
                     "Aligne les pages élastiquement pour compenser les débordements :\n"
                     "• Page REF couvre 2 pages CAND\n• Page CAND couvre 2 pages REF\n"
                     "Plus lent sur les gros documents.")
        txt_widgets.append(cb_elastic)

        def _toggle_text(*_):
            _set_children_state(txt_widgets, self._app._mode_text_var.get())
        self._app._mode_text_var.trace_add("write", _toggle_text)
        _toggle_text()

        # ── Bloc IMAGE ────────────────────────
        frm_img = ttk.LabelFrame(frm_modes, padding=8)
        frm_img.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        cb_img = ttk.Checkbutton(frm_img, text="Phase 2 — Comparaison IMAGE",
                                  variable=self._app._mode_image_var,
                                  style="Bold.TCheckbutton")
        cb_img.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        _add_tooltip(cb_img, "Active la comparaison visuelle pixel à pixel (Phase 2) :\ndétecte les différences graphiques après masquage du texte.")

        img_widgets = []
        for row_i, (lbl, var, vmin, vmax, tip) in enumerate([
            ("DPI :",               self._app._dpi_var,      50,  300,
             "Résolution de rasterisation.\nPlus élevé = plus précis mais plus lent. 100 est un bon défaut."),
            ("Tol. pixel :",        self._app._tol_pix_var,  0,   255,
             "Différence colorimétrique max par canal RGB (0-255).\nAbsorbe le bruit de rendu. Défaut : 10."),
            ("Aire min diff (px²):", self._app._min_area_var, 1,  9999,
             "Surface minimale d'une zone de diff pour être reportée.\nFiltre les micro-différences. Défaut : 50."),
            ("Tol. décalage (px) :", self._app._shift_tol_var, 0,  20,
             "Décalage de rendu toléré (±N px) avant de signaler une diff.\nDéfaut : 3. Mettre à 0 pour désactiver."),
        ], start=1):
            l, s = _make_spinrow(frm_img, lbl, var, vmin, vmax, tip, row_i)
            img_widgets += [l, s]

        cb_no_ss = ttk.Checkbutton(frm_img, text="Sans screenshots dans le rapport",
                                    variable=self._app._no_ss_var)
        cb_no_ss.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        _add_tooltip(cb_no_ss, "Désactive les captures d'écran dans le rapport HTML.\n"
                               "Réduit la taille du fichier et le temps de génération.")
        img_widgets.append(cb_no_ss)

        def _toggle_image(*_):
            _set_children_state(img_widgets, self._app._mode_image_var.get())
        self._app._mode_image_var.trace_add("write", _toggle_image)
        _toggle_image()

        # ── Options générales ─────────────────
        frm_gen = ttk.LabelFrame(self, text="Options générales", padding=8)
        frm_gen.pack(fill="x", **pad)

        gen_opts = [
            ("Processus :", self._app._workers_var, 1, 32,
             f"Nombre de processus parallèles.\nDéfaut : nb cœurs CPU - 1 ({max(2,(os.cpu_count() or 2)-1)})."),
        ]
        for col_i, (lbl, var, vmin, vmax, tip) in enumerate(gen_opts):
            l = ttk.Label(frm_gen, text=lbl)
            l.grid(row=0, column=col_i*2, sticky="w", padx=(0, 2), pady=2)
            s = ttk.Spinbox(frm_gen, from_=vmin, to=vmax, textvariable=var, width=7)
            s.grid(row=0, column=col_i*2+1, sticky="w", pady=2, padx=(0, 16))
            _add_tooltip(l, tip); _add_tooltip(s, tip)

        bool_opts = [
            (self._app._open_var,     "Ouvrir le rapport après génération",
             "Ouvre automatiquement le rapport HTML dans le navigateur une fois terminé."),
            (self._app._limit100_var, "Limiter à 100 pages (test rapide)",
             "Compare uniquement les 100 premières pages.\nUtile pour tester les paramètres sur un document volumineux."),
        ]
        for col_i, (var, text, tip) in enumerate(bool_opts):
            cb = ttk.Checkbutton(frm_gen, text=text, variable=var)
            cb.grid(row=0, column=4 + col_i*2, sticky="w", padx=(12, 0), pady=2)
            _add_tooltip(cb, tip)

        # ── Boutons ───────────────────────────
        frm_btn = ttk.Frame(self, padding=(10, 4)); frm_btn.pack(fill="x")
        self._app._btn_run  = ttk.Button(frm_btn, text="▶  Lancer", command=self._app._run, style="Accent.TButton")
        self._app._btn_run.pack(side="left")
        self._app._btn_stop = ttk.Button(frm_btn, text="⏹  Annuler", command=self._app._stop, state="disabled")
        self._app._btn_stop.pack(side="left", padx=8)
        self._app._zones_lbl = ttk.Label(frm_btn, text="", foreground="#e74c3c", font=("Segoe UI", 8))
        self._app._zones_lbl.pack(side="left")
        ttk.Button(frm_btn, text="Ouvrir rapport", command=self._app._open_report).pack(side="right")

        # ── Progression ───────────────────────
        ttk.Label(self, textvariable=self._app._progress_lbl, foreground="#555").pack(anchor="w", padx=10)
        ttk.Progressbar(self, variable=self._app._progress_var, maximum=100).pack(fill="x", padx=10, pady=(0,4))

        # ── Journal ───────────────────────────
        frm_log = ttk.LabelFrame(self, text="Journal", padding=4)
        frm_log.pack(fill="both", expand=True, padx=10, pady=(0,10))
        self._app._log = scrolledtext.ScrolledText(frm_log, height=10, state="disabled",
                                                    font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self._app._log.pack(fill="both", expand=True)
        self._app._log.tag_config("ok",    foreground="#4ec9b0")
        self._app._log.tag_config("warn",  foreground="#dcdcaa")
        self._app._log.tag_config("error", foreground="#f44747")


# ─────────────────────────────────────────────
#  ONGLET 2 — APERÇU DIFF
# ─────────────────────────────────────────────
class TabPreview(ttk.Frame):
    """
    Onglet aperçu : compare la page courante en temps réel.
    3 colonnes : REF | DIFF colorée | CAND
    Chaque colonne a sa propre barre de navigation indépendante.
    La barre centrale (DIFF) avance REF et CAND ensemble.
    Molette et flèches directionnelles fonctionnent sur la colonne survolée.
    """

    def __init__(self, parent, app):
        super().__init__(parent)
        self._app = app
        self._ref_page_idx = 0   # index 0-based indépendant pour REF
        self._new_page_idx = 0   # index 0-based indépendant pour CAND
        self._calc_thread  = None
        self._cancel_flag  = threading.Event()
        self._tk_ref = self._tk_mid = self._tk_new = None
        # Comptage pages
        self._ref_page_count = 0
        self._new_page_count = 0
        self._hovered_col = None   # 0=REF, 1=DIFF, 2=CAND

        # ── Caches perf ───────────────────────
        # Documents ouverts en permanence (évite fitz.open() à chaque page)
        self._doc_cache: dict = {}   # {path: fitz.Document}
        # Pixmaps PIL mis en cache (LRU, max 20 entrées)
        # clé : (path, page_idx, dpi)
        from collections import OrderedDict
        self._pix_cache: OrderedDict = OrderedDict()
        self._PIX_CACHE_MAX = 20
        self._build()

    # ── Helpers page count ────────────────────
    def _refresh_page_counts(self):
        ref = self._app._ref_var.get()
        new = self._app._new_var.get()
        try:
            if ref and os.path.isfile(ref):
                d = self._get_doc(ref); self._ref_page_count = d.page_count
            if new and os.path.isfile(new):
                d = self._get_doc(new); self._new_page_count = d.page_count
        except Exception:
            pass

    def _get_doc(self, path: str):
        """Retourne le fitz.Document mis en cache, l'ouvre si nécessaire."""
        if path not in self._doc_cache:
            self._doc_cache[path] = fitz.open(path)
        return self._doc_cache[path]

    def _get_pix(self, path: str, page_idx: int, dpi: int) -> Image.Image:
        """Retourne l'image PIL de la page, depuis le cache LRU ou en la rasterisant."""
        key = (path, page_idx, dpi)
        if key in self._pix_cache:
            # Déplacer en fin (MRU)
            self._pix_cache.move_to_end(key)
            return self._pix_cache[key]
        doc  = self._get_doc(path)
        scale = dpi / 72.0
        pix  = doc[page_idx].get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB)
        img  = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        self._pix_cache[key] = img
        self._pix_cache.move_to_end(key)
        # Éviction LRU
        while len(self._pix_cache) > self._PIX_CACHE_MAX:
            self._pix_cache.popitem(last=False)
        return img

    def _invalidate_cache(self):
        """Vide les caches doc et pixmap (appelé quand les fichiers changent)."""
        for doc in self._doc_cache.values():
            try: doc.close()
            except Exception: pass
        self._doc_cache.clear()
        self._pix_cache.clear()

    def _build(self):
        # ── Corps : 3 colonnes ────────────────
        # Layout : header_row (barres nav) + canvas_row
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, uniform="col")
        body.columnconfigure(1, weight=1, uniform="col")
        body.columnconfigure(2, weight=1, uniform="col")
        body.rowconfigure(0, weight=0)   # légende
        body.rowconfigure(1, weight=0)   # barres nav
        body.rowconfigure(2, weight=0)   # statut
        body.rowconfigure(3, weight=1)   # canvases

        # ── Légende (ligne 0, pleine largeur) ──
        leg_outer = tk.Frame(body, bg="#f0f0f0", bd=1, relief="solid")
        leg_outer.grid(row=0, column=0, columnspan=3, sticky="ew", padx=4, pady=(4, 2))

        tk.Label(leg_outer, text="Code couleur — DIFF :",
                 bg="#f0f0f0", font=("Segoe UI", 8, "bold"), fg="#444").pack(side="left", padx=(8, 12))
        for bg, border, label, tooltip in [
            ("#e6b800", "#b38c00", "Texte REF absent du CAND",   "Bloc texte présent dans REF mais introuvable dans le CAND"),
            ("#e03030", "#a01010", "Texte CAND absent du REF",   "Bloc texte présent dans le CAND mais introuvable dans REF"),
            ("#c8b400", "#9a8800", "Texte déplacé",              "Bloc texte présent des deux côtés mais à une position différente"),
            ("#6050d0", "#3020a0", "Différence visuelle (image)", "Zone de différence graphique détectée après masquage du texte commun"),
        ]:
            cell = tk.Frame(leg_outer, bg=bg, bd=1, relief="solid",
                            highlightbackground=border, highlightthickness=1)
            cell.pack(side="left", padx=4, pady=3)
            lbl = tk.Label(cell, text=f"  {label}  ", bg=bg,
                           font=("Segoe UI", 8, "bold"), fg="white")
            lbl.pack(padx=2, pady=2)
            _add_tooltip(cell, tooltip)

        # ── Barres de navigation (ligne 1) ────
        def _nav_bar(parent, col):
            """Construit une barre de navigation pour la colonne col (0=REF,1=DIFF,2=CAND)."""
            outer = ttk.Frame(parent, padding=(2, 2))
            outer.grid(row=1, column=col, sticky="ew", padx=2)
            outer.columnconfigure(0, weight=1)

            is_diff = (col == 1)
            if is_diff:
                outer.configure(style="Diff.TFrame")

            # Sous-frame avec taille FIXE — on le centre via pack dans outer
            # mais on lui donne une taille fixe pour que le changement du label
            # statut (colonne voisine) ne le fasse pas bouger.
            wrapper = ttk.Frame(outer)
            wrapper.grid(row=0, column=0)          # centré dans outer
            frm = ttk.Frame(wrapper)
            frm.pack(anchor="center")              # centré dans wrapper, taille fixe

            # ⏮ début
            ttk.Button(frm, text="⏮", width=2,
                       command=lambda: self._go_first(col)).pack(side="left", padx=1)
            # ◀ précédent
            ttk.Button(frm, text="◀", width=2,
                       command=lambda: self._go_prev(col)).pack(side="left", padx=1)

            # Label page — largeur FIXE en caractères
            lbl = ttk.Label(frm, text="—", width=20 if is_diff else 10, anchor="center",
                             font=("Segoe UI", 8, "bold" if is_diff else "normal"))
            lbl.pack(side="left", padx=2)

            # ▶ suivant
            ttk.Button(frm, text="▶", width=2,
                       command=lambda: self._go_next(col)).pack(side="left", padx=1)
            # ⏭ fin
            ttk.Button(frm, text="⏭", width=2,
                       command=lambda: self._go_last(col)).pack(side="left", padx=1)

            # Aller à
            ttk.Label(frm, text=" p.", font=("Segoe UI", 8)).pack(side="left")
            goto_var = tk.StringVar()
            e = ttk.Entry(frm, textvariable=goto_var, width=4)
            e.pack(side="left", padx=1)
            e.bind("<Return>", lambda _, c=col, v=goto_var: self._go_to(c, v))
            ttk.Button(frm, text="↵", width=2,
                       command=lambda c=col, v=goto_var: self._go_to(c, v)).pack(side="left", padx=1)

            if is_diff:
                ttk.Separator(frm, orient="vertical").pack(side="left", fill="y", padx=8)

            return lbl

        self._lbl_ref  = _nav_bar(body, 0)
        self._lbl_diff = _nav_bar(body, 1)
        self._lbl_new  = _nav_bar(body, 2)

        # Statut DIFF — largeur fixe sur toute la ligne, hauteur réservée
        status_row = tk.Frame(body, height=20)
        status_row.grid(row=2, column=0, columnspan=3, sticky="ew")
        status_row.pack_propagate(False)   # hauteur fixe — ne grandit pas
        status_row.columnconfigure(0, weight=1)
        self._status_lbl = tk.Label(status_row,
                                     text="Chargez les PDFs dans Configuration puis ouvrez cet onglet",
                                     fg="#888", font=("Segoe UI", 8),
                                     anchor="center", bg=status_row.cget("bg"))
        self._status_lbl.pack(fill="x")

        # ── Canvases dans un PanedWindow (ligne 3) ───────────────────
        paned = tk.PanedWindow(body, orient="horizontal", sashwidth=6,
                               sashrelief="raised", bg="#888", sashpad=1)
        paned.grid(row=3, column=0, columnspan=3, sticky="nsew", padx=2, pady=2)

        self._canvases   = []
        self._zoom       = [1.0, 1.0, 1.0]   # zoom par colonne
        self._imgs_orig  = [None, None, None] # images PIL originales (pleine résolution)
        self._fit_done   = [False, False, False]  # premier fit déjà effectué ?

        for col in range(3):
            cf = tk.Frame(paned, bg="#444")
            paned.add(cf, stretch="always", minsize=80)
            cf.columnconfigure(0, weight=1)
            cf.rowconfigure(0, weight=1)

            c = tk.Canvas(cf, bg="#333", cursor="arrow", highlightthickness=0)
            sv = ttk.Scrollbar(cf, orient="vertical",   command=c.yview)
            sh = ttk.Scrollbar(cf, orient="horizontal", command=c.xview)
            c.configure(yscrollcommand=sv.set, xscrollcommand=sh.set)
            sv.grid(row=0, column=1, sticky="ns")
            sh.grid(row=1, column=0, sticky="ew")
            c.grid(row=0, column=0, sticky="nsew")

            self._canvases.append(c)

            c.bind("<Enter>", lambda e, col=col: (
                setattr(self, '_hovered_col', col), self._canvases[col].focus_set()))
            c.bind("<Leave>", lambda e: setattr(self, '_hovered_col', None))
            # Resize → re-fit si pas encore zoomé manuellement
            c.bind("<Configure>", lambda e, col=col: self._on_canvas_resize(col))

        # Zoom : Ctrl+Molette sur la colonne survolée
        def _on_wheel(event):
            col = self._hovered_col
            if col is None: return
            ctrl = (event.state & 0x4) != 0   # Ctrl enfoncé
            if ctrl:
                # Zoom
                if event.delta:
                    factor = 1.1 if event.delta > 0 else 1/1.1
                else:
                    factor = 1/1.1 if event.num == 5 else 1.1
                self._zoom[col] = max(0.05, min(8.0, self._zoom[col] * factor))
                self._fit_done[col] = True   # zoom manuel → ne plus auto-fitter
                self._redraw_col(col)
            else:
                # Page suivante/précédente
                if event.delta:
                    direction = 1 if event.delta < 0 else -1
                else:
                    direction = 1 if event.num == 5 else -1
                if direction > 0: self._go_next(col)
                else:             self._go_prev(col)

        def _on_key(event):
            col = self._hovered_col
            if col is None: return
            if event.keysym in ("Right", "Down"):   self._go_next(col)
            elif event.keysym in ("Left", "Up"):    self._go_prev(col)
            elif event.keysym == "plus":
                self._zoom[col] = min(8.0, self._zoom[col] * 1.1)
                self._fit_done[col] = True; self._redraw_col(col)
            elif event.keysym == "minus":
                self._zoom[col] = max(0.05, self._zoom[col] / 1.1)
                self._fit_done[col] = True; self._redraw_col(col)
            elif event.keysym == "0":
                self._fit_done[col] = False; self._fit_col(col)

        for c in self._canvases:
            for seq in ("<MouseWheel>","<Button-4>","<Button-5>"):
                c.bind(seq, _on_wheel)
            for seq in ("<Right>","<Left>","<Up>","<Down>","<plus>","<minus>","<Key-0>"):
                c.bind(seq, _on_key)

    # ── Navigation ────────────────────────────
    def _clamp_ref(self, n): return max(0, min(n, self._ref_page_count - 1)) if self._ref_page_count else 0
    def _clamp_new(self, n): return max(0, min(n, self._new_page_count - 1)) if self._new_page_count else 0

    def _go_first(self, col):
        if col == 0:
            self._ref_page_idx = 0
        elif col == 2:
            self._new_page_idx = 0
        else:
            self._ref_page_idx = 0; self._new_page_idx = 0
        self._update_nav_labels(); self._trigger_calc()

    def _go_last(self, col):
        if col == 0:
            self._ref_page_idx = self._clamp_ref(self._ref_page_count - 1)
        elif col == 2:
            self._new_page_idx = self._clamp_new(self._new_page_count - 1)
        else:
            self._ref_page_idx = self._clamp_ref(self._ref_page_count - 1)
            self._new_page_idx = self._clamp_new(self._new_page_count - 1)
        self._update_nav_labels(); self._trigger_calc()

    def _go_prev(self, col):
        if col == 0:
            self._ref_page_idx = self._clamp_ref(self._ref_page_idx - 1)
        elif col == 2:
            self._new_page_idx = self._clamp_new(self._new_page_idx - 1)
        else:
            self._ref_page_idx = self._clamp_ref(self._ref_page_idx - 1)
            self._new_page_idx = self._clamp_new(self._new_page_idx - 1)
        self._update_nav_labels(); self._trigger_calc()

    def _go_next(self, col):
        if col == 0:
            self._ref_page_idx = self._clamp_ref(self._ref_page_idx + 1)
        elif col == 2:
            self._new_page_idx = self._clamp_new(self._new_page_idx + 1)
        else:
            self._ref_page_idx = self._clamp_ref(self._ref_page_idx + 1)
            self._new_page_idx = self._clamp_new(self._new_page_idx + 1)
        self._update_nav_labels(); self._trigger_calc()

    def _go_to(self, col, var):
        try:
            n = int(var.get()) - 1
            if col == 0:
                self._ref_page_idx = self._clamp_ref(n)
            elif col == 2:
                self._new_page_idx = self._clamp_new(n)
            else:
                self._ref_page_idx = self._clamp_ref(n)
                self._new_page_idx = self._clamp_new(n)
            self._update_nav_labels(); self._trigger_calc()
        except ValueError:
            pass
        var.set("")

    def _update_nav_labels(self):
        rn = self._ref_page_count or 1
        nn = self._new_page_count or 1
        self._lbl_ref.config( text=f"p. {self._ref_page_idx+1} / {rn}")
        self._lbl_new.config( text=f"p. {self._new_page_idx+1} / {nn}")
        self._lbl_diff.config(text=f"REF p.{self._ref_page_idx+1}  ↔  CAND p.{self._new_page_idx+1}")

    # ── Recalage intelligent ──────────────────
    def _word_bag(self, path: str, page_idx: int) -> dict:
        """Sac de mots de la page (depuis le cache doc)."""
        from collections import Counter
        try:
            doc  = self._get_doc(path)
            page = doc[page_idx]
            bag  = Counter(w.lower() for w in page.get_text("words", flags=0)
                           if isinstance(w, str) and len(w) > 1)
            return bag
        except Exception:
            return {}

    def _dice(self, bag_a: dict, bag_b: dict) -> float:
        if not bag_a or not bag_b: return 0.0
        from collections import Counter
        inter = sum((Counter(bag_a) & Counter(bag_b)).values())
        total = sum(bag_a.values()) + sum(bag_b.values())
        return 2 * inter / total if total else 0.0

    def _find_best_match(self, source_path: str, source_idx: int,
                          target_path: str, target_current: int,
                          target_count: int, window: int = 30) -> int:
        """
        Cherche dans target_path la page la plus similaire à source_path[source_idx]
        dans une fenêtre de ±window pages autour de target_current.
        Retourne l'index 0-based de la meilleure page.
        """
        bag_src = self._word_bag(source_path, source_idx)
        best_idx   = target_current
        best_score = -1.0
        lo = max(0, target_current - window)
        hi = min(target_count - 1, target_current + window)
        for j in range(lo, hi + 1):
            bag_j = self._word_bag(target_path, j)
            score = self._dice(bag_src, bag_j)
            if score > best_score:
                best_score = score
                best_idx   = j
        return best_idx

    def _align_to_ref(self):
        """Recale CAND sur REF : trouve la page CAND la plus proche de REF courante."""
        ref  = self._app._ref_var.get()
        new  = self._app._new_var.get()
        if not ref or not new: return
        if not self._ref_page_count or not self._new_page_count:
            self._refresh_page_counts()
        self._status_lbl.config(text="Recalage en cours…", fg="#888")
        self.after(10, lambda: self._do_align("ref"))

    def _align_to_new(self):
        """Recale REF sur CAND : trouve la page REF la plus proche de CAND courante."""
        ref  = self._app._ref_var.get()
        new  = self._app._new_var.get()
        if not ref or not new: return
        if not self._ref_page_count or not self._new_page_count:
            self._refresh_page_counts()
        self._status_lbl.config(text="Recalage en cours…", fg="#888")
        self.after(10, lambda: self._do_align("new"))

    def _do_align(self, anchor: str):
        """Effectue le recalage dans un thread pour ne pas bloquer l'UI."""
        ref = self._app._ref_var.get()
        new = self._app._new_var.get()
        def worker():
            try:
                if anchor == "ref":
                    # REF fixe → chercher meilleure page CAND
                    best = self._find_best_match(
                        ref, self._ref_page_idx,
                        new, self._new_page_idx, self._new_page_count)
                    self._new_page_idx = best
                else:
                    # CAND fixe → chercher meilleure page REF
                    best = self._find_best_match(
                        new, self._new_page_idx,
                        ref, self._ref_page_idx, self._ref_page_count)
                    self._ref_page_idx = best
                self.after(0, lambda: (self._update_nav_labels(), self._trigger_calc()))
            except Exception as ex:
                self.after(0, lambda: self._status_lbl.config(
                    text=f"Erreur recalage : {ex}", fg="#cc4444"))
        threading.Thread(target=worker, daemon=True).start()

    # ── Calcul diff ───────────────────────────
    def _trigger_calc(self, calc=True, side=None):
        """
        Lance le rendu/calcul.
        calc=False + side="ref"|"new" : re-render uniquement cette colonne sans diff.
        calc=True  : (re)calcul complet de la diff.
        """
        ref = self._app._ref_var.get()
        new = self._app._new_var.get()
        if not ref or not new or not PIL_OK:
            self._status_lbl.config(text="Chargez d'abord les deux PDFs", fg="#cc4444")
            return
        if not os.path.isfile(ref) or not os.path.isfile(new):
            self._status_lbl.config(text="Fichier introuvable", fg="#cc4444")
            return

        # Mettre à jour les comptages si nécessaire
        if not self._ref_page_count or not self._new_page_count:
            self._refresh_page_counts()
            self._update_nav_labels()

        # Annuler calcul précédent
        self._cancel_flag.set()
        if self._calc_thread and self._calc_thread.is_alive():
            self._calc_thread.join(timeout=0.3)
        self._cancel_flag.clear()

        self._status_lbl.config(text="Calcul en cours…", fg="#888")

        self._calc_thread = threading.Thread(
            target=self._calc_worker,
            args=(ref, new, self._ref_page_idx, self._new_page_idx, calc, side),
            daemon=True)
        self._calc_thread.start()

    def _calc_worker(self, ref_path, new_path, ref_idx, new_idx, do_diff, side):
        try:
            dpi        = self._app._dpi_var.get()
            tol_pix    = self._app._tol_pix_var.get()
            min_area   = self._app._min_area_var.get()
            shift_tol  = self._app._shift_tol_var.get()
            tol_pt     = self._app._tol_txt_var.get() * 72 / max(dpi, 1)
            ex_zones   = list(self._app._exclude_zones)
            scale_pdf  = dpi / 72.0
            run_text   = self._app._mode_text_var.get()
            run_image  = self._app._mode_image_var.get()

            if self._cancel_flag.is_set(): return

            ref_doc = self._get_doc(ref_path)
            new_doc = self._get_doc(new_path)

            ref_page = ref_doc[ref_idx]
            new_page = new_doc[new_idx]

            ref_img = self._get_pix(ref_path, ref_idx, dpi)
            new_img = self._get_pix(new_path, new_idx, dpi)

            if self._cancel_flag.is_set(): return

            if not do_diff:
                
                if side == "ref":
                    self._app.after(0, lambda: self._display_single(0, ref_img))
                elif side == "new":
                    self._app.after(0, lambda: self._display_single(2, new_img))
                self._app.after(0, lambda: self._status_lbl.config(
                    text=f"REF p.{ref_idx+1}  ↔  CAND p.{new_idx+1}", fg="#2a7a2a"))
                return

            # ── Extraction texte (toujours nécessaire pour le masquage Phase 2) ──
            def get_blocks(page):
                lines = {}
                for w in page.get_text("words"):
                    x0, y0, x1, y1, text, bn, ln, _ = w
                    key = (bn, ln)
                    if key not in lines:
                        lines[key] = {"parts": [text], "x0": x0, "y0": y0, "x1": x1, "y1": y1}
                    else:
                        d = lines[key]
                        d["parts"].append(text)
                        d["x0"] = min(d["x0"], x0); d["y0"] = min(d["y0"], y0)
                        d["x1"] = max(d["x1"], x1); d["y1"] = max(d["y1"], y1)
                out = []
                for d in lines.values():
                    txt = " ".join(d["parts"]).strip()
                    if not txt: continue
                    excluded = any(
                        not (d["x1"] < ex_x0 or d["x0"] > ex_x1 or d["y1"] < ex_y0 or d["y0"] > ex_y1)
                        for ex_x0, ex_y0, ex_x1, ex_y1 in ex_zones)
                    if not excluded:
                        out.append((txt, d["x0"], d["y0"], d["x1"], d["y1"]))
                return out

            ref_blocks = get_blocks(ref_page)
            new_blocks = get_blocks(new_page)

            if self._cancel_flag.is_set(): return

            # ── Phase 1 : matching texte ─────
            missing_ref  = []
            added_new    = []
            moved_ref    = []
            matched_ref_blocks = []   # blocs REF qui ont trouvé un match CAND
            matched_new_blocks = []   # blocs CAND correspondants

            if run_text:
                matched_new = set()
                for rb in ref_blocks:
                    txt, rx0, ry0, rx1, ry1 = rb
                    cands = [(i, b) for i, b in enumerate(new_blocks)
                             if i not in matched_new and b[0] == txt]
                    if cands:
                        best_idx, best = min(cands, key=lambda c: abs(c[1][1]-rx0)+abs(c[1][2]-ry0))
                        matched_new.add(best_idx)
                        matched_ref_blocks.append(rb)
                        matched_new_blocks.append(best)
                        if abs(rx0 - best[1]) > tol_pt or abs(ry0 - best[2]) > tol_pt:
                            moved_ref.append(rb)
                    else:
                        missing_ref.append(rb)
                for i, b in enumerate(new_blocks):
                    if i not in matched_new:
                        added_new.append(b)
            else:
                # Phase texte désactivée : tous les blocs sont "matchés" pour le masquage
                matched_ref_blocks = ref_blocks
                matched_new_blocks = new_blocks

            if self._cancel_flag.is_set(): return

            # ── Phase 2 : diff visuelle ──────
            vis_boxes = []
            new_img_r = new_img

            if run_image:
                def mask_zones(img, blocks):
                    draw = ImageDraw.Draw(img)
                    for _, bx0, by0, bx1, by1 in blocks:
                        draw.rectangle([int(bx0*scale_pdf)-2, int(by0*scale_pdf)-4,
                                        int(bx1*scale_pdf)+2, int(by1*scale_pdf)+4], fill=(255,255,255))
                    return img

                # N'effacer que les blocs qui ont matché des deux côtés :
                # les blocs manquants/ajoutés pourraient couvrir des éléments
                # graphiques (codes-barres, logos) présents d'un seul côté.
                ref_masked = mask_zones(ref_img.copy(), matched_ref_blocks)
                new_masked = mask_zones(new_img.copy(), matched_new_blocks)
                for img_m in [ref_masked, new_masked]:
                    draw = ImageDraw.Draw(img_m)
                    for ex_x0, ex_y0, ex_x1, ex_y1 in ex_zones:
                        draw.rectangle([int(ex_x0*scale_pdf), int(ex_y0*scale_pdf),
                                        int(ex_x1*scale_pdf), int(ex_y1*scale_pdf)], fill=(255,255,255))

                if new_masked.size != ref_masked.size:
                    new_masked = new_masked.resize(ref_masked.size, Image.LANCZOS)
                    new_img_r  = new_img.resize(ref_img.size, Image.LANCZOS)

                ref_arr   = np.array(ref_masked, dtype=np.int16)
                new_arr   = np.array(new_masked, dtype=np.int16)
                diff_mask = np.abs(ref_arr - new_arr).max(axis=2) > tol_pix

                MERGE_GAP = 15
                def dilate(arr, gap, axis):
                    padded = np.pad(arr.astype(np.int8),
                                    [(gap,gap) if i==axis else (0,0) for i in range(2)],
                                    constant_values=0)
                    cs = np.cumsum(padded, axis=axis)
                    h, w = arr.shape
                    return ((cs[:, gap*2:gap*2+w] - cs[:, :w]) > 0) if axis==1 \
                           else ((cs[gap*2:gap*2+h,:] - cs[:h,:]) > 0)

                if diff_mask.any():
                    dilated = dilate(dilate(diff_mask, MERGE_GAP, 1), MERGE_GAP, 0)
                    row_ch = np.diff(dilated.any(axis=1).astype(np.int8), prepend=0, append=0)
                    for ys, ye in zip(np.where(row_ch==1)[0], np.where(row_ch==-1)[0]):
                        col_ch = np.diff(dilated[ys:ye].any(axis=0).astype(np.int8), prepend=0, append=0)
                        for xs, xe in zip(np.where(col_ch==1)[0], np.where(col_ch==-1)[0]):
                            if int(diff_mask[ys:ye, xs:xe].sum()) < min_area: continue
                            if shift_tol > 0:
                                h2, w2 = ref_arr.shape[:2]
                                rz = ref_arr[ys:ye+1, xs:xe+1]
                                min_sc = float('inf')
                                for dy2 in range(-shift_tol, shift_tol+1):
                                    for dx2 in range(-shift_tol, shift_tol+1):
                                        ny0,ny1 = ys+dy2, ye+dy2+1; nx0,nx1 = xs+dx2, xe+dx2+1
                                        if ny0<0 or nx0<0 or ny1>h2 or nx1>w2: continue
                                        nz = new_arr[ny0:ny1, nx0:nx1]
                                        if nz.shape != rz.shape: continue
                                        sc = float(np.abs(rz-nz).mean())
                                        if sc < min_sc: min_sc = sc
                                        if min_sc == 0: break
                                    else: continue
                                    break
                                if min_sc < tol_pix: continue
                            vis_boxes.append((int(xs), int(ys), int(xe-1), int(ye-1)))

            if self._cancel_flag.is_set(): return

            # ── Composition DIFF ─────────────
            mid_img = ref_img.convert("RGBA")
            overlay = Image.new("RGBA", mid_img.size, (0,0,0,0))
            draw    = ImageDraw.Draw(overlay)
            for _, bx0, by0, bx1, by1 in missing_ref:
                draw.rectangle([int(bx0*scale_pdf), int(by0*scale_pdf),
                                int(bx1*scale_pdf), int(by1*scale_pdf)], fill=DIFF_REF_FILL)
            for _, bx0, by0, bx1, by1 in moved_ref:
                draw.rectangle([int(bx0*scale_pdf), int(by0*scale_pdf),
                                int(bx1*scale_pdf), int(by1*scale_pdf)], fill=(255,200,0,90))
            for _, bx0, by0, bx1, by1 in added_new:
                draw.rectangle([int(bx0*scale_pdf), int(by0*scale_pdf),
                                int(bx1*scale_pdf), int(by1*scale_pdf)], fill=DIFF_NEW_FILL)
            for bx0, by0, bx1, by1 in vis_boxes:
                draw.rectangle([bx0,by0,bx1,by1], fill=DIFF_VIS_FILL,
                               outline=(80,50,200,220), width=2)
            mid_img = Image.alpha_composite(mid_img, overlay).convert("RGB")

            
            if self._cancel_flag.is_set(): return

            modes = []
            if run_text:  modes.append("texte")
            if run_image: modes.append("image")
            mode_str = "+".join(modes) if modes else "aucun mode actif"
            status = (f"REF p.{ref_idx+1} ↔ CAND p.{new_idx+1}  [{mode_str}] — "
                      f"{len(missing_ref)} manquant(s) | {len(added_new)} ajouté(s) | "
                      f"{len(moved_ref)} déplacé(s) | {len(vis_boxes)} diff(s) visuelle(s)")
            self._app.after(0, lambda: self._display_images(ref_img, mid_img, new_img_r, status))

        except Exception:
            import traceback
            msg = traceback.format_exc().splitlines()[-1]
            self._app.after(0, lambda m=msg: self._status_lbl.config(text=f"Erreur : {m}", fg="#cc4444"))

    def _fit_col(self, col):
        """Calcule et applique le zoom fit-to-canvas pour la colonne col."""
        img = self._imgs_orig[col]
        if img is None: return
        c = self._canvases[col]
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 10 or ch < 10: return   # canvas pas encore rendu
        self._zoom[col] = min(cw / img.width, ch / img.height)
        self._redraw_col(col)

    def _redraw_col(self, col):
        """Redessine la colonne col avec le zoom courant."""
        img = self._imgs_orig[col]
        if img is None: return
        z = self._zoom[col]
        new_w = max(1, int(img.width  * z))
        new_h = max(1, int(img.height * z))
        resized = img.resize((new_w, new_h), Image.LANCZOS if z < 1.0 else Image.NEAREST)
        tk_img = ImageTk.PhotoImage(resized)
        c = self._canvases[col]
        c.delete("all")
        c.create_image(0, 0, anchor="nw", image=tk_img)
        c.configure(scrollregion=(0, 0, new_w, new_h))
        # Stocker la référence (anti-GC)
        if col == 0: self._tk_ref = tk_img
        elif col == 1: self._tk_mid = tk_img
        else: self._tk_new = tk_img

    def _on_canvas_resize(self, col):
        """Appelé lors du resize de la fenêtre — refit si pas de zoom manuel."""
        if not self._fit_done[col]:
            self._fit_col(col)

    def _display_single(self, col, img):
        """Affiche une image dans une seule colonne."""
        self._imgs_orig[col] = img
        self._fit_done[col]  = False   # reset → fit auto
        self._fit_col(col)

    def _display_images(self, ref_img, mid_img, new_img, status):
        imgs = [ref_img, mid_img, new_img]
        for col, img in enumerate(imgs):
            self._imgs_orig[col] = img
            self._fit_done[col]  = False   # reset → fit auto
            self._fit_col(col)
        self._status_lbl.config(text=status, fg="#2a7a2a")


# ─────────────────────────────────────────────
#  ONGLET 3 — ZONES À EXCLURE (Zone Selector intégré)
# ─────────────────────────────────────────────
class TabZones(ttk.Frame):
    """
    Sélecteur visuel de zones à exclure.
    - Dessiner  : clic-glisser sur zone vide
    - Déplacer  : clic-glisser à l'intérieur d'une zone existante
    - Redimensionner : clic-glisser sur un bord/coin (±8 px)
    - Éditer    : clic droit → dialogue coordonnées + mots-clés
    """

    HANDLE_PX = 8    # tolérance détection bord en pixels canvas

    def __init__(self, parent, app):
        super().__init__(parent)
        self._app      = app
        self._doc      = None
        self._pdf_src  = "ref"
        self._page_idx = 0
        self._scale    = 1.0
        self._tk_img   = None
        self._page_w_px = 0
        self._page_h_px = 0

        # État interaction
        self._draw_mode   = "zone"   # "zone" | "page_rule"
        self._action      = None     # None | "draw" | "move" | "resize"
        self._hit_idx     = None     # index zone/règle sous le curseur
        self._hit_list    = None     # "zone" | "rule"
        self._hit_handle  = None     # "nw"|"n"|"ne"|"e"|"se"|"s"|"sw"|"w"|"body"
        self._drag_start_canvas = None   # (cx, cy) au ButtonPress
        self._drag_start_pdf    = None   # (x0,y0,x1,y1) de la zone au début du drag
        self._draw_start  = None
        self._draw_rect   = None

        self._build()

    # ── Construction UI ───────────────────────
    def _build(self):
        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(fill="x", side="top")

        ttk.Label(bar, text="Afficher :").pack(side="left")
        self._src_var = tk.StringVar(value="ref")
        ttk.Radiobutton(bar, text="Référence", variable=self._src_var,
                         value="ref", command=lambda: self._load_pdf("ref")).pack(side="left", padx=4)
        ttk.Radiobutton(bar, text="Candidat",  variable=self._src_var,
                         value="new", command=lambda: self._load_pdf("new")).pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Button(bar, text="◀", width=2, command=self._prev_page).pack(side="left")
        self._page_lbl = ttk.Label(bar, text="Page 1/1", width=12, anchor="center")
        self._page_lbl.pack(side="left")
        ttk.Button(bar, text="▶", width=2, command=self._next_page).pack(side="left")

        ttk.Label(bar, text="  Aller à :").pack(side="left")
        self._goto_var = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self._goto_var, width=5)
        e.pack(side="left", padx=2)
        e.bind("<Return>", lambda _: self._goto_page())
        ttk.Button(bar, text="OK", width=3, command=self._goto_page).pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Label(bar, text="Mode :").pack(side="left")
        self._mode_var = tk.StringVar(value="zone")
        ttk.Radiobutton(bar, text="Zone d'exclusion", variable=self._mode_var,
                         value="zone", command=lambda: self._set_mode("zone")).pack(side="left", padx=4)
        ttk.Radiobutton(bar, text="Exclusion de pages", variable=self._mode_var,
                         value="page_rule", command=lambda: self._set_mode("page_rule")).pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Label(bar, text="Zoom :").pack(side="left")
        self._zoom_var = tk.IntVar(value=100)
        for z in (75, 100, 150, 200):
            ttk.Radiobutton(bar, text=f"{z}%", variable=self._zoom_var,
                             value=z, command=self._apply_zoom).pack(side="left")

        ttk.Button(bar, text="⟳ Recharger PDF", command=self._reload_pdf).pack(side="right", padx=4)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        cf = ttk.Frame(body)
        cf.pack(side="left", fill="both", expand=True)

        self._canvas = tk.Canvas(cf, bg="#666", cursor="crosshair")
        hbar = ttk.Scrollbar(cf, orient="horizontal", command=self._canvas.xview)
        vbar = ttk.Scrollbar(cf, orient="vertical",   command=self._canvas.yview)
        self._canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        hbar.pack(side="bottom", fill="x")
        self._canvas.pack(fill="both", expand=True)

        self._canvas.bind("<Motion>",          self._on_motion)
        self._canvas.bind("<ButtonPress-1>",   self._on_mouse_down)
        self._canvas.bind("<B1-Motion>",       self._on_mouse_move)
        self._canvas.bind("<ButtonRelease-1>", self._on_mouse_up)
        self._canvas.bind("<Button-3>",        self._on_right_click)

        # Panneau droit
        right = ttk.Frame(body, width=280, padding=6)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        ttk.Label(right, text="Zones d'exclusion de comparaison",
                  font=("Segoe UI", 9, "bold"), foreground=ZONE_COLOR).pack(anchor="w")
        ttk.Label(right, text="(s'appliquent sur toutes les pages)",
                  font=("Segoe UI", 8), foreground="#888").pack(anchor="w")

        self._zone_list = tk.Listbox(right, height=8, selectmode="single", font=("Consolas", 8))
        self._zone_list.pack(fill="x", pady=(4, 0))
        self._zone_list.bind("<<ListboxSelect>>", self._on_zone_select)
        ttk.Button(right, text="🗑 Supprimer zone sélectionnée", command=self._del_zone).pack(fill="x", pady=2)
        ttk.Button(right, text="🗑 Tout effacer",                command=self._clear_zones).pack(fill="x", pady=(0, 8))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)

        ttk.Label(right, text="Règles d'exclusion de pages",
                  font=("Segoe UI", 9, "bold"), foreground=PAGE_EX_COLOR).pack(anchor="w")
        ttk.Label(right, text="Zone + mots-clés → page ignorée si match",
                  font=("Segoe UI", 8), foreground="#888").pack(anchor="w")

        self._rule_list = tk.Listbox(right, height=7, selectmode="single", font=("Consolas", 8))
        self._rule_list.pack(fill="x", pady=(4, 0))
        self._rule_list.bind("<<ListboxSelect>>", self._on_rule_select)

        kw_frame = ttk.Frame(right); kw_frame.pack(fill="x", pady=4)
        self._kw_lbl = ttk.Label(kw_frame, text="Mots-clés (séparés par ;) :")
        self._kw_lbl.pack(anchor="w")
        self._kw_var = tk.StringVar()
        ttk.Entry(kw_frame, textvariable=self._kw_var).pack(fill="x")
        self._kw_hint = ttk.Label(kw_frame,
                  text="Tracez un rectangle puis saisissez les mots-clés",
                  font=("Segoe UI", 8), foreground="#888", wraplength=260)
        self._kw_hint.pack(anchor="w")

        self._btn_apply_kw = ttk.Button(right,
                  text="✏  Appliquer les mots-clés à la règle sélectionnée",
                  command=self._apply_keywords, state="disabled")
        self._btn_apply_kw.pack(fill="x", pady=(0, 4))
        ttk.Button(right, text="🗑 Supprimer règle sélectionnée", command=self._del_rule).pack(fill="x", pady=2)
        ttk.Button(right, text="🗑 Tout effacer",                 command=self._clear_rules).pack(fill="x", pady=(0, 8))

        self._status = tk.StringVar(value="Tracez un rectangle — clic droit sur une zone pour l'éditer")
        ttk.Label(self, textvariable=self._status, foreground="#555",
                  font=("Segoe UI", 8)).pack(side="bottom", anchor="w", padx=8, pady=2)

        self._refresh_lists()

    # ── PDF ───────────────────────────────────
    def on_tab_activated(self):
        if self._doc is None:
            self._load_pdf(self._pdf_src)

    def _reload_pdf(self):
        self._load_pdf(self._pdf_src)

    def _load_pdf(self, src: str):
        if self._doc:
            try: self._doc.close()
            except Exception: pass
            self._doc = None
        path = self._app._ref_var.get() if src == "ref" else self._app._new_var.get()
        if not path:
            self._status.set("Sélectionnez d'abord les PDFs dans l'onglet Configuration."); return
        if not os.path.isfile(path):
            self._status.set(f"Fichier introuvable : {path}"); return
        try:
            self._doc = fitz.open(path); self._pdf_src = src
            self._page_idx = 0; self._render_page()
        except Exception as ex:
            self._status.set(f"Erreur ouverture PDF : {ex}")

    def _render_page(self):
        if not self._doc: return
        n = self._doc.page_count
        self._page_lbl.config(text=f"Page {self._page_idx+1}/{n}")
        zoom = self._zoom_var.get() / 100
        scale = RENDER_DPI_SEL * zoom / 72.0
        self._scale = scale
        page = self._doc[self._page_idx]
        pix  = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB)
        img  = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        self._page_w_px = pix.width
        self._page_h_px = pix.height
        self._tk_img = ImageTk.PhotoImage(img)
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor="nw", image=self._tk_img, tags="page")
        self._canvas.configure(scrollregion=(0, 0, pix.width, pix.height))
        self._draw_existing_zones()

    def _draw_existing_zones(self):
        s = self._scale
        for i, (x0, y0, x1, y1) in enumerate(self._app._exclude_zones):
            px0, py0, px1, py1 = x0*s, y0*s, x1*s, y1*s
            self._canvas.create_rectangle(px0, py0, px1, py1,
                                           outline=ZONE_COLOR, width=2, dash=(4,4),
                                           tags=f"zone_{i}")
            self._canvas.create_text((px0+px1)/2, (py0+py1)/2,
                                      text=f"Z{i+1}", fill=ZONE_COLOR,
                                      font=("Segoe UI", 8, "bold"), tags=f"zone_{i}_lbl")
        for i, rule in enumerate(self._app._page_ex_rules):
            x0, y0, x1, y1 = rule["zone"]
            px0, py0, px1, py1 = x0*s, y0*s, x1*s, y1*s
            kw = ", ".join(rule.get("keywords", []))
            self._canvas.create_rectangle(px0, py0, px1, py1,
                                           outline=PAGE_EX_COLOR, width=2, dash=(6,2),
                                           tags=f"rule_{i}")
            self._canvas.create_text((px0+px1)/2, py0-6,
                                      text=f"P{i+1}: {kw[:30]}", fill=PAGE_EX_COLOR,
                                      font=("Segoe UI", 7), tags=f"rule_{i}_lbl")

    # ── Navigation ────────────────────────────
    def _prev_page(self):
        if self._doc and self._page_idx > 0:
            self._page_idx -= 1; self._render_page()

    def _next_page(self):
        if self._doc and self._page_idx < self._doc.page_count - 1:
            self._page_idx += 1; self._render_page()

    def _goto_page(self):
        try:
            n = int(self._goto_var.get()) - 1
            if self._doc and 0 <= n < self._doc.page_count:
                self._page_idx = n; self._render_page()
        except ValueError: pass
        self._goto_var.set("")

    def _apply_zoom(self): self._render_page()

    def _set_mode(self, mode):
        self._draw_mode = mode
        self._status.set("Mode : Zone d'exclusion — tracez un rectangle (rouge)"
                         if mode == "zone" else
                         "Mode : Exclusion de pages — tracez un rectangle puis saisissez les mots-clés (orange)")

    # ── Hit-test ──────────────────────────────
    def _canvas_xy(self, event):
        """Coordonnées canvas réelles (tenant compte du scroll)."""
        return self._canvas.canvasx(event.x), self._canvas.canvasy(event.y)

    def _canvas_to_pdf(self, cx, cy):
        return cx / self._scale, cy / self._scale

    def _get_handle(self, cx, cy, px0, py0, px1, py1):
        """
        Retourne le handle touché ('nw','n','ne','e','se','s','sw','w','body')
        ou None si hors de la zone.
        """
        H = self.HANDLE_PX
        in_x = px0 - H <= cx <= px1 + H
        in_y = py0 - H <= cy <= py1 + H
        if not in_x or not in_y:
            return None
        on_left   = abs(cx - px0) <= H
        on_right  = abs(cx - px1) <= H
        on_top    = abs(cy - py0) <= H
        on_bottom = abs(cy - py1) <= H

        if on_top    and on_left:  return "nw"
        if on_top    and on_right: return "ne"
        if on_bottom and on_left:  return "sw"
        if on_bottom and on_right: return "se"
        if on_top:                 return "n"
        if on_bottom:              return "s"
        if on_left:                return "w"
        if on_right:               return "e"
        # Intérieur strict
        if px0 <= cx <= px1 and py0 <= cy <= py1:
            return "body"
        return None

    def _hit_test(self, cx, cy):
        """
        Cherche quelle zone/règle est touchée.
        Retourne (liste, index, handle) ou (None, None, None).
        Priorité : coins > bords > body ; zones avant règles.
        """
        s = self._scale
        best = (None, None, None)
        priority = {"nw":0,"ne":0,"sw":0,"se":0,"n":1,"s":1,"e":1,"w":1,"body":2}

        for i, (x0, y0, x1, y1) in enumerate(self._app._exclude_zones):
            h = self._get_handle(cx, cy, x0*s, y0*s, x1*s, y1*s)
            if h and (best[2] is None or priority[h] < priority.get(best[2], 99)):
                best = ("zone", i, h)

        for i, rule in enumerate(self._app._page_ex_rules):
            x0, y0, x1, y1 = rule["zone"]
            h = self._get_handle(cx, cy, x0*s, y0*s, x1*s, y1*s)
            if h and (best[2] is None or priority[h] < priority.get(best[2], 99)):
                best = ("rule", i, h)

        return best

    def _cursor_for_handle(self, handle):
        return {
            "nw": "top_left_corner",  "ne": "top_right_corner",
            "sw": "bottom_left_corner","se": "bottom_right_corner",
            "n":  "top_side",         "s":  "bottom_side",
            "w":  "left_side",        "e":  "right_side",
            "body": "fleur",
        }.get(handle, "crosshair")

    # ── Événements souris ─────────────────────
    def _on_motion(self, event):
        """Mise à jour du curseur selon la position."""
        if self._action: return   # déjà en drag
        cx, cy = self._canvas_xy(event)
        lst, idx, handle = self._hit_test(cx, cy)
        if handle:
            self._canvas.config(cursor=self._cursor_for_handle(handle))
        else:
            self._canvas.config(cursor="crosshair")

    def _on_mouse_down(self, event):
        cx, cy = self._canvas_xy(event)
        lst, idx, handle = self._hit_test(cx, cy)

        if handle:
            # Édition d'une zone existante
            self._action     = "move" if handle == "body" else "resize"
            self._hit_list   = lst
            self._hit_idx    = idx
            self._hit_handle = handle
            self._drag_start_canvas = (cx, cy)
            if lst == "zone":
                self._drag_start_pdf = tuple(self._app._exclude_zones[idx])
            else:
                self._drag_start_pdf = tuple(self._app._page_ex_rules[idx]["zone"])
        else:
            # Dessin d'une nouvelle zone
            self._action = "draw"
            self._draw_start = (cx, cy)
            color = ZONE_COLOR if self._draw_mode == "zone" else PAGE_EX_COLOR
            self._draw_rect = self._canvas.create_rectangle(
                cx, cy, cx, cy, outline=color, width=2, dash=(4,4))

    def _on_mouse_move(self, event):
        cx, cy = self._canvas_xy(event)

        if self._action == "draw":
            if self._draw_rect and self._draw_start:
                x0, y0 = self._draw_start
                ncx = max(0, min(cx, self._page_w_px-1)) if self._page_w_px else cx
                ncy = max(0, min(cy, self._page_h_px-1)) if self._page_h_px else cy
                self._canvas.coords(self._draw_rect, x0, y0, ncx, ncy)

        elif self._action in ("move", "resize"):
            scx, scy = self._drag_start_canvas
            dx_pdf = (cx - scx) / self._scale
            dy_pdf = (cy - scy) / self._scale
            ox0, oy0, ox1, oy1 = self._drag_start_pdf

            if self._action == "move":
                nx0 = ox0 + dx_pdf; ny0 = oy0 + dy_pdf
                nx1 = ox1 + dx_pdf; ny1 = oy1 + dy_pdf
            else:
                nx0, ny0, nx1, ny1 = ox0, oy0, ox1, oy1
                h = self._hit_handle
                if "w" in h: nx0 = ox0 + dx_pdf
                if "e" in h: nx1 = ox1 + dx_pdf
                if "n" in h: ny0 = oy0 + dy_pdf
                if "s" in h: ny1 = oy1 + dy_pdf

            # Clamp page
            pw = self._page_w_px / self._scale
            ph = self._page_h_px / self._scale
            nx0 = max(0, min(nx0, pw)); ny0 = max(0, min(ny0, ph))
            nx1 = max(0, min(nx1, pw)); ny1 = max(0, min(ny1, ph))

            # Mise à jour visuelle
            s = self._scale
            if self._hit_list == "zone":
                self._app._exclude_zones[self._hit_idx] = (
                    round(nx0,1), round(ny0,1), round(nx1,1), round(ny1,1))
            else:
                self._app._page_ex_rules[self._hit_idx]["zone"] = (
                    round(nx0,1), round(ny0,1), round(nx1,1), round(ny1,1))
            self._render_page()

    def _on_mouse_up(self, event):
        if self._action == "draw":
            self._finish_draw(event)
        elif self._action in ("move", "resize"):
            self._refresh_lists()
            self._update_zones_label()
            self._status.set(f"Zone mise à jour")
        self._action = self._hit_idx = self._hit_list = self._hit_handle = None
        self._draw_start = self._draw_rect = None
        self._drag_start_canvas = self._drag_start_pdf = None

    def _finish_draw(self, event):
        """Finalise le dessin d'un nouveau rectangle."""
        if not self._draw_start: return
        cx, cy = self._canvas_xy(event)
        x0c, y0c = self._draw_start
        x1c, y1c = cx, cy
        if x0c > x1c: x0c, x1c = x1c, x0c
        if y0c > y1c: y0c, y1c = y1c, y0c
        if self._page_w_px and self._page_h_px:
            x0c = max(0, min(x0c, self._page_w_px-1)); y0c = max(0, min(y0c, self._page_h_px-1))
            x1c = max(0, min(x1c, self._page_w_px-1)); y1c = max(0, min(y1c, self._page_h_px-1))
        if abs(x1c-x0c) < 5 or abs(y1c-y0c) < 5:
            if self._draw_rect: self._canvas.delete(self._draw_rect)
            return
        px0, py0 = self._canvas_to_pdf(x0c, y0c)
        px1, py1 = self._canvas_to_pdf(x1c, y1c)
        zone = (round(px0,1), round(py0,1), round(px1,1), round(py1,1))

        if self._draw_mode == "zone":
            self._app._exclude_zones.append(zone)
            self._status.set(f"Zone ajoutée : {zone}")
        else:
            kw_raw = self._kw_var.get().strip()
            if not kw_raw:
                messagebox.showwarning("Mots-clés manquants",
                    "Saisissez au moins un mot-clé avant de tracer la zone.")
                if self._draw_rect: self._canvas.delete(self._draw_rect)
                return
            keywords = [k.strip() for k in kw_raw.split(";") if k.strip()]
            self._app._page_ex_rules.append({"zone": zone, "keywords": keywords})
            self._kw_var.set("")
            self._status.set(f"Règle ajoutée : {keywords} dans {zone}")

        self._refresh_lists()
        self._render_page()
        self._update_zones_label()

    # ── Clic droit : dialogue d'édition ───────
    def _on_right_click(self, event):
        cx, cy = self._canvas_xy(event)
        lst, idx, handle = self._hit_test(cx, cy)
        if lst is None:
            return
        self._open_edit_dialog(lst, idx, event.x_root, event.y_root)

    def _open_edit_dialog(self, lst, idx, root_x, root_y):
        """Dialogue modal léger pour éditer les coordonnées (et mots-clés si règle)."""
        if lst == "zone":
            zone = self._app._exclude_zones[idx]
            title = f"Éditer zone Z{idx+1}"
        else:
            zone = self._app._page_ex_rules[idx]["zone"]
            title = f"Éditer règle P{idx+1}"

        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.geometry(f"+{root_x+10}+{root_y+10}")

        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both", expand=True)

        vars_ = []
        for i, (lbl, val) in enumerate([("X0 (pt)", zone[0]), ("Y0 (pt)", zone[1]),
                                         ("X1 (pt)", zone[2]), ("Y1 (pt)", zone[3])]):
            ttk.Label(frm, text=lbl).grid(row=i, column=0, sticky="w", pady=2, padx=(0,8))
            v = tk.StringVar(value=str(val))
            ttk.Entry(frm, textvariable=v, width=10).grid(row=i, column=1, sticky="ew")
            vars_.append(v)

        kw_var = None
        if lst == "rule":
            ttk.Separator(frm, orient="horizontal").grid(row=4, column=0, columnspan=2,
                                                          sticky="ew", pady=6)
            ttk.Label(frm, text="Mots-clés (séparés par ;) :").grid(
                row=5, column=0, columnspan=2, sticky="w")
            kw_var = tk.StringVar(value=" ; ".join(
                self._app._page_ex_rules[idx].get("keywords", [])))
            ttk.Entry(frm, textvariable=kw_var, width=28).grid(
                row=6, column=0, columnspan=2, sticky="ew", pady=(2, 8))

        def _apply():
            try:
                new_zone = tuple(round(float(v.get()), 1) for v in vars_)
            except ValueError:
                messagebox.showerror("Valeur invalide", "Les coordonnées doivent être des nombres.")
                return
            x0, y0, x1, y1 = new_zone
            if x0 >= x1 or y0 >= y1:
                messagebox.showerror("Coordonnées invalides", "X0 < X1 et Y0 < Y1 requis.")
                return
            if lst == "zone":
                self._app._exclude_zones[idx] = new_zone
            else:
                self._app._page_ex_rules[idx]["zone"] = new_zone
                if kw_var:
                    kws = [k.strip() for k in kw_var.get().split(";") if k.strip()]
                    if not kws:
                        messagebox.showerror("Mots-clés vides", "Au moins un mot-clé requis.")
                        return
                    self._app._page_ex_rules[idx]["keywords"] = kws
            self._refresh_lists()
            self._render_page()
            self._update_zones_label()
            self._status.set(f"{'Zone' if lst == 'zone' else 'Règle'} {idx+1} mise à jour")
            dlg.destroy()

        def _delete():
            if messagebox.askyesno("Supprimer", f"Supprimer {'Z' if lst=='zone' else 'P'}{idx+1} ?",
                                   parent=dlg):
                dlg.destroy()
                if lst == "zone": self._app._exclude_zones.pop(idx)
                else:             self._app._page_ex_rules.pop(idx)
                self._refresh_lists(); self._render_page(); self._update_zones_label()

        btn_row = ttk.Frame(frm); btn_row.grid(row=7, column=0, columnspan=2, pady=(4,0))
        ttk.Button(btn_row, text="✔ Appliquer", command=_apply,
                   style="Accent.TButton").pack(side="left", padx=4)
        ttk.Button(btn_row, text="🗑 Supprimer", command=_delete).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Annuler", command=dlg.destroy).pack(side="left", padx=4)

    # ── Listes ────────────────────────────────
    def _on_zone_select(self, event):
        sel = self._zone_list.curselection()
        if not sel: return
        # Mettre en évidence la zone dans le canvas
        idx = sel[0]
        self._canvas.itemconfig(f"zone_{idx}", width=3)

    def _update_zones_label(self):
        n_zones = len(self._app._exclude_zones)
        n_rules = len(self._app._page_ex_rules)
        parts = []
        if n_zones: parts.append(f"{n_zones} zone(s) ignorée(s)")
        if n_rules: parts.append(f"{n_rules} règle(s) d'exclusion")
        self._app._zones_lbl.config(text=" | ".join(parts) if parts else "")

    def _refresh_lists(self):
        self._zone_list.delete(0, "end")
        for i, (x0, y0, x1, y1) in enumerate(self._app._exclude_zones):
            self._zone_list.insert("end", f"Z{i+1}  ({x0:.0f},{y0:.0f}) → ({x1:.0f},{y1:.0f})")
        self._rule_list.delete(0, "end")
        for i, rule in enumerate(self._app._page_ex_rules):
            x0, y0, x1, y1 = rule["zone"]
            kw = " ; ".join(rule.get("keywords", []))
            self._rule_list.insert("end", f"P{i+1}  [{kw[:25]}] @ ({x0:.0f},{y0:.0f})→({x1:.0f},{y1:.0f})")

    def _del_zone(self):
        sel = self._zone_list.curselection()
        if sel:
            self._app._exclude_zones.pop(sel[0])
            self._refresh_lists(); self._render_page(); self._update_zones_label()

    def _clear_zones(self):
        if messagebox.askyesno("Confirmer", "Effacer toutes les zones d'exclusion ?"):
            self._app._exclude_zones.clear()
            self._refresh_lists(); self._render_page(); self._update_zones_label()

    def _on_rule_select(self, event):
        sel = self._rule_list.curselection()
        if not sel: self._btn_apply_kw.config(state="disabled"); return
        rule = self._app._page_ex_rules[sel[0]]
        self._kw_var.set(" ; ".join(rule.get("keywords", [])))
        self._kw_lbl.config(text=f"Mots-clés règle P{sel[0]+1} (séparés par ;) :")
        self._btn_apply_kw.config(state="normal")

    def _apply_keywords(self):
        sel = self._rule_list.curselection()
        if not sel: return
        kw_raw = self._kw_var.get().strip()
        if not kw_raw:
            messagebox.showwarning("Mots-clés vides", "Saisissez au moins un mot-clé."); return
        keywords = [k.strip() for k in kw_raw.split(";") if k.strip()]
        self._app._page_ex_rules[sel[0]]["keywords"] = keywords
        self._kw_var.set(""); self._kw_lbl.config(text="Mots-clés (séparés par ;) :")
        self._btn_apply_kw.config(state="disabled")
        self._refresh_lists(); self._render_page()

    def _del_rule(self):
        sel = self._rule_list.curselection()
        if sel:
            self._app._page_ex_rules.pop(sel[0])
            self._kw_var.set(""); self._kw_lbl.config(text="Mots-clés (séparés par ;) :")
            self._btn_apply_kw.config(state="disabled")
            self._refresh_lists(); self._render_page(); self._update_zones_label()

    def _clear_rules(self):
        if messagebox.askyesno("Confirmer", "Effacer toutes les règles d'exclusion de pages ?"):
            self._app._page_ex_rules.clear()
            self._refresh_lists(); self._render_page(); self._update_zones_label()


# ─────────────────────────────────────────────
#  APPLICATION PRINCIPALE
# ─────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Comparateur de PDF")
        self.minsize(1100, 700)
        self._running       = False
        self._stop_event    = threading.Event()
        self._exclude_zones = []
        self._page_ex_rules = []

        # Variables partagées entre onglets
        self._ref_var       = tk.StringVar()
        self._new_var       = tk.StringVar()
        self._out_var       = tk.StringVar(value="rapport_comparaison.html")
        self._ref_pages     = tk.StringVar(value="")
        self._new_pages     = tk.StringVar(value="")
        self._ref_skip_start = tk.IntVar(value=0)
        self._ref_skip_end   = tk.IntVar(value=0)
        self._new_skip_start = tk.IntVar(value=0)
        self._new_skip_end   = tk.IntVar(value=0)
        self._dpi_var        = tk.IntVar(value=100)
        self._tol_txt_var    = tk.IntVar(value=3)
        self._tol_pix_var    = tk.IntVar(value=10)
        self._min_area_var   = tk.IntVar(value=50)
        self._workers_var    = tk.IntVar(value=max(2, (os.cpu_count() or 2) - 1))
        self._shift_tol_var  = tk.IntVar(value=3)
        self._no_ss_var      = tk.BooleanVar(value=False)
        self._open_var       = tk.BooleanVar(value=True)
        self._limit100_var   = tk.BooleanVar(value=False)
        self._elastic_var    = tk.BooleanVar(value=False)
        self._mode_text_var  = tk.BooleanVar(value=True)
        self._mode_image_var = tk.BooleanVar(value=True)
        self._progress_var   = tk.DoubleVar(value=0)
        self._progress_lbl   = tk.StringVar(value="")

        # Widgets créés par les onglets (référencés ici pour _worker / _save_prefs)
        self._btn_run = self._btn_stop = self._zones_lbl = self._log = None

        self._build_ui()
        self._load_prefs()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        self._tab_config  = TabConfig(nb, self)
        self._tab_preview = TabPreview(nb, self)
        self._tab_zones   = TabZones(nb, self)

        nb.add(self._tab_config,  text="  ⚙  Configuration  ")
        nb.add(self._tab_zones,   text="  🗺  Zones à exclure  ")
        nb.add(self._tab_preview, text="  🔍  Aperçu diff  ")

        def _on_tab_change(event):
            selected = nb.index(nb.select())
            if selected == 1:   # Zones (2e onglet)
                self._tab_zones.on_tab_activated()
            elif selected == 2:  # Aperçu diff (3e onglet)
                self._tab_preview._trigger_calc()
        nb.bind("<<NotebookTabChanged>>", _on_tab_change)

    # ── Fichiers ─────────────────────────────
    def _update_report_name(self):
        ref = self._ref_var.get(); new = self._new_var.get()
        if ref and new: self._out_var.set(_make_report_name(ref, new))
        elif ref:
            out_dir = os.path.dirname(ref) or "."
            stem = os.path.splitext(os.path.basename(ref))[0]
            self._out_var.set(os.path.join(out_dir, f"{stem}.html"))

    def _browse_ref(self):
        p = filedialog.askopenfilename(title="PDF Référence", filetypes=[("PDF","*.pdf")])
        if p:
            self._ref_var.set(p); self._ref_pages.set("…"); self._update_report_name()
            threading.Thread(target=self._load_page_count, args=(p, self._ref_pages), daemon=True).start()
            self._reset_preview_to_page1()

    def _browse_new(self):
        p = filedialog.askopenfilename(title="PDF Candidat", filetypes=[("PDF","*.pdf")])
        if p:
            self._new_var.set(p); self._new_pages.set("…"); self._update_report_name()
            threading.Thread(target=self._load_page_count, args=(p, self._new_pages), daemon=True).start()
            self._reset_preview_to_page1()

    def _reset_preview_to_page1(self):
        """Remet la visionneuse à la page 1 et vide les caches (nouveau fichier)."""
        preview = self._tab_preview
        preview._invalidate_cache()
        preview._ref_page_idx    = 0
        preview._new_page_idx    = 0
        preview._ref_page_count  = 0
        preview._new_page_count  = 0
        preview._imgs_orig       = [None, None, None]
        preview._fit_done        = [False, False, False]
        for c in preview._canvases:
            c.delete("all")

    def _load_page_count(self, path, var):
        n = _pdf_page_count(path)
        self.after(0, lambda: var.set(f"({n} pages)" if n else ""))

    def _browse_out(self):
        p = filedialog.asksaveasfilename(title="Enregistrer le rapport",
                                          defaultextension=".html",
                                          filetypes=[("HTML","*.html"),("Tous","*.*")])
        if p: self._out_var.set(p)

    # ── Journal ──────────────────────────────
    def _log_append(self, text: str, tag: str = ""):
        def _do():
            if self._log is None: return
            self._log.configure(state="normal")
            self._log.insert("end", text, tag)
            self._log.see("end")
            self._log.configure(state="disabled")
        self.after(0, _do)

    def _stdout_cb(self, text: str):
        if "\r" in text: return
        tag = "ok"    if ("Terminé" in text or "Rapport généré" in text) else \
              "error" if ("Erreur"  in text or "Error"          in text) else \
              "warn"  if ("⚠"       in text or "ATTENTION"      in text) else ""
        if text.strip():
            self._log_append(text if text.endswith("\n") else text+"\n", tag)

    def _progress_cb(self, current: int, total: int, ref_page: int):
        elapsed = time.time() - self._run_start
        eta     = int(elapsed / current * (total - current)) if current > 0 else 0
        eta_str = f" — Temps restant estimé : {_fmt_eta(eta)}" if eta > 1 else ""
        pct     = current / total * 100 if total else 100
        msg     = f"Page {current} / {total}{eta_str}"
        self.after(0, lambda p=pct: self._progress_var.set(p))
        self.after(0, lambda m=msg: self._progress_lbl.set(m))
        self._log_append(f"  [{current:>{len(str(total))}}/{total}] REF p.{ref_page}\n")

    # ── Validation ───────────────────────────
    def _validate(self) -> bool:
        ref = self._ref_var.get(); new = self._new_var.get(); out = self._out_var.get()
        for path, label in [(ref,"Référence"), (new,"Candidat")]:
            if not path:
                messagebox.showwarning("Champ manquant", f"Sélectionnez le PDF {label}."); return False
            if not os.path.isfile(path):
                messagebox.showerror("Fichier introuvable", f"{label} introuvable :\n{path}"); return False
            if not path.lower().endswith(".pdf"):
                messagebox.showerror("Format invalide", f"{label} : le fichier doit être un PDF."); return False
            try:
                doc = fitz.open(path)
                if doc.page_count == 0:
                    messagebox.showerror("PDF vide", f"{label} : le PDF est vide ou corrompu."); return False
                doc.close()
            except Exception as e:
                messagebox.showerror("PDF invalide", f"{label} : impossible d'ouvrir le fichier.\n{e}"); return False
        if not out:
            messagebox.showwarning("Champ manquant", "Indiquez le fichier de sortie."); return False
        return True

    # ── Lancer ───────────────────────────────
    def _run(self):
        if self._running: return
        if not self._validate(): return
        self._running = True
        self._stop_event.clear()
        if self._btn_run:  self._btn_run.configure(state="disabled")
        if self._btn_stop: self._btn_stop.configure(state="normal")
        self._progress_var.set(0); self._progress_lbl.set("Démarrage…")
        if self._log:
            self._log.configure(state="normal"); self._log.delete("1.0","end")
            self._log.configure(state="disabled")
        self._old_stdout = sys.stdout
        sys.stdout = _StreamRedirect(self._stdout_cb)
        self._run_start = time.time()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            import pdf_compare as pc
            ref_skip_end = self._ref_skip_end.get()
            new_skip_end = self._new_skip_end.get()
            if self._limit100_var.get():
                ref_n = fitz.open(self._ref_var.get()).page_count
                new_n = fitz.open(self._new_var.get()).page_count
                ref_skip_end = max(ref_skip_end, ref_n - self._ref_skip_start.get() - 100)
                new_skip_end = max(new_skip_end, new_n - self._new_skip_start.get() - 100)

            resultats = pc.compare_pdfs(
                reference_pdf       = self._ref_var.get(),
                candidate_pdf       = self._new_var.get(),
                tolerance           = self._tol_txt_var.get() * 72 / max(self._dpi_var.get(), 1),
                ref_skip_start      = self._ref_skip_start.get(),
                ref_skip_end        = ref_skip_end,
                new_skip_start      = self._new_skip_start.get(),
                new_skip_end        = new_skip_end,
                exclude_zones       = list(self._exclude_zones),
                page_exclude_rules  = list(self._page_ex_rules),
                dpi                 = self._dpi_var.get(),
                pixel_tolerance     = self._tol_pix_var.get(),
                min_diff_area       = self._min_area_var.get(),
                include_screenshots = not self._no_ss_var.get(),
                shift_tolerance     = self._shift_tol_var.get(),
                elastic_align       = self._elastic_var.get(),
                run_phase1          = self._mode_text_var.get(),
                run_phase2          = self._mode_image_var.get(),
                progress_callback   = self._progress_cb,
                stop_event          = self._stop_event,
                max_workers         = self._workers_var.get(),
            )

            if self._stop_event.is_set():
                self.after(0, self._on_cancelled); return

            self._log_append("\nGénération du rapport HTML en cours…\n", "warn")
            self.after(0, lambda: self._progress_lbl.set("Génération du rapport…"))

            t_analyse    = time.time() - self._run_start
            t_rapp_start = time.time()
            pc.generate_html_report(
                results             = resultats,
                output_path         = self._out_var.get(),
                ref_info            = {"path": self._ref_var.get()},
                new_info            = {"path": self._new_var.get()},
                include_screenshots = not self._no_ss_var.get(),
                timing_info         = {'analyse': t_analyse, 'rapport': None, 'total': None},
            )
            t_rapp_s  = time.time() - t_rapp_start
            t_total_s = time.time() - self._run_start
            pc._patch_timings(self._out_var.get(), t_rapp_s, t_total_s)
            self.after(0, self._on_success)

        except Exception:
            import traceback
            self._log_append(f"\n[ERREUR]\n{traceback.format_exc()}\n", "error")
            self.after(0, self._on_error, "Voir le journal pour les détails.")

    def _stop(self):
        self._stop_event.set()
        self._log_append("\n⚠ Annulation demandée — fin de la page en cours puis arrêt…\n", "warn")
        if self._btn_stop: self._btn_stop.configure(state="disabled")

    def _on_success(self):
        self._running = False
        if self._btn_run:  self._btn_run.configure(state="normal")
        if self._btn_stop: self._btn_stop.configure(state="disabled")
        self._progress_var.set(100); self._progress_lbl.set("Terminé ✓")
        sys.stdout = self._old_stdout
        self._save_prefs()
        if self._open_var.get(): self._open_report()

    def _on_cancelled(self):
        self._running = False
        if self._btn_run:  self._btn_run.configure(state="normal")
        if self._btn_stop: self._btn_stop.configure(state="disabled")
        self._progress_lbl.set("Annulé")
        sys.stdout = self._old_stdout
        self._log_append("\nTraitement annulé.\n", "warn")

    def _on_error(self, msg):
        self._running = False
        if self._btn_run:  self._btn_run.configure(state="normal")
        if self._btn_stop: self._btn_stop.configure(state="disabled")
        self._progress_lbl.set("Erreur ✗")
        sys.stdout = self._old_stdout
        messagebox.showerror("Erreur", f"La comparaison a échoué :\n{msg}")

    def _open_report(self):
        path = self._out_var.get()
        if not path: return
        if not path.lower().endswith(".html"): path = os.path.splitext(path)[0]+".html"
        if os.path.isfile(path): webbrowser.open(f"file://{os.path.abspath(path)}")
        else: messagebox.showinfo("Rapport introuvable", f"Le fichier n'existe pas encore :\n{path}")

    # ── Préférences ───────────────────────────
    def _save_prefs(self):
        try:
            prefs = {
                "ref": self._ref_var.get(), "new": self._new_var.get(),
                "dpi": self._dpi_var.get(), "tol_txt": self._tol_txt_var.get(),
                "tol_pix": self._tol_pix_var.get(), "min_area": self._min_area_var.get(),
                "workers": self._workers_var.get(), "shift_tol": self._shift_tol_var.get(),
                "no_ss": self._no_ss_var.get(), "limit100": self._limit100_var.get(),
                "elastic": self._elastic_var.get(), "open_after": self._open_var.get(),
                "mode_text": self._mode_text_var.get(), "mode_image": self._mode_image_var.get(),
                "ref_skip_s": self._ref_skip_start.get(), "ref_skip_e": self._ref_skip_end.get(),
                "new_skip_s": self._new_skip_start.get(), "new_skip_e": self._new_skip_end.get(),
                "exclude_zones": self._exclude_zones, "page_ex_rules": self._page_ex_rules,
                "geometry": self.geometry(),
            }
            with open(PREFS_FILE, "w", encoding="utf-8") as f:
                json.dump(prefs, f, indent=2)
        except Exception: pass

    def _load_prefs(self):
        try:
            with open(PREFS_FILE, encoding="utf-8") as f:
                p = json.load(f)
            if p.get("ref") and os.path.isfile(p["ref"]):
                self._ref_var.set(p["ref"])
                n = _pdf_page_count(p["ref"])
                self._ref_pages.set(f"({n} pages)" if n else "")
            if p.get("new") and os.path.isfile(p["new"]):
                self._new_var.set(p["new"])
                n = _pdf_page_count(p["new"])
                self._new_pages.set(f"({n} pages)" if n else "")
            for key, var in [("dpi",self._dpi_var),("tol_txt",self._tol_txt_var),
                              ("tol_pix",self._tol_pix_var),("min_area",self._min_area_var),
                              ("workers",self._workers_var),("shift_tol",self._shift_tol_var)]:
                if p.get(key) is not None: var.set(p[key])
            self._no_ss_var.set(p.get("no_ss", False))
            self._limit100_var.set(p.get("limit100", False))
            self._elastic_var.set(p.get("elastic", False))
            self._open_var.set(p.get("open_after", True))
            self._mode_text_var.set(p.get("mode_text", True))
            self._mode_image_var.set(p.get("mode_image", True))
            self._ref_skip_start.set(p.get("ref_skip_s", 0))
            self._ref_skip_end.set(p.get("ref_skip_e", 0))
            self._new_skip_start.set(p.get("new_skip_s", 0))
            self._new_skip_end.set(p.get("new_skip_e", 0))
            self._update_report_name()
            if p.get("exclude_zones"):
                self._exclude_zones[:] = [tuple(z) for z in p["exclude_zones"]]
            if p.get("page_ex_rules"):
                self._page_ex_rules[:] = [
                    {"zone": tuple(r["zone"]), "keywords": r["keywords"]}
                    for r in p["page_ex_rules"]]
            if p.get("geometry"):
                self.geometry(p["geometry"])
                # Vérifier que la fenêtre est visible sur l'écran courant
                # (configuration multi-écrans différente de la dernière session)
                self.update_idletasks()
                sw = self.winfo_screenwidth()
                sh = self.winfo_screenheight()
                wx = self.winfo_x()
                wy = self.winfo_y()
                ww = self.winfo_width()
                wh = self.winfo_height()
                # Si la fenêtre est majoritairement hors écran → la recentrer
                if wx + ww < 50 or wy + wh < 50 or wx > sw - 50 or wy > sh - 50:
                    self.geometry(f"+{max(0, (sw-ww)//2)}+{max(0, (sh-wh)//2)}")
        except Exception: pass

    def _on_close(self):
        self._save_prefs(); self.destroy()


# ─────────────────────────────────────────────
#  LANCEMENT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    app = App()
    style = ttk.Style(app)
    try: style.theme_use("clam")
    except Exception: pass
    style.configure("Accent.TButton",      font=("Segoe UI", 10, "bold"))
    style.configure("Bold.TCheckbutton",   font=("Segoe UI", 9,  "bold"))
    app.mainloop()
