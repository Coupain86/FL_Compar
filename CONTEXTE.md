# Comparateur PDF — Contexte du projet

## Contrainte absolue
**100% local** — aucun appel réseau, aucune dépendance externe au-delà des bibliothèques Python installées localement.

---

## Fichiers du projet

| Fichier | Rôle |
|---|---|
| `pdf_compare.py` | Moteur de comparaison + génération rapport HTML |
| `pdf_compare_gui.py` | Interface graphique tkinter |
| `pdf_zone_selector.py` | Fenêtre de sélection visuelle des zones à exclure |

Les trois fichiers doivent être dans le même répertoire.

---

## Dépendances Python

```
pip install pymupdf Pillow numpy
```

**Python 3.13 compatible** — scipy a été remplacé par du NumPy pur (incompatibilité DLL scipy/Python 3.13 sur Windows).

---

## Architecture du moteur (`pdf_compare.py`)

### Flux de traitement

```
1. Validation fichiers (fitz)
2. Extraction texte (fitz.get_text) → TextBlock
3. Exclusion de pages par règles mot-clé (indépendante REF / CANDIDAT)
4. Réappariement séquentiel des pages filtrées
5. Pour chaque paire de pages (parallélisé via ProcessPoolExecutor) :
   ├── Phase 1 : comparaison texte (matching + tolérance position)
   └── Phase 2 : image diff (rasterisation → masquage → diff NumPy → clustering → recalage)
6. Détection anomalies communes (Counter)
7. Génération rapport HTML (sections repliables, filtrage JS, screenshots JPEG)
```

### Paramètres clés

| Paramètre | Défaut | Description |
|---|---|---|
| `RENDER_DPI` | 100 | DPI rasterisation Phase 2 |
| `PIXEL_TOLERANCE` | 10 | Tolérance colorimétrique par canal RGB (0-255) |
| `MIN_DIFF_AREA_PX` | 50 | Surface min d'une zone diff (px²) |
| `CLUSTER_PADDING` | 8 | Padding autour des bounding boxes |
| `MERGE_GAP_PX` | 15 | Distance max entre clusters pour fusion |
| `MAX_WORKERS` | nb_cœurs-1 | Processus parallèles |

### Tolérance de position texte
Saisie en **pixels** dans le GUI, convertie en points PDF : `pt = px × 72 / DPI`.

### Recalage visuel (`shift_tolerance`)
Pour chaque zone diff détectée, on teste tous les décalages (dx, dy) dans [-N, +N] pixels et on retient le score **minimum**. Si ce score < `pixel_tolerance`, la zone est ignorée (décalage de rendu, pas une vraie différence). Défaut : N=3.

### Clustering sans scipy
`cluster_diff_mask` utilise une dilatation séparable via `cumsum` NumPy + segmentation par projections ligne/colonne. Très rapide (O(H+W) par pixel).

### Exclusion de pages par mot-clé
Les deux listes (REF et CANDIDAT) sont filtrées **indépendamment** :
- Si un mot-clé est trouvé dans la zone définie sur une page REF → cette page REF est retirée, le CANDIDAT avance normalement
- Idem dans l'autre sens
- Les deux listes filtrées sont ensuite réappariées séquentiellement

**Limitation connue** : si une page REF "déborde" sur deux pages CANDIDAT (tableau long), l'appariement séquentiel reste décalé. → Voir chantier suivant.

### Anomalies communes
Un `Counter` compte les occurrences de chaque anomalie texte (sérialisée). Si une anomalie apparaît sur plus d'une page, elle est regroupée dans une section "Différences communes" en haut du rapport.

---

## Interface GUI (`pdf_compare_gui.py`)

### Fonctionnalités
- Sélection REF / CANDIDAT / rapport avec auto-nommage `ref vs candidat.html` dans le dossier REF
- Affichage du nombre de pages de chaque PDF (thread daemon)
- Paramètres avancés avec tooltips (DPI, tolérances, processus, shift_tolerance)
- Case "Limiter à 100 pages" pour test rapide
- Barre de progression + journal (log sombre type terminal)
- Arrêt propre via `threading.Event` → `ProcessPoolExecutor.cancel_futures`
- Préférences persistantes dans `~/.pdf_compare_prefs.json`
- Bouton "🗺 Zones à exclure" → ouvre `ZoneSelectorWindow`
- Compteur de zones actives affiché dans la barre de boutons
- `multiprocessing.freeze_support()` pour compatibilité Windows

### Timings dans le rapport
Mécanisme par placeholders (`__RAPPORT_TIME__`, `__TOTAL_TIME__`) : le rapport est généré une fois avec les placeholders, puis `_patch_timings` les remplace après mesure.

