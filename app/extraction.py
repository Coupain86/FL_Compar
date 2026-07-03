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

# Ordre = spécificité décroissante : un dossier de regroupement cite par nature
# les anciens prêts (perso, auto…) qu'il rachète — il doit donc être testé en
# premier ; « consommation » est le libellé le plus générique, testé en dernier.
CREDIT_TYPES = {
    "regroupement": ["regroupement de crédits", "rachat de crédits", "restructuration"],
    "immobilier": ["prêt immobilier", "crédit immobilier", "immobilier", "acquisition", "hypothécaire"],
    "auto": ["crédit auto", "prêt auto", "financement automobile", "véhicule"],
    "consommation": ["prêt personnel", "crédit à la consommation", "crédit consommation", "prêt conso"],
}

REGIONS = [
    "Île-de-France", "Auvergne-Rhône-Alpes", "Nouvelle-Aquitaine", "Occitanie",
    "Hauts-de-France", "Grand Est", "Provence-Alpes-Côte d'Azur", "Pays de la Loire",
    "Bretagne", "Normandie", "Bourgogne-Franche-Comté", "Centre-Val de Loire",
    "Corse", "Outre-mer",
]

# Départements -> région (pour détecter la région du bien/de l'emprunteur)
_REGION_DEPTS = {
    "Île-de-France": ["75", "77", "78", "91", "92", "93", "94", "95"],
    "Auvergne-Rhône-Alpes": ["01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"],
    "Nouvelle-Aquitaine": ["16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"],
    "Occitanie": ["09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"],
    "Hauts-de-France": ["02", "59", "60", "62", "80"],
    "Grand Est": ["08", "10", "51", "52", "54", "55", "57", "67", "68", "88"],
    "Provence-Alpes-Côte d'Azur": ["04", "05", "06", "13", "83", "84"],
    "Pays de la Loire": ["44", "49", "53", "72", "85"],
    "Bretagne": ["22", "29", "35", "56"],
    "Normandie": ["14", "27", "50", "61", "76"],
    "Bourgogne-Franche-Comté": ["21", "25", "39", "58", "70", "71", "89", "90"],
    "Centre-Val de Loire": ["18", "28", "36", "37", "41", "45"],
    "Corse": ["20"],
    "Outre-mer": ["971", "972", "973", "974", "975", "976"],
}
DEPT_TO_REGION = {d: r for r, ds in _REGION_DEPTS.items() for d in ds}

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
    sub_loans: list = field(default_factory=list)  # composantes d'une offre multi-prêts
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
    r"((?:\d{1,3}(?:[\s  .]\d{3})+|\d{1,7})(?:[.,]\d{2})?)\s*(?:€|eur(?:os?)?\b)")
_DUR_RE = re.compile(r"(\d{1,3})\s*(ans?\b|annees?\b|mois\b)")
_DATE_RE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b")


_WS_RE = re.compile(r"[\s\u00a0\u202f]")  # espaces, insécables ET retours à la ligne
# (un « 265 000,00 EUR » en fin de ligne justifiée peut se couper en deux)


def _to_float(s: str) -> float:
    s = _WS_RE.sub("", s)
    return float(s.replace(".", "").replace(",", ".")
                 if s.count(",") == 1 and s.count(".") >= 1
                 else s.replace(",", "."))


def _money(s: str) -> float:
    s = _WS_RE.sub("", s)
    # "200.000,00" ou "200000,00" ou "200.000" : la virgule est décimale, le point des milliers
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") == 1 and len(s.split(".")[1]) == 3:
        s = s.replace(".", "")  # point de milliers
    return float(s)


# Contextes qui disqualifient (presque) une valeur : exemples réglementaires,
# taux d'appel marketing, taux de sanction… Ce sont les grands pièges du réel.
# Frontières de mots obligatoires : « actuel » ne doit pas matcher dans
# « préconTRACTUELLES » ni « contrACTUEL » (qui étiquette le VRAI taux).
_NEG_RE = re.compile(
    r"\b(exemple\w*|representati\w*|simulation\w*|promotionnel\w*|usure|"
    r"retard\w*|majore\w*|ancien\w*|actuel\w*|rachet\w*)\b")

# Les montants du dossier d'assurance miment ceux du crédit : « coût total de
# l'ASSURANCE » contient le libellé « coût total », « capital emprunté assuré »
# contient « capital emprunté ». Entre le libellé et la valeur, ces mots
# disqualifient — pour les champs en euros uniquement (le TAEA, lui, vit
# légitimement au milieu du vocabulaire d'assurance).
_EUR_NEG_RE = re.compile(r"\b(assuran\w*|assure\w*|cotisation\w*)\b")


