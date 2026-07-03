"""
Génère des offres de crédit PDF réalistes et VOLONTAIREMENT piégeuses pour
éprouver l'extracteur, puis note l'extraction champ par champ contre la
vérité terrain.

Chaque document contient TOUS les champs de l'outil (banque, type, montant,
durée, type de taux, taux nominal, TAEG, TAEA, frais, coût total, date),
noyés dans du texte réglementaire réaliste plein de leurres : taux d'usure,
taux de retard, Euribor, taux de période, anciens crédits rachetés, dates de
validité, SIREN, capital social…

Usage :  python -m tools.make_test_offers          (génère + note)
Sortie :  samples/hard/*.pdf
"""

import io
import os
import sys
from datetime import date

import fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import extraction as ex  # noqa: E402

OUT = os.path.join("samples", "hard")


# ─────────────────────────────────────────────
#  Mise en page
# ─────────────────────────────────────────────
class Page:
    """Compositeur multi-pages : en-tête/pied répétés, saut de page automatique,
    logo vectoriel couleur, bandeaux, tableaux zébrés (avec reprise d'en-tête)."""

    def __init__(self, doc, header, footer, accent=(0.15, 0.35, 0.6)):
        self.doc, self.header, self.footer, self.accent = doc, header, footer, accent
        self.n = 0
        self._new_page()

    def _new_page(self):
        self.n += 1
        self.page = self.doc.new_page(width=595, height=842)
        self.y = 62.0
        if self.header:
            self.page.insert_text((50, 38), self.header, fontsize=7.5, color=(0.35, 0.35, 0.35))
            self.page.draw_line(fitz.Point(50, 44), fitz.Point(545, 44), width=0.5,
                                color=(0.65, 0.65, 0.65))
        self.page.insert_text((50, 820), f"{self.footer}  -  Page {self.n}",
                              fontsize=6.5, color=(0.45, 0.45, 0.45))

    def need(self, h):
        if self.y + h > 792:
            self._new_page()

    def pagebreak(self):
        self._new_page()

    def logo(self, text, shape="square"):
        self.need(56)
        c = self.accent
        if shape == "square":
            self.page.draw_rect(fitz.Rect(50, self.y, 74, self.y + 24), fill=c)
            self.page.draw_rect(fitz.Rect(56, self.y + 6, 68, self.y + 18), fill=(1, 1, 1))
        elif shape == "circle":
            self.page.draw_circle(fitz.Point(62, self.y + 12), 12, fill=c)
            self.page.draw_circle(fitz.Point(62, self.y + 12), 5, fill=(1, 1, 1))
        else:  # bars
            for i in range(3):
                self.page.draw_rect(fitz.Rect(50 + i * 9, self.y + 4 + i * 3,
                                              56 + i * 9, self.y + 24), fill=c)
        self.page.insert_text((84, self.y + 18), text, fontsize=17, color=c)
        self.y += 44

    def band(self, text):
        self.need(34)
        self.page.draw_rect(fitz.Rect(50, self.y - 6, 545, self.y + 14), fill=self.accent)
        self.page.insert_text((58, self.y + 8), text, fontsize=10.5, color=(1, 1, 1))
        self.y += 32

    def title(self, text, size=13):
        self.need(30)
        self.y += 8
        self.page.insert_text((50, self.y), text, fontsize=size)
        self.page.draw_line(fitz.Point(50, self.y + 4), fitz.Point(545, self.y + 4), width=0.7)
        self.y += 20

    def para(self, text, size=8.6, indent=50, leading=11.4):
        words, line = text.split(), ""
        for w in words:
            if len(line) + len(w) + 1 > 108:
                self.need(leading + 2)
                self.page.insert_text((indent, self.y), line, fontsize=size)
                self.y += leading
                line = w
            else:
                line = (line + " " + w).strip()
        if line:
            self.need(leading + 2)
            self.page.insert_text((indent, self.y), line, fontsize=size)
            self.y += leading

    def kv(self, label, value, size=9.2):
        self.need(16)
        self.page.insert_text((60, self.y), label, fontsize=size)
        self.page.insert_text((330, self.y), value, fontsize=size)
        self.y += 13.5

    def box(self, lines, fill=(0.97, 0.94, 0.86), size=8.6):
        h = 14 * len(lines) + 10
        self.need(h + 6)
        self.page.draw_rect(fitz.Rect(50, self.y - 8, 545, self.y + h - 12),
                            fill=fill, color=self.accent, width=0.8)
        for ln in lines:
            self.page.insert_text((58, self.y + 4), ln, fontsize=size)
            self.y += 14
        self.y += 10

    def table(self, headers, rows, widths, size=7.6):
        xs = [50]
        for w in widths:
            xs.append(xs[-1] + w)

        def head():
            self.need(16)
            self.page.draw_rect(fitz.Rect(50, self.y - 8, xs[-1], self.y + 4), fill=self.accent)
            for x, htxt in zip(xs, headers):
                self.page.insert_text((x + 3, self.y), htxt, fontsize=size, color=(1, 1, 1))
            self.y += 13

        head()
        for i, row in enumerate(rows):
            if self.y + 11 > 792:
                self._new_page()
                head()
            if i % 2:
                self.page.draw_rect(fitz.Rect(50, self.y - 7.5, xs[-1], self.y + 3),
                                    fill=(0.93, 0.95, 0.97))
            for x, cell in zip(xs, row):
                self.page.insert_text((x + 3, self.y), cell, fontsize=size)
            self.y += 10.6
        self.y += 10

    def gap(self, h=8):
        self.y += h


def eur(x):
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


def amort_rows(principal, annual_pct, n_months, start):
    """Tableau d'amortissement complet (vraies maths -> centaines de leurres)."""
    r = annual_pct / 100 / 12
    monthly = principal * r / (1 - (1 + r) ** -n_months)
    rows, crd = [], principal
    for k in range(1, n_months + 1):
        it = crd * r
        cap = monthly - it
        crd = max(0.0, crd - cap)
        m0 = start.month - 1 + k
        d = date(start.year + m0 // 12, m0 % 12 + 1, min(start.day, 28))
        rows.append((str(k), d.strftime("%d/%m/%Y"), eur(monthly), eur(it), eur(cap), eur(crd)))
    return rows, monthly


def doc_pdf(builder):
    doc = fitz.open()
    builder(doc)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def rasterize(pdf_bytes, dpi=170):
    """Transforme un PDF texte en PDF image (simule un scan)."""
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()
    for page in src:
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csRGB)
        p = out.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(page.rect, stream=pix.tobytes("jpeg"))
    buf = io.BytesIO()
    out.save(buf)
    out.close(); src.close()
    return buf.getvalue()


LEGAL_COMMON = (
    "En application des articles L.313-1 et suivants du Code de la consommation, l'emprunteur dispose "
    "d'un delai de reflexion de dix jours a compter de la reception de la presente offre. L'acceptation "
    "ne peut intervenir avant l'expiration de ce delai. Un credit vous engage et doit etre rembourse. "
    "Verifiez vos capacites de remboursement avant de vous engager. Le taux d'usure applicable a la "
    "categorie de prets concernee s'etablit a 6,04 % au titre du trimestre en cours. En cas de "
    "defaillance de l'emprunteur, le taux d'interet applicable aux sommes restant dues sera majore de "
    "3,00 points, sans pouvoir exceder le taux d'usure. Toute reclamation peut etre adressee au "
    "service clientele ; a defaut de reponse sous 60 jours, le mediateur peut etre saisi."
)


