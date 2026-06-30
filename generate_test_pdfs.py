"""
generate_test_pdfs.py — Génère des jeux de PDF de test pour le Comparateur PDF.

Crée, dans un dossier de sortie (défaut : ./test_pdfs), des PAIRES de fichiers :
    NN_nom_REF.pdf        ← le document de référence
    NN_nom_CANDIDAT.pdf   ← la version à comparer (avec des différences VOLONTAIRES)

Chaque paire cible un cas tordu différent (texte déplacé, manquant, ajouté,
modifié, diff visuelle, décalage de pages, caractères spéciaux, tailles de page
différentes, filigrane à exclure, page identique de contrôle, et un cas
« tout cassé »). Une paire de PERFORMANCE multi-pages est aussi générée.

Dépendances : pymupdf  (pip install pymupdf)

Usage :
    py generate_test_pdfs.py
    py generate_test_pdfs.py --out mon_dossier --perf-pages 500
"""

import argparse
import os
import fitz  # PyMuPDF

# Dimensions A4 portrait en points PDF
A4_W, A4_H = 595.0, 842.0
BLACK = (0, 0, 0)


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def new_doc():
    return fitz.open()


def add_page(doc, width=A4_W, height=A4_H):
    return doc.new_page(width=width, height=height)


def text(page, x, y, s, size=11, color=BLACK, font="helv"):
    page.insert_text((x, y), s, fontsize=size, color=color, fontname=font)


def title(page, s):
    text(page, 50, 60, s, size=18)
    page.draw_line(fitz.Point(50, 70), fitz.Point(A4_W - 50, 70), color=BLACK, width=1)


def rect(page, x0, y0, x1, y1, color=None, fill=None, width=1):
    page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=color, fill=fill, width=width)


def fake_barcode(page, x, y, seed=0, w=160, h=40):
    """Dessine une suite de barres verticales = pseudo code-barres (élément graphique)."""
    import random
    rng = random.Random(seed)
    cx = x
    while cx < x + w:
        bw = rng.choice([1, 1, 2, 3])
        if rng.random() > 0.4:
            rect(page, cx, y, cx + bw, y + h, fill=BLACK, color=BLACK, width=0)
        cx += bw + 1


def save(doc, out_dir, name):
    path = os.path.join(out_dir, name)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


# ─────────────────────────────────────────────
#  01 — Texte DÉPLACÉ (mêmes blocs, positions différentes)
# ─────────────────────────────────────────────
def case_01_moved(out):
    lines = ["Ligne Alpha — inchangee",
             "Ligne Bravo — sera deplacee",
             "Ligne Charlie — sera deplacee aussi",
             "Ligne Delta — inchangee"]
    ref = new_doc(); p = add_page(ref); title(p, "01 - Texte deplace (REF)")
    for i, s in enumerate(lines):
        text(p, 60, 120 + i * 40, s)
    save(ref, out, "01_texte_deplace_REF.pdf")

    cand = new_doc(); p = add_page(cand); title(p, "01 - Texte deplace (CANDIDAT)")
    text(p, 60, 120, lines[0])             # Alpha : identique
    text(p, 260, 300, lines[1])            # Bravo : deplace loin
    text(p, 60, 500, lines[2])             # Charlie : descendu
    text(p, 60, 240, lines[3])             # Delta : remonte
    save(cand, out, "01_texte_deplace_CANDIDAT.pdf")


# ─────────────────────────────────────────────
#  02 — Texte MANQUANT / AJOUTÉ
# ─────────────────────────────────────────────
def case_02_missing_added(out):
    ref = new_doc(); p = add_page(ref); title(p, "02 - Manquant/Ajoute (REF)")
    for i, s in enumerate(["Article 1 : conditions generales",
                           "Article 2 : SERA SUPPRIME dans le candidat",
                           "Article 3 : responsabilites",
                           "Article 4 : SERA AUSSI SUPPRIME"]):
        text(p, 60, 120 + i * 40, s)
    save(ref, out, "02_manquant_ajoute_REF.pdf")

    cand = new_doc(); p = add_page(cand); title(p, "02 - Manquant/Ajoute (CANDIDAT)")
    text(p, 60, 120, "Article 1 : conditions generales")
    text(p, 60, 160, "Article 3 : responsabilites")
    text(p, 60, 200, "Article 5 : CLAUSE TOTALEMENT NOUVELLE")
    text(p, 60, 240, "Article 6 : AUTRE AJOUT")
    save(cand, out, "02_manquant_ajoute_CANDIDAT.pdf")


