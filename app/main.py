"""
Comparateur d'offres de crédit — application web (Phase 1 : le benchmark).

Parcours particulier (U1→U9 de la spécification) :
  accueil → type de crédit → dépôt + consentement → vérification des champs
  lus → profil en tranches → positionnement (ou analyse qualitative si la
  cohorte est trop mince) → actions (rapport / alerte / recontact) → mon espace.

Back-office minimal (/admin) : vue d'ensemble, corrections (boucle
d'amélioration), couverture des formats, densité par cohorte.

Confidentialité, par construction :
  - le document déposé est traité en mémoire et n'est JAMAIS écrit sur disque ;
  - seuls les champs comparables sont stockés (pas de nom, pas d'adresse) ;
  - le contact ne vit que dans un consentement révocable ;
  - « Supprimer mes données » efface tout ce qui est lié à la session.
"""

import os
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import benchmark as bm
from . import extraction as ex
from .db import Base, engine, get_db
from .models import Consent, Correction, Offer

app = FastAPI(title="TrustRate")

_HERE = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")

MAX_UPLOAD = 10 * 1024 * 1024
ALLOWED_EXT = (".pdf", ".jpg", ".jpeg", ".png")

CREDIT_LABELS = {
    "immobilier": "Immobilier", "consommation": "Consommation / Perso",
    "auto": "Auto", "regroupement": "Regroupement de crédits",
}

# Champs de l'écran de vérification : (nom, libellé, type)
VERIFY_FIELDS = [
    ("bank", "Banque", "text"),
    ("rate_type", "Type de taux", "rate_type"),
    ("amount", "Montant emprunté (€)", "number"),
    ("duration_months", "Durée (mois)", "int"),
    ("rate_nominal", "Taux nominal (%)", "number"),
    ("taeg", "TAEG (%)", "number"),
    ("taea", "Coût de l'assurance — TAEA (%)", "number"),
    ("fees", "Frais de dossier (€)", "number"),
    ("total_cost", "Coût total du crédit (€)", "number"),
    ("offer_date", "Date de l'offre", "date"),
]

FLASH = {
    "rapport": "C'est noté — on vous écrira dès que votre rapport sera prêt.",
    "alerte": "Alerte activée. On vous prévient dès qu'il y a mieux.",
    "recontact": "Demande envoyée. Un partenaire pourra vous recontacter sous peu.",
    "supprime": "Vos données ont été supprimées.",
}


Base.metadata.create_all(bind=engine)


@app.on_event("startup")
def _startup():
    Base.metadata.create_all(bind=engine)
    if os.environ.get("SEED_ON_START", "0") == "1":
        from . import seed
        seed.run()


# ─────────────────────────────────────────────
#  Session anonyme (cookie)
# ─────────────────────────────────────────────
def _sid(request: Request) -> str:
    return request.cookies.get("sid") or uuid.uuid4().hex


def _with_cookie(request: Request, response):
    if not request.cookies.get("sid"):
        response.set_cookie("sid", _sid(request), httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 365)
    return response


def _render(request: Request, template: str, **ctx):
    ctx.setdefault("flash", FLASH.get(request.query_params.get("ok", "")))
    resp = templates.TemplateResponse(request, template, ctx)
    return _with_cookie(request, resp)


def _own_offer(db: Session, request: Request, offer_id: str) -> Offer | None:
    offer = db.get(Offer, offer_id)
    if offer and offer.session_id == request.cookies.get("sid"):
        return offer
    return None


# ─────────────────────────────────────────────
#  Parcours particulier
# ─────────────────────────────────────────────
@app.get("/sante")
def sante():
    return {"status": "ok"}


@app.get("/")
def accueil(request: Request, db: Session = Depends(get_db)):
    count = db.query(Offer).filter(Offer.status == "confirmed").count()
    return _render(request, "accueil.html", count=count)


@app.get("/comparer")
def comparer(request: Request, erreur: str = ""):
    return _depot_page(request, erreur)


@app.get("/comparer/depot")
def depot(request: Request, type: str = "", erreur: str = ""):
    return _depot_page(request, erreur)


def _depot_page(request: Request, erreur: str = ""):
    messages = {
        "illisible": "On n'arrive pas à lire ce document. Reprenez une photo bien nette "
                     "et à plat, ou déposez le PDF d'origine.",
        "format": "Ce format n'est pas accepté. Déposez un PDF, un JPG ou un PNG.",
        "taille": "Fichier trop volumineux (10 Mo maximum).",
        "fichier": "Choisissez un fichier avant de continuer.",
        "consentement": "Cochez la case de consentement pour continuer.",
    }
    return _render(request, "depot.html", erreur=messages.get(erreur, ""))


