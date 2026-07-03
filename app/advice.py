"""
Moteur de conseil en négociation : transforme une offre + sa cohorte + les
éventuelles offres concurrentes du même utilisateur en un plan d'action
chiffré et priorisé.

Principes :
  - 100 % déterministe (aucune IA) : mathématiques d'amortissement exactes,
    percentiles de cohorte, règles métier explicites.
  - Chaque levier indique SUR QUOI il s'appuie (médiane de cohorte, offre
    concurrente en main, hypothèse prudente) — jamais un chiffre sorti de
    nulle part.
  - Un levier ne se déclenche que si les données nécessaires existent ; les
    montants sont des ESTIMATIONS, affichées comme telles.

Le même plan alimente les deux rapports :
  - rapport « personne » : tous les leviers + la méthode de mise en
    concurrence multi-banques (toujours l'action n° 1) ;
  - rapport « banque » : uniquement les postes factuels et chiffrables
    (taux, assurance, frais, clauses), avec la cible qui rendrait l'offre
    compétitive — jamais les tactiques du client.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from . import benchmark as bm
from .models import Offer

# ─────────────────────────────────────────────
#  Mathématiques d'amortissement (exactes)
# ─────────────────────────────────────────────
def monthly_payment(principal: float, annual_pct: float, months: int) -> float:
    r = annual_pct / 100 / 12
    if r <= 0:
        return principal / months
    return principal * r / (1 - (1 + r) ** -months)


def total_interest(principal: float, annual_pct: float, months: int) -> float:
    return monthly_payment(principal, annual_pct, months) * months - principal


def rate_cut_gain(principal: float, months: int, rate_from: float, rate_to: float) -> float:
    """Économie exacte (€ sur la durée) si le taux passe de rate_from à rate_to."""
    return (monthly_payment(principal, rate_from, months)
            - monthly_payment(principal, rate_to, months)) * months


def insurance_cut_gain(principal: float, months: int, nominal: float,
                       taea_from: float, taea_to: float) -> float:
    """Économie exacte si le TAEA baisse : le TAEA agit comme un supplément de
    taux sur le crédit (c'est sa définition réglementaire : TAEG avec
    assurance = TAEG hors assurance + TAEA)."""
    return rate_cut_gain(principal, months, nominal + taea_from, nominal + taea_to)


# ─────────────────────────────────────────────
#  Statistiques de cohorte
# ─────────────────────────────────────────────
def _pctile(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    idx = q * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


@dataclass
class CohortStats:
    size: int
    taeg_p25: float | None = None
    taeg_med: float | None = None
    taeg_p75: float | None = None
    nominal_p25: float | None = None
    nominal_med: float | None = None
    taea_p25: float | None = None
    taea_med: float | None = None
    fees_p25: float | None = None
    fees_med: float | None = None
    better_than_pct: int | None = None   # position de l'offre étudiée


def cohort_stats(db: Session, offer: Offer) -> CohortStats | None:
    if not offer.credit_type or not offer.region:
        return None
    cohort = bm.cohort_query(db, offer).all()
    if len(cohort) < bm.MIN_COHORT:
        return None

    def series(name):
        return [getattr(o, name) for o in cohort if getattr(o, name) is not None]

    taegs = series("taeg")
    st = CohortStats(size=len(cohort),
                     taeg_p25=round(_pctile(taegs, 0.25), 2),
                     taeg_med=round(_pctile(taegs, 0.50), 2),
                     taeg_p75=round(_pctile(taegs, 0.75), 2))
    if offer.taeg is not None:
        st.better_than_pct = round(100 * sum(1 for t in taegs if t > offer.taeg) / len(taegs))
    for name, p25_attr, med_attr in [("rate_nominal", "nominal_p25", "nominal_med"),
                                     ("taea", "taea_p25", "taea_med"),
                                     ("fees", "fees_p25", "fees_med")]:
        vals = series(name)
        if len(vals) >= 3:
            setattr(st, p25_attr, round(_pctile(vals, 0.25), 2))
            setattr(st, med_attr, round(_pctile(vals, 0.50), 2))
    return st


# ─────────────────────────────────────────────
#  Le plan de négociation
# ─────────────────────────────────────────────
EASE_ORDER = {"offre en main": 0, "quasi certain": 1, "réaliste": 2,
              "ambitieux": 3, "à étudier": 4}

# Repères nationaux prudents, utilisés UNIQUEMENT en l'absence de cohorte
# suffisante, et toujours annoncés comme hypothèses.
_FALLBACK_RATE_CUT = {"réaliste": 0.15, "ambitieux": 0.35}     # points de taux
_FALLBACK_TAEA_TARGET = {"immobilier": 0.35, "auto": 0.60,
                         "consommation": 0.60, "regroupement": 0.60}
_FEES_FLOOR = 150.0          # plafond « raisonnable » de frais de dossier négociés
_BROKER_CUT = 0.20           # décote courtier typique (hypothèse annoncée)


@dataclass
class Lever:
    code: str
    category: str
    title: str
    detail: str                 # explication pour la personne
    say: str = ""               # l'argument, mot pour mot
    gain_total: float | None = None    # € sur la durée (estimation)
    gain_monthly: float | None = None  # € par mois (estimation)
    ease: str = "à étudier"
    basis: str = ""             # sur quoi s'appuie le chiffre
    bank_ask: str = ""          # formulation côté rapport banque ("" = absent)
    bank_target: str = ""       # cible côté banque (ex : "3,45 %")


@dataclass
class AdvicePlan:
    levers: list[Lever] = field(default_factory=list)
    method: list[str] = field(default_factory=list)   # méthode multi-banques
    stats: CohortStats | None = None
    competitors: list[dict] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def top(self, n=3):
        """Les n plus gros gisements, UN par famille de levier : baisser le
        taux via la cohorte, via une offre concurrente ou via un courtier,
        c'est le même gisement — on ne l'additionne pas trois fois."""
        def family(l):
            return ("taux" if l.code.startswith(("taux", "rival", "courtier"))
                    else l.code)
        best: dict[str, Lever] = {}
        for l in self.levers:
            if l.gain_total and (family(l) not in best
                                 or l.gain_total > best[family(l)].gain_total):
                best[family(l)] = l
        return sorted(best.values(), key=lambda l: -l.gain_total)[:n]

    def by_category(self):
        cats: dict[str, list[Lever]] = {}
        for l in self.levers:
            cats.setdefault(l.category, []).append(l)
        return cats

    def bank_points(self):
        return [l for l in self.levers if l.bank_ask]


CAT_RATE = "Taux et structure du crédit"
CAT_INSURANCE = "Assurance emprunteur"
CAT_FEES = "Frais et coûts annexes"
CAT_FLEX = "Flexibilité du contrat"
CAT_COUNTER = "Contreparties commerciales"
CAT_METHOD = "Méthode et timing"

METHOD_STEPS = [
    "Sollicitez un MAXIMUM de banques : votre banque actuelle, 2-3 grandes banques de réseau, "
    "2 banques en ligne et une banque régionale. Chaque offre écrite est une arme de négociation.",
    "Exigez systématiquement une offre ÉCRITE (ou une simulation détaillée signée). Un taux "
    "annoncé à l'oral n'engage personne.",
    "Faites jouer les offres les unes contre les autres : montrez la meilleure offre à chaque "
    "banque et demandez-lui de faire mieux — puis recommencez un tour.",
    "Synchronisez le calendrier : les offres sont valables 30 jours. Demandez-les sur une "
    "période resserrée pour pouvoir les comparer toutes en même temps.",
    "Négociez en fin de mois ou de trimestre : les conseillers ont des objectifs commerciaux "
    "et sont plus enclins à un geste pour signer.",
    "N'acceptez JAMAIS la première offre — même correcte, elle contient toujours une marge.",
    "Pensez au courtier si vous manquez de temps : un seul dossier interroge des dizaines "
    "d'établissements, avec des décotes de gros volume.",
]


def _fmt_pct(v) -> str:
    return f"{v:.2f} %".replace(".", ",") if v is not None else "—"


def _fmt_eur(v) -> str:
    return f"{v:,.0f} €".replace(",", " ") if v is not None else "—"


def build_plan(db: Session, offer: Offer, others: list[Offer] | None = None) -> AdvicePlan:
    plan = AdvicePlan(method=list(METHOD_STEPS))
    plan.stats = cohort_stats(db, offer)
    others = [o for o in (others or []) if o.id != offer.id]

    amount, months = offer.amount, offer.duration_months
    nominal, taeg, taea, fees = offer.rate_nominal, offer.taeg, offer.taea, offer.fees
    years = months / 12 if months else None
    can_math = bool(amount and months and nominal is not None)

    if plan.stats is None:
        plan.caveats.append(
            "Pas encore assez d'offres comparables dans votre cohorte : les objectifs chiffrés "
            "s'appuient sur des hypothèses prudentes de marché, annoncées à chaque fois.")

    # ── Offres concurrentes du même utilisateur ──
    for o in sorted(others, key=lambda o: (o.taeg is None, o.taeg or 99)):
        plan.competitors.append({
            "id": o.id, "bank": o.bank or "Banque non identifiée",
            "taeg": o.taeg, "rate_nominal": o.rate_nominal, "taea": o.taea,
            "amount": o.amount, "duration_months": o.duration_months,
            "fees": o.fees, "offer_date": o.offer_date,
        })
    best_rival = next((c for c in plan.competitors if c["taeg"] is not None), None)

    L = plan.levers.append

    # ═════ A. TAUX ═════
    if best_rival and taeg is not None and best_rival["taeg"] < taeg:
        delta = taeg - best_rival["taeg"]
        gain = None
        if amount and years:
            gain = amount * (delta / 100) * years / 2   # approximation prudente
        L(Lever(
            code="rival", category=CAT_RATE,
            title=f"Faire jouer votre offre concurrente ({best_rival['bank']})",
            detail=f"Vous détenez déjà une offre à {_fmt_pct(best_rival['taeg'])} de TAEG, soit "
                   f"{_fmt_pct(delta)[:-2].strip()} point(s) de moins. C'est l'argument le plus "
                   "puissant qui existe : une offre écrite, réelle, signable.",
            say=f"« J'ai une offre écrite de {best_rival['bank']} à "
                f"{_fmt_pct(best_rival['taeg'])} de TAEG. Je préfère travailler avec vous : "
                "pouvez-vous l'égaler ou faire mieux ? »",
            gain_total=gain, gain_monthly=(gain / months if gain and months else None),
            ease="offre en main",
            basis=f"votre offre {best_rival['bank']} à {_fmt_pct(best_rival['taeg'])}",
            bank_ask="Le client dispose d'une offre concurrente écrite plus avantageuse "
                     f"(TAEG {_fmt_pct(best_rival['taeg'])}). Aligner le TAEG sécurise la signature.",
            bank_target=_fmt_pct(best_rival["taeg"])))

    if can_math:
        st = plan.stats
        scenarios = []
        if st and st.nominal_med is not None and st.nominal_med < nominal:
            scenarios.append(("réaliste", st.nominal_med,
                              f"médiane de votre cohorte, {st.size} offres"))
        if st and st.nominal_p25 is not None and st.nominal_p25 < nominal:
            scenarios.append(("ambitieux", st.nominal_p25,
                              f"meilleur quart de votre cohorte, {st.size} offres"))
        if not scenarios:
            for ease, cut in _FALLBACK_RATE_CUT.items():
                scenarios.append((ease, round(nominal - cut, 2),
                                  f"hypothèse prudente de marché (−{cut:.2f} pt)"))
        for ease, target, basis in scenarios:
            if target >= nominal:
                continue
            gain = rate_cut_gain(amount, months, nominal, target)
            L(Lever(
                code=f"taux_{ease}", category=CAT_RATE,
                title=f"Négocier le taux nominal : {_fmt_pct(nominal)} → {_fmt_pct(target)}",
                detail=f"Objectif {ease} fondé sur {basis}. Calcul exact d'amortissement, "
                       "pas une règle de trois.",
                say=f"« Les offres comparables à mon profil se négocient autour de "
                    f"{_fmt_pct(target)}. Qu'est-ce que vous pouvez faire sur le taux ? »",
                gain_total=gain, gain_monthly=gain / months,
                ease=ease, basis=basis,
                bank_ask=f"Taux débiteur : {_fmt_pct(nominal)} constaté, contre "
                         f"{_fmt_pct(target)} pour les offres comparables ({basis}).",
                bank_target=_fmt_pct(target)))
            break  # un seul objectif « principal » ; l'ambitieux vit dans le rapport détaillé

        # scénario ambitieux (affiché en plus si distinct)
        amb = [s for s in scenarios if s[0] == "ambitieux" and s[1] < nominal]
        if amb and (not scenarios or amb[0][1] < scenarios[0][1]):
            ease, target, basis = amb[0]
            gain = rate_cut_gain(amount, months, nominal, target)
            L(Lever(
                code="taux_ambitieux", category=CAT_RATE,
                title=f"Scénario ambitieux : viser {_fmt_pct(target)}",
                detail=f"Si la mise en concurrence fonctionne à plein — base : {basis}.",
                say="« Une autre banque me propose mieux — alignez-vous sur le meilleur quart "
                    "du marché et je signe chez vous. »",
                gain_total=gain, gain_monthly=gain / months,
                ease="ambitieux", basis=basis))

    if can_math:
        gain = rate_cut_gain(amount, months, nominal, max(0.0, nominal - _BROKER_CUT))
        L(Lever(
            code="courtier", category=CAT_RATE,
            title="Passer par un courtier en crédit",
            detail="Les courtiers apportent du volume aux banques et obtiennent des décotes "
                   "inaccessibles à un particulier. Un seul dossier interroge des dizaines "
                   "d'établissements. Frais de courtage à mettre en regard du gain.",
            say="« Je consulte aussi un courtier — son réseau obtient des conditions de gros. »",
            gain_total=gain, gain_monthly=gain / months,
            ease="réaliste", basis=f"hypothèse décote courtier −{_BROKER_CUT:.2f} point"))

    if months and months >= 120 and can_math:
        new_months = months - 24
        gain = (total_interest(amount, nominal, months)
                - total_interest(amount, nominal, new_months))
        extra = monthly_payment(amount, nominal, new_months) - monthly_payment(amount, nominal, months)
        L(Lever(
            code="duree", category=CAT_RATE,
            title=f"Raccourcir la durée de 24 mois ({months} → {new_months} mois)",
            detail=f"Mensualité en hausse d'environ {_fmt_eur(extra)}, mais "
                   f"{_fmt_eur(gain)} d'intérêts en moins. À n'envisager que si votre budget "
                   "l'absorbe confortablement. Une durée plus courte obtient aussi souvent un "
                   "meilleur taux.",
            say="« Sur 2 ans de moins, quel taux pouvez-vous me proposer ? »",
            gain_total=gain, gain_monthly=None,
            ease="à étudier", basis="calcul exact à taux constant"))

    if offer.rate_type == "variable" and can_math:
        risk = rate_cut_gain(amount, months, nominal + 1.0, nominal)
        L(Lever(
            code="variable", category=CAT_RATE,
            title="Taux variable : exiger un cap strict ou repasser en fixe",
            detail=f"Si votre taux atteint son plafond (+1 point), le surcoût serait d'environ "
                   f"{_fmt_eur(risk)} sur la durée. Négociez un cap de variation serré, une "
                   "option de passage à taux fixe sans frais, ou comparez avec une offre fixe.",
            say="« Je veux une clause de passage à taux fixe sans frais, ou un cap à ±1 point "
                "maximum, écrit dans le contrat. »",
            ease="réaliste", basis="calcul exact au taux plafond",
            bank_ask="Taux révisable : le client demande un cap de variation contractuel "
                     "serré ou une option de passage à taux fixe sans frais.",
            bank_target="cap ±1 pt / option fixe"))

    if offer.credit_type == "immobilier":
        sub_loans = (offer.extraction_meta or {}).get("sub_loans", [])
        if not sub_loans:
            L(Lever(
                code="ptz", category=CAT_RATE,
                title="Vérifier votre éligibilité aux prêts aidés (PTZ, Action Logement…)",
                detail="Aucun prêt aidé n'apparaît dans votre offre. Selon vos revenus, la zone "
                       "et le projet, un Prêt à Taux Zéro peut financer une partie de "
                       "l'opération à 0 % — ainsi que l'éco-PTZ (travaux) ou le prêt Action "
                       "Logement (salariés du privé). C'est un droit, pas une faveur de la banque.",
                say="« Ai-je droit au PTZ ou à un prêt Action Logement ? Merci d'étudier le "
                    "montage et de me répondre par écrit. »",
                ease="à étudier", basis="dispositifs publics sous conditions"))
        elif len(sub_loans) >= 2:
            L(Lever(
                code="lissage", category=CAT_RATE,
                title="Demander le lissage de vos prêts",
                detail="Votre financement combine plusieurs prêts : demandez le lissage des "
                       "échéances (mensualité totale constante) pour éviter les paliers de "
                       "remboursement et optimiser le coût global.",
                say="« Merci de me proposer le montage avec lissage des mensualités. »",
                ease="quasi certain", basis="votre offre contient plusieurs prêts",
                bank_ask="Financement multi-prêts : le client demande un montage avec "
                         "lissage des échéances.", bank_target="lissage"))
        L(Lever(
            code="intercalaires", category=CAT_RATE,
            title="Limiter les intérêts intercalaires (achat sur plan / travaux)",
            detail="Si les fonds sont débloqués progressivement, des intérêts intercalaires "
                   "courent avant même la première vraie mensualité. Négociez leur franchise, "
                   "leur plafonnement, ou un démarrage d'amortissement immédiat.",
            say="« Comment sont traités les intérêts intercalaires ? Je demande leur "
                "franchise ou leur plafonnement. »",
            ease="réaliste", basis="uniquement si déblocage progressif"))

    # ═════ B. ASSURANCE ═════
    if taea is not None and can_math:
        st = plan.stats
        target, basis = None, ""
        if st and st.taea_med is not None and st.taea_med < taea:
            target, basis = st.taea_med, f"TAEA médian de votre cohorte ({st.size} offres)"
        fallback = _FALLBACK_TAEA_TARGET.get(offer.credit_type or "", 0.60)
        if (target is None or fallback < target) and fallback < taea:
            target, basis = fallback, "niveau couramment obtenu en délégation d'assurance"
        if target is not None:
            gain = insurance_cut_gain(amount, months, nominal, taea, target)
            L(Lever(
                code="delegation", category=CAT_INSURANCE,
                title=f"Délégation d'assurance : TAEA {_fmt_pct(taea)} → {_fmt_pct(target)}",
                detail=f"Objectif fondé sur {basis}. La loi Lagarde vous permet de choisir "
                       "votre assureur dès la souscription, et la loi Lemoine de changer à "
                       "TOUT MOMENT ensuite, sans frais, à garanties équivalentes. La banque "
                       "ne peut pas refuser un contrat aux garanties équivalentes ni modifier "
                       "le taux du crédit en représailles (c'est illégal).",
                say="« Je ferai jouer la délégation d'assurance (lois Lagarde et Lemoine). "
                    "Soit votre contrat groupe s'aligne, soit je prends un assureur externe. »",
                gain_total=gain, gain_monthly=gain / months,
                ease="quasi certain", basis=basis,
                bank_ask=f"Assurance : TAEA constaté {_fmt_pct(taea)}, contre {_fmt_pct(target)} "
                         f"({basis}). Le client fera jouer la délégation (Lagarde/Lemoine) si le "
                         "contrat groupe ne s'aligne pas.",
                bank_target=_fmt_pct(target)))
    elif taea is None:
        L(Lever(
            code="taea_absent", category=CAT_INSURANCE,
            title="Récupérer le TAEA — c'est souvent le premier gisement d'économie",
            detail="Le coût de votre assurance (TAEA) n'est pas renseigné. Il figure sur la "
                   "FISE ou la notice d'assurance. Sur un crédit immobilier, l'assurance pèse "
                   "souvent 25 à 40 % du coût total : impossible de bien négocier sans ce chiffre.",
            say="« Merci de m'indiquer le TAEA exact et le coût total de l'assurance sur la "
                "durée, par écrit. »",
            ease="quasi certain", basis="donnée manquante dans votre dossier"))

    L(Lever(
        code="assurance_fine", category=CAT_INSURANCE,
        title="Affiner le contrat d'assurance lui-même",
        detail="Quatre réglages qui baissent la cotisation sans vous fragiliser : "
               "1) Quotités — à deux emprunteurs, 100/100 est le plus protecteur mais "
               "100/50 ou 70/30 coûte bien moins si vos revenus sont déséquilibrés. "
               "2) Garanties superflues — la garantie perte d'emploi est chère et très "
               "encadrée ; l'ITT peut être inutile selon votre statut (fonctionnaire…). "
               "3) Franchise — passer de 90 à 180 jours de franchise réduit la cotisation si "
               "vous avez une bonne prévoyance par ailleurs. "
               "4) Santé — droit à l'oubli et convention AERAS si vous avez eu un problème de "
               "santé : les surprimes ne sont pas une fatalité.",
        say="« Détaillez-moi le coût de chaque garantie et de chaque quotité : je veux payer "
            "pour ce qui me protège, pas pour le reste. »",
        ease="réaliste", basis="réglages contractuels standard"))

    # ═════ C. FRAIS ═════
    if fees is not None and fees > 0:
        st = plan.stats
        target, basis = 0.0, "geste commercial très couramment accordé"
        if st and st.fees_med is not None and st.fees_med < fees:
            basis = f"médiane de votre cohorte : {_fmt_eur(st.fees_med)}"
        gain = fees  # on vise la gratuité ; le plancher sert d'argument de repli
        L(Lever(
            code="frais_dossier", category=CAT_FEES,
            title=f"Frais de dossier : demander la gratuité ({_fmt_eur(fees)} en jeu)",
            detail=f"Les frais de dossier sont le geste le plus facile à obtenir ({basis}). "
                   f"Position de repli : un plafond strict à {_fmt_eur(_FEES_FLOOR)}-"
                   f"{_fmt_eur(300)}. Gain modeste mais quasi garanti — demandez-le en dernier, "
                   "une fois le taux et l'assurance acquis, comme condition de signature.",
            say="« Pour finaliser aujourd'hui, je demande la gratuité des frais de dossier. »",
            gain_total=gain, gain_monthly=None,
            ease="quasi certain", basis=basis,
            bank_ask=f"Frais de dossier : {_fmt_eur(fees)} constatés. La gratuité (ou un "
                     f"plafond à {_fmt_eur(300)}) est un geste standard de finalisation.",
            bank_target="0 €"))

    if offer.credit_type == "immobilier" and amount:
        restit = amount * 0.0075
        L(Lever(
            code="garantie", category=CAT_FEES,
            title="Choisir la caution mutuelle plutôt que l'hypothèque",
            detail="Avec un organisme de caution (Crédit Logement, CAMCA, CMH…), une partie du "
                   f"versement initial vous est RESTITUÉE à la fin du prêt (souvent de l'ordre "
                   f"de {_fmt_eur(restit)} sur votre montant) — et vous évitez les frais de "
                   "mainlevée d'hypothèque en cas de revente anticipée. Demandez les deux "
                   "devis de garantie et comparez le coût NET (après restitution).",
            say="« Je veux le comparatif chiffré caution vs hypothèque, restitution finale "
                "incluse. »",
            ease="réaliste", basis="ordre de grandeur usuel de restitution (~0,75 %)",
            bank_ask="Garantie : le client demande le comparatif chiffré caution mutuelle / "
                     "hypothèque, restitution finale incluse.", bank_target="comparatif écrit"))

    L(Lever(
        code="tenue_compte", category=CAT_FEES,
        title="Gratuité des frais de tenue de compte et refus des packages",
        detail=("Si la banque demande la domiciliation de vos revenus, la moindre des "
                "contreparties est la gratuité de la tenue de compte"
                + (f" (2 à 4 € par mois, soit jusqu'à {_fmt_eur(4 * months)} sur la durée)"
                   if months else "")
                + ". Refusez les packages de services facturés (cartes premium, assurances de "
                  "moyens de paiement…) que vous n'avez pas demandés."),
        say="« Domiciliation contre gratuité de la tenue de compte, et aucun package que je "
            "n'ai pas explicitement demandé. »",
        ease="quasi certain", basis="contrepartie standard de la domiciliation",
        bank_ask="Tenue de compte : gratuité demandée en contrepartie de la domiciliation "
                 "des revenus.", bank_target="0 €/mois"))

    if offer.credit_type != "immobilier":
        L(Lever(
            code="ira_conso", category=CAT_FEES,
            title="Remboursement anticipé : connaître les plafonds légaux",
            detail="Sur un crédit à la consommation, l'indemnité de remboursement anticipé est "
                   "plafonnée par la loi : 1 % du montant remboursé (0,5 % si moins d'un an "
                   "restant), et INTERDITE si le remboursement est inférieur à 10 000 € sur "
                   "12 mois. Beaucoup de contrats l'appliquent à tort : vérifiez la clause.",
            say="« La clause de remboursement anticipé respecte-t-elle les plafonds légaux ? "
                "Je demande sa suppression pure et simple. »",
            ease="quasi certain", basis="plafonds légaux (Code de la consommation)"))

    # ═════ D. FLEXIBILITÉ ═════
    ira_note = ""
    if can_math:
        ira_cost = min(0.03 * amount, 6 * amount * nominal / 100 / 12)
        ira_note = (f" À titre d'illustration, cette clause pourrait vous coûter jusqu'à "
                    f"{_fmt_eur(ira_cost)} si vous remboursiez tôt.")
    L(Lever(
        code="ira", category=CAT_FLEX,
        title="Supprimer les indemnités de remboursement anticipé (IRA)",
        detail="Exigez l'annulation des pénalités de remboursement anticipé. Le compromis "
               "standard, que les banques acceptent : suppression sauf en cas de rachat par "
               "une banque concurrente. Cela ne coûte rien à la banque aujourd'hui et vous "
               "rend libre demain (revente, rentrée d'argent, renégociation)." + ira_note,
        say="« Je demande la suppression des IRA, sauf rachat par un concurrent — c'est la "
            "clause d'usage. »",
        ease="réaliste", basis="clause d'usage",
        bank_ask="IRA : suppression demandée sauf rachat par un établissement concurrent "
                 "(clause d'usage, sans coût immédiat).", bank_target="suppression"))

    L(Lever(
        code="modularite", category=CAT_FLEX,
        title="Modularité des échéances (à la hausse ET à la baisse)",
        detail="Demandez le droit de moduler vos mensualités de ±10 à ±50 % par an, sans "
               "frais, selon l'évolution de vos revenus. À la hausse, c'est des intérêts en "
               "moins ; à la baisse, c'est une soupape en cas de coup dur. Vérifiez que "
               "l'option figure au CONTRAT, pas seulement dans la plaquette.",
        say="« La modularité ±30 % sans frais est-elle écrite au contrat ? »",
        ease="quasi certain", basis="option standard des contrats récents",
        bank_ask="Modularité des échéances : inscription au contrat demandée (±30 %, sans "
                 "frais).", bank_target="au contrat"))

    L(Lever(
        code="report", category=CAT_FLEX,
        title="Report d'échéances en cas de coup dur",
        detail="Négociez le droit de suspendre 1 à 6 mensualités (une à deux fois sur la vie "
               "du prêt) en cas d'accident de la vie. Attention : un report allonge le prêt "
               "et coûte des intérêts — c'est une assurance de souplesse, pas un cadeau ; "
               "elle doit être GRATUITE à la mise en place.",
        say="« L'option de report d'échéances est-elle incluse, gratuite, et écrite ? »",
        ease="quasi certain", basis="option standard",
        bank_ask="Report d'échéances : option gratuite demandée au contrat.",
        bank_target="incluse"))

    L(Lever(
        code="remb_partiel", category=CAT_FLEX,
        title="Remboursements partiels libres, sans minimum dissuasif",
        detail="Certains contrats imposent un minimum de remboursement partiel (10 % du "
               "capital…). Faites-le supprimer : chaque rentrée d'argent (prime, héritage) "
               "doit pouvoir réduire votre crédit sans contrainte.",
        say="« Aucun montant minimum pour un remboursement partiel — c'est possible ? »",
        ease="réaliste", basis="clause contractuelle"))

    if offer.credit_type == "immobilier":
        L(Lever(
            code="transfert", category=CAT_FLEX,
            title="Clause de transférabilité du prêt",
            detail="Rare mais précieuse : elle permet, en cas de revente puis rachat, de "
                   "TRANSFÉRER votre taux actuel sur le nouveau bien. Si les taux remontent, "
                   "cette clause vaut de l'or. Elle ne coûte rien à demander.",
            say="« Le prêt est-il transférable sur un futur achat ? Je demande la clause. »",
            ease="ambitieux", basis="clause rare, à forte valeur d'option"))

    # ═════ E. CONTREPARTIES ═════
    L(Lever(
        code="domiciliation", category=CAT_COUNTER,
        title="Domicilier vos revenus — mais en le monnayant",
        detail="La domiciliation des salaires est LE service que veut la banque : ne l'offrez "
               "jamais gratuitement. La loi encadre cette clause : 10 ans maximum, et elle "
               "doit s'accompagner d'un avantage individualisé (taux préférentiel…) écrit au "
               "contrat. Pas d'avantage écrit = pas d'engagement de domiciliation.",
        say="« Je domicilie mes revenus si cela se traduit par un avantage écrit : lequel ? »",
        ease="quasi certain", basis="cadre légal de la domiciliation"))
    L(Lever(
        code="iard", category=CAT_COUNTER,
        title="Assurances habitation/auto : une monnaie d'échange temporaire",
        detail="Proposez de souscrire l'assurance habitation (obligatoire pour un achat de "
               "toute façon) ou auto dans leur filiale en échange d'une baisse de taux. Ces "
               "contrats se résilient facilement après un an (loi Hamon) : la concession est "
               "réversible, la baisse de taux, elle, est acquise.",
        say="« Si je prends l'assurance habitation chez vous, que gagnez-vous… et que "
            "gagne-t-on sur le taux ? »",
        ease="réaliste", basis="contrats résiliables après 1 an (loi Hamon)"))
    L(Lever(
        code="epargne", category=CAT_COUNTER,
        title="Mettre votre épargne dans la balance",
        detail="Assurance-vie, PEA, livrets : annoncer le transfert (même partiel) de votre "
               "épargne après signature pèse dans la décision — la banque gagne sur la "
               "relation globale, pas seulement sur le crédit. Ne transférez qu'APRÈS "
               "obtention des conditions, et gardez la main sur vos placements.",
        say="« J'ai X € d'épargne à placer. Faites un effort sur le taux et la relation "
            "s'installe chez vous. »",
        ease="réaliste", basis="valeur relation globale pour la banque"))
    L(Lever(
        code="profil", category=CAT_COUNTER,
        title="Vendre votre profil, pas seulement votre dossier",
        detail="Âge, perspectives d'évolution salariale, profession du co-emprunteur, épargne "
               "de précaution, absence de crédits en cours, stabilité professionnelle : la "
               "banque achète vos 20 prochaines années, pas votre dossier du jour. Un jeune "
               "profil évolutif justifie un effort commercial — dites-le explicitement.",
        say="« Regardez le potentiel du dossier sur 20 ans, pas seulement la photo "
            "d'aujourd'hui. »",
        ease="réaliste", basis="argument commercial classique"))

    # ═════ F. MÉTHODE ═════
    if offer.credit_type == "regroupement":
        L(Lever(
            code="regroupement_duree", category=CAT_METHOD,
            title="Regroupement : surveillez la durée comme le lait sur le feu",
            detail="Une mensualité qui baisse cache souvent une durée qui explose — et un "
                   "coût total qui gonfle. Exigez, pour chaque proposition, le COÛT TOTAL et "
                   "la durée, et comparez à vos crédits actuels conservés tels quels. "
                   "Négociez la durée la plus courte que votre budget supporte.",
            say="« Donnez-moi le coût total de l'opération et l'écart avec mes crédits "
                "actuels conservés. »",
            ease="quasi certain", basis="mécanique du regroupement"))
    if taeg is not None and can_math:
        L(Lever(
            code="renego_future", category=CAT_METHOD,
            title="Gardez ce repère pour l'avenir : le seuil de renégociation",
            detail=f"Votre taux en poche, notez ceci : si les taux de marché passent un jour "
                   f"environ 0,7 point sous votre taux nominal ({_fmt_pct(nominal)}, donc un "
                   f"marché à ~{_fmt_pct(max(0.1, nominal - 0.7))}) et qu'il vous reste plus "
                   "d'un tiers de la durée, une renégociation ou un rachat redevient gagnant. "
                   "TrustRate peut vous alerter à ce moment-là.",
            say="—",
            ease="à étudier", basis="règle des 0,7 point / tiers de durée"))

    # tri : les chiffrés d'abord (gain décroissant), puis par facilité
    plan.levers.sort(key=lambda l: (l.gain_total is None,
                                    -(l.gain_total or 0),
                                    EASE_ORDER.get(l.ease, 9)))
    return plan