# ─────────────────────────────────────────────
#  03 — Texte MODIFIÉ (1 caractère) → montre la limite "missing+added"
# ─────────────────────────────────────────────
def case_03_modified(out):
    ref = new_doc(); p = add_page(ref); title(p, "03 - Modifie (REF)")
    text(p, 60, 120, "Montant total : 1000 EUR")
    text(p, 60, 160, "Reference dossier : ABC-2024-001")
    text(p, 60, 200, "Statut : VALIDE")
    save(ref, out, "03_modifie_REF.pdf")

    cand = new_doc(); p = add_page(cand); title(p, "03 - Modifie (CANDIDAT)")
    text(p, 60, 120, "Montant total : 1500 EUR")        # 1000 -> 1500
    text(p, 60, 160, "Reference dossier : ABC-2024-002")  # 001 -> 002
    text(p, 60, 200, "Statut : REFUSE")                  # VALIDE -> REFUSE
    save(cand, out, "03_modifie_CANDIDAT.pdf")


# ─────────────────────────────────────────────
#  04 — DIFF VISUELLE (formes, couleurs, code-barres, image)
# ─────────────────────────────────────────────
def case_04_visual(out):
    def base(page, tag):
        title(page, f"04 - Diff visuelle ({tag})")
        text(page, 60, 120, "Le texte ci-dessous est IDENTIQUE des deux cotes.")
        text(page, 60, 150, "Seuls les elements graphiques changent.")

    ref = new_doc(); p = add_page(ref); base(p, "REF")
    rect(p, 60, 200, 200, 300, fill=(0.2, 0.4, 0.9), color=BLACK, width=1)   # bleu
    rect(p, 250, 200, 390, 300, color=(0.9, 0.1, 0.1), width=3)             # contour rouge
    fake_barcode(p, 60, 350, seed=1)
    save(ref, out, "04_diff_visuelle_REF.pdf")

    cand = new_doc(); p = add_page(cand); base(p, "CANDIDAT")
    rect(p, 60, 200, 200, 300, fill=(0.2, 0.9, 0.3), color=BLACK, width=1)   # vert (couleur changee)
    rect(p, 250, 200, 420, 320, color=(0.9, 0.1, 0.1), width=3)             # taille changee
    fake_barcode(p, 60, 350, seed=999)                                       # code-barres different
    save(cand, out, "04_diff_visuelle_CANDIDAT.pdf")


# ─────────────────────────────────────────────
#  05 — PAGES DÉCALÉES (page inseree) → teste l'appariement elastique
# ─────────────────────────────────────────────
def case_05_page_shift(out):
    contenu = ["PAGE A : introduction du document",
               "PAGE B : developpement principal",
               "PAGE C : conclusion et annexes"]
    ref = new_doc()
    for i, c in enumerate(contenu):
        p = add_page(ref); title(p, f"05 - Page {i+1} (REF)")
        text(p, 60, 130, c)
        for k in range(8):
            text(p, 60, 180 + k * 30, f"Paragraphe commun numero {k+1} de la {c[:6]}")
    save(ref, out, "05_pages_decalees_REF.pdf")

    cand = new_doc()
    # Page intercalaire inseree en 2e position -> tout est decale d'1 page
    p = add_page(cand); title(p, "05 - Page 1 (CANDIDAT)")
    text(p, 60, 130, contenu[0])
    for k in range(8):
        text(p, 60, 180 + k * 30, f"Paragraphe commun numero {k+1} de la PAGE A")
    p = add_page(cand); title(p, "05 - PAGE INSEREE (CANDIDAT)")
    text(p, 60, 130, "Cette page n'existe pas dans la reference !")
    for i, c in enumerate(contenu[1:], start=2):
        p = add_page(cand); title(p, f"05 - Page {i+1} (CANDIDAT)")
        text(p, 60, 130, c)
        for k in range(8):
            text(p, 60, 180 + k * 30, f"Paragraphe commun numero {k+1} de la {c[:6]}")
    save(cand, out, "05_pages_decalees_CANDIDAT.pdf")