# ─────────────────────────────────────────────
#  Les 6 documents (avec leur vérité terrain)
# ─────────────────────────────────────────────
def build_credit_agricole(doc):
    p = Page(doc, "CREDIT AGRICOLE - Caisse Regionale de Credit Agricole Mutuel de Touraine Poitou",
             "Credit Agricole Touraine Poitou - Societe cooperative a capital variable - SIREN 399 780 097 - "
             "Capital social 7 729 097 640,00 EUR - Siege : 18 rue Salvador Allende, 86000 Poitiers")
    p.title("OFFRE DE PRET IMMOBILIER N° 2026-078-445120")
    p.para("La presente offre de credit immobilier est emise le 12/06/2026 par la Caisse Regionale de "
           "Credit Agricole. Elle est valable jusqu'au 11/07/2026 inclus. Passe ce delai, une nouvelle "
           "etude sera necessaire. Reference dossier : TP-2026-445120. Conseiller : agence de Poitiers "
           "Centre, tel 05 49 00 00 00.")
    p.gap()
    p.title("I. CARACTERISTIQUES DU CREDIT", 10)
    p.kv("Nature du pret", "Pret immobilier amortissable - acquisition residence principale")
    p.kv("Bien finance", "Maison individuelle situee a Poitiers (86)")
    p.kv("Montant du credit", "245 000,00 EUR")
    p.kv("Duree totale", "300 mois (25 ans)")
    p.kv("Taux debiteur fixe", "3,45 % l'an")
    p.kv("Taux de periode (mensuel)", "0,2875 %")
    p.kv("Mensualite hors assurance", "1 221,48 EUR")
    p.kv("Nombre d'echeances", "300")
    p.gap()
    p.title("II. COUT DU CREDIT", 10)
    p.kv("Taux Annuel Effectif Global (TAEG)", "3,98 %")
    p.kv("Frais de dossier", "1 200,00 EUR")
    p.kv("Frais de garantie (caution CAMCA)", "2 850,00 EUR")
    p.kv("Cout total du credit", "148 736,52 EUR")
    p.gap()
    p.title("III. ASSURANCE EMPRUNTEUR", 10)
    p.para("Assurance deces, perte totale et irreversible d'autonomie, incapacite de travail souscrite "
           "aupres de CA Assurances, quotite 100 %. Cotisation mensuelle : 106,17 EUR. Le Taux Annuel "
           "Effectif de l'Assurance (TAEA) s'etablit a 0,52 %. La franchise applicable en cas "
           "d'incapacite est de 90 jours. L'emprunteur peut souscrire une assurance equivalente aupres "
           "de l'assureur de son choix (deleguation d'assurance, loi Lagarde).")
    p.gap()
    p.title("IV. DISPOSITIONS DIVERSES", 10)
    p.para("Indemnite de remboursement anticipe : 3 % du capital restant du, plafonnee a six mois "
           "d'interets. " + LEGAL_COMMON)
    p.para("Fait a Poitiers, le 12/06/2026, en deux exemplaires.")


TRUTH_CA = {"region": "Nouvelle-Aquitaine", "bank": "Crédit Agricole", "credit_type": "immobilier", "rate_type": "fixe",
            "amount": 245000.0, "duration_months": 300, "rate_nominal": 3.45, "taeg": 3.98,
            "taea": 0.52, "fees": 1200.0, "total_cost": 148736.52, "offer_date": date(2026, 6, 12)}


def build_bnp_variable(doc):
    p = Page(doc, "BNP PARIBAS - Banque Nationale de Paris - Reseau Banque de Detail en France",
             "BNP Paribas SA au capital de 2 468 663 292 EUR - SIREN 662 042 449 RCS Paris - "
             "ORIAS 07 022 735 - 16 bd des Italiens 75009 Paris")
    p.title("OFFRE DE CREDIT IMMOBILIER - PRET A TAUX REVISABLE")
    p.para("Offre emise le 03/04/2026 et valable trente jours. Le present pret immobilier est un pret "
           "a taux variable indexe sur l'Euribor 3 mois (valeur de reference au jour de l'emission : "
           "2,45 %), majore d'une marge de 1,27 point. Le taux est revisable annuellement a la date "
           "anniversaire, dans la limite d'un cap de variation de plus ou moins 1,00 point par rapport "
           "au taux initial (soit un taux plafond de 4,72 %).")
    p.gap()
    p.title("CONDITIONS FINANCIERES", 10)
    p.kv("Montant emprunte", "312.500,00 EUR")
    p.kv("Duree", "240 mois")
    p.kv("Taux debiteur initial (variable)", "3,72 %")
    p.kv("TAEG", "4,12 %")
    p.kv("Frais de dossier", "950,00 EUR")
    p.kv("Montant total du par l'emprunteur", "455.812,40 EUR")
    p.kv("Cout total du credit", "143.312,40 EUR")
    p.gap()
    p.title("ASSURANCE", 10)
    p.para("TAEA : 0,41 %. Cotisation initiale 89,30 EUR par mois, revisable selon l'age de l'assure. "
           "Bareme groupe : deces 0,25 %, incapacite 0,16 %.")
    p.gap()
    p.para(LEGAL_COMMON)
    p.para("Edite le 03/04/2026 - Direction des Credits aux Particuliers.")


TRUTH_BNP = {"bank": "BNP Paribas", "credit_type": "immobilier", "rate_type": "variable",
             "amount": 312500.0, "duration_months": 240, "rate_nominal": 3.72, "taeg": 4.12,
             "taea": 0.41, "fees": 950.0, "total_cost": 143312.40, "offer_date": date(2026, 4, 3)}


def build_sofinco(doc):
    p = Page(doc, "SOFINCO - CA Consumer Finance - Credit a la consommation",
             "CA Consumer Finance SA au capital de 554 482 422 EUR - SIREN 542 097 522 RCS Evry - "
             "1 rue Victor Basch 91068 Massy Cedex")
    p.title("OFFRE DE CONTRAT DE CREDIT - PRET PERSONNEL")
    p.para("Offre de pret personnel (credit a la consommation) emise le 21/05/2026. L'emprunteur "
           "dispose d'un delai de retractation de quatorze jours calendaires a compter de la signature. "
           "Score d'acceptation : dossier n° 88-441-002. Le vendeur n'est pas habilite a percevoir de "
           "fonds. Taux applicable en cas de retard de paiement : 9,54 %.")
    p.gap()
    p.title("ENCADRE D'INFORMATIONS PRECONTRACTUELLES", 10)
    p.kv("Montant du credit", "12 800,00 EUR")
    p.kv("Duree du contrat", "48 mois")
    p.kv("Taux debiteur fixe", "6,90 %")
    p.kv("Taux Annuel Effectif Global (TAEG)", "7,54 %")
    p.kv("Frais de dossier", "0,00 EUR")
    p.kv("Mensualite (assurance comprise)", "311,79 EUR")
    p.kv("Cout total du credit", "2 165,92 EUR")
    p.gap()
    p.title("ASSURANCE FACULTATIVE", 10)
    p.para("Assurance emprunteur facultative DIM (deces, invalidite, maladie). Cout mensuel de "
           "l'assurance : 5,87 EUR. Taux annuel effectif de l'assurance (TAEA) : 1,10 %. En cas "
           "d'adhesion, l'assurance est resiliable annuellement.")
    p.gap()
    p.para(LEGAL_COMMON)


TRUTH_SOF = {"bank": "Sofinco", "credit_type": "consommation", "rate_type": "fixe",
             "amount": 12800.0, "duration_months": 48, "rate_nominal": 6.90, "taeg": 7.54,
             "taea": 1.10, "fees": 0.0, "total_cost": 2165.92, "offer_date": date(2026, 5, 21)}


def build_caisse_epargne_regroupement(doc):
    p = Page(doc, "CAISSE D'EPARGNE Ile-de-France - Regroupement de credits",
             "Caisse d'Epargne IDF - Banque cooperative - SIREN 382 900 942 - 19 rue du Louvre 75001 Paris")
    p.title("OFFRE DE REGROUPEMENT DE CREDITS")
    p.para("La presente operation de regroupement de credits, emise le 09/02/2026, se substitue aux "
           "engagements suivants, qui seront rembourses par anticipation a la mise en place :")
    p.para("- Credit renouvelable FLOA n° 4471 : solde 6 240,18 EUR, taux actuel 12,40 % ;", indent=62)
    p.para("- Pret personnel Cofidis n° 9982 : solde 11 380,00 EUR, taux actuel 7,90 % ;", indent=62)
    p.para("- Credit auto CIC n° 5521 : solde 8 920,45 EUR, taux actuel 4,30 %.", indent=62)
    p.para("Total des soldes rachetes : 26 540,63 EUR, auxquels s'ajoutent une tresorerie complementaire "
           "de 31 859,37 EUR et les indemnites de remboursement anticipe.")
    p.gap()
    p.title("CONDITIONS DU NOUVEAU CREDIT", 10)
    p.kv("Montant du credit", "58 400,00 EUR")
    p.kv("Duree", "120 mois")
    p.kv("Taux debiteur fixe", "5,20 %")
    p.kv("TAEG", "5,96 %")
    p.kv("TAEA", "0,80 %")
    p.kv("Frais de dossier", "990,00 EUR")
    p.kv("Cout total du credit", "18 244,80 EUR")
    p.kv("Mensualite assurance comprise", "638,71 EUR")
    p.gap()
    p.para("Attention : regrouper des credits peut allonger la duree de remboursement et augmenter le "
           "cout total du credit. " + LEGAL_COMMON)
    p.para("Fait le 09/02/2026 a Paris.")


TRUTH_CE = {"bank": "Caisse d'Épargne", "credit_type": "regroupement", "rate_type": "fixe",
            "amount": 58400.0, "duration_months": 120, "rate_nominal": 5.20, "taeg": 5.96,
            "taea": 0.80, "fees": 990.0, "total_cost": 18244.80, "offer_date": date(2026, 2, 9)}