@app.post("/comparer/analyser")
async def analyser(request: Request, db: Session = Depends(get_db),
                   fichier: UploadFile = File(None),
                   credit_type: str = Form(""),
                   consentement: str = Form("")):
    if consentement != "on":
        return RedirectResponse("/comparer?erreur=consentement", status_code=303)
    if fichier is None or not fichier.filename:
        return RedirectResponse("/comparer?erreur=fichier", status_code=303)
    if not fichier.filename.lower().endswith(ALLOWED_EXT):
        return RedirectResponse("/comparer?erreur=format", status_code=303)

    data = await fichier.read()          # en mémoire uniquement — jamais sur disque
    if len(data) > MAX_UPLOAD:
        return RedirectResponse("/comparer?erreur=taille", status_code=303)

    try:
        result = ex.extract(data, fichier.filename)
    except Exception:
        result = ex.ExtractionResult()
    del data                             # le document meurt ici
    if result.text_chars < 40:
        return RedirectResponse("/comparer?erreur=illisible", status_code=303)

    sid = _sid(request)
    offer = Offer(session_id=sid, status="draft",
                  credit_type=credit_type or result.get("credit_type"))
    meta = {"filename": os.path.basename(fichier.filename or ""),
            "warnings": result.warnings, "used_ocr": result.used_ocr, "fields": {}}
    for name, _label, _kind in VERIFY_FIELDS:
        e = result.fields.get(name)
        if e is not None:
            value = e.value.isoformat() if name == "offer_date" else e.value
            setattr(offer, name, e.value)
            meta["fields"][name] = {"value": str(value), "confidence": e.confidence,
                                    "source": e.source}
    meta["sub_loans"] = result.sub_loans
    region = result.fields.get("region")
    if region:
        offer.region = region.value
        meta["fields"]["region"] = {"value": region.value, "confidence": region.confidence,
                                    "source": region.source}
    offer.extraction_meta = meta
    db.add(offer)
    db.commit()

    resp = RedirectResponse(f"/comparer/verifier/{offer.id}", status_code=303)
    if not request.cookies.get("sid"):
        resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 365)

    db.add(Consent(session_id=sid, offer_id=offer.id, kind="analyse"))
    db.commit()
    return resp


def _field_view(offer: Offer):
    meta = (offer.extraction_meta or {}).get("fields", {})
    rows = []
    for name, label, kind in VERIFY_FIELDS:
        value = getattr(offer, name)
        if name == "offer_date" and value:
            value = value.isoformat()
        conf = meta.get(name, {}).get("confidence", 0.0)
        state = "ok"
        if value in (None, ""):
            state, badge = "missing", "à compléter"
        elif conf < 0.7:
            state, badge = "check", "à vérifier"
        else:
            badge = ""
        rows.append({"name": name, "label": label, "kind": kind,
                     "value": "" if value is None else value, "state": state, "badge": badge})
    return rows


@app.get("/comparer/verifier/{offer_id}")
def verifier(offer_id: str, request: Request, db: Session = Depends(get_db)):
    offer = _own_offer(db, request, offer_id)
    if not offer:
        return RedirectResponse("/", status_code=303)
    meta = offer.extraction_meta or {}
    return _render(request, "verifier.html", offer=offer,
                   rows=_field_view(offer), warnings=meta.get("warnings", []),
                   sub_loans=meta.get("sub_loans", []),
                   type_label=CREDIT_LABELS.get(offer.credit_type))


def _parse_value(kind: str, raw: str):
    raw = (raw or "").strip()
    if raw == "":
        return None
    if kind in ("number",):
        return float(raw.replace(" ", "").replace(",", "."))
    if kind == "int":
        return int(float(raw.replace(",", ".")))
    if kind == "date":
        return datetime.strptime(raw, "%Y-%m-%d").date()
    return raw


