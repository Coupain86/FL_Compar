"""
Génération des rapports PDF TrustRate (aucune dépendance nouvelle : PyMuPDF).

Deux documents, construits à partir du même AdvicePlan (app/advice.py) :
  - rapport « personne » : plan d'action de négociation complet, priorisé
    par gain, avec l'argument à prononcer pour chaque levier ;
  - rapport « banque »   : document factuel à remettre au conseiller —
    position de l'offre poste par poste face au marché et cibles chiffrées.

Tout est vectoriel (logo compris) ; rien n'est écrit sur disque : les
fonctions retournent les octets du PDF.
"""

from __future__ import annotations

import io
from datetime import date

import fitz

from .advice import AdvicePlan, CAT_METHOD, _fmt_eur, _fmt_pct
from .models import Offer

ACCENT = (0.10, 0.35, 0.65)
DARK = (0.13, 0.15, 0.18)
GREY = (0.42, 0.45, 0.50)
OK = (0.10, 0.50, 0.28)
OK_BG = (0.92, 0.97, 0.93)
WARN = (0.75, 0.45, 0.05)
LIGHT_BG = (0.95, 0.96, 0.98)

EASE_COLORS = {"offre en main": OK, "quasi certain": OK, "réaliste": ACCENT,
               "ambitieux": WARN, "à étudier": GREY}

DISCLAIMER = ("TrustRate - document indicatif fondé sur les données déclarées et les offres "
              "déposées par la communauté. Estimations non contractuelles ; ceci n'est pas un "
              "conseil financier réglementé.")

# Les polices PDF intégrées (Helvetica…) sont limitées au Latin-1 : les
# caractères hors jeu (€, flèches…) sortiraient en « · ». On translittère.
_SUBST = {"€": "EUR", "≈": "~", "→": "->", "—": "-", "–": "-", "…": "...",
          "’": "'", "œ": "oe", "⚠": "/!\\", "·": "-"}


def _latin(text: str) -> str:
    for bad, good in _SUBST.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