# ─────────────────────────────────────────────
#  06 — CARACTÈRES SPÉCIAUX (< > & " accents) → teste l'echappement HTML du rapport
# ─────────────────────────────────────────────
def case_06_special_chars(out):
    ref = new_doc(); p = add_page(ref); title(p, "06 - Caracteres speciaux (REF)")
    text(p, 60, 120, 'Condition : a < b & c > d')
    text(p, 60, 160, 'Balise <script>alert(1)</script> dans le texte')
    text(p, 60, 200, 'Citation : "guillemets" et accents : eee aa cc')
    text(p, 60, 240, "Formule H2O & CO2 <= seuil")
    save(ref, out, "06_caracteres_speciaux_REF.pdf")

    cand = new_doc(); p = add_page(cand); title(p, "06 - Caracteres speciaux (CANDIDAT)")
    text(p, 60, 120, 'Condition : a > b & c < d')               # inverse
    text(p, 60, 160, 'Balise <b>gras</b> et <i>italique</i>')   # change
    text(p, 60, 200, 'Citation : "autres" et accents : ooo uu')
    text(p, 60, 240, "Formule H2SO4 & NaCl >= seuil")
    save(cand, out, "06_caracteres_speciaux_CANDIDAT.pdf")


# ─────────────────────────────────────────────
#  07 — TAILLES DE PAGE DIFFÉRENTES (portrait vs paysage)
# ─────────────────────────────────────────────
def case_07_page_size(out):
    ref = new_doc(); p = add_page(ref, A4_W, A4_H); title(p, "07 - Taille page (REF portrait)")
    text(p, 60, 130, "Contenu identique mais format de page different.")
    rect(p, 60, 200, 300, 400, color=BLACK, width=2)
    save(ref, out, "07_taille_page_REF.pdf")

    # Candidat en paysage (largeur/hauteur inversees)
    cand = new_doc(); p = add_page(cand, A4_H, A4_W); title(p, "07 - Taille page (CANDIDAT paysage)")
    text(p, 60, 130, "Contenu identique mais format de page different.")
    rect(p, 60, 200, 300, 400, color=BLACK, width=2)
    save(cand, out, "07_taille_page_CANDIDAT.pdf")


# ─────────────────────────────────────────────
#  08 — FILIGRANE BROUILLON → teste les règles d'exclusion de pages (mot-clé)
# ─────────────────────────────────────────────
def case_08_watermark(out):
    ref = new_doc()
    for i in range(3):
        p = add_page(ref); title(p, f"08 - Page {i+1} (REF)")
        text(p, 60, 130, f"Contenu officiel de la page {i+1}.")
    save(ref, out, "08_filigrane_REF.pdf")

    cand = new_doc()
    for i in range(3):
        p = add_page(cand); title(p, f"08 - Page {i+1} (CANDIDAT)")
        text(p, 60, 130, f"Contenu officiel de la page {i+1}.")
        if i == 1:  # page 2 marquee BROUILLON
            text(p, 180, 430, "BROUILLON", size=60, color=(0.85, 0.85, 0.85))
    save(cand, out, "08_filigrane_CANDIDAT.pdf")
    # Astuce : dans l'appli, ajoute une regle d'exclusion de pages avec le
    # mot-cle "BROUILLON" sur la zone centrale -> la page 2 sera ignoree.


# ─────────────────────────────────────────────
#  09 — IDENTIQUE (controle : aucune diff attendue)
# ─────────────────────────────────────────────
def case_09_identical(out):
    def build(tag):
        d = new_doc(); p = add_page(d); title(p, "09 - Document de controle")
        for i in range(12):
            text(p, 60, 120 + i * 30, f"Ligne {i+1} strictement identique des deux cotes.")
        rect(p, 350, 200, 500, 350, color=BLACK, width=2)
        return d
    save(build("REF"), out, "09_identique_REF.pdf")
    save(build("CAND"), out, "09_identique_CANDIDAT.pdf")