def build_bourso_auto(doc):
    p = Page(doc, "BOURSOBANK - Credit auto en ligne",
             "Boursobank SA au capital de 51 171 597 EUR - SIREN 351 058 151 RCS Nanterre - "
             "44 rue Traversiere 92100 Boulogne-Billancourt")
    p.title("OFFRE DE CREDIT AUTO - VEHICULE D'OCCASION")
    p.para("Offre emise le 28/06/2026 pour le financement automobile d'un vehicule d'occasion de moins "
           "de 36 mois (Peugeot 308, immatriculee a Toulouse (31) en 2024, prix d'achat 26 900,00 EUR, apport personnel "
           "2 600,00 EUR). Offre valable jusqu'au 12/07/2026. Premiere echeance 30 jours apres "
           "deblocage des fonds.")
    p.gap()
    p.title("CONDITIONS FINANCIERES", 10)
    p.kv("Montant du credit", "24 300,00 EUR")
    p.kv("Duree", "60 mois")
    p.kv("Taux debiteur fixe", "4,85 %")
    p.kv("Taux Annuel Effectif Global (TAEG)", "5,32 %")
    p.kv("Frais de dossier", "150,00 EUR")
    p.kv("Cout total du credit", "3 391,00 EUR")
    p.kv("Mensualite hors assurance", "456,52 EUR")
    p.gap()
    p.title("ASSURANCE FACULTATIVE", 10)
    p.para("TAEA : 0,60 %. Cotisation 6,08 EUR/mois. Souscription en ligne, resiliable a tout moment "
           "apres la premiere annee. Taux de retard applicable en cas d'impaye : 8,32 %.")
    p.gap()
    p.para(LEGAL_COMMON)


TRUTH_BB = {"region": "Occitanie", "bank": "Boursobank", "credit_type": "auto", "rate_type": "fixe",
            "amount": 24300.0, "duration_months": 60, "rate_nominal": 4.85, "taeg": 5.32,
            "taea": 0.60, "fees": 150.0, "total_cost": 3391.0, "offer_date": date(2026, 6, 28)}


def build_lbp_scan(doc):
    """Sera rasterisé (PDF image) après construction -> force le chemin OCR."""
    p = Page(doc, "LA BANQUE POSTALE - Pret immobilier",
             "La Banque Postale SA au capital de 6 585 350 218 EUR - SIREN 421 100 645 RCS Paris")
    p.title("OFFRE DE PRET IMMOBILIER")
    p.para("Offre emise le 17/03/2026. Pret immobilier destine a l'acquisition d'une residence "
           "principale situee a Rennes (35).")
    p.gap()
    p.title("CONDITIONS FINANCIERES", 10)
    p.kv("Montant du credit", "185 000,00 EUR")
    p.kv("Duree", "240 mois")
    p.kv("Taux debiteur fixe", "3,55 %")
    p.kv("TAEG", "4,05 %")
    p.kv("TAEA", "0,48 %")
    p.kv("Frais de dossier", "800,00 EUR")
    p.kv("Cout total du credit", "84 620,00 EUR")
    p.gap()
    p.para("Delai de reflexion de dix jours. Un credit vous engage et doit etre rembourse. "
           "Fait le 17/03/2026.")


TRUTH_LBP = {"region": "Bretagne", "bank": "La Banque Postale", "credit_type": "immobilier", "rate_type": "fixe",
             "amount": 185000.0, "duration_months": 240, "rate_nominal": 3.55, "taeg": 4.05,
             "taea": 0.48, "fees": 800.0, "total_cost": 84620.0, "offer_date": date(2026, 3, 17)}




# ─────────────────────────────────────────────
#  Documents "extrêmes" : multi-pages, logos, couleurs, pièges maximum
# ─────────────────────────────────────────────
def build_socgen_gros_dossier(doc):
    p = Page(doc, "SOCIETE GENERALE - Credit Immobilier - Direction Clientele des Particuliers",
             "Societe Generale SA au capital de 1 003 724 927,50 EUR - SIREN 552 120 222 RCS Paris - "
             "ORIAS 07 022 493 - 29 bd Haussmann 75009 Paris", accent=(0.83, 0.09, 0.16))
    # Page 1 - couverture
    p.logo("SOCIETE GENERALE", "square")
    p.band("OFFRE DE PRET IMMOBILIER N° SG-2026-1187-334907")
    p.para("Emprunteurs : M. et Mme X (parts respectives 50/50). Objet : acquisition d'une residence "
           "principale a Nantes (44). Notaire : Me Y, office notarial de Nantes. La presente offre est "
           "emise le 05/05/2026 et demeure valable jusqu'au 19/05/2026. L'acceptation ne peut intervenir "
           "avant l'expiration d'un delai de reflexion de 10 jours, soit au plus tot le 16/05/2026.")
    p.gap()
    p.title("SOMMAIRE", 10)
    for line in ["1. Fiche d'information standardisee europeenne (FISE)",
                 "2. Conditions particulieres du credit", "3. Tableau d'amortissement previsionnel",
                 "4. Notice d'assurance emprunteur", "5. Conditions generales et mentions legales",
                 "6. Acceptation de l'offre"]:
        p.para(line, indent=62)
    p.pagebreak()

    # Page 2 - FISE avec EXEMPLE REPRESENTATIF (piege majeur : d'autres taux !)
    p.band("1. FICHE D'INFORMATION STANDARDISEE EUROPEENNE")
    p.para("La presente fiche a un caractere purement informatif. Les valeurs ci-dessous constituent un "
           "exemple representatif au sens de la reglementation et NE constituent PAS les conditions de "
           "votre credit, detaillees en section 2.")
    p.box(["Exemple representatif : pour un credit immobilier de 200 000,00 EUR sur 240 mois,",
           "taux debiteur fixe de 3,90 %, TAEG de 4,80 %, mensualite de 1 208,00 EUR,",
           "cout total du credit de 98 456,00 EUR, assurance TAEA 0,61 %, frais de dossier 1 000,00 EUR."])
    p.para("Le taux d'usure applicable s'etablit a 6,04 %. En cas d'impaye, taux majore de 3,00 points. "
           "Indice de reference des prets a taux revisable : Euribor 12 mois, valeur 2,61 %.")
    p.pagebreak()

    # Page 3 - CONDITIONS PARTICULIERES (les vraies valeurs)
    p.band("2. CONDITIONS PARTICULIERES DU CREDIT")
    p.kv("Nature", "Pret immobilier amortissable a taux fixe")
    p.kv("Montant du credit", "289 000,00 EUR")
    p.kv("Duree", "264 mois (22 ans)")
    p.kv("Taux debiteur fixe", "3,42 % l'an")
    p.kv("Taux Annuel Effectif Global (TAEG)", "3,87 %")
    p.kv("Taux Annuel Effectif de l'Assurance (TAEA)", "0,38 %")
    p.kv("Frais de dossier", "1 150,00 EUR")
    p.kv("Frais de garantie (Credit Logement)", "3 120,00 EUR")
    p.kv("Cout total du credit", "121 743,44 EUR")
    p.kv("Mensualite hors assurance", "1 561,02 EUR")
    p.gap()
    p.para("Le taux de periode mensuel s'etablit a 0,2850 %. Domiciliation des salaires demandee sur le "
           "compte SG n° 30003 01187 00050078965 33 (IBAN FR76 3000 3011 8700 0500 7896 533).")
    p.pagebreak()

    # Pages 4-8 : TABLEAU D'AMORTISSEMENT COMPLET (264 lignes de leurres)
    p.band("3. TABLEAU D'AMORTISSEMENT PREVISIONNEL")
    p.para("Montants exprimes en euros, hors assurance. Tableau etabli sous reserve du deblocage complet "
           "des fonds au 01/07/2026.")
    rows, _m = amort_rows(289000.0, 3.42, 264, date(2026, 7, 1))
    p.table(["N°", "Echeance", "Mensualite", "Interets", "Capital amorti", "Capital restant du"],
            rows, [40, 85, 95, 95, 105, 115])

    # Notice assurance (pleine de % leurres)
    p.band("4. NOTICE D'ASSURANCE EMPRUNTEUR")
    p.para("Contrat groupe Sogecap n° 2971. Garanties souscrites : deces (quotite 100 %), perte totale "
           "et irreversible d'autonomie (quotite 100 %), incapacite temporaire totale au-dela d'une "
           "franchise de 90 jours, invalidite permanente si taux d'invalidite superieur a 66 %. "
           "Prise en charge partielle entre 33 % et 66 %. Cotisation mensuelle : 91,52 EUR. "
           "Le Taux Annuel Effectif de l'Assurance (TAEA) ressort a 0,38 %, soit un cout total "
           "d'assurance de 24 161,28 EUR sur la duree du pret. Possibilite de deleguation d'assurance "
           "(lois Lagarde et Lemoine) sous reserve d'equivalence des garanties.")
    p.pagebreak()

    # Mentions legales + recap (repetition des VRAIES valeurs)
    p.band("5. CONDITIONS GENERALES ET MENTIONS LEGALES")
    p.para(LEGAL_COMMON)
    p.para("Indemnite de remboursement anticipe : 3 % du capital restant du dans la limite de six mois "
           "d'interets. Frais de mainlevee d'hypotheque le cas echeant : 380,00 EUR.")
    p.gap()
    p.band("6. ACCEPTATION - RECAPITULATIF")
    p.box(["Recapitulatif de votre credit : montant du credit 289 000,00 EUR sur 264 mois,",
           "taux debiteur fixe 3,42 %, TAEG 3,87 %, TAEA 0,38 %,",
           "cout total du credit 121 743,44 EUR, frais de dossier 1 150,00 EUR."],
          fill=(0.93, 0.97, 0.93))
    p.para("Offre emise le 05/05/2026 par la Societe Generale. Signature precedee de la mention "
           "manuscrite « lu et approuve, bon pour acceptation de l'offre ».")