class Doc:
    """Compositeur multi-pages : en-tête avec logo, pied avec mention légale,
    saut de page automatique, briques (bandeaux, paragraphes, lignes clé/valeur,
    encadrés, tableaux, jauge)."""

    def __init__(self, title: str, subtitle: str):
        self.pdf = fitz.open()
        self.title, self.subtitle = title, subtitle
        self.n = 0
        self._page()

    # ── infra ──
    def _t(self, pos, text, **kw):
        self.page.insert_text(pos, _latin(str(text)), **kw)

    def _page(self):
        self.n += 1
        self.page = self.pdf.new_page(width=595, height=842)
        self._logo(50, 34)
        self._t((545 - 4.2 * len(self.title), 46), self.title,
                              fontsize=8.5, color=GREY)
        self.page.draw_line(fitz.Point(50, 66), fitz.Point(545, 66), width=0.8, color=ACCENT)
        self._footer()
        self.y = 86.0

    def _logo(self, x, y):
        p = self.page
        p.draw_rect(fitz.Rect(x, y, x + 22, y + 22), fill=ACCENT)
        # coche blanche
        p.draw_line(fitz.Point(x + 5, y + 12), fitz.Point(x + 9.5, y + 16.5),
                    color=(1, 1, 1), width=2.4)
        p.draw_line(fitz.Point(x + 9.5, y + 16.5), fitz.Point(x + 17, y + 6),
                    color=(1, 1, 1), width=2.4)
        p.insert_text((x + 28, y + 16), "TrustRate", fontsize=15, fontname="hebo", color=ACCENT)

    def _footer(self):
        cut = DISCLAIMER.rfind(" ", 0, 150)  # coupe entre deux mots
        self._t((50, 812), DISCLAIMER[:cut], fontsize=6.2, color=GREY)
        self._t((50, 820), DISCLAIMER[cut + 1:], fontsize=6.2, color=GREY)
        self._t((522, 828), f"Page {self.n}", fontsize=6.8, color=GREY)

    def need(self, h):
        if self.y + h > 790:
            self._page()

    def gap(self, h=8):
        self.y += h

    # ── briques ──
    def band(self, text, color=ACCENT):
        self.need(36)
        self.page.draw_rect(fitz.Rect(50, self.y - 4, 545, self.y + 16), fill=color)
        self._t((58, self.y + 10), text, fontsize=10.5,
                              fontname="hebo", color=(1, 1, 1))
        self.y += 34

    def h2(self, text, color=DARK):
        self.need(26)
        self._t((50, self.y + 8), text, fontsize=11.5, fontname="hebo", color=color)
        self.y += 22

    def para(self, text, size=8.8, indent=50, color=DARK, leading=None, font="helv",
             max_x=545):
        leading = leading or size * 1.34
        limit = int((max_x - indent) / (size * 0.5))
        words, line = text.split(), ""
        for w in words:
            if len(line) + len(w) + 1 > limit:
                self.need(leading + 2)
                self._t((indent, self.y), line, fontsize=size,
                                      fontname=font, color=color)
                self.y += leading
                line = w
            else:
                line = (line + " " + w).strip()
        if line:
            self.need(leading + 2)
            self._t((indent, self.y), line, fontsize=size,
                                  fontname=font, color=color)
            self.y += leading

    def kv(self, label, value, strong=False):
        self.need(16)
        self._t((58, self.y), label, fontsize=9, color=GREY)
        self._t((330, self.y), str(value), fontsize=9.4,
                              fontname="hebo" if strong else "helv", color=DARK)
        self.y += 14

    def chip(self, x, text, color, filled=True):
        w = 4.6 * len(text) + 12
        r = fitz.Rect(x, self.y - 8.5, x + w, self.y + 3.5)
        if filled:
            self.page.draw_rect(r, fill=color)
            self._t((x + 6, self.y), text, fontsize=7.2,
                                  fontname="hebo", color=(1, 1, 1))
        else:
            self.page.draw_rect(r, color=color, width=0.8)
            self._t((x + 6, self.y), text, fontsize=7.2,
                                  fontname="hebo", color=color)
        return x + w + 6

    def gauge(self, pct):
        """Jauge de positionnement 0-100 %."""
        self.need(34)
        self.page.draw_rect(fitz.Rect(58, self.y, 470, self.y + 9), fill=(0.90, 0.91, 0.93))
        self.page.draw_rect(fitz.Rect(58, self.y, 58 + 412 * pct / 100, self.y + 9), fill=ACCENT)
        self._t((478, self.y + 8), f"{pct} %", fontsize=10,
                              fontname="hebo", color=ACCENT)
        self._t((58, self.y + 19), "0 % = offre la plus chère",
                              fontsize=6.6, color=GREY)
        self._t((388, self.y + 19), "100 % = la meilleure",
                              fontsize=6.6, color=GREY)
        self.y += 32

    def box(self, lines, fill=LIGHT_BG, border=ACCENT, size=8.8, pad=10, title=""):
        h = (14 if title else 0) + size * 1.5 * len(lines) + 2 * pad - 6
        self.need(h + 8)
        top = self.y - pad + 2
        self.page.draw_rect(fitz.Rect(50, top, 545, top + h), fill=fill, color=border, width=0.9)
        if title:
            self._t((60, self.y + 2), title, fontsize=9.4,
                                  fontname="hebo", color=border)
            self.y += 15
        for ln in lines:
            self._t((60, self.y + 2), ln, fontsize=size, color=DARK)
            self.y += size * 1.5
        self.y += pad + 4

    def table(self, headers, rows, widths, aligns=None, size=8.4, highlight_col=None):
        xs = [50]
        for w in widths:
            xs.append(xs[-1] + w)
        self.need(18 + 15 * (len(rows) + 1))
        self.page.draw_rect(fitz.Rect(50, self.y - 9, xs[-1], self.y + 4), fill=ACCENT)
        for i, (x, htxt) in enumerate(zip(xs, headers)):
            self._t((x + 5, self.y), htxt, fontsize=size,
                                  fontname="hebo", color=(1, 1, 1))
        self.y += 16
        for r_i, row in enumerate(rows):
            self.need(15)
            if r_i % 2:
                self.page.draw_rect(fitz.Rect(50, self.y - 9, xs[-1], self.y + 4),
                                    fill=(0.94, 0.96, 0.98))
            if highlight_col is not None:
                self.page.draw_rect(fitz.Rect(xs[highlight_col], self.y - 9,
                                              xs[highlight_col + 1], self.y + 4),
                                    fill=OK_BG)
            for c_i, (x, cell) in enumerate(zip(xs, row)):
                bold = highlight_col is not None and c_i == highlight_col
                self._t((x + 5, self.y), str(cell), fontsize=size,
                                      fontname="hebo" if bold else "helv",
                                      color=OK if bold else DARK)
            self.y += 15
        self.y += 8

    def bytes(self) -> bytes:
        buf = io.BytesIO()
        self.pdf.save(buf)
        self.pdf.close()
        return buf.getvalue()


