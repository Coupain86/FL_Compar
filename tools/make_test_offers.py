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
    def __init__(self, doc, header, footer):
        self.page = doc.new_page(width=595, height=842)
        self.y = 60.0
        self.footer = footer
        if header:
            self.page.insert_text((50, self.y), header, fontsize=8, color=(0.35, 0.35, 0.35))
            self.y += 18
        self.page.insert_text((50, 820), footer, fontsize=6.5, color=(0.45, 0.45, 0.45))

    def title(self, text, size=13):
        self.y += 8
        self.page.insert_text((50, self.y), text, fontsize=size)
        self.page.draw_line(fitz.Point(50, self.y + 4), fitz.Point(545, self.y + 4), width=0.7)
        self.y += 20

    def para(self, text, size=8.6, indent=50, leading=11.4):
        # découpe naïve à ~108 caractères par ligne
        words, line = text.split(), ""
        for w in words:
            if len(line) + len(w) + 1 > 108:
                self.page.insert_text((indent, self.y), line, fontsize=size)
                self.y += leading
                line = w
            else:
                line = (line + " " + w).strip()
        if line:
            self.page.insert_text((indent, self.y), line, fontsize=size)
            self.y += leading

    def kv(self, label, value, size=9.2):
        self.page.insert_text((60, self.y), label, fontsize=size)
        self.page.insert_text((330, self.y), value, fontsize=size)
        self.y += 13.5

    def gap(self, h=8):
        self.y += h


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


TRUTH_CA = {"bank": "Crédit Agricole", "credit_type": "immobilier", "rate_type": "fixe",
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
           "de 36 mois (Peugeot 308, immatriculee en 2024, prix d'achat 26 900,00 EUR, apport personnel "
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


TRUTH_BB = {"bank": "Boursobank", "credit_type": "auto", "rate_type": "fixe",
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


TRUTH_LBP = {"bank": "La Banque Postale", "credit_type": "immobilier", "rate_type": "fixe",
             "amount": 185000.0, "duration_months": 240, "rate_nominal": 3.55, "taeg": 4.05,
             "taea": 0.48, "fees": 800.0, "total_cost": 84620.0, "offer_date": date(2026, 3, 17)}


DOCS = [
    ("credit_agricole_immobilier.pdf", build_credit_agricole, TRUTH_CA, False),
    ("bnp_immobilier_variable.pdf", build_bnp_variable, TRUTH_BNP, False),
    ("sofinco_pret_personnel.pdf", build_sofinco, TRUTH_SOF, False),
    ("caisse_epargne_regroupement.pdf", build_caisse_epargne_regroupement, TRUTH_CE, False),
    ("boursobank_auto.pdf", build_bourso_auto, TRUTH_BB, False),
    ("banque_postale_immobilier_SCAN.pdf", build_lbp_scan, TRUTH_LBP, True),
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