TRUTH_SG = {"region": "Pays de la Loire", "bank": "Société Générale", "credit_type": "immobilier", "rate_type": "fixe",
            "amount": 289000.0, "duration_months": 264, "rate_nominal": 3.42, "taeg": 3.87,
            "taea": 0.38, "fees": 1150.0, "total_cost": 121743.44, "offer_date": date(2026, 5, 5)}


def build_cofidis_promo(doc):
    p = Page(doc, "COFIDIS - Credit et solutions de paiement - www.cofidis.fr",
             "Cofidis SA au capital de 67 500 000 EUR - SIREN 325 307 106 RCS Lille Metropole - "
             "Parc de la Haute Borne, 61 av Halley, 59866 Villeneuve-d'Ascq", accent=(0.78, 0.05, 0.1))
    # Couverture avec PROMO piege (taux d'appel)
    p.logo("cofidis", "circle")
    p.band("OFFRE DE CONTRAT DE CREDIT - PRET PERSONNEL PROJET")
    p.box(["OFFRE FLASH : taux debiteur promotionnel de 1,00 % pendant les 3 premiers mois,",
           "puis taux contractuel. Offre promotionnelle soumise a conditions, reservee aux",
           "nouveaux clients pour toute demande avant le 31/01/2026."],
          fill=(1.0, 0.92, 0.92))
    p.para("Offre emise le 11/01/2026. Dossier n° CF-2026-00441-887. Delai de retractation de 14 jours "
           "calendaires. Taux applicable en cas de retard de paiement : 10,12 %. Le taux d'usure de la "
           "categorie s'etablit a 12,55 %.")
    p.pagebreak()

    # Informations europeennes normalisees avec EXEMPLE piege
    p.band("INFORMATIONS PRECONTRACTUELLES EUROPEENNES NORMALISEES")
    p.para("Exemple representatif : un credit de 15 000,00 EUR sur 60 mois au taux debiteur fixe de "
           "6,50 % correspond a un TAEG de 7,20 %, 60 mensualites de 293,49 EUR et un cout total du "
           "credit de 2 609,40 EUR. Cet exemple ne constitue pas votre offre.")
    p.gap()
    p.band("CONDITIONS DE VOTRE CREDIT")
    p.kv("Montant du credit", "9 500,00 EUR")
    p.kv("Duree du contrat", "42 mois")
    p.kv("Taux debiteur fixe", "5,49 %")
    p.kv("Taux Annuel Effectif Global (TAEG)", "6,17 %")
    p.kv("Frais de dossier", "0,00 EUR")
    p.kv("Mensualite (hors assurance)", "252,82 EUR")
    p.kv("Cout total du credit", "1 118,26 EUR")
    p.kv("Montant total du", "10 618,26 EUR")
    p.pagebreak()

    # Assurance facultative (leurres %)
    p.band("ASSURANCE FACULTATIVE")
    p.para("Assurance DIM facultative. Garanties : deces (quotite 100 %), invalidite si taux superieur "
           "a 66 %, maladie au-dela de 60 jours d'arret. Cout mensuel : 8,93 EUR. Taux Annuel Effectif "
           "de l'Assurance (TAEA) : 1,25 %. L'adhesion n'est pas une condition d'octroi du credit.")
    p.gap()
    # Tableau d'amortissement complet
    p.band("TABLEAU D'AMORTISSEMENT")
    rows, _m = amort_rows(9500.0, 5.49, 42, date(2026, 2, 5))
    p.table(["N°", "Echeance", "Mensualite", "Interets", "Capital", "Restant du"],
            rows, [40, 85, 95, 95, 100, 110])

    p.band("RECAPITULATIF")
    p.box(["Votre credit : 9 500,00 EUR sur 42 mois - taux debiteur fixe 5,49 % - TAEG 6,17 %",
           "TAEA 1,25 % - frais de dossier 0,00 EUR - cout total du credit 1 118,26 EUR."],
          fill=(0.93, 0.97, 0.93))
    p.para(LEGAL_COMMON)
    p.para("Fait a Villeneuve-d'Ascq, le 11/01/2026.")


TRUTH_COF = {"bank": "Cofidis", "credit_type": "consommation", "rate_type": "fixe",
             "amount": 9500.0, "duration_months": 42, "rate_nominal": 5.49, "taeg": 6.17,
             "taea": 1.25, "fees": 0.0, "total_cost": 1118.26, "offer_date": date(2026, 1, 11)}


def build_banquepop_regroupement_dossier(doc):
    p = Page(doc, "BANQUE POPULAIRE Val de France - Solutions de regroupement de credits",
             "Banque Populaire Val de France - Societe cooperative - SIREN 549 800 373 - "
             "9 av Newton 78180 Montigny-le-Bretonneux", accent=(0.0, 0.35, 0.65))
    p.logo("BANQUE POPULAIRE", "bars")
    p.band("OFFRE DE REGROUPEMENT DE CREDITS N° BP-2026-0455-112")
    p.para("Offre emise le 30/04/2026, valable jusqu'au 14/05/2026. La presente operation de "
           "regroupement de credits est assortie d'une garantie hypothecaire de premier rang sur le "
           "bien situe a Tours (37), dont la valeur estimee par expertise s'etablit a 310 000,00 EUR.")
    p.gap()
    p.title("SITUATION AVANT REGROUPEMENT", 10)
    p.para("Les engagements suivants seront rembourses par anticipation a la mise en place (taux "
           "actuels constates au 15/04/2026) :")
    p.table(["Organisme", "Nature", "Solde (EUR)", "Taux actuel", "Mensualite (EUR)"],
            [("FLOA Bank", "Credit renouvelable", "8 442,17", "15,90 %", "312,00"),
             ("Cofidis", "Pret personnel", "12 380,50", "11,20 %", "298,45"),
             ("CIC", "Credit auto", "9 926,00", "6,75 %", "245,10"),
             ("Oney", "Paiement fractionne", "2 151,33", "9,90 %", "89,60"),
             ("Franfinance", "Credit travaux", "18 000,00", "4,10 %", "340,22")],
            [110, 120, 90, 85, 90], size=8.2)
    p.para("Total des soldes rachetes : 50 900,00 EUR. Indemnites de remboursement anticipe estimees : "
           "612,40 EUR. Tresorerie complementaire : 23 387,60 EUR.")
    p.pagebreak()

    p.band("CONDITIONS DU NOUVEAU CREDIT")
    p.kv("Montant du credit", "74 900,00 EUR")
    p.kv("Duree", "144 mois (12 ans)")
    p.kv("Taux debiteur fixe", "4,95 %")
    p.kv("Taux Annuel Effectif Global (TAEG)", "5,74 %")
    p.kv("Taux Annuel Effectif de l'Assurance (TAEA)", "0,72 %")
    p.kv("Frais de dossier", "1 490,00 EUR")
    p.kv("Frais d'inscription hypothecaire", "1 123,00 EUR")
    p.kv("Cout total du credit", "24 862,12 EUR")
    p.kv("Mensualite assurance comprise", "731,17 EUR")
    p.gap()
    p.para("Reglement par prelevement sur le compte IBAN FR76 1020 7000 4104 0410 5678 921. Frais de "
           "notaire et de mainlevee eventuels non compris. Nouvelle mensualite totale ramenee de "
           "1 285,37 EUR a 731,17 EUR, soit une baisse de 43 %, en contrepartie d'un allongement de la "
           "duree de remboursement et d'une augmentation du cout total du credit.")
    p.pagebreak()

    p.band("TABLEAU D'AMORTISSEMENT PREVISIONNEL")
    rows, _m = amort_rows(74900.0, 4.95, 144, date(2026, 6, 5))
    p.table(["N°", "Echeance", "Mensualite", "Interets", "Capital amorti", "Capital restant du"],
            rows, [40, 85, 95, 95, 105, 115])

    p.band("RECAPITULATIF ET ACCEPTATION")
    p.box(["Nouveau credit : montant du credit 74 900,00 EUR sur 144 mois,",
           "taux debiteur fixe 4,95 % - TAEG 5,74 % - TAEA 0,72 %,",
           "frais de dossier 1 490,00 EUR - cout total du credit 24 862,12 EUR."],
          fill=(0.93, 0.97, 0.93))
    p.para("Attention : regrouper des credits peut allonger la duree de remboursement et augmenter le "
           "cout total du credit. " + LEGAL_COMMON)
    p.para("Fait a Tours, le 30/04/2026, en deux exemplaires originaux.")


