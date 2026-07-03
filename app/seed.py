"""
Amorçage pour le test local :
  - offres synthétiques (marquées source="seed") pour que les cohortes
    existent et que l'écran de positionnement soit démontrable ;
  - deux PDF d'offres factices dans samples/ pour tester le dépôt.

La cohorte « auto / Occitanie » est volontairement laissée sous le seuil
pour pouvoir démontrer l'écran « pas assez de données » (U7b).
"""

import os
import random
from datetime import date, datetime, timedelta

import fitz

from .db import SessionLocal
from .models import Offer

_BANKS = ["BNP Paribas", "Crédit Agricole", "Banque Populaire", "Caisse d'Épargne",
          "Société Générale", "Crédit Mutuel", "La Banque Postale", "Boursobank"]

# (type, région, nb d'offres, taeg médian visé)
_COHORTS = [
    ("immobilier", "Île-de-France", 34, 3.74),
    ("immobilier", "Bretagne", 14, 3.68),
    ("immobilier", "Auvergne-Rhône-Alpes", 12, 3.79),
    ("consommation", "Île-de-France", 12, 6.30),
    ("auto", "Occitanie", 3, 5.10),          # < seuil → démontre U7b
]


def seed_offers():
    rng = random.Random(42)
    db = SessionLocal()
    try:
        if db.query(Offer).count() > 0:
            return 0
        n = 0
        for ctype, region, count, median in _COHORTS:
            for _ in range(count):
                taeg = round(rng.gauss(median, 0.22), 2)
                nominal = round(taeg - rng.uniform(0.2, 0.45), 2)
                amount = (rng.randrange(120, 420) * 1000 if ctype == "immobilier"
                          else rng.randrange(5, 35) * 1000)
                months = (rng.choice([180, 240, 300]) if ctype == "immobilier"
                          else rng.choice([36, 48, 60, 72]))
                db.add(Offer(
                    session_id="seed", status="confirmed", source="seed",
                    credit_type=ctype, region=region, bank=rng.choice(_BANKS),
                    amount=float(amount), duration_months=months, rate_type="fixe",
                    rate_nominal=nominal, taeg=taeg,
                    taea=round(rng.uniform(0.15, 0.55), 2),
                    fees=float(rng.randrange(0, 12) * 100),
                    offer_date=date.today() - timedelta(days=rng.randrange(5, 300)),
                    created_at=datetime.utcnow() - timedelta(days=rng.randrange(0, 300)),
                    income_bracket=rng.choice(["2 000 – 3 500 €", "3 500 – 5 000 €"]),
                    deposit_bracket=rng.choice(["5 – 10 %", "10 – 20 %"]),
                ))
                n += 1
        db.commit()
        return n
    finally:
        db.close()


# ─────────────────────────────────────────────
#  PDF d'exemple (pour tester le dépôt)
# ─────────────────────────────────────────────
def _make_pdf(path, lines, title):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 70), title, fontsize=16)
    page.draw_line(fitz.Point(60, 82), fitz.Point(535, 82))
    y = 120
    for line in lines:
        page.insert_text((60, y), line, fontsize=11)
        y += 26
    doc.save(path)
    doc.close()


def make_samples(folder="samples"):
    os.makedirs(folder, exist_ok=True)
    p1 = os.path.join(folder, "offre_exemple_immobilier.pdf")
    if not os.path.exists(p1):
        _make_pdf(p1, [
            "Banque Populaire Val de France",
            "Offre de pret immobilier - Taux fixe",
            "",
            "Montant du credit : 200 000,00 EUR",
            "Duree : 240 mois",
            "Taux debiteur fixe : 3,60 %",
            "Taux Annuel Effectif Global (TAEG) : 3,90 %",
            "Taux Annuel Effectif de l'Assurance (TAEA) : 0,34 %",
            "Frais de dossier : 900,00 EUR",
            "Cout total du credit : 92 400,00 EUR",
            "",
            "Offre editee le 14/03/2026",
        ], "OFFRE DE PRET IMMOBILIER")
    p2 = os.path.join(folder, "offre_exemple_conso.pdf")
    if not os.path.exists(p2):
        _make_pdf(p2, [
            "Cetelem - Credit a la consommation",
            "Pret personnel",
            "",
            "Montant du credit : 15 000,00 EUR",
            "Duree : 60 mois",
            "Taux debiteur : 5,90 %",
            "TAEG : 6,40 %",
            "Frais de dossier : 0,00 EUR",
            "",
            "Fait le 02/05/2026",
        ], "OFFRE DE CREDIT")
    return folder


def run():
    n = seed_offers()
    try:
        make_samples()
    except OSError:
        pass  # dossier en lecture seule (conteneur) : les PDF sont déjà embarqués
    return n


if __name__ == "__main__":
    print(f"{run()} offre(s) synthétique(s) insérée(s) ; PDF d'exemple dans samples/")