# ─────────────────────────────────────────────
#  Utilitaires communs
# ─────────────────────────────────────────────
def _offer_kv(d: Doc, offer: Offer, type_label: str):
    d.kv("Banque", offer.bank or "—", strong=True)
    d.kv("Type de crédit", type_label)
    if offer.amount:
        d.kv("Montant emprunté", _fmt_eur(offer.amount), strong=True)
    if offer.duration_months:
        d.kv("Durée", f"{offer.duration_months} mois")
    if offer.rate_nominal is not None:
        d.kv("Taux nominal", _fmt_pct(offer.rate_nominal), strong=True)
    if offer.taeg is not None:
        d.kv("TAEG", _fmt_pct(offer.taeg), strong=True)
    if offer.taea is not None:
        d.kv("Assurance (TAEA)", _fmt_pct(offer.taea))
    if offer.fees is not None:
        d.kv("Frais de dossier", _fmt_eur(offer.fees))
    if offer.total_cost is not None:
        d.kv("Coût total du crédit", _fmt_eur(offer.total_cost))
    if offer.offer_date:
        d.kv("Date de l'offre", offer.offer_date.strftime("%d/%m/%Y"))


def _gain_str(lever) -> str:
    if not lever.gain_total:
        return ""
    s = f"≈ {_fmt_eur(lever.gain_total)}"
    if lever.gain_monthly and lever.gain_monthly >= 1:
        s += f" ({_fmt_eur(lever.gain_monthly)}/mois)"
    return s


