"""
ai_vision.py — Comparaison visuelle de pages PDF par une IA (Claude).

⚠️ ATTENTION — CONTRAINTE 100 % LOCAL
    Ce module est le SEUL du projet qui fait un APPEL RÉSEAU : il envoie les
    pages (sous forme d'images) à l'API Anthropic. Il est volontairement séparé
    des 3 fichiers du comparateur : si tu ne lances pas ce fichier, l'application
    reste 100 % locale et hors-ligne.

Ce qu'il fait :
    Rasterise une page REF et une page CANDIDAT (via PyMuPDF, comme l'appli),
    les envoie à Claude, et renvoie une comparaison rédigée en français
    (éléments graphiques ajoutés/supprimés/modifiés, texte visiblement
    différent, mise en page, verdict). Complète le diff pixel : utile sur les
    scans, logos, signatures, codes-barres, tableaux — là où le diff bloc à
    bloc se noie dans le bruit.

Dépendances :
    pip install pymupdf Pillow anthropic

Clé API (à faire une fois) :
    L'IA a besoin d'une clé API Anthropic, lue dans la variable d'environnement
    ANTHROPIC_API_KEY. Sous Windows (invite de commandes) :
        setx ANTHROPIC_API_KEY "sk-ant-..."
    puis ferme/rouvre la fenêtre. (Une clé se crée sur console.anthropic.com.)

Usage :
    py ai_vision.py reference.pdf candidat.pdf
    py ai_vision.py reference.pdf candidat.pdf --ref-page 1 --cand-page 1
    py ai_vision.py reference.pdf candidat.pdf --dpi 120
    py ai_vision.py reference.pdf                      (décrit une seule page)
"""

import argparse
import base64
import io
import os
import sys

import fitz  # PyMuPDF
from PIL import Image

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
MODEL          = "claude-opus-4-8"   # modèle Claude le plus capable
RENDER_DPI     = 110                 # DPI de rasterisation envoyé à l'IA
MAX_LONG_SIDE  = 1568                # bride la taille image -> borne le coût en tokens
JPEG_QUALITY   = 85
MAX_TOKENS     = 2048                # longueur max de la réponse de l'IA


