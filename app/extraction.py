"""
Extraction déterministe des champs d'une offre de crédit.

Architecture "candidats + scoring" (pas d'IA, pas de gabarit par banque) :
  1. On récupère le texte du document (couche texte du PDF ; OCR Tesseract
     en repli pour les scans/photos, si disponible).
  2. On génère TOUS les candidats : pourcentages, montants, durées, dates.
  3. Pour chaque champ cible, on score chaque candidat selon la proximité
     d'un mot-clé (TAEG, taux débiteur, frais de dossier, …).
  4. Affectation gloutonne (meilleur score d'abord), un candidat ne sert
     qu'une fois — évite que le même % soit à la fois TAEG et taux nominal.
  5. Règles de validation métier (TAEG >= nominal, bornes plausibles…)
     qui abaissent la confiance au lieu d'avaler une erreur en silence.

Chaque champ sort avec {valeur, confiance 0-1, source} pour que l'UI sache
quoi faire vérifier, et pour que la boucle de corrections soit analysable.

Aucune donnée nominative n'est retournée : uniquement les champs comparables.
Le document lui-même n'est jamais écrit sur disque par ce module.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

import fitz  # PyMuPDF

# OCR optionnel (scans / photos). Déterministe, 100 % local.
try:
    import pytesseract
    from PIL import Image
    _OCR = True
except Exception:  # pragma: no cover - dépend de l'environnement
    _OCR = False

# ─────────────────────────────────────────────
#  Référentiels
# ─────────────────────────────────────────────
BANKS = [
    "BNP Paribas", "Crédit Agricole", "LCL", "Banque Populaire",
    "Caisse d'Épargne", "Société Générale", "Crédit Mutuel", "CIC",
    "La Banque Postale", "Boursobank", "Boursorama", "Fortuneo",
    "Hello bank", "Monabanq", "BforBank", "Cetelem", "Sofinco",
    "Cofidis", "Franfinance", "Younited", "Floa", "Oney",
    "Carrefour Banque", "CCF", "HSBC", "AXA Banque", "Crédit du Nord",
]

CREDIT_TYPES = {
    "immobilier": ["prêt immobilier", "crédit immobilier", "immobilier", "acquisition", "hypothécaire"],
    "consommation": ["prêt personnel", "crédit à la consommation", "crédit consommation", "prêt conso"],
    "auto": ["crédit auto", "prêt auto", "financement automobile", "véhicule"],
    "regroupement": ["regroupement de crédits", "rachat de crédits", "restructuration"],
}

REGIONS = [
    "Île-de-France", "Auvergne-Rhône-Alpes", "Nouvelle-Aquitaine", "Occitanie",
    "Hauts-de-France", "Grand Est", "Provence-Alpes-Côte d'Azur", "Pays de la Loire",
    "Bretagne", "Normandie", "Bourgogne-Franche-Comté", "Centre-Val de Loire",
    "Corse", "Outre-mer",
]

INCOME_BRACKETS = ["moins de 2 000 €", "2 000 – 3 500 €", "3 500 – 5 000 €", "plus de 5 000 €"]
DEPOSIT_BRACKETS = ["0 %", "5 – 10 %", "10 – 20 %", "plus de 20 %"]

# Mots-clés par champ : (mot-clé normalisé, poids)
_PCT_LABELS = {
    "taeg": [("taux annuel effectif global", 3.0), ("taeg", 3.0)],
    "rate_nominal": [("taux debiteur", 3.0), ("taux nominal", 3.0),
                     ("taux d'interet", 2.2), ("taux fixe", 1.6), ("taux", 0.6)],
    "taea": [("taux annuel effectif de l'assurance", 3.0), ("taea", 3.0), ("assurance", 1.2)],
}
_EUR_LABELS = {
    "amount": [("montant du credit", 3.0), ("capital emprunte", 3.0),
               ("montant emprunte", 3.0), ("montant du pret", 3.0), ("montant finance", 2.5)],
    "fees": [("frais de dossier", 3.0), ("frais de garantie", 1.5)],
    "total_cost": [("cout total", 3.0), ("montant total du", 3.0)],
}

_WINDOW = 90  # caractères de contexte examinés avant chaque valeur


@dataclass
class Extracted:
    value: object
    confidence: float
    source: str  # règle/mot-clé qui a gagné (traçabilité pour la boucle)


@dataclass
class ExtractionResult:
    fields: dict = field(default_factory=dict)   # nom -> Extracted
    warnings: list = field(default_factory=list)
    text_chars: int = 0
    used_ocr: bool = False

    def get(self, name):
        e = self.fields.get(name)
        return e.value if e else None

    def conf(self, name):
        e = self.fields.get(name)
        return e.confidence if e else 0.0


# ─────────────────────────────────────────────
#  1. Texte du document
# ─────────────────────────────────────────────
def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def document_text(data: bytes, filename: str) -> tuple[str, bool]:
    """Texte du document. PDF natif d'abord ; OCR en repli. (texte, ocr_utilisé)"""
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        with fitz.open(stream=data, filetype="pdf") as doc:
            text = "\n".join(page.get_text("text") for page in doc)
            if len(text.strip()) >= 40:
                return text, False
            # PDF scanné : rasteriser puis OCR
            if _OCR:
                chunks = []
                for page in doc:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2), colorspace=fitz.csRGB)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    chunks.append(_ocr(img))
                return "\n".join(chunks), True
            return text, False
    # Image (photo / scan)
    if _OCR:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        return _ocr(img), True
    return "", False