@app.post("/comparer/verifier/{offer_id}")
async def verifier_post(offer_id: str, request: Request, db: Session = Depends(get_db)):
    offer = _own_offer(db, request, offer_id)
    if not offer:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    meta_fields = (offer.extraction_meta or {}).get("fields", {})

    for name, _label, kind in VERIFY_FIELDS:
        if name not in form:
            continue  # champ non soumis ≠ champ vidé : on n'y touche pas
        try:
            new = _parse_value(kind, form.get(name, ""))
        except (ValueError, TypeError):
            continue  # saisie invalide : on garde la valeur existante
        old = getattr(offer, name)
        old_cmp = old.isoformat() if name == "offer_date" and old else old
        new_cmp = new.isoformat() if name == "offer_date" and new else new
        if str(old_cmp) != str(new_cmp):
            info = meta_fields.get(name, {})
            db.add(Correction(
                offer_id=offer.id, field=name,
                extracted_value="" if old is None else str(old_cmp),
                corrected_value="" if new is None else str(new_cmp),
                confidence=info.get("confidence"), source_rule=info.get("source", ""),
                bank=offer.bank))
            setattr(offer, name, new)
    if form.get("credit_type"):
        offer.credit_type = form.get("credit_type")
    db.commit()
    return RedirectResponse(f"/comparer/profil/{offer.id}", status_code=303)


@app.get("/comparer/profil/{offer_id}")
def profil(offer_id: str, request: Request, db: Session = Depends(get_db)):
    offer = _own_offer(db, request, offer_id)
    if not offer:
        return RedirectResponse("/", status_code=303)
    return _render(request, "profil.html", offer=offer,
                   incomes=ex.INCOME_BRACKETS, deposits=ex.DEPOSIT_BRACKETS,
                   regions=ex.REGIONS, is_immo=(offer.credit_type == "immobilier"))


@app.post("/comparer/profil/{offer_id}")
def profil_post(offer_id: str, request: Request, db: Session = Depends(get_db),
                income: str = Form(""), deposit: str = Form(""), region: str = Form("")):
    offer = _own_offer(db, request, offer_id)
    if not offer:
        return RedirectResponse("/", status_code=303)
    offer.income_bracket = income or None
    offer.deposit_bracket = deposit or None
    offer.region = region or None
    offer.status = "confirmed"
    db.commit()
    return RedirectResponse(f"/resultat/{offer.id}", status_code=303)


@app.get("/resultat/{offer_id}")
def resultat(offer_id: str, request: Request, db: Session = Depends(get_db)):
    offer = _own_offer(db, request, offer_id)
    if not offer:
        return RedirectResponse("/", status_code=303)
    result = bm.compute(db, offer)
    notes = bm.qualitative_notes(offer)
    return _render(request, "resultat.html", offer=offer, bench=result, notes=notes,
                   type_label=CREDIT_LABELS.get(offer.credit_type, offer.credit_type or "—"),
                   sub_loans=(offer.extraction_meta or {}).get("sub_loans", []),
                   min_cohort=bm.MIN_COHORT)


@app.post("/resultat/{offer_id}/action")
def action(offer_id: str, request: Request, db: Session = Depends(get_db),
           kind: str = Form(...), contact: str = Form("")):
    offer = _own_offer(db, request, offer_id)
    if not offer or kind not in ("rapport", "alerte", "recontact"):
        return RedirectResponse("/", status_code=303)
    if not contact.strip():
        return RedirectResponse(f"/resultat/{offer.id}", status_code=303)
    db.add(Consent(session_id=offer.session_id, offer_id=offer.id,
                   kind=kind, contact=contact.strip()))
    db.commit()
    return RedirectResponse(f"/resultat/{offer.id}?ok={kind}", status_code=303)


# ─────────────────────────────────────────────
#  Mon espace (session) + droit à l'effacement
# ─────────────────────────────────────────────
@app.get("/mon-espace")
def mon_espace(request: Request, db: Session = Depends(get_db)):
    sid = request.cookies.get("sid", "")
    offers = (db.query(Offer).filter(Offer.session_id == sid, Offer.source == "upload")
              .order_by(Offer.created_at.desc()).all())
    consents = (db.query(Consent).filter(Consent.session_id == sid, Consent.active,
                                         Consent.kind != "analyse").all())
    items = [{"offer": o, "bench": bm.compute(db, o) if o.status == "confirmed" else None,
              "type_label": CREDIT_LABELS.get(o.credit_type, o.credit_type or "—")}
             for o in offers]
    return _render(request, "espace.html", items=items, consents=consents)


