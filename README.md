# TrustRate — comparateur d'offres de crédit (Phase 1 : benchmark)

Un particulier dépose son offre de crédit (PDF ou photo). Le site la lit
automatiquement (extraction déterministe, **sans IA**), la compare aux offres
similaires déposées par la communauté, et lui rend un positionnement clair
(« vous êtes mieux placé que X % ») avec des pistes d'économie.

Ce dépôt contient la Phase 1 du dossier produit : le parcours particulier
complet (U1→U9) + un back-office minimal (vue d'ensemble, boucle de
corrections, densité par cohorte).

## Lancer en local (Docker)

Prérequis : Docker Desktop (ou Docker + le plugin compose).

**Sans rien installer d'autre** — gardez uniquement `docker-compose.hub.yml`
sur votre poste (Docker récupère le code sur GitHub et construit tout) :

```bash
docker compose -f docker-compose.hub.yml up --build
```

**Depuis un clone du dépôt** (développement) :

```bash
docker compose up --build
```

Puis :

- **Le site** : http://localhost:8000
- **Back-office** : http://localhost:8000/admin — identifiants `admin` / `admin`
- Des offres de démonstration sont insérées au premier démarrage, et deux
  **PDF d'exemple à déposer** sont générés dans `samples/`
  (`offre_exemple_immobilier.pdf`, `offre_exemple_conso.pdf`).

Arrêt : `Ctrl+C` puis `docker compose -f docker-compose.hub.yml down`
(ajouter `-v` pour effacer aussi la base).

## Scénario de test conseillé

1. Ouvrir http://localhost:8000 → « Comparer mon offre » → Immobilier.
2. Déposer `samples/offre_exemple_immobilier.pdf`, cocher le consentement.
3. Vérifier les champs lus (TAEG 3,90 %, montant 200 000 €…), corriger si besoin.
4. Profil : revenus « 2 000 – 3 500 € », apport « 10 – 20 % », région **Île-de-France**.
5. → Positionnement chiffré (la cohorte Île-de-France est seedée).
6. Refaire avec type **Auto** + région **Occitanie** → écran « pas assez de
   données » (cohorte volontairement sous le seuil).
7. Corriger un champ à l'étape 3 puis regarder `/admin` : la correction
   apparaît dans la boucle d'amélioration.

## Ce que fait (et ne fait pas) cette version

**Fait** : parcours complet, extraction déterministe candidats+scoring
(PDF natif + OCR Tesseract pour les scans), confiance par champ, écran de
vérification, benchmark par cohorte avec seuil de fiabilité, consentements
par finalité, suppression des données par l'utilisateur, back-office.

**Pas encore** : envoi réel d'emails (les demandes sont enregistrées),
espace banque (Phase 2-3), comptes utilisateurs (session par cookie),
HTTPS/déploiement (ce compose est prévu pour le test local uniquement).

## Confidentialité (par construction)

- Le document déposé est traité **en mémoire** et n'est **jamais** écrit sur disque.
- Seuls les champs comparables sont stockés — pas de nom, pas d'adresse.
- Le contact (email/téléphone) n'existe que rattaché à un consentement
  explicite par finalité, et « Supprimer mes données » efface tout.

## Configuration (variables d'environnement)

| Variable | Défaut | Rôle |
|---|---|---|
| `DATABASE_URL` | SQLite locale | PostgreSQL dans le compose |
| `SEED_ON_START` | `0` | `1` = données de démo au premier démarrage |
| `MIN_COHORT` | `8` | offres comparables minimum pour un verdict chiffré |
| `COHORT_WINDOW_DAYS` | `365` | fenêtre de comparaison |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin`/`admin` | accès back-office (local) |

## Développement sans Docker (optionnel)

```bash
pip install -r requirements.txt
SEED_ON_START=1 uvicorn app.main:app --reload
```
(SQLite est utilisée automatiquement si `DATABASE_URL` n'est pas définie.)
