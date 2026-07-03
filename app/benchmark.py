"""
Moteur de comparaison : positionne une offre face à sa cohorte.

Règle d'or (spec E-2) : un verdict chiffré n'est rendu que si la cohorte
contient au moins MIN_COHORT offres comparables. En dessous : analyse
qualitative uniquement (écran U7b).

Cohorte = même type de crédit + même région + offres confirmées de moins
de WINDOW_DAYS jours (l'offre elle-même exclue). La banque n'est pas un
critère de cohorte (on compare au marché) mais elle est montrée à part.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_
from sqlalchemy.orm import Session

from .models import Offer

MIN_COHORT = int(os.environ.get("MIN_COHORT", "8"))
WINDOW_DAYS = int(os.environ.get("COHORT_WINDOW_DAYS", "365"))


@dataclass
class Benchmark:
    cohort_size: int
    better_than_pct: int          # « mieux placé que X % »
    median_taeg: float
    delta_taeg: float             # taeg offre - médiane (positif = plus cher)
    savings_estimate: float       # € sur la durée restante (approximation prudente)
    median_nominal: float | None
    median_taea: float | None
    window_days: int


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def cohort_query(db: Session, offer: Offer):
    since = datetime.utcnow() - timedelta(days=WINDOW_DAYS)
    return db.query(Offer).filter(and_(
        Offer.status == "confirmed",
        Offer.id != offer.id,
        Offer.credit_type == offer.credit_type,
        Offer.region == offer.region,
        Offer.taeg.isnot(None),
        Offer.created_at >= since,
    ))


def compute(db: Session, offer: Offer) -> Benchmark | None:
    """Retourne le positionnement, ou None si la cohorte est trop mince."""
    if offer.taeg is None or not offer.credit_type or not offer.region:
        return None
    cohort = cohort_query(db, offer).all()
    if len(cohort) < MIN_COHORT:
        return None

    taegs = [o.taeg for o in cohort]
    worse = sum(1 for t in taegs if t > offer.taeg)   # offres plus chères que la sienne
    better_than = round(100 * worse / len(taegs))
    median = _median(taegs)
    delta = offer.taeg - median

    # Approximation prudente du surcoût : capital moyen restant dû ≈ montant/2.
    savings = 0.0
    if delta > 0 and offer.amount and offer.duration_months:
        years = offer.duration_months / 12
        savings = offer.amount * (delta / 100) * years / 2

    nominals = [o.rate_nominal for o in cohort if o.rate_nominal is not None]
    taeas = [o.taea for o in cohort if o.taea is not None]

    return Benchmark(
        cohort_size=len(cohort),
        better_than_pct=better_than,
        median_taeg=round(median, 2),
        delta_taeg=round(delta, 2),
        savings_estimate=round(savings, -1),  # arrondi à la dizaine d'euros
        median_nominal=round(_median(nominals), 2) if len(nominals) >= 3 else None,
        median_taea=round(_median(taeas), 2) if len(taeas) >= 3 else None,
        window_days=WINDOW_DAYS,
    )


def qualitative_notes(offer: Offer) -> list[str]:
    """Analyse de repli quand la cohorte est trop mince (U7b)."""
    notes = []
    if offer.taeg and offer.rate_nominal:
        wrapper = round(offer.taeg - offer.rate_nominal, 2)
        notes.append(f"Votre TAEG ({offer.taeg} %) = taux nominal ({offer.rate_nominal} %) "
                     f"+ {wrapper} point(s) d'assurance et de frais.")
    if offer.taea is None:
        notes.append("Coût de l'assurance non renseigné — c'est souvent le premier poste "
                     "d'économie (délégation d'assurance).")
    elif offer.taea > 0.5:
        notes.append(f"Votre TAEA ({offer.taea} %) est un poste à challenger : "
                     "la délégation d'assurance permet souvent de le réduire.")
    if offer.fees and offer.fees > 800:
        notes.append(f"Frais de dossier plutôt élevés ({offer.fees:.0f} €) — négociables.")
    if offer.rate_type == "variable":
        notes.append("Taux variable : vérifiez le plafond de variation (cap) dans l'offre.")
    if not notes:
        notes.append("Offre lisible et complète. Revenez bientôt : plus il y a d'offres "
                     "déposées, plus la comparaison devient précise.")
    return notes