def _ocr(img) -> str:
    for lang in ("fra+eng", None):
        try:
            return pytesseract.image_to_string(img, **({"lang": lang} if lang else {}))
        except Exception:
            continue
    return ""


# ─────────────────────────────────────────────
#  2-3. Candidats + scoring
# ─────────────────────────────────────────────
_PCT_RE = re.compile(r"(\d{1,2}(?:[.,]\d{1,3})?)\s*%")
_EUR_RE = re.compile(
    r"((?:\d{1,3}(?:[\s  .]\d{3})+|\d{3,7})(?:[.,]\d{2})?)\s*(?:€|eur(?:os?)?\b)")
_DUR_RE = re.compile(r"(\d{1,3})\s*(ans?\b|annees?\b|mois\b)")
_DATE_RE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b")


def _to_float(s: str) -> float:
    return float(s.replace(" ", "").replace(" ", "").replace(" ", "")
                 .replace(".", "").replace(",", ".") if s.count(",") == 1 and s.count(".") >= 1
                 else s.replace(" ", "").replace(" ", "").replace(" ", "").replace(",", "."))


def _money(s: str) -> float:
    s = s.replace(" ", "").replace(" ", "").replace(" ", "")
    # "200.000,00" ou "200000,00" ou "200.000" : la virgule est décimale, le point des milliers
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") == 1 and len(s.split(".")[1]) == 3:
        s = s.replace(".", "")  # point de milliers
    return float(s)


def _score(norm_text: str, start: int, labels) -> tuple[float, str]:
    """Score d'un candidat = poids du mot-clé + bonus de proximité."""
    window = norm_text[max(0, start - _WINDOW):start]
    best, src = 0.0, ""
    for kw, weight in labels:
        idx = window.rfind(kw)
        if idx >= 0:
            proximity = 1.0 - (len(window) - (idx + len(kw))) / _WINDOW  # 0..1
            s = weight + max(0.0, proximity)
            if s > best:
                best, src = s, kw
    return best, src


def _conf(score: float) -> float:
    return round(min(0.97, score / 4.0), 2)


