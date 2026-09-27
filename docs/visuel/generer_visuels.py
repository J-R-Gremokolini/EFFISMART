"""Génère les visuels « Registre silencieux » (skill canvas-design) dans la palette « Verre ».

Sorties :
- docs/visuel/registre-silencieux.png / .pdf  — affiche 30 × 42 cm, 300 dpi ;
- backend/assets/connexion.png               — panneau de l'écran de connexion ;
- backend/assets/calme.png                   — illustration des états vides (aucune dérive).

Les visuels sont des « carpet plots » réels : 48 demi-heures × N jours de courbe de charge
issus du MockDataProvider d'EffiSmart. Philosophie : philosophie-registre-silencieux.md.

Usage (depuis la racine du projet) :
    .venv\\Scripts\\python.exe docs\\visuel\\generer_visuels.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.local import configure_local_environment  # noqa: E402

configure_local_environment()

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.models import Fluid  # noqa: E402
from app.providers.mock import DEFAULT_ANOMALIES, AnomalyKind, MockDataProvider  # noqa: E402
from app.timeutils import LOCAL_TZ, yesterday_local  # noqa: E402

FONTS = ROOT / ".claude/skills/canvas-design/canvas-fonts"
REF = "30001000000003"  # Clinique du Parc — groupe froid resté allumé certains week-ends

# --- Palette « Verre » : une échelle (ardoise → vert → blanc), une exception (bleu ciel) ---
BG = (11, 17, 32)
INK_DIM = (138, 152, 176)
INK = (226, 232, 240)
EXCEPTION = (56, 189, 248)
RAMP = [(17, 25, 43), (22, 51, 60), (21, 103, 70), (34, 197, 94), (167, 243, 196), (240, 253, 244)]


def ramp(t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t)) ** 0.85
    pos = t * (len(RAMP) - 1)
    i = min(int(pos), len(RAMP) - 2)
    f = pos - i
    a, b = RAMP[i], RAMP[i + 1]
    return tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"{name}.ttf"), size)


def load_grid(start: date, days: int, anomalies: bool = True):
    """Puissances moyennes [jour][demi-heure] (changements d'heure normalisés à 48 pas)."""
    provider = MockDataProvider(fluid=Fluid.ELEC, anomalies=None if anomalies else [])
    grid = [[0.0] * 48 for _ in range(days)]
    counts = [[0] * 48 for _ in range(days)]
    for m in provider.fetch_load_curve(REF, start, start + timedelta(days=days - 1)):
        local = m.time.astimezone(LOCAL_TZ)
        d, s = (local.date() - start).days, local.hour * 2 + local.minute // 30
        grid[d][s] += m.avg_power_kw
        counts[d][s] += 1
    for d in range(days):
        for s in range(48):
            grid[d][s] = grid[d][s] / counts[d][s] if counts[d][s] else grid[d][s - 1]
    spec = next(a for a in DEFAULT_ANOMALIES if a.external_ref == REF and a.kind == AnomalyKind.WEEKEND_ON)
    flagged = set()
    if anomalies:
        flagged = {d for d in range(days)
                   if (start + timedelta(days=d)).weekday() >= 5 and spec.applies_on(start + timedelta(days=d))}
    values = sorted(v for row in grid for v in row)
    lo, hi = values[int(len(values) * 0.01)], values[int(len(values) * 0.995)]
    return grid, flagged, lo, hi, len(values)


def draw_carpet(dr: ImageDraw.ImageDraw, grid, flagged, lo, hi, x0, y0, col_w, row_h, inset=(1, 7)):
    """17 520 marques identiques : trait vertical fin inscrit dans sa cellule."""
    ix, iy = inset
    for d, row in enumerate(grid):
        x = x0 + d * col_w
        for s, v in enumerate(row):
            y = y0 + s * row_h
            t = (v - lo) / (hi - lo)
            if d in flagged:
                k = 0.35 + 0.65 * max(0.0, min(1.0, t)) ** 0.8
                color = tuple(round(BG[i] + (EXCEPTION[i] - BG[i]) * k) for i in range(3))
                dr.rectangle([x + ix + 1, y + iy + 1, x + col_w - ix - 2, y + row_h - iy - 2], fill=color)
            else:
                dr.rectangle([x + ix, y + iy, x + col_w - ix - 1, y + row_h - iy - 1], fill=ramp(t))


def poster(out_dir: Path) -> None:
    end = yesterday_local()
    start = end - timedelta(days=364)
    grid, flagged, lo, hi, n = load_grid(start, 365)

    W, H, M = 3600, 5091, 300
    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)
    col_w, row_h = 8, 60
    cx0 = (W - 365 * col_w) // 2 + 60
    cy0 = 1220
    carpet_w, carpet_h = 365 * col_w, 48 * row_h
    draw_carpet(dr, grid, flagged, lo, hi, cx0, cy0, col_w, row_h)

    mono, small = font("DMMono-Regular", 34), font("DMMono-Regular", 36)
    for s, label in ((0, "00"), (12, "06"), (24, "12"), (36, "18"), (47, "24")):
        y = cy0 + s * row_h + (row_h if s == 47 else 0)
        dr.line([cx0 - 70, y, cx0 - 30, y], fill=INK_DIM, width=2)
        dr.text((cx0 - 90 - dr.textlength(label, font=mono), y - 20), label, font=mono, fill=INK_DIM)
    mois = "JFMAMJJASOND"
    yb = cy0 + carpet_h + 40
    for d in range(365):
        day = start + timedelta(days=d)
        if day.day == 1:
            x = cx0 + d * col_w
            dr.line([x, yb, x, yb + 36], fill=INK_DIM, width=2)
            dr.text((x + 12, yb + 2), mois[day.month - 1], font=mono, fill=INK_DIM)

    title = font("Jura-Light", 190)
    dr.text((cx0, M + 20), "REGISTRE", font=title, fill=INK)
    dr.text((cx0, M + 250), "SILENCIEUX", font=title, fill=INK)
    meta = f"PRM {REF}   ·   48 × 365   ·   {n:,} relevés".replace(",", " ")
    dr.text((cx0, M + 540), meta, font=small, fill=INK_DIM)
    tr = f"{start:%d.%m.%Y} — {end:%d.%m.%Y}   ·   J+1"
    dr.text((cx0 + carpet_w - dr.textlength(tr, font=small), M + 540), tr, font=small, fill=INK_DIM)
    dr.line([cx0, M + 620, cx0 + carpet_w, M + 620], fill=(38, 50, 72), width=2)

    ly = yb + 190
    for i in range(600):
        dr.line([cx0 + i, ly, cx0 + i, ly + 22], fill=ramp(i / 599))
    dr.text((cx0, ly + 44), f"{lo:.0f} kW", font=small, fill=INK_DIM)
    hl = f"{hi:.0f} kW"
    dr.text((cx0 + 600 - dr.textlength(hl, font=small), ly + 44), hl, font=small, fill=INK_DIM)
    ax = cx0 + 760
    dr.rectangle([ax, ly, ax + 22, ly + 22], fill=EXCEPTION)
    dr.text((ax + 44, ly - 8), "week-end qui ne dort pas", font=small, fill=INK_DIM)

    serif = font("InstrumentSerif-Italic", 64)
    sig = "cartographie d'un talon"
    dr.text((cx0 + carpet_w - dr.textlength(sig, font=serif), H - M - 40), sig, font=serif, fill=INK)
    dr.text((cx0, H - M - 18), "EFFISMART", font=font("Jura-Medium", 40), fill=INK_DIM)

    out_dir.mkdir(parents=True, exist_ok=True)
    img.save(out_dir / "registre-silencieux.png", dpi=(300, 300))
    img.save(out_dir / "registre-silencieux.pdf", resolution=300)