def _score(norm_text: str, start: int, labels, extra_neg=None) -> tuple[float, str]:
    """Score d'un candidat = poids du mot-clé + bonus de proximité.
    Pénalité si un mot de contexte suspect apparaît ENTRE le libellé et la
    valeur (ou juste avant le libellé) : « taux debiteur PROMOTIONNEL de
    1,00 % » est pénalisé, mais un « retard » mentionné à la ligne
    précédente ne disqualifie pas un champ correctement étiqueté."""
    window = norm_text[max(0, start - _WINDOW):start]
    best, src, best_idx = 0.0, "", -1
    for kw, weight in labels:
        idx = window.rfind(kw)
        if idx >= 0:
            proximity = 1.0 - (len(window) - (idx + len(kw))) / _WINDOW  # 0..1
            s = weight + max(0.0, proximity)
            if s > best:
                best, src, best_idx = s, kw, idx
    if best:
        zone = window[max(0, best_idx - 12):]
        if _NEG_RE.search(zone):
            best *= 0.35
            src += " (contexte suspect)"
        elif extra_neg and extra_neg.search(zone):
            best *= 0.35
            src += " (contexte assurance)"
    return best, src


def _conf(score: float) -> float:
    return round(min(0.97, score / 4.0), 2)


def _repetition_bonus(proposals):
    """Une même valeur étiquetée plusieurs fois pour le même champ est presque
    sûrement la vraie (le TAEG réel est répété : encadré, conditions, récap ;
    l'« exemple représentatif », lui, n'apparaît qu'une fois)."""
    counts = {}
    for score, f_name, _pos, val, _src in proposals:
        if score > 1.2:
            counts[(f_name, val)] = counts.get((f_name, val), 0) + 1
    return [(score + min(0.6, 0.3 * (counts.get((f_name, val), 1) - 1)),
             f_name, pos, val, src)
            for score, f_name, pos, val, src in proposals]


_POSTAL_RE = re.compile(r"\b(\d{5})\b")
_DEPT_PAREN_RE = re.compile(r"\((\d{2,3})\)")
# Contextes de localisation du bien / de l'emprunteur. Les adresses de siège
# (RCS, capital social) n'en font jamais partie : elles ne votent pas.
_REGION_CONTEXT = ("situe", "situee", "residence", "acquisition", "demeurant",
                   "immatricule", "bien ", "agence de", "logement")


def _detect_region(norm: str):
    votes = {}

    def vote(dept, pos):
        region = DEPT_TO_REGION.get(dept)
        if not region:
            return
        window = norm[max(0, pos - 80):pos]
        if any(k in window for k in _REGION_CONTEXT):
            votes[region] = votes.get(region, 0) + 1

    for m in _POSTAL_RE.finditer(norm):
        cp = m.group(1)
        dept = cp[:3] if cp.startswith("97") else cp[:2]
        vote(dept, m.start())
    for m in _DEPT_PAREN_RE.finditer(norm):
        vote(m.group(1).zfill(2), m.start())

    if not votes:
        return None
    best = max(votes, key=votes.get)
    return Extracted(best, 0.75 if votes[best] >= 2 else 0.6,
                     "code postal / département en contexte de localisation")


# Composantes possibles d'une offre immobilière multi-prêts. Chaque prêt a
# son propre montant / taux / durée ; le TAEG global couvre l'ensemble.
_SUB_LOAN_KWS = [
    (r"\bpret a taux zero\b|\bptz\b", "Prêt à taux zéro (PTZ)"),
    (r"\beco-?ptz\b", "Éco-PTZ"),
    (r"\bpret conventionne\b", "Prêt conventionné"),
    (r"\bpret d'accession sociale\b", "Prêt d'accession sociale"),
    (r"\baction logement\b|\bpret patronal\b", "Prêt Action Logement"),
    (r"\bpret relais\b", "Prêt relais"),
    (r"\bpret principal\b", "Prêt principal"),
]


def _detect_sub_loans(norm: str) -> list:
    """Repère les composantes d'une offre multi-prêts, chacune avec le
    montant / taux / durée qui la suivent immédiatement."""
    loans, seen = [], set()
    for pattern, label in _SUB_LOAN_KWS:
        for m in re.finditer(pattern, norm):
            seg = norm[m.start():m.start() + 240]
            loan = {"label": label}
            a = _EUR_RE.search(seg)
            if a:
                loan["amount"] = _money(a.group(1))
            r = _PCT_RE.search(seg)
            if r:
                loan["rate"] = _to_float(r.group(1))
            d = _DUR_RE.search(seg)
            if d:
                loan["duration_months"] = int(d.group(1)) * (12 if d.group(2).startswith("an") else 1)
            key = (label, loan.get("amount"))
            if key not in seen:
                seen.add(key)
                loans.append(loan)
            break  # une occurrence par libellé suffit (le récap dupliquerait)
    # Une offre n'est "multi-prêts" que s'il y a au moins une composante
    # aidée/complémentaire, ou plusieurs prêts nommés.
    named = [l for l in loans if l["label"] != "Prêt principal"]
    return loans if (named and len(loans) >= 2) else []


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
    proposals = _repetition_bonus(proposals)
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
            score, src = _score(norm, pos, labels, extra_neg=_EUR_NEG_RE)
            if score > 0.8:
                proposals.append((score, f_name, pos, val, src))
    proposals = _repetition_bonus(proposals)
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

    region = _detect_region(norm)
    if region:
        res.fields["region"] = region

    res.sub_loans = _detect_sub_loans(norm)

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
