"""Prépare le logo EffiSmart « Jauge » à partir de la planche source (docs/visuel/logo-source.png).

La planche contient : le logo pour fond clair (haut), le logo pour fond sombre (bas gauche)
et l'icône d'application (carré vert foncé, bas droite).

Sorties (backend/assets/) :
- logo-sombre.png : logo pour fond clair, détouré, en haute définition ;
- logo-clair.png  : logo pour fond sombre — même tracé haute définition, recoloré avec les couleurs
                    de la version sombre de la planche (texte #F1F5F2, arc vert #7AD48A) ;
- icone.png       : icône d'application détourée (coins arrondis transparents), pour l'onglet.

Détourage : chaque pixel est décomposé en « fond + encre » ; son opacité est la part d'encre.
Usage (depuis la racine du projet) : .venv\\Scripts\\python.exe docs\\visuel\\preparer_logo.py
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs/visuel/logo-source.png"
ASSETS = ROOT / "backend/assets"

# Zones mesurées sur la planche (x0, y0, x1, y1), avec une petite marge.
LOGO_BOX = (237, 64, 902, 172)
ICON_BOX = (686, 376, 818, 508)

BG = (251, 250, 248)  # fond crème de la planche
INK = (20, 50, 44)  # vert très foncé (texte, partie foncée de la jauge)
GREEN = (63, 174, 85)  # arc vert
DARK_TEXT = (241, 245, 242)  # version sombre : texte et partie claire de la jauge
DARK_GREEN = (122, 212, 138)  # version sombre : arc vert


def dist(a, b) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def cutout(im: Image.Image, ink_color, green_color) -> Image.Image:
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    src, dst = im.load(), out.load()
    d_ink, d_green = dist(BG, INK), dist(BG, GREEN)
    for y in range(im.height):
        for x in range(im.width):
            p = src[x, y]
            greenish = p[1] > p[0] + 25 and p[1] > p[2] + 15
            alpha = max(0.0, min(1.0, dist(BG, p) / (d_green if greenish else d_ink)))
            if alpha < 0.03:
                continue
            dst[x, y] = (*(green_color if greenish else ink_color), round(alpha * 255))
    return out


def icon_cutout(im: Image.Image) -> Image.Image:
    """Rend transparents les coins blancs autour du carré arrondi de l'icône.

    Seul le blanc relié au bord de l'image est traité (remplissage depuis les coins) :
    la partie claire de la jauge, à l'intérieur du carré, est conservée.
    """
    out = im.convert("RGBA")
    px = out.load()
    w, h = out.size

    def whiteness(x, y):
        r, g, b, _ = px[x, y]
        return min(r, g, b) if max(r, g, b) - min(r, g, b) < 20 else 0

    seen, stack = set(), [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    while stack:
        x, y = stack.pop()
        if (x, y) in seen or not (0 <= x < w and 0 <= y < h) or whiteness(x, y) <= 150:
            continue
        seen.add((x, y))
        stack.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    for x, y in seen:
        # Blanc pur → transparent ; pixels d'anticrénelage du bord → opacité partielle.
        alpha = round(255 * (255 - whiteness(x, y)) / 105)
        px[x, y] = (20, 50, 44, max(0, min(255, alpha)))
    return out


def main() -> None:
    sheet = Image.open(SOURCE).convert("RGB")
    logo = sheet.crop(LOGO_BOX)
    ASSETS.mkdir(parents=True, exist_ok=True)
    cutout(logo, INK, GREEN).save(ASSETS / "logo-sombre.png", optimize=True)
    cutout(logo, DARK_TEXT, DARK_GREEN).save(ASSETS / "logo-clair.png", optimize=True)
    icon = icon_cutout(sheet.crop(ICON_BOX))
    icon.resize((128, 128), Image.LANCZOS).save(ASSETS / "icone.png", optimize=True)
    print("Logo préparé.")


if __name__ == "__main__":
    main()