TRUTH_BP = {"region": "Centre-Val de Loire", "bank": "Banque Populaire", "credit_type": "regroupement", "rate_type": "fixe",
            "amount": 74900.0, "duration_months": 144, "rate_nominal": 4.95, "taeg": 5.74,
            "taea": 0.72, "fees": 1490.0, "total_cost": 24862.12, "offer_date": date(2026, 4, 30)}




def build_creditmutuel_multiprets(doc):
    p = Page(doc, "CREDIT MUTUEL Nord Europe - Pret immobilier - Financement multi-prets",
             "Caisse Federale du Credit Mutuel Nord Europe - SIREN 320 342 264 - "
             "4 place Richebe 59000 Lille", accent=(0.05, 0.25, 0.55))
    p.logo("Credit Mutuel", "square")
    p.band("OFFRE DE PRET IMMOBILIER - OPERATION COMPOSEE DE PLUSIEURS PRETS")
    p.para("Offre emise le 20/06/2026, valable 30 jours. Financement de l'acquisition d'une "
           "residence principale situee a Lille (59). L'operation est financee par DEUX prets "
           "distincts decrits ci-dessous ; le TAEG global couvre l'ensemble de l'operation.")
    p.gap()
    p.band("PLAN DE FINANCEMENT")
    p.kv("Montant du credit (operation globale)", "260 000,00 EUR")
    p.kv("TAEG global de l'operation", "3,71 %")
    p.kv("TAEA (assurance, ensemble des prets)", "0,36 %")
    p.kv("Frais de dossier", "980,00 EUR")
    p.kv("Cout total du credit", "118 240,00 EUR")
    p.gap()
    p.title("PRET N° 1 - PRET PRINCIPAL", 10)
    p.kv("Montant", "220 000,00 EUR")
    p.kv("Duree", "300 mois")
    p.kv("Taux debiteur fixe", "3,45 %")
    p.kv("Mensualite hors assurance", "1 096,45 EUR")
    p.gap()
    p.title("PRET N° 2 - PRET A TAUX ZERO (PTZ)", 10)
    p.kv("Montant", "40 000,00 EUR")
    p.kv("Duree", "240 mois")
    p.kv("Taux debiteur", "0,00 %")
    p.kv("Mensualite", "166,67 EUR")
    p.para("Le pret a taux zero est accorde sous conditions de ressources (article L.31-10-1 du "
           "Code de la construction). Periode de differe possible selon la tranche de revenus.")
    p.pagebreak()
    p.band("RECAPITULATIF")
    p.box(["Operation globale : montant du credit 260 000,00 EUR - TAEG global 3,71 %",
           "Pret principal 220 000,00 EUR sur 300 mois au taux debiteur fixe de 3,45 %",
           "Pret a taux zero 40 000,00 EUR sur 240 mois - TAEA 0,36 % - cout total du credit 118 240,00 EUR."],
          fill=(0.93, 0.97, 0.93))
    p.para(LEGAL_COMMON)
    p.para("Fait a Lille, le 20/06/2026.")


TRUTH_CM = {"region": "Hauts-de-France", "bank": "Crédit Mutuel", "credit_type": "immobilier",
            "rate_type": "fixe", "amount": 260000.0, "duration_months": 300,
            "rate_nominal": 3.45, "taeg": 3.71, "taea": 0.36, "fees": 980.0,
            "total_cost": 118240.0, "offer_date": date(2026, 6, 20), "n_sub_loans": 2}


# ─────────────────────────────────────────────
#  Vague v5 : assurance mélangée au crédit, valeurs éparpillées entre les
#  pages ou reléguées en toute fin, murs de texte réglementaire.
# ─────────────────────────────────────────────
CGV_WALL = [
    "Article 1 - Objet. Le present contrat a pour objet de definir les conditions dans lesquelles le "
    "preteur consent a l'emprunteur le credit vise aux conditions particulieres. L'emprunteur reconnait "
    "avoir recu, prealablement a la presente offre, la fiche d'information precontractuelle ainsi que la "
    "notice d'information relative au contrat d'assurance de groupe, et avoir ete informe qu'il peut "
    "souscrire une assurance equivalente aupres de l'assureur de son choix.",
    "Article 2 - Deblocage des fonds. Les fonds seront verses en une ou plusieurs fois sur instruction "
    "de l'emprunteur, au plus tard dans les quatre mois de l'acceptation. A defaut d'utilisation dans ce "
    "delai, l'offre sera caduque et les sommes eventuellement versees devront etre restituees sans delai, "
    "majorees des interets courus calcules prorata temporis sur la base d'une annee de 360 jours.",
    "Article 3 - Remboursement anticipe. L'emprunteur peut rembourser tout ou partie du credit par "
    "anticipation. Une indemnite pourra etre exigee, egale a un semestre d'interets sur le capital "
    "rembourse au taux moyen du credit, sans pouvoir depasser 3 % du capital restant du avant "
    "remboursement. Aucune indemnite n'est due lorsque le remboursement est consecutif a la vente du "
    "bien faisant suite a un changement du lieu d'activite professionnelle, au deces ou a la cessation "
    "forcee d'activite de l'un des emprunteurs.",
    "Article 4 - Defaillance. En cas de defaillance de l'emprunteur, le preteur pourra exiger le "
    "remboursement immediat du capital restant du, majore des interets echus mais non payes. Les sommes "
    "restant dues produisent des interets de retard a un taux majore de 3,00 points jusqu'a la date du "
    "reglement effectif. Les incidents de paiement caracterises font l'objet d'une inscription au "
    "fichier FICP tenu par la Banque de France.",
    "Article 5 - Mediation et reclamation. Toute reclamation peut etre adressee au service relations "
    "clientele du preteur. A defaut de reponse satisfaisante dans un delai de 60 jours, le mediateur de "
    "l'etablissement peut etre saisi gratuitement. La liste des pieces necessaires a l'instruction du "
    "dossier de mediation est disponible sur le site du mediateur.",
]

NOTICE_ASSURANCE_WALL = [
    "La presente notice d'information definit les garanties du contrat d'assurance de groupe souscrit "
    "par le preteur au profit de ses clients emprunteurs. Garanties : deces (quotite 100 %), perte "
    "totale et irreversible d'autonomie (quotite 100 %), incapacite temporaire totale de travail "
    "au-dela d'une franchise de 90 jours, invalidite permanente totale lorsque le taux d'invalidite "
    "est superieur ou egal a 66 %, prise en charge partielle lorsque ce taux est compris entre 33 % "
    "et 66 %. La periode d'indemnisation est limitee a 1 095 jours par sinistre.",
    "Exclusions principales : faits volontaires de l'assure, sports aeriens pratiques a titre "
    "professionnel, sejours de plus de 90 jours consecutifs hors Union europeenne, affections dorsales "
    "et psychiques non objectivables sauf hospitalisation superieure a 10 jours. Les garanties cessent "
    "au 31 decembre de l'annee du soixante-dixieme anniversaire de l'assure.",
    "En application des lois Lagarde et Lemoine, l'emprunteur peut souscrire une assurance individuelle "
    "presentant un niveau de garantie equivalent aupres de l'assureur de son choix, et resilier le "
    "contrat en cours a tout moment sans frais ni penalite.",
]