# ─────────────────────────────────────────────
#  Rapport « personne » : plan de négociation
# ─────────────────────────────────────────────
def client_report(offer: Offer, plan: AdvicePlan, type_label: str) -> bytes:
    d = Doc("Rapport de négociation", "")
    d.h2("Votre plan de négociation", ACCENT)
    d.para(f"Préparé le {date.today().strftime('%d/%m/%Y')} pour votre offre "
           f"{offer.bank or ''} ({type_label.lower()}"
           + (f", {_fmt_eur(offer.amount)}" if offer.amount else "") + "). "
           "Gratuit, sans engagement. Chaque conseil indique l'économie estimée, sa "
           "difficulté, et la phrase à prononcer face au conseiller.", color=GREY)
    d.gap(6)

    # L'essentiel
    top = plan.top(3)
    if top:
        total = sum(l.gain_total for l in top)
        lines = [f"{i}.  {l.title}  —  {_gain_str(l)}" for i, l in enumerate(top, 1)]
        lines.append(f"Potentiel cumulé de ces trois actions : ≈ {_fmt_eur(total)}.")
        d.box(lines, fill=OK_BG, border=OK, title="L'ESSENTIEL — vos 3 plus gros gisements")
        d.gap(2)

    # Action n° 1 : la concurrence
    d.band("ACTION N° 1 — METTRE UN MAXIMUM DE BANQUES EN CONCURRENCE")
    d.para("Aucun levier ne pèse plus lourd que la concurrence : une banque ne fait un effort "
           "que si elle risque de perdre le dossier. Avant toute négociation :", size=9.2)
    d.gap(2)
    for i, step in enumerate(plan.method, 1):
        d.para(f"{i}.  {step}", indent=58)
        d.gap(1)
    d.gap(6)

    # Position marché
    if plan.stats and plan.stats.better_than_pct is not None:
        d.band("VOTRE POSITION SUR LE MARCHÉ")
        d.para(f"Comparée à {plan.stats.size} offres similaires (même type de crédit, même "
               f"région, moins d'un an), votre offre est mieux placée que "
               f"{plan.stats.better_than_pct} % d'entre elles.", size=9.2)
        d.gauge(plan.stats.better_than_pct)
        rows = []
        if offer.taeg is not None:
            rows.append(("TAEG", _fmt_pct(offer.taeg), _fmt_pct(plan.stats.taeg_med),
                         _fmt_pct(plan.stats.taeg_p25)))
        if offer.rate_nominal is not None and plan.stats.nominal_med is not None:
            rows.append(("Taux nominal", _fmt_pct(offer.rate_nominal),
                         _fmt_pct(plan.stats.nominal_med), _fmt_pct(plan.stats.nominal_p25)))
        if offer.taea is not None and plan.stats.taea_med is not None:
            rows.append(("Assurance (TAEA)", _fmt_pct(offer.taea),
                         _fmt_pct(plan.stats.taea_med), _fmt_pct(plan.stats.taea_p25)))
        if offer.fees is not None and plan.stats.fees_med is not None:
            rows.append(("Frais de dossier", _fmt_eur(offer.fees),
                         _fmt_eur(plan.stats.fees_med), _fmt_eur(plan.stats.fees_p25)))
        if rows:
            d.table(["Poste", "Votre offre", "Médiane du marché", "Meilleur quart"],
                    rows, [140, 115, 130, 110])
        d.gap(4)

    # Offres concurrentes
    if plan.competitors:
        d.band("VOS OFFRES EN CONCURRENCE")
        d.para("Les offres que vous avez déposées sur TrustRate, comparées entre elles. "
               "La meilleure sert de référence dans le plan d'action.", size=9)
        rows = [(offer.bank or "—", _fmt_eur(offer.amount), _fmt_pct(offer.taeg),
                 _fmt_pct(offer.taea), _fmt_eur(offer.fees), "offre étudiée")]
        for c in plan.competitors:
            rows.append((c["bank"], _fmt_eur(c["amount"]), _fmt_pct(c["taeg"]),
                         _fmt_pct(c["taea"]), _fmt_eur(c["fees"]), ""))
        d.table(["Banque", "Montant", "TAEG", "TAEA", "Frais", ""],
                rows, [125, 85, 70, 70, 75, 70])
        d.gap(4)

    # Plan d'action détaillé
    d.band("VOTRE PLAN D'ACTION DÉTAILLÉ")
    for caveat in plan.caveats:
        d.para("⚠ " + caveat, color=WARN, size=8.4)
        d.gap(3)
    for category, levers in plan.by_category().items():
        d.h2(category)
        for lever in levers:
            d.need(60)
            d.para(lever.title, size=9.6, font="hebo", color=DARK)
            x = 50
            gain = _gain_str(lever)
            if gain:
                x = d.chip(x, gain, OK)
            x = d.chip(x, lever.ease, EASE_COLORS.get(lever.ease, GREY), filled=False)
            if lever.basis:
                d._t((x + 2, d.y), f"base : {lever.basis}",
                                   fontsize=7.2, color=GREY)
            d.y += 12
            d.para(lever.detail, indent=58, size=8.6, color=DARK)
            if lever.say and lever.say != "—":
                d.gap(2)
                d.para(lever.say, indent=58, size=8.6, font="heit", color=ACCENT)
            d.gap(8)
        d.gap(2)

    d.band("ET MAINTENANT ?", color=(0.16, 0.18, 0.22))
    d.para("1.  Demandez leurs offres aux autres banques (l'action n° 1) — visez-en au moins "
           "quatre ou cinq.", indent=58)
    d.para("2.  Remettez à chaque banque le rapport TrustRate « banque » : il chiffre "
           "l'effort attendu, poste par poste.", indent=58)
    d.para("3.  Redéposez chaque nouvelle offre sur TrustRate : votre comparatif se met à "
           "jour et vos rapports aussi.", indent=58)
    return d.bytes()