def extract_fields(text: str) -> ExtractionResult:
    res = ExtractionResult(text_chars=len(text.strip()))
    if res.text_chars < 40:
        res.warnings.append("Document illisible ou vide.")
        return res
    norm = _normalize(text)

    # ── Pourcentages : affectation gloutonne taeg / nominal / taea ──
    pct_candidates = [(m.start(1), _to_float(m.group(1))) for m in _PCT_RE.finditer(norm)]
    proposals = []
    for f_name, labels in _PCT_LABELS.items():
        for pos, val in pct_candidates:
            score, src = _score(norm, pos, labels)
            if score > 0.4:
                proposals.append((score, f_name, pos, val, src))
    proposals.sort(key=lambda p: -p[0])
    used_pos, got = set(), set()
    for score, f_name, pos, val, src in proposals:
        if f_name in got or pos in used_pos:
            continue
        if not (0.0 < val < 30.0):
            continue
        res.fields[f_name] = Extracted(val, _conf(score), f"mot-clé « {src} »")
        used_pos.add(pos); got.add(f_name)

    # ── Montants € : même mécanique ──
    eur_candidates = [(m.start(1), _money(m.group(1))) for m in _EUR_RE.finditer(norm)]
    proposals = []
    for f_name, labels in _EUR_LABELS.items():
        for pos, val in eur_candidates:
            score, src = _score(norm, pos, labels)
            if score > 0.8:
                proposals.append((score, f_name, pos, val, src))
    proposals.sort(key=lambda p: -p[0])
    used_pos, got = set(), set()
    for score, f_name, pos, val, src in proposals:
        if f_name in got or pos in used_pos:
            continue
        res.fields[f_name] = Extracted(val, _conf(score), f"mot-clé « {src} »")
        used_pos.add(pos); got.add(f_name)

    # ── Durée (convertie en mois) ──
    best = (0.0, None, "")
    for m in _DUR_RE.finditer(norm):
        months = int(m.group(1)) * (12 if m.group(2).startswith("an") else 1)
        score, src = _score(norm, m.start(1), [("duree", 2.5), ("periode", 1.2), ("sur", 0.6)])
        base = score if score > 0 else 0.3
        if 3 <= months <= 420 and base > best[0]:
            best = (base, months, src or "motif durée")
    if best[1]:
        res.fields["duration_months"] = Extracted(best[1], _conf(best[0] + 1.0), best[2])

    # ── Date de l'offre ──
    best = (0.0, None, "")
    for m in _DATE_RE.finditer(norm):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        try:
            dt = date(y, mo, d)
        except ValueError:
            continue
        if not (2000 <= y <= datetime.now().year + 1):
            continue
        score, src = _score(norm, m.start(1), [
            ("date de l'offre", 3.0), ("fait le", 2.5), ("edite le", 2.5),
            ("emise le", 2.5), ("le", 0.5)])
        base = max(score, 0.4)
        if base > best[0]:
            best = (base, dt, src or "première date")
    if best[1]:
        res.fields["offer_date"] = Extracted(best[1], _conf(best[0] + 1.0), best[2])

    # ── Banque, type de crédit, type de taux ──
    for bank in BANKS:
        if _normalize(bank) in norm:
            canonical = "Boursobank" if bank == "Boursorama" else bank
            res.fields["bank"] = Extracted(canonical, 0.9, "nom détecté dans le document")
            break
    for ctype, kws in CREDIT_TYPES.items():
        if any(_normalize(k) in norm for k in kws):
            res.fields["credit_type"] = Extracted(ctype, 0.85, "vocabulaire du document")
            break
    if "taux fixe" in norm or "fixe" in norm.split():
        res.fields["rate_type"] = Extracted("fixe", 0.8, "mention « taux fixe »")
    elif "variable" in norm or "revisable" in norm:
        res.fields["rate_type"] = Extracted("variable", 0.8, "mention « variable »")

    _validate(res)
    return res


# ─────────────────────────────────────────────
#  5. Validations métier
# ─────────────────────────────────────────────
def _degrade(res: ExtractionResult, name: str, warning: str):
    if name in res.fields:
        res.fields[name].confidence = min(res.fields[name].confidence, 0.4)
    res.warnings.append(warning)


def _validate(res: ExtractionResult):
    taeg, nominal = res.get("taeg"), res.get("rate_nominal")
    if taeg is not None and nominal is not None and taeg < nominal:
        _degrade(res, "taeg", "Le TAEG semble inférieur au taux nominal. Vérifiez ces deux valeurs.")
        _degrade(res, "rate_nominal", "")
        res.warnings = [w for w in res.warnings if w]
    for name, lo, hi, label in [
        ("taeg", 0.05, 25, "TAEG"), ("rate_nominal", 0.05, 25, "taux nominal"),
        ("taea", 0.0, 6, "TAEA"), ("amount", 500, 3_000_000, "montant"),
        ("fees", 0, 15_000, "frais de dossier"),
    ]:
        v = res.get(name)
        if v is not None and not (lo <= v <= hi):
            _degrade(res, name, f"La valeur lue pour {label} paraît hors norme.")
    # « montant total dû » = capital + coût : doit dépasser le montant emprunté.
    # « coût total du crédit » = intérêts + frais : normalement inférieur. On ne
    # signale donc une incohérence que pour le premier cas.
    amount, total = res.get("amount"), res.get("total_cost")
    total_field = res.fields.get("total_cost")
    if (amount and total and total < amount
            and total_field and "montant total" in total_field.source):
        _degrade(res, "total_cost", "Le montant total dû lu est inférieur au montant emprunté.")


def extract(data: bytes, filename: str) -> ExtractionResult:
    """Point d'entrée : octets du document -> champs extraits. Rien n'est écrit sur disque."""
    text, used_ocr = document_text(data, filename)
    res = extract_fields(text)
    res.used_ocr = used_ocr
    return res