def build_lcl_eparpille(doc):
    """Les informations sont volontairement dispersées : une valeur par page,
    en pleine phrase, jamais de tableau récapitulatif en tête de document."""
    p = Page(doc, "LCL - Le Credit Lyonnais - Direction des Prets aux Particuliers",
             "LCL SA au capital de 2 037 713 591 EUR - SIREN 954 509 741 RCS Lyon - "
             "18 rue de la Republique 69002 Lyon", accent=(0.0, 0.2, 0.5))
    p.logo("LCL", "bars")
    p.band("OFFRE DE PRET IMMOBILIER N° LCL-2026-0663-118 - DOSSIER COMPLET")
    p.para("La presente offre de pret immobilier est emise le 14/04/2026 et demeure valable jusqu'au "
           "13/05/2026 inclus. Les caracteristiques financieres de votre credit sont detaillees dans "
           "les differentes sections du present dossier, chacune traitant d'un aspect de l'operation ; "
           "aucune synthese chiffree n'est fournie en tete de document.")
    p.gap()
    p.title("SOMMAIRE", 10)
    for line in ["1. Presentation de l'operation", "2. Objet du financement",
                 "3. Conditions generales applicables", "4. Assurance emprunteur",
                 "5. Taux applicables au credit", "6. Frais et cout du credit", "7. Acceptation"]:
        p.para(line, indent=62)
    p.pagebreak()

    # Page 2 : le MONTANT, en pleine phrase, entoure de montants leurres
    p.band("1. PRESENTATION DE L'OPERATION - 2. OBJET DU FINANCEMENT")
    p.para("L'operation financee est l'achat d'une residence principale situee a Lyon (69), quartier "
           "de la Croix-Rousse, dependant de l'agence de Lyon Terreaux. Prix d'achat du bien tel que "
           "stipule au compromis de vente : 265 000,00 EUR, auquel s'ajoutent les frais de notaire "
           "estimes a 19 875,00 EUR. L'emprunteur declare disposer d'un apport personnel de "
           "66 500,00 EUR verse directement entre les mains du notaire au jour de la signature.")
    p.para("Apres imputation de l'apport, le montant du credit consenti par LCL s'eleve a "
           "198 500,00 EUR, le solde du prix et des frais etant couvert par l'apport personnel. Le "
           "deblocage interviendra en une seule fois entre les mains du notaire.")
    p.pagebreak()

    # Page 3 : mur de texte + la DUREE en pleine phrase
    p.band("3. CONDITIONS GENERALES APPLICABLES")
    for art in CGV_WALL:
        p.para(art)
        p.gap(4)
    p.para("Le remboursement du credit s'effectue par echeances mensuelles constantes, sur une duree "
           "de 276 mois a compter du premier deblocage des fonds, sous reserve des cas de differe "
           "vises aux conditions generales.")
    p.pagebreak()

    # Page 4 : assurance (le TAEA au milieu de pourcentages leurres)
    p.band("4. ASSURANCE EMPRUNTEUR")
    for blk in NOTICE_ASSURANCE_WALL:
        p.para(blk)
        p.gap(4)
    p.para("Capital assure : 198 500,00 EUR (quotite 100 % sur chaque tete). Cotisation mensuelle "
           "d'assurance : 76,12 EUR, prelevee avec l'echeance du credit. Le Taux Annuel Effectif de "
           "l'Assurance (TAEA) s'etablit a 0,46 %.")
    p.pagebreak()

    # Page 5 : les TAUX, en pleine phrase, avec taux de periode et usure en leurres
    p.band("5. TAUX APPLICABLES AU CREDIT")
    p.para("Le credit est consenti au taux debiteur fixe de 3,58 % l'an, soit un taux de periode "
           "mensuel de 0,2983 %. Ce taux est ferme et definitif pour toute la duree du credit. Le "
           "taux d'usure applicable a la categorie s'etablit a 6,04 % ; en toute hypothese, le taux "
           "applique ne pourra exceder ce plafond reglementaire.")
    p.para("Le taux annuel effectif global (TAEG) du credit, calcule conformement aux articles "
           "R.314-1 et suivants du Code de la consommation et integrant les interets, les frais de "
           "dossier, les frais de garantie et les primes d'assurance obligatoire, ressort a 4,02 %.")
    p.pagebreak()

    # Page 6 : les FRAIS et le COUT TOTAL, en pleine phrase
    p.band("6. FRAIS ET COUT DU CREDIT")
    p.para("Les frais de dossier s'elevent a 1 350,00 EUR, preleves sur le compte de l'emprunteur au "
           "premier deblocage. Les frais de garantie (inscription d'hypotheque conventionnelle) sont "
           "estimes a 2 940,00 EUR et regles directement aupres du notaire. Aucuns frais de courtage "
           "ne sont dus au titre de la presente operation.")
    p.para("Compte tenu de l'ensemble de ces elements, le cout total du credit (interets, frais et "
           "assurance obligatoire compris) s'etablit a 132 419,16 EUR, et le montant total du par "
           "l'emprunteur a 330 919,16 EUR.")
    p.pagebreak()

    # Page 7 : acceptation (l'unique rappel des valeurs, en pleine phrase)
    p.band("7. ACCEPTATION")
    p.para("Je soussigne(e), apres avoir pris connaissance de l'integralite du present dossier, "
           "declare accepter l'offre de pret immobilier de LCL portant sur un montant du credit de "
           "198 500,00 EUR au taux debiteur fixe de 3,58 %, TAEG de 4,02 %, TAEA de 0,46 %, pour un "
           "cout total du credit de 132 419,16 EUR. Mention manuscrite « lu et approuve, bon pour "
           "acceptation de l'offre ».")
    p.para("Fait a Lyon, le 14/04/2026, en deux exemplaires originaux.")


TRUTH_LCL = {"region": "Auvergne-Rhône-Alpes", "bank": "LCL", "credit_type": "immobilier",
             "rate_type": "fixe", "amount": 198500.0, "duration_months": 276,
             "rate_nominal": 3.58, "taeg": 4.02, "taea": 0.46, "fees": 1350.0,
             "total_cost": 132419.16, "offer_date": date(2026, 4, 14)}


def build_cic_annexe_fin(doc):
    """12+ pages : FISE avec exemple représentatif, murs de conditions générales,
    notice d'assurance pleine de leurres, tableau d'amortissement de 300 lignes…
    et les VRAIES conditions particulières uniquement en ANNEXE, en toute fin."""
    p = Page(doc, "CIC - Credit Industriel et Commercial - Pret immobilier",
             "CIC SA au capital de 611 858 064 EUR - SIREN 542 016 381 RCS Paris - "
             "6 avenue de Provence 75452 Paris Cedex 09", accent=(0.15, 0.45, 0.35))
    p.logo("CIC", "square")
    p.band("OFFRE DE PRET IMMOBILIER N° CIC-2026-8812-004")
    p.para("Offre emise le 18/05/2026, valable jusqu'au 17/06/2026. Objet : acquisition d'une "
           "residence principale situee a Strasbourg (67). IMPORTANT : les conditions particulieres "
           "chiffrees de votre credit figurent en ANNEXE 1, en toute fin du present dossier, apres "
           "les documents d'information reglementaires.")
    p.gap()
    p.title("SOMMAIRE", 10)
    for line in ["1. Fiche d'information standardisee europeenne (FISE)",
                 "2. Conditions generales du pret", "3. Notice d'assurance emprunteur",
                 "4. Tableau d'amortissement previsionnel",
                 "ANNEXE 1 - Conditions particulieres du credit (valeurs contractuelles)"]:
        p.para(line, indent=62)
    p.pagebreak()

    p.band("1. FICHE D'INFORMATION STANDARDISEE EUROPEENNE")
    p.para("Les valeurs ci-dessous constituent un exemple representatif au sens de la reglementation "
           "et NE constituent PAS les conditions de votre credit, qui figurent en annexe 1.")
    p.box(["Exemple representatif : pour un credit immobilier de 210 000,00 EUR sur 240 mois,",
           "taux debiteur fixe de 3,95 %, TAEG de 4,85 %, mensualite de 1 268,00 EUR,",
           "cout total du credit de 101 220,00 EUR, TAEA de 0,58 %, frais de dossier de 990,00 EUR."])
    p.para("Le taux d'usure applicable s'etablit a 6,04 %. Indice de reference des prets a taux "
           "revisable : Euribor 12 mois, valeur constatee 2,61 %. En cas d'impaye, les sommes dues "
           "portent interet a un taux majore de 3,00 points.")
    p.pagebreak()

    p.band("2. CONDITIONS GENERALES DU PRET")
    for art in CGV_WALL:
        p.para(art)
        p.gap(4)
    p.para("Article 6 - Garanties. Le present credit est garanti par le cautionnement de la societe "
           "Credit Logement. La commission de caution, reglee au deblocage, s'eleve a 3 260,00 EUR "
           "dont une part est restituable au terme du credit en l'absence d'incident.")
    p.para("Article 7 - Domiciliation. L'emprunteur s'engage a domicilier ses revenus sur un compte "
           "ouvert dans les livres du preteur pendant une duree de dix ans. En cas de non-respect, le "
           "taux debiteur pourra etre majore dans les conditions prevues aux conditions generales.")
    p.pagebreak()

    p.band("3. NOTICE D'ASSURANCE EMPRUNTEUR - CONTRAT GROUPE ACM")
    for blk in NOTICE_ASSURANCE_WALL:
        p.para(blk)
        p.gap(4)
    p.para("Bareme du contrat groupe : deces 0,24 %, perte totale et irreversible d'autonomie 0,11 %, "
           "incapacite de travail 0,14 % du capital initial par an. Cotisation mensuelle totale : "
           "94,30 EUR. Cout total de l'assurance sur la duree du pret : 26 040,00 EUR. Notice "
           "etablie le 02/01/2026, referentiel ACM-2026-A.")
    p.pagebreak()

    p.band("4. TABLEAU D'AMORTISSEMENT PREVISIONNEL")
    p.para("Tableau etabli hors assurance, sous reserve d'un deblocage unique des fonds au "
           "01/08/2026. Montants exprimes en euros.")
    rows, _m = amort_rows(276000.0, 3.49, 300, date(2026, 8, 1))
    p.table(["N°", "Echeance", "Mensualite", "Interets", "Capital amorti", "Capital restant du"],
            rows, [40, 85, 95, 95, 105, 115])
    p.pagebreak()

    p.band("ANNEXE 1 - CONDITIONS PARTICULIERES DU CREDIT")
    p.para("Les valeurs ci-dessous constituent les conditions contractuelles de votre credit et "
           "prevalent sur toute autre mention du dossier.")
    p.kv("Montant du credit", "276 000,00 EUR")
    p.kv("Duree", "300 mois")
    p.kv("Taux debiteur fixe", "3,49 %")
    p.kv("Taux Annuel Effectif Global (TAEG)", "3,94 %")
    p.kv("Taux Annuel Effectif de l'Assurance (TAEA)", "0,42 %")
    p.kv("Frais de dossier", "1 250,00 EUR")
    p.kv("Cout total du credit", "139 887,60 EUR")
    p.kv("Mensualite hors assurance", "1 380,22 EUR")
    p.gap()
    p.box(["Recapitulatif contractuel : montant du credit 276 000,00 EUR sur 300 mois,",
           "taux debiteur fixe 3,49 % - TAEG 3,94 % - TAEA 0,42 %,",
           "frais de dossier 1 250,00 EUR - cout total du credit 139 887,60 EUR."],
          fill=(0.93, 0.97, 0.93))
    p.para("Fait a Strasbourg, le 18/05/2026, en deux exemplaires originaux.")