---

## Sélecteur de zones (`pdf_zone_selector.py`)

### Fonctionnalités
- Visionneuse PDF scrollable (REF ou CANDIDAT, zoom 75/100/150/200%)
- Navigation page par page + aller-à numéro
- **Mode Zone d'exclusion** (rouge) : rectangle appliqué sur toutes les pages, exclut la zone de Phase 1 et Phase 2
- **Mode Exclusion de pages** (orange) : rectangle + mots-clés → page retirée si match
- Édition des mots-clés d'une règle existante (clic sur règle → champ prérempli → modifier → Appliquer)
- Clipping des rectangles aux dimensions de la page (impossible de déborder)
- Zones persistées dans les préférences GUI

---

## Rapport HTML

### Structure
- Bloc méta : chemins, pages comparées, timings (analyse / génération / total)
- Bandeau avertissement si nombre de pages différent
- Filtrage à la volée (texte + zones)
- Résumé compteurs (déplacés / manquants / nouveaux / diffs visuelles)
- Section "Différences communes" repliable
- Sections par page repliables avec chevron + compteur d'anomalies
- Boutons "Tout déplier / Tout replier"

### Screenshots
- Format **JPEG qualité 80** (au lieu de PNG) → ~10× plus léger
- `loading="lazy"` sur toutes les `<img>` → fluidité sur gros rapports
- `--no-screenshots` / case GUI pour désactiver complètement

### Problème connu : génération lente sur gros jeux
Sur 3000 pages avec beaucoup d'anomalies texte (ex: 16 000 blocs manquants + 19 000 nouveaux), `rect_to_b64` est appelé ~35 000 fois lors de la génération du rapport → 43 minutes observées.
**À traiter** : désactiver les screenshots texte au-delà d'un seuil, ou paralléliser la génération.

---

## Chantiers identifiés pour la suite

### 1. ~~Alignement élastique (priorité haute)~~ ✅ IMPLÉMENTÉ

**Problème résolu** : si une page REF "déborde" sur deux pages CANDIDAT (tableau long, image), **ou** qu'une page CANDIDAT déborde sur deux pages REF (sens inverse), l'appariement séquentiel était décalé sur tout le reste du document.

**Solution implémentée** : DTW (Dynamic Time Warping) bidirectionnel sur la similarité textuelle (coefficient Dice sur sacs de mots).

**Transitions autorisées** à chaque pas :
- `(i+1, j+1)` : correspondance 1-1 normale
- `(i+1, j)` : REF avance, CAND reste → page CAND déborde sur 2 pages REF
- `(i, j+1)` : CAND avance, REF reste → page REF déborde sur 2 pages CAND

**Paramètres** :
- `elastic_band` (défaut 50) : rayon de la bande diagonale — O(N × band) au lieu de O(N × M)
- `sim_threshold` (0.05) : pages quasi-vides traitées comme totalement différentes

**Activation** :
- GUI : case "Appariement élastique (DTW)" (tooltip détaillé)
- CLI : `--elastic` + `--elastic-band N`
- API Python : `elastic_align=True, elastic_band=50`

**Désactivé par défaut** pour rester rétrocompatible.

**Nouvelle fonction** : `_elastic_align(ref_pages, new_pages, band_radius, sim_threshold)` → `List[Tuple[ref_page_num, new_page_num]]`

### 2. Génération rapport lente
Sur gros jeux de données avec beaucoup d'anomalies texte, `rect_to_b64` (appel fitz) est appelé pour chaque anomalie lors de la génération HTML.
**Solution** : désactiver automatiquement les screenshots texte au-delà d'un seuil configurable (ex: > 500 anomalies par page ou > 10 000 au total).

### 3. Profiling détaillé
Le mode `--profile N` a été simplifié lors de la refonte parallèle et n'affiche plus le détail par sous-étape (Phase 1, rasterisation, masquage, diff, clustering, crops). À réintégrer.

---

## CLI

```bash
python pdf_compare.py ref.pdf candidat.pdf
python pdf_compare.py ref.pdf candidat.pdf -o rapport.html --dpi 150
python pdf_compare.py ref.pdf candidat.pdf --skip-ref-start 1 --skip-new-start 4
python pdf_compare.py ref.pdf candidat.pdf --no-screenshots --workers 4
python pdf_compare.py ref.pdf candidat.pdf --shift-tolerance 5
python pdf_compare.py ref.pdf candidat.pdf --profile 10
python pdf_compare.py --help
```

## Lancement GUI

```bash
python pdf_compare_gui.py
```
