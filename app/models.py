"""Modèle de données — volontairement minimal et anonymisé.

Une Offre ne contient QUE les champs comparables + un profil en tranches.
Jamais de nom, d'adresse, ni le document d'origine (il n'est jamais stocké).
Le contact (email/téléphone) ne vit que dans Consent, avec sa finalité,
et il est supprimable indépendamment.
"""

import uuid
from datetime import datetime

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, Float, Integer,
                        String, Text)

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class Offer(Base):
    __tablename__ = "offers"

    id = Column(String(32), primary_key=True, default=_uuid)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    session_id = Column(String(64), index=True)          # cookie anonyme
    status = Column(String(16), default="draft")         # draft -> confirmed
    source = Column(String(16), default="upload")        # upload | seed

    # Champs comparables (le schéma des ~14 champs)
    credit_type = Column(String(24), index=True)         # immobilier / consommation / auto / regroupement
    bank = Column(String(64), index=True)
    region = Column(String(48), index=True)
    amount = Column(Float)                                # €
    duration_months = Column(Integer)
    rate_type = Column(String(12))                        # fixe / variable
    rate_nominal = Column(Float)                          # %
    taeg = Column(Float, index=True)                      # %
    taea = Column(Float)                                  # % (assurance)
    fees = Column(Float)                                  # € frais de dossier
    total_cost = Column(Float)                            # €
    offer_date = Column(Date)

    # Profil en tranches (jamais de valeurs exactes)
    income_bracket = Column(String(24))
    deposit_bracket = Column(String(24))
    # « oui » / « non » / NULL (non répondu). Sert UNIQUEMENT à adapter les
    # conseils de négociation — jamais de critère de cohorte (ça diviserait
    # les cohortes par deux et aggraverait le démarrage à froid).
    has_other_credits = Column(String(8))

    # Traçabilité de l'extraction (confiances par champ, avertissements)
    extraction_meta = Column(JSON, default=dict)


class Correction(Base):
    """Boucle d'amélioration : chaque champ corrigé par l'utilisateur,
    avec ce que l'extracteur avait proposé et d'où venait sa proposition."""
    __tablename__ = "corrections"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    offer_id = Column(String(32), index=True)
    field = Column(String(32))
    extracted_value = Column(Text)     # ce que l'algorithme avait lu (peut être vide)
    corrected_value = Column(Text)     # ce que l'utilisateur a mis
    confidence = Column(Float)         # confiance qu'on affichait
    source_rule = Column(Text)         # règle/mot-clé à l'origine de la proposition
    bank = Column(String(64))          # pour prioriser les formats à améliorer


class Consent(Base):
    """Un accord explicite, par finalité. `contact` n'est rempli que si la
    finalité l'exige (alerte, recontact) et reste supprimable."""
    __tablename__ = "consents"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    session_id = Column(String(64), index=True)
    offer_id = Column(String(32), index=True)
    kind = Column(String(16))          # analyse | rapport | alerte | recontact
    contact = Column(String(128))      # email ou téléphone, selon finalité
    active = Column(Boolean, default=True)