TRUTH_CIC = {"region": "Grand Est", "bank": "CIC", "credit_type": "immobilier",
             "rate_type": "fixe", "amount": 276000.0, "duration_months": 300,
             "rate_nominal": 3.49, "taeg": 3.94, "taea": 0.42, "fees": 1250.0,
             "total_cost": 139887.60, "offer_date": date(2026, 5, 18)}


def build_fortuneo_mur(doc):
    """Aucun tableau, aucune ligne libellé/valeur : toutes les valeurs sont
    noyées en pleine phrase dans des paragraphes denses."""
    p = Page(doc, "FORTUNEO - Pret personnel en ligne - Espace credits",
             "Fortuneo - SIREN 384 288 890 - Tour Ariane, 5 place de la Pyramide, "
             "92088 Paris La Defense Cedex", accent=(0.85, 0.4, 0.0))
    p.logo("fortuneo", "circle")
    p.band("OFFRE DE CONTRAT DE CREDIT - PRET PERSONNEL")
    p.para("La presente offre de pret personnel est emise le 02/03/2026 par Fortuneo et reste "
           "valable quinze jours. L'emprunteur dispose d'un delai de retractation de quatorze jours "
           "calendaires a compter de la signature du contrat. Le taux applicable en cas de retard "
           "de paiement s'etablit a 9,88 %, et le taux d'usure de la categorie a 12,55 %. Dossier "
           "n° FT-2026-3321-908, etudie et accepte sous reserve des pieces justificatives.")
    p.pagebreak()

    p.band("CONDITIONS DE VOTRE CREDIT")
    p.para("Aux termes du present contrat, le montant du credit est fixe a 21 400,00 EUR, verse en "
           "une seule fois sur le compte de l'emprunteur dans un delai de sept jours suivant "
           "l'expiration du delai de retractation. Le credit est remboursable sur une duree de "
           "72 mois par echeances mensuelles constantes de 348,58 EUR hors assurance, prelevees le "
           "5 de chaque mois, la premiere echeance intervenant trente jours apres le versement des "
           "fonds. Le credit est consenti au taux debiteur fixe de 5,35 % l'an, ce qui, compte tenu "
           "de l'absence de frais annexes, conduit a un taux annuel effectif global (TAEG) de "
           "5,92 %. Aucuns frais de dossier ne sont factures au titre de la presente offre, soit des "
           "frais de dossier de 0,00 EUR. Le cout total du credit, correspondant a la difference "
           "entre le montant total du et le montant du credit, s'etablit a 3 744,72 EUR pour un "
           "montant total du de 25 144,72 EUR.")
    p.pagebreak()

    p.band("CONDITIONS GENERALES")
    for art in CGV_WALL:
        p.para(art)
        p.gap(4)
    p.pagebreak()

    p.band("ASSURANCE FACULTATIVE")
    for blk in NOTICE_ASSURANCE_WALL:
        p.para(blk)
        p.gap(4)
    p.para("En cas d'adhesion, la cotisation mensuelle s'etablit a 15,16 EUR pour un assure de "
           "moins de quarante-cinq ans, et le taux annuel effectif de l'assurance (TAEA) ressort a "
           "0,85 %. L'adhesion a l'assurance n'est pas une condition d'octroi du credit.")
    p.pagebreak()

    p.band("RECAPITULATIF ET SIGNATURE")
    p.para("Pour la parfaite information de l'emprunteur, il est rappele que le present pret "
           "personnel porte sur un montant du credit de 21 400,00 EUR remboursable sur une duree de "
           "72 mois au taux debiteur fixe de 5,35 %, que le TAEG s'etablit a 5,92 %, le TAEA de "
           "l'assurance facultative a 0,85 %, les frais de dossier a 0,00 EUR et le cout total du "
           "credit a 3 744,72 EUR. L'emprunteur reconnait avoir recu un exemplaire de l'offre et de "
           "la notice d'assurance. Fait le 02/03/2026.")


TRUTH_FOR = {"bank": "Fortuneo", "credit_type": "consommation", "rate_type": "fixe",
             "amount": 21400.0, "duration_months": 72, "rate_nominal": 5.35, "taeg": 5.92,
             "taea": 0.85, "fees": 0.0, "total_cost": 3744.72, "offer_date": date(2026, 3, 2)}


def build_axa_assurance_melangee(doc):
    """Dossier combiné crédit + assurance : la notice d'assurance domine le
    document et regorge de montants et de pourcentages qui miment ceux du
    crédit (coût total de l'assurance, capital assuré, barèmes, comparatif)."""
    p = Page(doc, "AXA BANQUE - Pret immobilier et assurance emprunteur - Dossier combine",
             "AXA Banque SA au capital de 89 467 136 EUR - SIREN 542 016 993 RCS Creteil - "
             "203 rue Carnot 94120 Fontenay-sous-Bois", accent=(0.0, 0.1, 0.55))
    p.logo("AXA Banque", "square")
    p.band("OFFRE DE PRET IMMOBILIER + DOSSIER D'ASSURANCE EMPRUNTEUR")
    p.para("Offre emise le 08/06/2026, valable trente jours, portant sur l'acquisition d'une "
           "residence principale situee a Marseille (13). Le present dossier regroupe l'offre de "
           "credit ET le dossier complet d'assurance emprunteur ; les elements relatifs a "
           "l'assurance ne se substituent pas aux conditions du credit.")
    p.pagebreak()

    p.band("CONDITIONS DU CREDIT")
    p.kv("Montant du credit", "231 000,00 EUR")
    p.kv("Duree", "288 mois")
    p.kv("Taux debiteur fixe", "3,62 %")
    p.kv("Taux Annuel Effectif Global (TAEG)", "4,18 %")
    p.kv("Frais de dossier", "1 100,00 EUR")
    p.kv("Cout total du credit", "128 316,48 EUR")
    p.kv("Mensualite hors assurance", "1 178,53 EUR")
    p.para("Le taux annuel effectif de l'assurance figure dans le dossier d'assurance ci-apres, "
           "partie integrante de la presente offre.")
    p.pagebreak()

    p.band("DOSSIER D'ASSURANCE EMPRUNTEUR - NOTICE D'INFORMATION")
    for blk in NOTICE_ASSURANCE_WALL:
        p.para(blk)
        p.gap(4)
    p.para("Le capital emprunte est assure a hauteur de 100 % sur chaque tete, soit une cotisation "
           "mensuelle de 84,90 EUR par assure. Frais d'adhesion a l'association des assures : "
           "25,00 EUR, preleves avec la premiere cotisation.")
    p.para("Cout total de l'assurance sur la duree du pret : 21 158,00 EUR. Le Taux Annuel Effectif "
           "de l'Assurance (TAEA) du contrat groupe retenu s'etablit a 0,44 %.")
    p.pagebreak()

    p.band("SIMULATION COMPARATIVE - DELEGATION D'ASSURANCE")
    p.para("A titre de simulation et pour la parfaite information de l'emprunteur, le tableau "
           "ci-dessous compare le contrat groupe a des contrats individuels du marche presentant un "
           "niveau de garantie equivalent. Ces valeurs ne constituent pas une offre.")
    p.table(["Assureur", "Formule", "Cotisation/mois (EUR)", "Taux assurance"],
            [("Contrat groupe AXA", "Confort", "84,90", "0,44 %"),
             ("Generali", "Emprunteur+", "61,20", "0,29 %"),
             ("MetLife", "Essentiel", "68,45", "0,34 %"),
             ("Suravenir", "Serenite", "97,10", "0,52 %"),
             ("MNCAP", "Integrale", "112,30", "0,61 %")],
            [130, 100, 120, 100], size=8.2)
    p.para("L'ecart de cotisation constate sur la simulation peut representer jusqu'a 8 916,00 EUR "
           "sur la duree du pret. La substitution reste possible a tout moment (loi Lemoine).")
    p.pagebreak()

    p.band("CONDITIONS GENERALES DU CREDIT")
    for art in CGV_WALL:
        p.para(art)
        p.gap(4)
    p.para("Montant total du par l'emprunteur au titre du credit : 359 316,48 EUR.")
    p.pagebreak()

    p.band("RECAPITULATIF ET ACCEPTATION")
    p.box(["Votre credit : montant du credit 231 000,00 EUR sur 288 mois,",
           "taux debiteur fixe 3,62 % - TAEG 4,18 % - TAEA 0,44 %,",
           "frais de dossier 1 100,00 EUR - cout total du credit 128 316,48 EUR."],
          fill=(0.93, 0.97, 0.93))
    p.para("Fait a Marseille, le 08/06/2026, en deux exemplaires originaux.")