# ─────────────────────────────────────────────
#  RASTERISATION  (réutilise la même approche que l'appli)
# ─────────────────────────────────────────────
def page_to_jpeg_b64(pdf_path: str, page_index_0based: int, dpi: int = RENDER_DPI) -> str:
    """Rend une page PDF en JPEG (redimensionné si trop grand) puis l'encode en base64."""
    doc = fitz.open(pdf_path)
    try:
        if not (0 <= page_index_0based < doc.page_count):
            raise IndexError(
                f"Page {page_index_0based + 1} hors limites "
                f"(le document a {doc.page_count} page(s)).")
        scale = dpi / 72.0
        pix = doc[page_index_0based].get_pixmap(
            matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()

    # Brider la résolution : le coût en tokens grimpe avec le nombre de pixels.
    if max(img.size) > MAX_LONG_SIDE:
        img.thumbnail((MAX_LONG_SIDE, MAX_LONG_SIDE), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _image_block(b64: str) -> dict:
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}


# ─────────────────────────────────────────────
#  PROMPTS
# ─────────────────────────────────────────────
SYSTEM_PROMPT = (
    "Tu es un expert en contrôle qualité documentaire. On te montre des pages "
    "rendues de deux versions d'un même document : une RÉFÉRENCE et un CANDIDAT. "
    "Tu réponds toujours en français, de façon concise et factuelle. "
    "Tu ne signales que des différences réellement visibles ; tu ignores le "
    "bruit de rendu (anticrénelage, micro-décalages d'un ou deux pixels)."
)

COMPARE_INSTRUCTIONS = (
    "Compare la page CANDIDAT à la page RÉFÉRENCE et réponds avec ces sections :\n"
    "1. VERDICT : « identiques » ou « différences détectées ».\n"
    "2. TEXTE : différences de contenu textuel visibles (ajouts, suppressions, "
    "valeurs modifiées). Cite les passages concernés.\n"
    "3. ÉLÉMENTS GRAPHIQUES : logos, signatures, tampons, codes-barres, images, "
    "tableaux, cadres — ajoutés, retirés, déplacés ou modifiés.\n"
    "4. MISE EN PAGE : changements de position, de taille ou de couleur.\n"
    "Si une section n'a rien à signaler, écris « RAS »."
)

DESCRIBE_INSTRUCTIONS = (
    "Décris cette page : nature du document, structure de la mise en page, et "
    "les éléments graphiques notables (logos, tampons, signatures, codes-barres, "
    "tableaux, images)."
)


# ─────────────────────────────────────────────
#  APPEL À L'IA
# ─────────────────────────────────────────────
def _make_client():
    """Crée le client Anthropic ou échoue avec un message clair si pas de clé."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "Erreur : aucune clé API trouvée.\n"
            "Définis la variable d'environnement ANTHROPIC_API_KEY puis relance.\n"
            'Sous Windows :  setx ANTHROPIC_API_KEY "sk-ant-..."  '
            "(ferme/rouvre l'invite de commandes ensuite).")
    import anthropic
    return anthropic.Anthropic()


def compare_pages_with_ai(
    ref_pdf: str,
    cand_pdf: str | None = None,
    ref_page: int = 1,
    cand_page: int = 1,
    dpi: int = RENDER_DPI,
    model: str = MODEL,
) -> str:
    """
    Envoie une (ou deux) page(s) à Claude et renvoie son analyse en texte.
    Si cand_pdf est None : décrit simplement la page REF.
    Les numéros de page sont en base 1 (comme dans l'appli).
    """
    client = _make_client()

    ref_b64 = page_to_jpeg_b64(ref_pdf, ref_page - 1, dpi)

    if cand_pdf is None:
        content = [
            {"type": "text", "text": "Page à décrire :"},
            _image_block(ref_b64),
            {"type": "text", "text": DESCRIBE_INSTRUCTIONS},
        ]
    else:
        cand_b64 = page_to_jpeg_b64(cand_pdf, cand_page - 1, dpi)
        content = [
            {"type": "text", "text": f"Page RÉFÉRENCE (page {ref_page}) :"},
            _image_block(ref_b64),
            {"type": "text", "text": f"Page CANDIDAT (page {cand_page}) :"},
            _image_block(cand_b64),
            {"type": "text", "text": COMPARE_INSTRUCTIONS},
        ]

    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    parts = [b.text for b in response.content if b.type == "text"]
    return "\n".join(parts).strip()


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Comparaison visuelle de pages PDF par une IA (Claude). "
                    "ATTENTION : fait un appel réseau (non local).")
    ap.add_argument("reference", help="PDF de référence")
    ap.add_argument("candidat", nargs="?", default=None,
                    help="PDF candidat (optionnel : sans lui, décrit la page REF)")
    ap.add_argument("--ref-page",  type=int, default=1, help="Page REF (défaut : 1)")
    ap.add_argument("--cand-page", type=int, default=1, help="Page CANDIDAT (défaut : 1)")
    ap.add_argument("--dpi",       type=int, default=RENDER_DPI,
                    help=f"DPI de rasterisation (défaut : {RENDER_DPI})")
    ap.add_argument("--model",     default=MODEL, help=f"Modèle (défaut : {MODEL})")
    args = ap.parse_args()

    if not os.path.isfile(args.reference):
        sys.exit(f"Fichier introuvable : {args.reference}")
    if args.candidat and not os.path.isfile(args.candidat):
        sys.exit(f"Fichier introuvable : {args.candidat}")

    if args.candidat:
        print(f"Analyse IA : REF p.{args.ref_page} vs CANDIDAT p.{args.cand_page} "
              f"(modèle {args.model})…\n")
    else:
        print(f"Description IA : {args.reference} p.{args.ref_page} "
              f"(modèle {args.model})…\n")

    result = compare_pages_with_ai(
        ref_pdf=args.reference,
        cand_pdf=args.candidat,
        ref_page=args.ref_page,
        cand_page=args.cand_page,
        dpi=args.dpi,
        model=args.model,
    )
    print("─" * 60)
    print(result)
    print("─" * 60)


if __name__ == "__main__":
    main()