@app.post("/mon-espace/supprimer")
def supprimer(request: Request, db: Session = Depends(get_db)):
    sid = request.cookies.get("sid", "")
    if sid:
        ids = [o.id for o in db.query(Offer).filter(Offer.session_id == sid).all()]
        if ids:
            db.query(Correction).filter(Correction.offer_id.in_(ids)).delete(synchronize_session=False)
        db.query(Consent).filter(Consent.session_id == sid).delete(synchronize_session=False)
        db.query(Offer).filter(Offer.session_id == sid).delete(synchronize_session=False)
        db.commit()
    resp = RedirectResponse("/?ok=supprime", status_code=303)
    resp.delete_cookie("sid")
    return resp


# ─────────────────────────────────────────────
#  Back-office (/admin) — protégé par mot de passe
# ─────────────────────────────────────────────
_basic = HTTPBasic()


def _admin(credentials: HTTPBasicCredentials = Depends(_basic)):
    user_ok = secrets.compare_digest(credentials.username,
                                     os.environ.get("ADMIN_USER", "admin"))
    pass_ok = secrets.compare_digest(credentials.password,
                                     os.environ.get("ADMIN_PASSWORD", "admin"))
    if not (user_ok and pass_ok):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@app.get("/admin/export.json")
def admin_export(request: Request, db: Session = Depends(get_db), _user: str = Depends(_admin)):
    """Journal d'extraction exportable : pour chaque fichier déposé, ce que
    l'extracteur a lu (valeur, confiance, règle), ce que l'utilisateur a
    corrigé, et les valeurs finales. Sert à valider les résultats de test."""
    from fastapi.responses import JSONResponse

    offers = (db.query(Offer).filter(Offer.source == "upload")
              .order_by(Offer.created_at.asc()).all())
    out = []
    for o in offers:
        meta = o.extraction_meta or {}
        corrections = db.query(Correction).filter(Correction.offer_id == o.id).all()
        final = {}
        for name, _label, _kind in VERIFY_FIELDS:
            v = getattr(o, name)
            final[name] = v.isoformat() if name == "offer_date" and v else v
        final["credit_type"] = o.credit_type
        final["region"] = o.region
        out.append({
            "fichier": meta.get("filename", "(inconnu)"),
            "depose_le": o.created_at.isoformat(timespec="seconds"),
            "statut": o.status,
            "ocr_utilise": meta.get("used_ocr", False),
            "avertissements": meta.get("warnings", []),
            "extraction_brute": meta.get("fields", {}),
            "prets_detectes": meta.get("sub_loans", []),
            "corrections_utilisateur": [
                {"champ": c.field, "lu": c.extracted_value, "corrige": c.corrected_value,
                 "confiance": c.confidence, "regle": c.source_rule} for c in corrections],
            "valeurs_finales": final,
        })
    payload = {"exporte_le": datetime.utcnow().isoformat(timespec="seconds") + "Z",
               "nb_fichiers": len(out), "offres": out}
    return JSONResponse(payload, headers={
        "Content-Disposition": "attachment; filename=trustrate_journal_extraction.json"})


@app.get("/admin")
def admin(request: Request, db: Session = Depends(get_db), _user: str = Depends(_admin)):
    uploads = db.query(Offer).filter(Offer.source == "upload")
    total_uploads = uploads.count()
    confirmed = uploads.filter(Offer.status == "confirmed").count()
    users = db.query(func.count(func.distinct(Offer.session_id))) \
              .filter(Offer.source == "upload").scalar() or 0
    corrections = db.query(Correction).order_by(Correction.created_at.desc()).limit(25).all()
    n_corrections = db.query(Correction).count()

    # Densité par cohorte (type × région) sur la fenêtre glissante
    since = datetime.utcnow() - timedelta(days=bm.WINDOW_DAYS)
    density = (db.query(Offer.credit_type, Offer.region, func.count(Offer.id))
               .filter(Offer.status == "confirmed", Offer.taeg.isnot(None),
                       Offer.created_at >= since)
               .group_by(Offer.credit_type, Offer.region)
               .order_by(func.count(Offer.id).desc()).all())

    # Couverture des formats : corrections moyennes par banque (uploads)
    per_bank = (db.query(Correction.bank, func.count(Correction.id))
                .group_by(Correction.bank).order_by(func.count(Correction.id).desc()).all())

    return _render(request, "admin.html",
                   total_uploads=total_uploads, confirmed=confirmed, users=users,
                   n_corrections=n_corrections, corrections=corrections,
                   density=density, per_bank=per_bank, min_cohort=bm.MIN_COHORT,
                   type_labels=CREDIT_LABELS)
