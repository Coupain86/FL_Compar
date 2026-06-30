"""
pdf_zone_selector.py — Fenêtre de sélection visuelle de zones à exclure
Utilisé par pdf_compare_gui.py — 100% local (fitz + tkinter)
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import math

try:
    from PIL import Image, ImageTk
    import fitz
except ImportError:
    fitz = None


# ─────────────────────────────────────────────
#  CONSTANTES
# ─────────────────────────────────────────────
RENDER_DPI    = 120   # DPI d'affichage dans la visionneuse
ZONE_COLOR    = "#e74c3c"   # Rouge — zones d'exclusion de comparaison
PAGE_EX_COLOR = "#f39c12"   # Orange — zones d'exclusion de pages


# ─────────────────────────────────────────────
#  FENÊTRE DE SÉLECTION
# ─────────────────────────────────────────────
class ZoneSelectorWindow(tk.Toplevel):
    """
    Fenêtre modale permettant de :
      1. Visualiser un PDF page par page (REF ou CANDIDAT)
      2. Dessiner des rectangles = zones à exclure de la comparaison
      3. Définir des zones + mots-clés = pages à exclure si le mot est trouvé dans la zone
    Retourne les zones via les listes partagées passées en paramètre.
    """

    def __init__(self, parent,
                 ref_path: str, new_path: str,
                 exclude_zones: list,
                 page_exclude_rules: list):
        """
        exclude_zones      : list de (x0,y0,x1,y1) en points PDF — zones à exclure
        page_exclude_rules : list de {"zone":(x0,y0,x1,y1), "keywords":[str]} — règles d'exclusion de pages
        Ces listes sont modifiées in-place.
        """
        super().__init__(parent)
        self.title("Sélection des zones à exclure")
        self.minsize(900, 700)
        self.grab_set()   # Modale

        self._ref_path  = ref_path
        self._new_path  = new_path
        self._ex_zones  = exclude_zones
        self._pg_rules  = page_exclude_rules

        self._doc       = None
        self._pdf_src   = "ref"   # "ref" ou "new"
        self._page_idx  = 0
        self._scale     = 1.0
        self._tk_img    = None

        # État du dessin
        self._draw_start  = None   # (x_canvas, y_canvas)
        self._draw_rect   = None   # id du rectangle temporaire sur le canvas
        self._draw_mode   = "zone"  # "zone" ou "page_rule"
        self._page_w_px   = 0      # dimensions de la page rendue en pixels
        self._page_h_px   = 0

        self._build_ui()
        self._load_pdf("ref")

    # ── Construction UI ───────────────────────
    def _build_ui(self):
        # ── Barre d'outils ────────────────────
        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(fill="x", side="top")

        # Source PDF
        ttk.Label(bar, text="Afficher :").pack(side="left")
        self._src_var = tk.StringVar(value="ref")
        rb_ref = ttk.Radiobutton(bar, text="Référence", variable=self._src_var,
                                  value="ref", command=lambda: self._load_pdf("ref"))
        rb_new = ttk.Radiobutton(bar, text="Candidat",  variable=self._src_var,
                                  value="new", command=lambda: self._load_pdf("new"))
        rb_ref.pack(side="left", padx=4)
        rb_new.pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        # Navigation pages
        ttk.Button(bar, text="◀", width=2, command=self._prev_page).pack(side="left")
        self._page_lbl = ttk.Label(bar, text="Page 1/1", width=12, anchor="center")
        self._page_lbl.pack(side="left")
        ttk.Button(bar, text="▶", width=2, command=self._next_page).pack(side="left")

        # Aller à page
        ttk.Label(bar, text="  Aller à :").pack(side="left")
        self._goto_var = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self._goto_var, width=5)
        e.pack(side="left", padx=2)
        e.bind("<Return>", lambda _: self._goto_page())
        ttk.Button(bar, text="OK", width=3, command=self._goto_page).pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        # Mode dessin
        ttk.Label(bar, text="Mode :").pack(side="left")
        self._mode_var = tk.StringVar(value="zone")
        rb_zone = ttk.Radiobutton(bar, text="Zone d'exclusion",
                                   variable=self._mode_var, value="zone",
                                   command=lambda: self._set_mode("zone"))
        rb_page = ttk.Radiobutton(bar, text="Exclusion de pages",
                                   variable=self._mode_var, value="page_rule",
                                   command=lambda: self._set_mode("page_rule"))
        rb_zone.pack(side="left", padx=4)
        rb_page.pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        # Zoom
        ttk.Label(bar, text="Zoom :").pack(side="left")
        self._zoom_var = tk.IntVar(value=100)
        for z in (75, 100, 150, 200):
            ttk.Radiobutton(bar, text=f"{z}%", variable=self._zoom_var,
                             value=z, command=self._apply_zoom).pack(side="left")

        # ── Corps : canvas + panneaux latéraux ─
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        # Canvas PDF (scrollable)
        canv_frame = ttk.Frame(body)
        canv_frame.pack(side="left", fill="both", expand=True)

        self._canvas = tk.Canvas(canv_frame, bg="#666", cursor="crosshair")
        hbar = ttk.Scrollbar(canv_frame, orient="horizontal",
                              command=self._canvas.xview)
        vbar = ttk.Scrollbar(canv_frame, orient="vertical",
                              command=self._canvas.yview)
        self._canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        vbar.pack(side="right",  fill="y")
        hbar.pack(side="bottom", fill="x")
        self._canvas.pack(fill="both", expand=True)

        self._canvas.bind("<ButtonPress-1>",   self._on_mouse_down)
        self._canvas.bind("<B1-Motion>",        self._on_mouse_move)
        self._canvas.bind("<ButtonRelease-1>",  self._on_mouse_up)

        # Panneau droit : listes de zones
        right = ttk.Frame(body, width=280, padding=6)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        # ── Zones d'exclusion ─────────────────
        ttk.Label(right, text="Zones d'exclusion de comparaison",
                  font=("Segoe UI", 9, "bold"), foreground=ZONE_COLOR).pack(anchor="w")
        ttk.Label(right, text="(s'appliquent sur toutes les pages)",
                  font=("Segoe UI", 8), foreground="#888").pack(anchor="w")

        self._zone_list = tk.Listbox(right, height=8, selectmode="single",
                                      font=("Consolas", 8))
        self._zone_list.pack(fill="x", pady=(4, 0))
        ttk.Button(right, text="🗑 Supprimer zone sélectionnée",
                   command=self._del_zone).pack(fill="x", pady=2)
        ttk.Button(right, text="🗑 Tout effacer",
                   command=self._clear_zones).pack(fill="x", pady=(0, 8))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)

        # ── Règles d'exclusion de pages ───────
        ttk.Label(right, text="Règles d'exclusion de pages",
                  font=("Segoe UI", 9, "bold"), foreground=PAGE_EX_COLOR).pack(anchor="w")
        ttk.Label(right, text="Zone + mots-clés → page ignorée si match",
                  font=("Segoe UI", 8), foreground="#888").pack(anchor="w")

        self._rule_list = tk.Listbox(right, height=7, selectmode="single",
                                      font=("Consolas", 8))
        self._rule_list.pack(fill="x", pady=(4, 0))
        self._rule_list.bind("<<ListboxSelect>>", self._on_rule_select)

        kw_frame = ttk.Frame(right)
        kw_frame.pack(fill="x", pady=4)
        self._kw_lbl = ttk.Label(kw_frame, text="Mots-clés (séparés par ;) :")
        self._kw_lbl.pack(anchor="w")
        self._kw_var = tk.StringVar()
        ttk.Entry(kw_frame, textvariable=self._kw_var).pack(fill="x")
        self._kw_hint = ttk.Label(kw_frame,
                  text="Tracez un rectangle puis saisissez les mots-clés",
                  font=("Segoe UI", 8), foreground="#888", wraplength=260)
        self._kw_hint.pack(anchor="w")
        # Bouton Appliquer (modifier une règle existante)
        self._btn_apply_kw = ttk.Button(right, text="✏  Appliquer les mots-clés à la règle sélectionnée",
                                         command=self._apply_keywords, state="disabled")
        self._btn_apply_kw.pack(fill="x", pady=(0, 4))

        ttk.Button(right, text="🗑 Supprimer règle sélectionnée",
                   command=self._del_rule).pack(fill="x", pady=2)
        ttk.Button(right, text="🗑 Tout effacer",
                   command=self._clear_rules).pack(fill="x", pady=(0, 8))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)

        # ── Bouton Valider ────────────────────
        ttk.Button(right, text="✔  Valider et fermer",
                   command=self._validate, style="Accent.TButton").pack(fill="x", pady=4)
        ttk.Button(right, text="✖  Annuler",
                   command=self.destroy).pack(fill="x")

        # Barre statut
        self._status = tk.StringVar(value="Tracez un rectangle sur la page PDF")
        ttk.Label(self, textvariable=self._status, foreground="#555",
                  font=("Segoe UI", 8)).pack(side="bottom", anchor="w", padx=8, pady=2)

        # Peupler les listes depuis les données existantes
        self._refresh_lists()

    # ── Chargement PDF ────────────────────────
    def _load_pdf(self, src: str):
        if self._doc:
            self._doc.close()
        path = self._ref_path if src == "ref" else self._new_path
        if not path:
            messagebox.showwarning("Fichier manquant",
                                   "Sélectionnez d'abord les PDFs dans le GUI principal.")
            return
        try:
            self._doc    = fitz.open(path)
            self._pdf_src = src
            self._page_idx = 0
            self._render_page()
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible d'ouvrir le PDF :\n{e}")

    def _render_page(self):
        if not self._doc:
            return
        n = self._doc.page_count
        self._page_lbl.config(text=f"Page {self._page_idx+1}/{n}")

        zoom = self._zoom_var.get() / 100
        dpi  = RENDER_DPI * zoom
        scale = dpi / 72.0
        self._scale = scale

        page = self._doc[self._page_idx]
        mat  = fitz.Matrix(scale, scale)
        pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        img  = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

        self._page_w_px = pix.width   # largeur page en pixels canvas
        self._page_h_px = pix.height  # hauteur page en pixels canvas
        self._tk_img = ImageTk.PhotoImage(img)
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor="nw", image=self._tk_img, tags="page")
        self._canvas.configure(scrollregion=(0, 0, pix.width, pix.height))

        # Re-dessiner les zones existantes
        self._draw_existing_zones()

    def _draw_existing_zones(self):
        """Redessine toutes les zones sur le canvas."""
        scale = self._scale
        for i, (x0, y0, x1, y1) in enumerate(self._ex_zones):
            px0, py0 = x0*scale, y0*scale
            px1, py1 = x1*scale, y1*scale
            self._canvas.create_rectangle(px0, py0, px1, py1,
                                           outline=ZONE_COLOR, width=2,
                                           dash=(4,4), tags=f"exzone_{i}")
            self._canvas.create_text((px0+px1)/2, (py0+py1)/2,
                                      text=f"Z{i+1}", fill=ZONE_COLOR,
                                      font=("Segoe UI", 8, "bold"))

        for i, rule in enumerate(self._pg_rules):
            x0, y0, x1, y1 = rule["zone"]
            px0, py0 = x0*scale, y0*scale
            px1, py1 = x1*scale, y1*scale
            kw = ", ".join(rule.get("keywords", []))
            self._canvas.create_rectangle(px0, py0, px1, py1,
                                           outline=PAGE_EX_COLOR, width=2,
                                           dash=(6,2), tags=f"pgrule_{i}")
            self._canvas.create_text((px0+px1)/2, py0-6,
                                      text=f"P{i+1}: {kw[:30]}",
                                      fill=PAGE_EX_COLOR,
                                      font=("Segoe UI", 7))

    # ── Navigation ────────────────────────────
    def _prev_page(self):
        if self._doc and self._page_idx > 0:
            self._page_idx -= 1
            self._render_page()

    def _next_page(self):
        if self._doc and self._page_idx < self._doc.page_count - 1:
            self._page_idx += 1
            self._render_page()

    def _goto_page(self):
        try:
            n = int(self._goto_var.get()) - 1
            if self._doc and 0 <= n < self._doc.page_count:
                self._page_idx = n
                self._render_page()
        except ValueError:
            pass
        self._goto_var.set("")

    def _apply_zoom(self):
        self._render_page()

    def _set_mode(self, mode: str):
        self._draw_mode = mode
        if mode == "zone":
            self._status.set("Mode : Zone d'exclusion — tracez un rectangle (rouge)")
            self._canvas.config(cursor="crosshair")
        else:
            self._status.set("Mode : Exclusion de pages — tracez un rectangle puis saisissez les mots-clés (orange)")
            self._canvas.config(cursor="crosshair")

    # ── Dessin ────────────────────────────────
    def _canvas_to_pdf(self, cx, cy):
        """Convertit des coordonnées canvas en points PDF."""
        # Prendre en compte le scroll
        x = self._canvas.canvasx(cx)
        y = self._canvas.canvasy(cy)
        return x / self._scale, y / self._scale

    def _on_mouse_down(self, event):
        self._draw_start = (event.x, event.y)
        color = ZONE_COLOR if self._draw_mode == "zone" else PAGE_EX_COLOR
        self._draw_rect = self._canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline=color, width=2, dash=(4, 4))

    def _on_mouse_move(self, event):
        if self._draw_rect and self._draw_start:
            x0, y0 = self._draw_start
            # Clipper le curseur aux limites de la page
            cx = max(0, min(event.x, self._page_w_px - 1)) if self._page_w_px else event.x
            cy = max(0, min(event.y, self._page_h_px - 1)) if self._page_h_px else event.y
            self._canvas.coords(self._draw_rect, x0, y0, cx, cy)

    def _on_mouse_up(self, event):
        if not self._draw_start:
            return
        x0c, y0c = self._draw_start
        x1c, y1c = event.x, event.y

        # Normaliser (toujours min→max)
        if x0c > x1c: x0c, x1c = x1c, x0c
        if y0c > y1c: y0c, y1c = y1c, y0c

        # Clipper aux dimensions de la page (pas de débordement hors page)
        if self._page_w_px and self._page_h_px:
            x0c = max(0, min(x0c, self._page_w_px - 1))
            y0c = max(0, min(y0c, self._page_h_px - 1))
            x1c = max(0, min(x1c, self._page_w_px - 1))
            y1c = max(0, min(y1c, self._page_h_px - 1))

        # Ignorer les rectangles trop petits (simple clic)
        if abs(x1c - x0c) < 5 or abs(y1c - y0c) < 5:
            if self._draw_rect:
                self._canvas.delete(self._draw_rect)
            self._draw_start = self._draw_rect = None
            return

        # Convertir en coordonnées PDF
        px0, py0 = self._canvas_to_pdf(x0c, y0c)
        px1, py1 = self._canvas_to_pdf(x1c, y1c)
        zone = (round(px0, 1), round(py0, 1), round(px1, 1), round(py1, 1))

        if self._draw_mode == "zone":
            self._ex_zones.append(zone)
            self._status.set(f"Zone d'exclusion ajoutée : {zone}")
        else:
            # Mode exclusion de pages : demander les mots-clés
            kw_raw = self._kw_var.get().strip()
            if not kw_raw:
                messagebox.showwarning("Mots-clés manquants",
                                       "Saisissez au moins un mot-clé avant de tracer la zone.\n"
                                       "Exemple : BROUILLON ; DRAFT ; ANNULÉ")
                if self._draw_rect:
                    self._canvas.delete(self._draw_rect)
                self._draw_start = self._draw_rect = None
                return
            keywords = [k.strip() for k in kw_raw.split(";") if k.strip()]
            self._pg_rules.append({"zone": zone, "keywords": keywords})
            self._kw_var.set("")
            self._status.set(f"Règle ajoutée : {keywords} dans {zone}")

        self._draw_rect = None
        self._draw_start = None
        self._refresh_lists()
        self._render_page()

    # ── Gestion des listes ────────────────────
    def _refresh_lists(self):
        self._zone_list.delete(0, "end")
        for i, (x0, y0, x1, y1) in enumerate(self._ex_zones):
            self._zone_list.insert("end",
                f"Z{i+1}  ({x0:.0f},{y0:.0f}) → ({x1:.0f},{y1:.0f})")

        self._rule_list.delete(0, "end")
        for i, rule in enumerate(self._pg_rules):
            x0, y0, x1, y1 = rule["zone"]
            kw = " ; ".join(rule.get("keywords", []))
            self._rule_list.insert("end",
                f"P{i+1}  [{kw[:25]}] @ ({x0:.0f},{y0:.0f})→({x1:.0f},{y1:.0f})")

    def _del_zone(self):
        sel = self._zone_list.curselection()
        if sel:
            self._ex_zones.pop(sel[0])
            self._refresh_lists()
            self._render_page()

    def _clear_zones(self):
        if messagebox.askyesno("Confirmer", "Effacer toutes les zones d'exclusion ?"):
            self._ex_zones.clear()
            self._refresh_lists()
            self._render_page()

    def _on_rule_select(self, event):
        """Quand on clique sur une règle : pré-remplir le champ mots-clés."""
        sel = self._rule_list.curselection()
        if not sel:
            self._btn_apply_kw.config(state="disabled")
            return
        rule = self._pg_rules[sel[0]]
        # Pré-remplir le champ avec les mots-clés existants
        self._kw_var.set(" ; ".join(rule.get("keywords", [])))
        self._kw_lbl.config(text=f"Mots-clés règle P{sel[0]+1} (séparés par ;) :")
        self._kw_hint.config(text="Modifiez puis cliquez \"Appliquer\" — ou tracez un nouveau rectangle")
        self._btn_apply_kw.config(state="normal")

    def _apply_keywords(self):
        """Applique les mots-clés saisis à la règle sélectionnée."""
        sel = self._rule_list.curselection()
        if not sel:
            return
        kw_raw = self._kw_var.get().strip()
        if not kw_raw:
            messagebox.showwarning("Mots-clés vides",
                                   "Saisissez au moins un mot-clé.")
            return
        keywords = [k.strip() for k in kw_raw.split(";") if k.strip()]
        self._pg_rules[sel[0]]["keywords"] = keywords
        self._kw_var.set("")
        self._kw_lbl.config(text="Mots-clés (séparés par ;) :")
        self._kw_hint.config(text="Tracez un rectangle puis saisissez les mots-clés")
        self._btn_apply_kw.config(state="disabled")
        self._refresh_lists()
        self._render_page()
        self._status.set(f"Règle P{sel[0]+1} mise à jour : {keywords}")

    def _del_rule(self):
        sel = self._rule_list.curselection()
        if sel:
            self._pg_rules.pop(sel[0])
            self._kw_var.set("")
            self._kw_lbl.config(text="Mots-clés (séparés par ;) :")
            self._kw_hint.config(text="Tracez un rectangle puis saisissez les mots-clés")
            self._btn_apply_kw.config(state="disabled")
            self._refresh_lists()
            self._render_page()

    def _clear_rules(self):
        if messagebox.askyesno("Confirmer", "Effacer toutes les règles d'exclusion de pages ?"):
            self._pg_rules.clear()
            self._refresh_lists()
            self._render_page()

    # ── Valider ───────────────────────────────
    def _validate(self):
        self._status.set("Zones validées.")
        if self._doc is not None:
            try: self._doc.close()
            except Exception: pass
            self._doc = None
        self.destroy()

    def destroy(self):
        if self._doc is not None:
            try: self._doc.close()
            except Exception: pass
            self._doc = None
        super().destroy()