# ─────────────────────────────────────────────
#  10 — TOUT CASSÉ (cocktail sur 2 pages)
# ─────────────────────────────────────────────
def case_10_kitchen_sink(out):
    ref = new_doc()
    p = add_page(ref); title(p, "10 - Page 1 (REF)")
    text(p, 60, 120, "Bloc fixe en haut")
    text(p, 60, 160, "Bloc qui sera deplace")
    text(p, 60, 200, "Bloc qui sera supprime")
    text(p, 60, 240, "Total : 9999 EUR")
    rect(p, 350, 150, 520, 300, fill=(0.3, 0.5, 0.8), color=BLACK, width=1)
    p2 = add_page(ref); title(p2, "10 - Page 2 (REF)")
    text(p2, 60, 120, "Texte avec < et > et & speciaux")
    fake_barcode(p2, 60, 200, seed=5)
    save(ref, out, "10_tout_casse_REF.pdf")

    cand = new_doc()
    p = add_page(cand); title(p, "10 - Page 1 (CANDIDAT)")
    text(p, 60, 120, "Bloc fixe en haut")
    text(p, 300, 420, "Bloc qui sera deplace")          # deplace
    text(p, 60, 200, "Bloc totalement nouveau ici")     # ajoute (l'ancien supprime)
    text(p, 60, 240, "Total : 1234 EUR")                # modifie
    rect(p, 350, 150, 520, 300, fill=(0.8, 0.3, 0.3), color=BLACK, width=1)  # couleur changee
    p2 = add_page(cand); title(p2, "10 - Page 2 (CANDIDAT)")
    text(p2, 60, 120, "Texte avec > et < et & speciaux")  # ordre change
    fake_barcode(p2, 60, 200, seed=42)                    # code-barres different
    save(cand, out, "10_tout_casse_CANDIDAT.pdf")


# ─────────────────────────────────────────────
#  PERF — gros document multi-pages avec diffs eparpillees
# ─────────────────────────────────────────────
def case_perf(out, n_pages):
    def page_lines(pnum):
        return [f"P{pnum:04d} L{j:02d} : donnee de reference colonne A={j*7%97} B={j*13%89}"
                for j in range(22)]

    ref = new_doc()
    for i in range(1, n_pages + 1):
        p = add_page(ref); title(p, f"PERF - Page {i}/{n_pages} (REF)")
        for j, s in enumerate(page_lines(i)):
            text(p, 50, 110 + j * 30, s, size=9)
    save(ref, out, f"perf_{n_pages}p_REF.pdf")

    cand = new_doc()
    for i in range(1, n_pages + 1):
        p = add_page(cand); title(p, f"PERF - Page {i}/{n_pages} (CANDIDAT)")
        lines = page_lines(i)
        if i % 10 == 0:                       # ~10% : une valeur modifiee
            lines[5] = lines[5].replace("A=", "A=MODIF ")
        for j, s in enumerate(lines):
            y = 110 + j * 30
            if i % 17 == 0 and j == 8:        # ~6% : un bloc deplace
                text(p, 250, 760, s, size=9)
            else:
                text(p, 50, y, s, size=9)
        if i % 23 == 0:                       # ~4% : une diff visuelle
            rect(p, 400, 700, 540, 780, fill=(0.9, 0.2, 0.2), color=BLACK, width=1)
    save(cand, out, f"perf_{n_pages}p_CANDIDAT.pdf")


# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Genere des PDF de test pour le Comparateur PDF.")
    ap.add_argument("--out", default="test_pdfs", help="Dossier de sortie (defaut: test_pdfs)")
    ap.add_argument("--perf-pages", type=int, default=300,
                    help="Nombre de pages du jeu de performance (defaut: 300)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    print(f"Generation dans : {os.path.abspath(args.out)}\n")
    cases = [
        ("01 texte deplace",        case_01_moved),
        ("02 manquant / ajoute",    case_02_missing_added),
        ("03 texte modifie",        case_03_modified),
        ("04 diff visuelle",        case_04_visual),
        ("05 pages decalees",       case_05_page_shift),
        ("06 caracteres speciaux",  case_06_special_chars),
        ("07 taille de page",       case_07_page_size),
        ("08 filigrane brouillon",  case_08_watermark),
        ("09 identique (controle)", case_09_identical),
        ("10 tout casse",           case_10_kitchen_sink),
    ]
    for label, fn in cases:
        fn(args.out)
        print(f"  OK  {label}")

    print(f"  ..  perf ({args.perf_pages} pages) en cours...")
    case_perf(args.out, args.perf_pages)
    print(f"  OK  perf {args.perf_pages} pages")

    print("\nTermine. Compare chaque paire _REF.pdf / _CANDIDAT.pdf dans l'appli.")
    print("Conseils :")
    print("  - cas 05 : coche 'Appariement elastique (DTW)'")
    print("  - cas 08 : ajoute une regle d'exclusion de pages avec le mot-cle BROUILLON")
    print("  - perf   : lance d'abord avec 'Limiter a 100 pages' pour un test rapide")


if __name__ == "__main__":
    main()