TRUTH_AXA = {"region": "Provence-Alpes-Côte d'Azur", "bank": "AXA Banque",
             "credit_type": "immobilier", "rate_type": "fixe", "amount": 231000.0,
             "duration_months": 288, "rate_nominal": 3.62, "taeg": 4.18, "taea": 0.44,
             "fees": 1100.0, "total_cost": 128316.48, "offer_date": date(2026, 6, 8)}


def build_carrefour_fin(doc):
    """Toutes les valeurs contractuelles sont regroupées SUR LA DERNIERE PAGE,
    après une offre promotionnelle, des CGV et une notice d'assurance."""
    p = Page(doc, "CARREFOUR BANQUE - Credit projet - Service clients particuliers",
             "Carrefour Banque SA au capital de 101 346 956 EUR - SIREN 313 811 515 RCS Evry - "
             "1 place Copernic 91051 Evry Cedex", accent=(0.0, 0.4, 0.75))
    p.logo("Carrefour Banque", "circle")
    p.band("OFFRE DE CONTRAT DE CREDIT - PRET PERSONNEL PROJET")
    p.box(["OFFRE SPECIALE PORTEURS DE CARTE : taux debiteur promotionnel de 2,90 % pendant",
           "les 6 premiers mois pour toute demande signee avant le 31/03/2026, puis taux",
           "contractuel. Voir conditions de l'operation en magasin ou sur le site."],
          fill=(0.92, 0.96, 1.0))
    p.para("Offre emise le 23/02/2026. Dossier n° CB-2026-1104-552. Exemple representatif : un "
           "credit de 10 000,00 EUR sur 48 mois au taux debiteur fixe de 5,90 % correspond a un "
           "TAEG de 6,53 %, a 48 mensualites de 234,32 EUR et a un cout total du credit de "
           "1 247,36 EUR. Cet exemple ne constitue pas votre offre, dont les conditions figurent "
           "dans l'encadre recapitulatif en derniere page du present document.")
    p.pagebreak()

    p.band("CONDITIONS GENERALES DE VENTE ET D'UTILISATION")
    for art in CGV_WALL:
        p.para(art)
        p.gap(4)
    p.para("Le taux applicable en cas de retard de paiement s'etablit a 10,45 %. Le taux d'usure "
           "de la categorie s'etablit a 12,55 % au titre du trimestre en cours.")
    p.pagebreak()

    p.band("ASSURANCE FACULTATIVE DES EMPRUNTEURS")
    for blk in NOTICE_ASSURANCE_WALL:
        p.para(blk)
        p.gap(4)
    p.para("Bareme : deces 0,32 %, incapacite 0,19 % du capital initial par an. Cotisation "
           "mensuelle en cas d'adhesion : 4,12 EUR. Cout total de l'assurance sur la duree du "
           "contrat : 247,20 EUR.")
    p.pagebreak()

    p.band("ENCADRE RECAPITULATIF - CONDITIONS DE VOTRE CREDIT")
    p.kv("Montant du credit", "14 600,00 EUR")
    p.kv("Duree du contrat", "60 mois")
    p.kv("Taux debiteur fixe", "6,40 %")
    p.kv("Taux Annuel Effectif Global (TAEG)", "7,12 %")
    p.kv("Taux Annuel Effectif de l'Assurance (TAEA)", "1,05 %")
    p.kv("Frais de dossier", "90,00 EUR")
    p.kv("Mensualite hors assurance", "285,21 EUR")
    p.kv("Cout total du credit", "2 612,40 EUR")
    p.kv("Montant total du", "17 212,40 EUR")
    p.gap()
    p.para("Je reconnais avoir pris connaissance de l'offre de pret personnel portant sur un "
           "montant du credit de 14 600,00 EUR sur une duree de 60 mois au taux debiteur fixe de "
           "6,40 %, TAEG de 7,12 %, TAEA de 1,05 %, frais de dossier de 90,00 EUR, pour un cout "
           "total du credit de 2 612,40 EUR. Fait le 23/02/2026.")


TRUTH_CAR = {"bank": "Carrefour Banque", "credit_type": "consommation", "rate_type": "fixe",
             "amount": 14600.0, "duration_months": 60, "rate_nominal": 6.40, "taeg": 7.12,
             "taea": 1.05, "fees": 90.0, "total_cost": 2612.40, "offer_date": date(2026, 2, 23)}


DOCS = [
    ("credit_agricole_immobilier.pdf", build_credit_agricole, TRUTH_CA, False),
    ("bnp_immobilier_variable.pdf", build_bnp_variable, TRUTH_BNP, False),
    ("sofinco_pret_personnel.pdf", build_sofinco, TRUTH_SOF, False),
    ("caisse_epargne_regroupement.pdf", build_caisse_epargne_regroupement, TRUTH_CE, False),
    ("boursobank_auto.pdf", build_bourso_auto, TRUTH_BB, False),
    ("banque_postale_immobilier_SCAN.pdf", build_lbp_scan, TRUTH_LBP, True),
    ("socgen_immobilier_GROS_DOSSIER.pdf", build_socgen_gros_dossier, TRUTH_SG, False),
    ("cofidis_promo_piege.pdf", build_cofidis_promo, TRUTH_COF, False),
    ("banquepop_regroupement_DOSSIER.pdf", build_banquepop_regroupement_dossier, TRUTH_BP, False),
    ("creditmutuel_MULTIPRETS_ptz.pdf", build_creditmutuel_multiprets, TRUTH_CM, False),
    ("lcl_immobilier_EPARPILLE.pdf", build_lcl_eparpille, TRUTH_LCL, False),
    ("cic_immobilier_ANNEXE_FIN.pdf", build_cic_annexe_fin, TRUTH_CIC, False),
    ("fortuneo_conso_MUR_DE_TEXTE.pdf", build_fortuneo_mur, TRUTH_FOR, False),
    ("axa_immobilier_ASSURANCE_MELANGEE.pdf", build_axa_assurance_melangee, TRUTH_AXA, False),
    ("carrefour_conso_TOUT_A_LA_FIN.pdf", build_carrefour_fin, TRUTH_CAR, False),
]


# ─────────────────────────────────────────────
#  Génération + notation
# ─────────────────────────────────────────────
def close(a, b):
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) < 0.005
    return a == b


def main():
    os.makedirs(OUT, exist_ok=True)
    grand_ok = grand_total = 0
    for name, builder, truth, scan in DOCS:
        pdf = doc_pdf(builder)
        if scan:
            pdf = rasterize(pdf)
        path = os.path.join(OUT, name)
        with open(path, "wb") as f:
            f.write(pdf)

        res = ex.extract(pdf, name)
        ok = 0
        print(f"\n── {name}{'  [OCR]' if res.used_ocr else ''} ──")
        for field_name, expected in truth.items():
            if field_name == "n_sub_loans":
                got = len(res.sub_loans)
                e = None
                good = got == expected
                ok += good
                print(f"  [{'OK ' if good else 'RATE'}] {field_name:16s} attendu={expected!s:12s} "
                      f"lu={got!s:12s} ({', '.join(l['label'] for l in res.sub_loans) or 'aucun'})")
                continue
            e = res.fields.get(field_name)
            got = e.value if e else None
            good = got is not None and close(got, expected)
            ok += good
            mark = "OK " if good else "RATE"
            conf = f"{e.confidence:.2f}" if e else " -- "
            print(f"  [{mark}] {field_name:16s} attendu={expected!s:12s} lu={got!s:12s} conf={conf}")
        print(f"  => {ok}/{len(truth)} champs corrects")
        grand_ok += ok
        grand_total += len(truth)
    pct = 100 * grand_ok / grand_total
    print(f"\n════ SCORE GLOBAL : {grand_ok}/{grand_total} champs ({pct:.0f} %) ════")


if __name__ == "__main__":
    main()
