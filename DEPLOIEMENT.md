# Mettre TrustRate en production — pas à pas

Ce guide part de zéro et ne suppose aucune connaissance technique au-delà du
copier-coller. Durée totale : environ 1 heure (dont l'attente du DNS).
Coût : ~5 à 10 €/mois de serveur + ~10 €/an de nom de domaine.

---

## 1. Louer un serveur (VPS)

Chez **Hetzner** (hetzner.com), **OVH** (ovhcloud.com) ou **Scaleway** :

- Choisissez un VPS avec **2 vCPU / 4 Go de RAM / 40 Go de disque**
  (chez Hetzner : « CX22 », ~5 €/mois — largement suffisant pour démarrer).
- Système : **Ubuntu 24.04**.
- À la création, notez **l'adresse IP** du serveur et le mot de passe root
  (ou la clé SSH proposée).

## 2. Acheter le nom de domaine et le pointer

Chez OVH, Gandi ou Ionos, achetez votre domaine (ex. `trustrate.fr`), puis
dans la zone DNS ajoutez :

| Type | Nom | Valeur |
|------|-----|--------|
| A    | @   | l'IP du serveur |
| A    | www | l'IP du serveur |

La propagation prend de 5 minutes à quelques heures. Vérification : sur
votre PC, `ping votredomaine.fr` doit répondre avec l'IP du serveur.

## 3. Se connecter au serveur et installer Docker

Sous Windows, ouvrez PowerShell :

```powershell
ssh root@IP_DU_SERVEUR
```

Puis, une fois connecté (une seule longue commande) :

```bash
curl -fsSL https://get.docker.com | sh
```

## 4. Installer TrustRate

Toujours connecté au serveur :

```bash
mkdir -p /opt/trustrate/backups && cd /opt/trustrate
curl -fsSL https://raw.githubusercontent.com/Coupain86/FL_Compar/main/docker-compose.prod.yml -o docker-compose.prod.yml
curl -fsSL https://raw.githubusercontent.com/Coupain86/FL_Compar/main/.env.example -o .env
nano .env
```

Dans l'éditeur qui s'ouvre, remplissez **toutes** les lignes :
votre domaine, deux mots de passe **longs et différents** (générez-les avec
`openssl rand -base64 24`), votre identité pour les mentions légales, et
l'hébergeur choisi. Sauvegardez : `Ctrl+O`, `Entrée`, puis `Ctrl+X`.

Puis démarrez :

```bash
docker compose -f docker-compose.prod.yml up -d
```

Premier démarrage : ~3 minutes (téléchargements + certificat HTTPS
automatique). Ensuite :

- Le site : `https://votredomaine.fr` (HTTPS automatique, rien à configurer)
- Le back-office : `https://votredomaine.fr/admin` (identifiants du .env)

Suivre ce qui se passe : `docker compose -f docker-compose.prod.yml logs -f web`
(quitter avec `Ctrl+C`).

## 5. Vérifier avant d'ouvrir au public

- [ ] Le cadenas HTTPS s'affiche dans le navigateur.
- [ ] Parcours complet avec un PDF de test : dépôt → vérification → profil →
      résultat → les 2 rapports se téléchargent.
- [ ] « Supprimer mes données » dans Mon espace efface bien tout.
- [ ] Pages « Mentions légales » et « Confidentialité » (liens en bas de
      page) : votre identité s'affiche, plus aucun `[À COMPLÉTER]`.
- [ ] `/admin` demande bien VOTRE mot de passe.
- [ ] Après vos tests : `/admin` → vérifier que vos dépôts d'essai
      n'encombrent pas les cohortes (ou les supprimer via Mon espace).

## 6. Au quotidien

**Mettre à jour** après un changement du code sur GitHub :

```bash
cd /opt/trustrate
docker compose -f docker-compose.prod.yml up -d --force-recreate web
```

**Sauvegardes** : automatiques chaque nuit dans `/opt/trustrate/backups`
(14 jours conservés). Pour restaurer une sauvegarde :

```bash
docker compose -f docker-compose.prod.yml exec -T db pg_restore -U credit -d credit --clean < backups/NOM_DU_FICHIER.dump
```

**Tout arrêter** : `docker compose -f docker-compose.prod.yml down`
(les données restent). Le serveur redémarre ? Tout repart tout seul
(`restart: unless-stopped`).

## 7. Ce que la production change automatiquement

Quand `APP_ENV=production` (déjà réglé dans le compose) :

- cookies marqués `Secure` (HTTPS uniquement) + HSTS ;
- `/admin` **désactivé** tant que le mot de passe par défaut n'a pas été
  remplacé — impossible de l'oublier ;
- pas d'offres de démonstration (`SEED_ON_START=0`) : la base démarre vide,
  les premiers utilisateurs voient l'analyse qualitative + les rapports
  (prévu pour) ;
- limite de dépôts par adresse IP (12 / 10 min) contre les abus ;
- la base de données et l'application ne sont **pas** exposées à Internet —
  seul le serveur HTTPS (Caddy) l'est.

## 8. Ce qui reste à votre main (non technique)

- **Statut juridique** : pour encaisser un jour des revenus (banques,
  partenaires), il faudra une structure (micro-entreprise suffit pour
  démarrer) — son nom/SIREN va dans le `.env`.
- **Email de contact** : créez une vraie boîte (contact@votredomaine.fr,
  souvent incluse chez le registrar) — elle est affichée sur les pages
  légales et sert aux demandes RGPD.
- **Amorçage des cohortes** : les 50 premières offres d'une région font tout.
  Entourage, forums, groupes locaux — chaque dépôt améliore le produit.