# ─────────────────────────────────────────────
#  Rapport « banque » : à remettre au conseiller
# ─────────────────────────────────────────────
def bank_report(offer: Offer, plan: AdvicePlan, type_label: str) -> bytes:
    d = Doc("Analyse remise par votre client", "")
    d.h2(f"Analyse comparative de votre offre de crédit — {offer.bank or 'votre établissement'}",
         ACCENT)
    d.para(f"Document établi le {date.today().strftime('%d/%m/%Y')} par TrustRate à la demande "
           "de votre client, sur la base de votre offre et des offres réellement déposées par "
           "les emprunteurs (données anonymisées). Il identifie, poste par poste, les "
           "ajustements qui rendraient votre offre compétitive et sécuriseraient la signature.",
           color=GREY)
    d.gap(6)

    d.band("L'OFFRE ANALYSÉE")
    _offer_kv(d, offer, type_label)
    d.gap(6)

    # Position marché
    st = plan.stats
    if st and st.better_than_pct is not None:
        d.band("POSITION DE VOTRE OFFRE FACE AU MARCHÉ")
        d.para(f"Cohorte de comparaison : {st.size} offres de même type et même région, "
               "déposées sur les 12 derniers mois. Votre offre est mieux placée que "
               f"{st.better_than_pct} % d'entre elles — il reste donc "
               f"{100 - st.better_than_pct} % d'offres plus compétitives que la vôtre.",
               size=9.2)
        d.gauge(st.better_than_pct)
        rows = []
        pts = {p.code: p for p in plan.bank_points()}
        if offer.taeg is not None:
            rows.append(("TAEG", _fmt_pct(offer.taeg), _fmt_pct(st.taeg_med),
                         _fmt_pct(st.taeg_p25), _fmt_pct(st.taeg_p25)))
        if offer.rate_nominal is not None and st.nominal_med is not None:
            target = next((pts[c].bank_target for c in ("taux_réaliste", "taux_ambitieux")
                           if c in pts), _fmt_pct(st.nominal_med))
            rows.append(("Taux nominal", _fmt_pct(offer.rate_nominal),
                         _fmt_pct(st.nominal_med), _fmt_pct(st.nominal_p25), target))
        if offer.taea is not None and st.taea_med is not None:
            target = pts["delegation"].bank_target if "delegation" in pts else _fmt_pct(st.taea_med)
            rows.append(("Assurance (TAEA)", _fmt_pct(offer.taea),
                         _fmt_pct(st.taea_med), _fmt_pct(st.taea_p25), target))
        if offer.fees is not None and st.fees_med is not None:
            rows.append(("Frais de dossier", _fmt_eur(offer.fees),
                         _fmt_eur(st.fees_med), _fmt_eur(st.fees_p25), "0 €"))
        if rows:
            d.table(["Poste", "Votre offre", "Médiane marché", "Meilleur quart", "Cible client"],
                    rows, [120, 95, 100, 100, 80], highlight_col=4)
    else:
        d.band("RÉFÉRENTIEL UTILISÉ")
        d.para("La cohorte locale est encore en construction : les cibles ci-dessous "
               "s'appuient sur des repères de marché prudents et, le cas échéant, sur les "
               "offres concurrentes écrites dont dispose le client.", size=9.2)
    d.gap(4)

    # Concurrence en main (anonymisée)
    rivals = [c for c in plan.competitors if c["taeg"] is not None]
    if rivals:
        best = rivals[0]
        d.band("ÉLÉMENT DÉTERMINANT — OFFRES CONCURRENTES EN MAIN", color=WARN)
        d.para(f"Votre client dispose de {len(rivals)} offre(s) concurrente(s) ÉCRITE(S). "
               f"La plus compétitive affiche un TAEG de {_fmt_pct(best['taeg'])}"
               + (f" (taux nominal {_fmt_pct(best['rate_nominal'])})"
                  if best["rate_nominal"] is not None else "")
               + ". Ces offres sont réelles et signables en l'état : l'alignement de vos "
               "conditions est la voie la plus directe vers la signature.", size=9.2)
        d.gap(4)

    # Les efforts demandés
    d.band("LES AJUSTEMENTS QUI SÉCURISERAIENT LA SIGNATURE")
    points = plan.bank_points()
    if not points:
        d.para("Aucun ajustement chiffrable n'a été identifié : l'offre est déjà bien "
               "positionnée sur les postes mesurés.", size=9.2)
    for i, p in enumerate(points, 1):
        d.need(40)
        d.para(f"{i}.  {p.bank_ask}", size=9.2)
        if p.bank_target:
            d.para(f"Cible proposée : {p.bank_target}", indent=64, size=8.8,
                   font="hebo", color=OK)
        d.gap(4)
    d.gap(4)

    d.box(["Un geste sur ces postes placerait votre offre dans le meilleur quart des offres",
           "comparables et transformerait cette négociation en signature.",
           "Le client reste libre du choix de son établissement."],
          fill=OK_BG, border=OK, title="EN RÉSUMÉ")
    d.para("Contact : ce document a été généré automatiquement par TrustRate à partir de "
           "données anonymisées. La cohorte de comparaison ne contient aucune donnée "
           "nominative.", color=GREY, size=7.6)
    return d.bytes()
