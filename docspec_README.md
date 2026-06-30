# Toolkit docspec & extras

Outils ajoutés autour du comparateur PDF. **100 % local** (sauf `ai_vision.py`, qui
est le seul à faire un appel réseau et reste optionnel).

| Fichier | Rôle | Réseau ? |
|---|---|---|
| `docspec.py` | Décompose une image/PDF en **spécification rejouable** (base + résidu + structure) et la régénère à fidélité mesurée | Non |
| `docspec_bench.py` | Banc d'essai : encode/régénère tout un dossier et produit un **rapport de preuve HTML** | Non |
| `generate_test_pdfs.py` | Génère des paires de PDF de test (cas tordus + perf) | Non |
| `ai_vision.py` | Compare visuellement 2 pages via Claude (IA) | **Oui** |

---

## docspec.py — l'outil principal

### Idée
Prend **tout format** (PDF, JPG, PNG…) et produit une spécification complète qui
permet de **régénérer l'image à l'identique** (exact, ou ≥ 99 % réglable). Architecture :

1. **Base** : codec image compact (WebP) — couvre tout, photos comprises.
2. **Résidu** : correction quantifiée — *garantit* la fidélité (step=1 → exact).
3. **Structure** (descriptif = la « spécification ») : fond, palette,
   **régions vectorielles** (rectangles), **formes polygonales** (contours tracés),
   **texte OCR** (mot, position, taille de police, couleur).

### Dépendances
```
pip install pymupdf Pillow numpy
pip install pytesseract        # OCR optionnel (+ installer Tesseract)
```

### Commandes
```
py docspec.py roundtrip image.jpg            # encode + régénère + rapport de fidélité
py docspec.py roundtrip doc.pdf --lossless   # reconstruction EXACTE (100 %)
py docspec.py encode  fichier -o spec.imgspec [--quality 80] [--target 0.99] [--dpi 150]
py docspec.py decode  spec.imgspec           # régénère le PNG fidèle
py docspec.py svg     spec.imgspec           # SVG hybride : image fidèle + texte/formes éditables
py docspec.py render  spec.imgspec           # rendu depuis la structure seule (voie vectorielle)
```

### Le conteneur `.imgspec`
Un zip contenant `manifest.json` (la spécification lisible) + les assets par page
(`page_XXX_base.webp`, `page_XXX_residual.npz` si présent).

### Le SVG hybride
- une `<image>` raster fidèle au fond (garantit l'identique) ;
- une couche **texte** OCR sélectionnable/éditable (comme un PDF cherchable) ;
- des couches **régions** et **formes polygonales** éditables (masquées par défaut).
Ouvrable dans n'importe quel navigateur.

### Résultats mesurés (corpus de test)
- Mode exact : **SSIM 1.0 / PSNR ∞** (reconstruction parfaite) sur 21/21 fichiers.
- Mode perceptuel (cible 0.99) : **SSIM moyen 0,9994**, fichiers souvent plus petits que la source.
- Rendu « structure seule » (vecteur+texte, sans résidu) : ~0,95–0,98 → c'est le
  régime *approximatif éditable* (la fidélité exacte vient de la base+résidu).

---

## docspec_bench.py — preuve à l'échelle
```
py docspec_bench.py                 # corpus par défaut : test_pdfs/
py docspec_bench.py dossier -o rapport.html --target 0.99 --dpi 120
```
Produit un HTML : pour chaque fichier, **original | régénéré | diff ×10** + tableau
SSIM/PSNR/taille, et un résumé global.

---

## generate_test_pdfs.py — jeux de test
```
py generate_test_pdfs.py [--out test_pdfs] [--perf-pages 300]
```
Crée 10 paires REF/CANDIDAT (texte déplacé, manquant/ajouté, modifié, diff visuelle,
pages décalées, caractères spéciaux, tailles de page, filigrane, identique, tout cassé)
+ une paire de performance.

---

## ai_vision.py — comparaison par IA (optionnel, réseau)
```
pip install anthropic
setx ANTHROPIC_API_KEY "sk-ant-..."     # Windows ; rouvrir l'invite ensuite
py ai_vision.py ref.pdf candidat.pdf
```
Envoie les pages à Claude et renvoie une comparaison en français (texte, éléments
graphiques, mise en page). **Brise la contrainte 100 % local** — à n'utiliser qu'à dessein.

---

## Intégration dans l'appli
L'onglet **Configuration** a un bouton **« 🧬 Exporter spécification (docspec) »** :
il encode le PDF/image Référence en `.imgspec` + SVG hybride, et journalise la fidélité.

## Limites connues / pistes
- Pas de **tracé Bézier** courbe (seulement polygones) ni d'OCR de **police exacte** :
  dépendances externes lourdes (potrace), et le résidu rend déjà le résultat fidèle.
- Sur du **bruit pur incompressible**, aucun codec ne réduit la taille — la fidélité
  reste néanmoins garantie.