def login_panel(out: Path) -> None:
    """Panneau vertical : 16 semaines de tapis, sans texte (tout le texte est dans l'interface)."""
    end = yesterday_local()
    start = end - timedelta(days=16 * 7 - 1)
    grid, flagged, lo, hi, _ = load_grid(start, 16 * 7)
    W, H = 1200, 1500
    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)
    col_w, row_h = 9, 26
    x0 = (W - len(grid) * col_w) // 2
    y0 = (H - 48 * row_h) // 2
    draw_carpet(dr, grid, flagged, lo, hi, x0, y0, col_w, row_h, inset=(1, 3))
    img.save(out, optimize=True)


def calm_tile(out: Path) -> None:
    """Une semaine ordinaire, sans exception : l'image du « rien à signaler »."""
    end = yesterday_local()
    start = end - timedelta(days=end.weekday() + 7)  # dernière semaine complète, du lundi au dimanche
    grid, flagged, lo, hi, _ = load_grid(start, 7, anomalies=False)
    W, H = 960, 360
    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)
    col_w, row_h = 96, 6
    x0 = (W - 7 * col_w) // 2
    y0 = (H - 48 * row_h) // 2
    draw_carpet(dr, grid, flagged, lo, hi, x0, y0, col_w, row_h, inset=(8, 1))
    img.save(out, optimize=True)


if __name__ == "__main__":
    poster(ROOT / "docs/visuel")
    assets = ROOT / "backend/assets"
    assets.mkdir(parents=True, exist_ok=True)
    login_panel(assets / "connexion.png")
    calm_tile(assets / "calme.png")
    print("Visuels générés.")
