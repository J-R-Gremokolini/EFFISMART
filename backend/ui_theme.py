"""Thème de l'interface Streamlit : mode clair ou sombre, au choix de chaque utilisateur.

Implémente le design system `.claude/skills/design-system-effismart/SKILL.md`
(élaboré avec la méthode typeui) : tokens CSS, en-têtes de page, cartes KPI avec
icône, tendance et mini-courbe à dégradé, pilules, statuts, barres de progression,
jauge et tableaux ; et, pour le tableau de bord, la photo du site avec ses mesures en
pastilles, la liste des sites, les pastilles de statistiques et l'anneau de répartition.

Mode sombre : thème « Verre » (ou « Nova » avec EFFISMART_THEME=nova). Mode clair : cartes
claires très arrondies sur un cadre blanc, barre latérale ardoise, mêmes couleurs EffiSmart.
Le mode est propre à chaque session (chaque session Streamlit s'exécute dans son propre fil) :
`set_mode` au début de chaque passage, puis les couleurs (`PRIMARY`, `MUTED`…) suivent le mode.

Tout texte dynamique inséré en HTML passe par `html.escape`.
"""
from __future__ import annotations

import base64
import html
import itertools
import math
import os
import threading
from collections.abc import Sequence
from pathlib import Path

import streamlit as st

# Thème sombre choisi au lancement : « glass » (défaut, recommandation UI/UX Pro Max) ou « nova ».
THEME = os.environ.get("EFFISMART_THEME", "glass").strip().lower()
LIGHT, DARK = "clair", "sombre"
MODES = (LIGHT, DARK)
DEFAULT_MODE = LIGHT

# Couleurs des graphiques et des SVG. En mode clair, les séries sont plus soutenues (≥ 3:1 sur fond blanc).
_PALETTES = {
    "nova": {"PRIMARY": "#9568ff", "SERIES_2": "#f59e0b", "MUTED": "#a7a3cb", "BORDER": "#1f1538",
             "SUCCESS_LINE": "#4ade80", "DANGER_LINE": "#fb7185", "TRACK": "#180c32", "TEXT": "#e2e8f0",
             "SELECTED": "#f8fafc"},
    "glass": {"PRIMARY": "#22c55e", "SERIES_2": "#38bdf8", "MUTED": "#94a3b8", "BORDER": "#334155",
              "SUCCESS_LINE": "#4ade80", "DANGER_LINE": "#f87171", "TRACK": "#273349", "TEXT": "#e2e8f0",
              "SELECTED": "#f8fafc"},
    LIGHT: {"PRIMARY": "#16a34a", "SERIES_2": "#0284c7", "MUTED": "#475569", "BORDER": "#e2e8f0",
            "SUCCESS_LINE": "#16a34a", "DANGER_LINE": "#dc2626", "TRACK": "#e2e8f0", "TEXT": "#0f172a",
            "SELECTED": "#0f172a"},
}
_session = threading.local()


def set_mode(mode: str | None) -> str:
    """Mode d'affichage de la session en cours (fil Streamlit) ; un mode inconnu revient au mode par défaut."""
    _session.mode = mode if mode in MODES else DEFAULT_MODE
    return _session.mode


def mode() -> str:
    return getattr(_session, "mode", DEFAULT_MODE)


def is_light() -> bool:
    return mode() == LIGHT


def palette() -> dict[str, str]:
    return _PALETTES[LIGHT] if is_light() else _PALETTES.get(THEME, _PALETTES["glass"])


def __getattr__(name: str) -> str:
    """`ui.PRIMARY`, `ui.MUTED`… : couleur du mode de la session en cours."""
    colors = palette()
    if name in colors:
        return colors[name]
    raise AttributeError(name)


_ids = itertools.count()

_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
  --color-bg: #05020c;
  --color-sidebar: #090017;
  --color-surface: #0d0719;
  --color-surface-2: #180c32;
  --color-surface-3: #221545;
  --color-border: #1f1538;
  --color-border-strong: #6d5bb0;
  --color-text: #ffffff;
  --color-text-2: #d5d1f6;
  --color-muted: #a7a3cb;
  --color-label: #8a8fb0;
  --color-subtitle: #b89fd9;
  --color-primary: #7c3aed;
  --color-primary-hover: #6d28d9;
  --color-on-primary: #ffffff;
  --color-mark: linear-gradient(135deg, #8b5cf6, #6d28d9);
  --color-track: #180c32;
  --color-accent: #9568ff;
  --color-amber: #f59e0b;
  --color-focus: #c4b5fd;
  --color-icon: #9568ff; --color-icon-bg: var(--color-surface-3);
  --color-ink: #f8fafc; --color-on-ink: #0f172a; --color-on-ink-muted: #475569;
  --shadow-card: none;
  --color-success: #86efac; --color-success-bg: #0e3018; --color-success-dot: #4ade80;
  --color-danger: #fda4c8;  --color-danger-bg: #290521;  --color-danger-dot: #fb7185;
  --color-warning: #fcd34d; --color-warning-bg: #2e1f05; --color-warning-dot: #f59e0b;
  --color-violet: #d2b0fd;  --color-violet-bg: #2c1b51;
  --font-sans: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
  --radius-sm: 8px;
  --radius-card: 24px;
}

html, body, .stApp, [data-testid="stAppViewContainer"] { background: var(--color-bg); }
.stApp, .stApp p, .stApp label, .stApp input, .stApp button, .stApp textarea, .stApp li {
  font-family: var(--font-sans);
  color: var(--color-text-2);
}
[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 1.75rem; max-width: 1280px; }
h1, h2, h3 { font-family: var(--font-sans) !important; color: var(--color-text) !important; letter-spacing: -0.015em; }
h1 { font-size: 34px !important; font-weight: 700 !important; }
h2, h3 { font-size: 20px !important; font-weight: 700 !important; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p, .stApp small { color: var(--color-muted) !important; }
[data-testid="stHeaderActionElements"] { display: none; }
.stApp a { color: var(--color-violet); }

/* --- Focus visible (WCAG 2.4.7) --- */
.stApp :focus-visible { outline: 2px solid var(--color-focus) !important; outline-offset: 2px !important; }

/* --- Sidebar --- */
[data-testid="stSidebar"] { background: var(--color-sidebar); border-right: 1px solid var(--color-border); }
[data-testid="stSidebar"] [role="radiogroup"] { gap: 4px; }
[data-testid="stSidebar"] [role="radiogroup"] label {
  width: 100%; min-height: 48px; padding: 10px 16px; margin: 0; border-radius: 14px;
  transition: background-color 150ms ease-out; cursor: pointer; align-items: center;
}
[data-testid="stSidebar"] [role="radiogroup"] label p { font-size: 16px; color: var(--color-text-2); font-weight: 500; }
[data-testid="stSidebar"] [role="radiogroup"] label:hover { background: var(--color-surface); }
[data-testid="stSidebar"] [role="radiogroup"] label[data-selected="true"] { background: var(--color-surface-2); }
[data-testid="stSidebar"] [role="radiogroup"] label[data-selected="true"] p { font-weight: 600; color: var(--color-text); }
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:focus-visible) { outline: 2px solid var(--color-focus); outline-offset: 2px; }
/* Pastille ronde du bouton radio masquée : l'élément actif est signalé par le fond et la graisse. */
[data-testid="stSidebar"] [role="radiogroup"] label > div > div:first-child:not([data-testid]) { display: none; }
[data-testid="stSidebar"] hr { border-color: var(--color-border); }

/* --- Boutons (pilules) --- */
.stButton button, .stFormSubmitButton button, .stDownloadButton button {
  border-radius: 999px; border: 2px solid var(--color-primary); background: transparent;
  color: var(--color-violet); font-weight: 600; min-height: 44px; padding: 0 20px;
  transition: background-color 150ms ease-out;
}
.stButton button p, .stFormSubmitButton button p, .stDownloadButton button p { color: var(--color-violet); font-weight: 600; }
.stButton button:hover, .stFormSubmitButton button:hover, .stDownloadButton button:hover {
  background: var(--color-surface-2); border-color: var(--color-accent); color: var(--color-text);
}
.stButton button[kind="primary"], .stFormSubmitButton button[kind="primaryFormSubmit"],
.stFormSubmitButton button[kind="primary"] {
  background: var(--color-primary); border-color: var(--color-primary); color: var(--color-on-primary);
}
.stButton button[kind="primary"] p, .stFormSubmitButton button[kind="primaryFormSubmit"] p,
.stFormSubmitButton button[kind="primary"] p { color: var(--color-on-primary); }
.stButton button[kind="primary"]:hover, .stFormSubmitButton button[kind="primaryFormSubmit"]:hover,
.stFormSubmitButton button[kind="primary"]:hover { background: var(--color-primary-hover); border-color: var(--color-primary-hover); }

/* --- Champs --- */
.stTextInput input, .stNumberInput input, .stDateInput input { background: var(--color-surface-2) !important; color: var(--color-text) !important; }
.stTextInput [data-baseweb="input"], .stNumberInput [data-baseweb="input"], .stDateInput [data-baseweb="input"],
[data-baseweb="select"] > div {
  background: var(--color-surface-2) !important; border: 1px solid var(--color-border-strong) !important; border-radius: 999px !important;
}

/* --- Conteneurs, formulaires, expanders, alertes --- */
[data-testid="stForm"], [data-testid="stExpander"] details, [data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--color-surface); border: 1px solid var(--color-border) !important; border-radius: var(--radius-card);
}
[data-testid="stForm"] { padding: 24px 28px; }
[data-testid="stAlert"] { border-radius: 16px; }

/* --- Barre horizontale du haut --- */
.st-key-es-topbar {
  position: sticky; top: 0; z-index: 50; background: var(--color-bg);
  border-bottom: 1px solid var(--color-border); padding: 14px 0 16px; margin-bottom: 24px; gap: 12px;
}
.st-key-es-topbar { flex-wrap: wrap !important; row-gap: 12px !important; }
/* Une seule ligne sur écran large ; sur écran étroit, le groupe de droite passe dessous, aligné à droite. */
/* Streamlit enveloppe chaque groupe dans un conteneur en colonne : la flexibilité se règle sur l'enveloppe. */
.st-key-es-topbar > [data-testid="stLayoutWrapper"]:has(> .st-key-es-topbar-left) { flex: 1 1 340px !important; min-width: 0; }
.st-key-es-topbar > [data-testid="stLayoutWrapper"]:has(> .st-key-es-topbar-right) { flex: 0 0 auto !important; margin-left: auto; }
.st-key-es-topbar-left { gap: 12px; flex-wrap: nowrap !important; min-width: 0; }
.st-key-es-topbar-right { gap: 12px; flex-wrap: nowrap !important; }
.st-key-es-topbar-left > div:has(.st-key-es_search), .st-key-es_search { flex: 1 1 auto !important; min-width: 160px; }
/* Flèche d'ouverture des menus ronds masquée (le rôle est donné par l'icône et l'infobulle). */
.st-key-es-bell button div[aria-hidden="true"], .st-key-es-bell-on button div[aria-hidden="true"],
.st-key-es-help button div[aria-hidden="true"], .st-key-es-avatar button div[aria-hidden="true"] { display: none; }
/* Sélecteur de client : pilule sombre (équivalent « Acme Inc »). */
.st-key-org_id [data-baseweb="select"] > div {
  background: var(--color-surface) !important; border: 1px solid var(--color-border) !important; min-height: 52px;
  font-weight: 700; color: var(--color-text);
}
.st-key-org_id [data-baseweb="select"] > div::before {
  content: ""; width: 30px; height: 30px; border-radius: 8px; margin-left: 10px; flex-shrink: 0;
  background: var(--color-mark);
}
.es-org-pill {
  display: inline-flex; align-items: center; gap: 10px; min-height: 52px; padding: 0 20px 0 12px; border-radius: 999px;
  background: var(--color-surface); border: 1px solid var(--color-border); font-weight: 700; color: var(--color-text);
}
.es-org-mark {
  width: 30px; height: 30px; border-radius: 8px; display: grid; place-items: center; color: var(--color-on-primary); font-size: 14px;
  background: var(--color-mark);
}
/* Recherche : pilule remplie. */
.st-key-es_search [data-baseweb="select"] > div { background: var(--color-surface-2) !important; border-color: var(--color-surface-3) !important; min-height: 52px; }
/* Période : pilule au contour violet (équivalent « Last 30 days »). */
.st-key-period [data-baseweb="select"] > div {
  background: transparent !important; border: 2px solid var(--color-primary) !important; min-height: 52px; font-weight: 500;
}
.st-key-period [data-baseweb="select"] * { color: var(--color-violet) !important; }
/* Boutons ronds : alertes, aide, compte. */
.st-key-es-bell button, .st-key-es-bell-on button, .st-key-es-help button, .st-key-es-avatar button,
.st-key-es-mode button {
  min-height: 52px; min-width: 52px; border-radius: 999px; border: 1px solid var(--color-border);
  background: var(--color-bg); padding: 0 14px;
}
.st-key-es-bell button p, .st-key-es-bell-on button p, .st-key-es-help button p, .st-key-es-mode button p { color: var(--color-text); }
.st-key-es-mode button [data-testid="stIconMaterial"] { color: var(--color-text); }
.st-key-es-bell-on button { position: relative; }
.st-key-es-bell-on button::after {
  content: ""; position: absolute; top: 12px; right: 14px; width: 8px; height: 8px; border-radius: 50%;
  background: var(--color-danger-dot);
}
.st-key-es-avatar button { background: var(--color-surface-3); border-color: var(--color-surface-3); padding: 0; width: 52px; }
.st-key-es-avatar button p { color: var(--color-text); font-weight: 700; }
.st-key-es-avatar [data-testid="stPopoverButton"] svg, .st-key-es-avatar button [data-testid="stIconMaterial"] { display: none; }
/* Libellés des boutons à icône : masqués visuellement mais lus par les lecteurs d'écran (nom accessible). */
.st-key-es-bell button p, .st-key-es-bell-on button p, .st-key-es-help button p, .st-key-es-avatar button p,
.st-key-es-mode button p {
  position: absolute !important; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap;
}
/* Compteur d'alertes et initiales : affichage seulement (le nom accessible vient du libellé). */
.st-key-es-bell-on button::before { content: ""; order: 2; margin-left: 6px; font-weight: 600; color: var(--color-text); }
.st-key-es-avatar button::before { font-weight: 700; color: var(--color-text); }
/* La barre fixe ne doit jamais masquer l'élément qui a le focus (WCAG 2.4.11). */
html, [data-testid="stMain"], [data-testid="stAppViewContainer"] { scroll-padding-top: 110px; }
.st-key-es-new-export button { min-height: 52px; padding: 0 24px; }
.st-key-es-new-export button p { font-size: 17px; font-weight: 700; }

/* --- Composants EffiSmart --- */
.es-logo { display: block; margin: 4px 0 28px; }
.es-logo img { display: block; height: 30px; width: auto; }
.es-logo.es-logo-large img { height: 44px; }
.es-user { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }
.es-avatar {
  width: 44px; height: 44px; border-radius: 50%; background: var(--color-surface-3); color: var(--color-text);
  display: grid; place-items: center; font-weight: 700; font-size: 14px; flex-shrink: 0;
}
.es-user-text { font-size: 14px; line-height: 1.35; color: var(--color-text); font-weight: 600; word-break: break-all; }
.es-user-text span { color: var(--color-muted); font-weight: 400; }
.es-section { font-size: 14px; font-weight: 600; color: var(--color-label); margin: 8px 0 8px 4px; }

.es-header { margin: 0 0 24px; }
.es-eyebrow { font-size: 15px; font-weight: 500; color: var(--color-label); }
.es-title { font-size: 34px; font-weight: 700; color: var(--color-text); letter-spacing: -0.02em; line-height: 1.2; margin: 8px 0 8px; }
.es-subtitle { font-size: 16px; color: var(--color-subtitle); }

.es-label { font-size: 14px; font-weight: 600; color: var(--color-label); }
.es-kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 24px; margin: 8px 0 28px; }
.es-card { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-card); padding: 28px 28px 20px; }
.es-kpi-head { display: flex; align-items: center; gap: 12px; }
.es-kpi-icon {
  width: 44px; height: 44px; border-radius: 50%; background: var(--color-icon-bg); color: var(--color-icon);
  display: grid; place-items: center; flex-shrink: 0;
}
.es-kpi-label { font-size: 16px; color: var(--color-muted); }
.es-kpi-value { font-size: 36px; font-weight: 700; line-height: 1.1; margin: 18px 0 12px; color: var(--color-text); letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.es-kpi-trend { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 14px; }
.es-kpi-note { font-size: 15px; color: var(--color-muted); }
.es-spark { display: block; width: 100%; height: 48px; }
/* Carte principale : deux colonnes sur deux lignes, les autres forment un carré 2 × 2 à côté. */
.es-kpis:has(.es-hero) { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.es-card.es-hero { grid-column: span 2; grid-row: span 2; display: flex; flex-direction: column; }
.es-card.es-hero .es-kpi-value { font-size: 64px; margin: auto 0 16px; }
.es-card.es-hero .es-spark { height: 96px; }
@media (max-width: 1100px) {
  .es-kpis:has(.es-hero) { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .es-card.es-hero { grid-row: auto; }
}
@media (max-width: 640px) {
  .es-kpis:has(.es-hero) { grid-template-columns: minmax(0, 1fr); }
  .es-card.es-hero { grid-column: auto; }
  .es-card.es-hero .es-kpi-value { font-size: 44px; }
}

.es-banner {
  display: flex; gap: 12px; align-items: baseline; padding: 14px 20px; border: 1px solid var(--color-border);
  border-radius: 16px; background: var(--color-surface); font-size: 14px; margin: 0 0 20px; color: var(--color-text-2);
}
.es-banner b { color: var(--color-text); }

.es-badge {
  display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 999px;
  font-size: 13px; font-weight: 600; white-space: nowrap; font-variant-numeric: tabular-nums;
}
.es-badge.neutral { background: var(--color-violet-bg); color: var(--color-violet); }
.es-badge.success { background: var(--color-success-bg); color: var(--color-success); }
.es-badge.warning { background: var(--color-warning-bg); color: var(--color-warning); }
.es-badge.danger { background: var(--color-danger-bg); color: var(--color-danger); }

.es-status { display: inline-flex; align-items: center; gap: 8px; padding: 4px 12px; border-radius: 999px; font-size: 13px; font-weight: 600; white-space: nowrap; background: var(--color-violet-bg); color: var(--color-violet); }
.es-status::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.es-status.success { background: var(--color-success-bg); color: var(--color-success); }
.es-status.success::before { background: var(--color-success-dot); }
.es-status.warning { background: var(--color-warning-bg); color: var(--color-warning); }
.es-status.danger { background: var(--color-danger-bg); color: var(--color-danger); }
.es-status.danger::before { background: var(--color-danger-dot); }

.es-progress { display: flex; align-items: center; gap: 12px; min-width: 170px; }
.es-progress-track { flex: 1; height: 8px; background: var(--color-surface-2); border-radius: 999px; overflow: hidden; }
.es-progress-fill { height: 100%; background: var(--color-accent); border-radius: 999px; }
.es-progress-fill.amber { background: var(--color-amber); }
.es-progress span { font-size: 13px; color: var(--color-muted); min-width: 44px; text-align: right; font-variant-numeric: tabular-nums; }

.es-table-wrap { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-card); overflow-x: auto; margin: 8px 0 20px; padding: 8px 12px; }
.es-table { width: 100%; border-collapse: collapse; font-size: 15px; }
.es-table th {
  font-size: 14px; font-weight: 600; color: var(--color-label);
  text-align: left; padding: 16px 14px; border-bottom: 1px solid var(--color-border); white-space: nowrap;
}
.es-table td { padding: 18px 14px; border-bottom: 1px solid var(--color-border); vertical-align: middle; color: var(--color-text-2); }
.es-table tr:last-child td { border-bottom: none; }
.es-table td.num, .es-table th.num { text-align: right; font-variant-numeric: tabular-nums; }
.es-table b { color: var(--color-text); font-weight: 600; }
.es-table .sub { display: block; font-size: 13px; color: var(--color-muted); margin-top: 2px; }

.es-gauge { text-align: center; }
.es-gauge-value { font-size: 34px; font-weight: 700; margin-top: -8px; color: var(--color-text); font-variant-numeric: tabular-nums; }
.es-gauge-caption { font-size: 14px; color: var(--color-muted); }

.es-feed { list-style: none; margin: 0; padding: 0; }
.es-feed li { display: flex; gap: 12px; align-items: center; padding: 14px 0; border-bottom: 1px solid var(--color-border); font-size: 15px; flex-wrap: wrap; color: var(--color-text-2); }
.es-feed li:last-child { border-bottom: none; }
.es-feed .when { font-size: 13px; color: var(--color-muted); min-width: 92px; font-variant-numeric: tabular-nums; }

/* --- Visuels canvas-design --- */
.es-art { display: block; width: 100%; height: auto; border-radius: var(--radius-card); border: 1px solid var(--color-border); }
.es-empty { text-align: center; padding: 28px 24px; border: 1px solid var(--color-border); border-radius: var(--radius-card); background: var(--color-surface); }
.es-empty-art { display: block; width: min(420px, 100%); height: auto; margin: 0 auto 18px; border-radius: 12px; }
.es-empty-title { font-size: 18px; font-weight: 700; color: var(--color-text); margin-bottom: 6px; }
.es-empty-text { font-size: 15px; color: var(--color-muted); }

/* --- Principe P1 : sorties de la plateforme expliquées --- */
.es-output-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
.es-output-title { font-size: 17px; font-weight: 700; color: var(--color-text); margin: 2px 0 6px; }
.es-output-action { font-size: 15px; color: var(--color-text-2); margin: 4px 0; }
.es-output-gain { font-size: 15px; color: var(--color-text-2); margin: 8px 0 4px; }
.es-output-gain b { color: var(--color-text); }
.es-output-meta { font-size: 13px; color: var(--color-muted); margin-top: 4px; }
.es-output-context { border-left: 3px solid var(--color-accent); padding: 6px 0 6px 12px; margin: 10px 0 6px; }
.es-output-context .es-output-head { margin-bottom: 4px; }
ol.es-steps { margin: 4px 0 12px 20px; padding: 0; }
ol.es-steps li { margin: 5px 0; font-size: 14px; color: var(--color-text-2); line-height: 1.5; }
ul.es-factors { list-style: none; margin: 4px 0 8px; padding: 0; }
ul.es-factors li { display: flex; gap: 12px; margin: 4px 0; font-size: 14px; color: var(--color-text-2); }
ul.es-factors .delta { min-width: 84px; font-weight: 600; font-variant-numeric: tabular-nums; }
ul.es-factors .delta.up { color: var(--color-success); }
ul.es-factors .delta.down { color: var(--color-danger); }

/* --- Graphe physique --- */
.es-chain { font-size: 14px; color: var(--color-text-2); padding: 8px 0; border-bottom: 1px solid var(--color-border); }
ul.es-warnings { margin: 0 0 0 18px; padding: 0; }
ul.es-warnings li { font-size: 14px; color: var(--color-warning); margin: 4px 0; }
.es-legend { display: flex; flex-wrap: wrap; gap: 16px; font-size: 13px; color: var(--color-muted); margin-top: 8px; }
.es-legend span { display: inline-flex; align-items: center; gap: 6px; }
.es-legend i { width: 14px; height: 14px; border-radius: 4px; border: 2px solid; display: inline-block; }
.es-legend .k-meter { background: #0c4a6e; border-color: #38bdf8; }
.es-legend .k-equipment { background: #14532d; border-color: #22c55e; }
.es-legend .k-flow { background: #1e293b; border-color: #94a3b8; border-radius: 50%; }
.es-legend .k-zone { background: #312e81; border-color: #a5b4fc; }
.es-legend .k-usage { background: #422006; border-color: #fbbf24; }
.es-legend .k-target { background: transparent; border-color: #fbbf24; border-width: 3px; }

@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""


# --- Thème « Verre » : design system généré par UI/UX Pro Max pour EffiSmart -----------------
# Glassmorphism sombre, ardoise + accent vert, Fira Sans / Fira Code. Contrastes vérifiés
# (texte ≥ 4,5:1, bordures de champs et séries ≥ 3:1) ; flou désactivé si mouvement réduit.
_GLASS_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Fira+Code:wght@500;600;700&family=Fira+Sans:wght@400;500;600;700&display=swap');
:root {
  --color-bg: #0f172a;
  --color-sidebar: rgba(15, 23, 42, 0.72);
  --color-surface: rgba(30, 41, 59, 0.62);
  --color-surface-2: rgba(39, 51, 73, 0.85);
  --color-surface-3: #273349;
  --color-border: rgba(148, 163, 184, 0.18);
  --color-border-strong: #94a3b8;
  --color-text: #f8fafc;
  --color-text-2: #e2e8f0;
  --color-muted: #94a3b8;
  --color-label: #94a3b8;
  --color-subtitle: #cbd5e1;
  --color-primary: #22c55e;
  --color-primary-hover: #16a34a;
  --color-on-primary: #0f172a;
  --color-accent: #22c55e;
  --color-amber: #38bdf8;
  --color-focus: #ffffff;
  --color-mark: linear-gradient(135deg, #22c55e, #0ea5e9);
  --color-track: #273349;
  --color-violet: #bbf7d0; --color-violet-bg: rgba(34, 197, 94, 0.16);
  --font-sans: 'Fira Sans', system-ui, -apple-system, 'Segoe UI', sans-serif;
  --radius-card: 26px;
  --color-icon: #22c55e;
}
html, body, .stApp, [data-testid="stAppViewContainer"] {
  background:
    radial-gradient(900px 600px at 12% -10%, rgba(34, 197, 94, 0.18), transparent 60%),
    radial-gradient(800px 600px at 95% 10%, rgba(14, 165, 233, 0.16), transparent 60%),
    radial-gradient(700px 500px at 60% 110%, rgba(99, 102, 241, 0.14), transparent 60%),
    #0f172a;
  background-attachment: fixed;
}
.es-kpi-value { font-family: 'Fira Code', ui-monospace, monospace !important; letter-spacing: -0.03em; }
.es-card, .es-table-wrap, .es-banner, [data-testid="stForm"], [data-testid="stExpander"] details,
[data-testid="stVerticalBlockBorderWrapper"], [data-testid="stSidebar"], .st-key-es-topbar {
  background: var(--color-surface); backdrop-filter: blur(16px) saturate(140%); -webkit-backdrop-filter: blur(16px) saturate(140%);
}
[data-testid="stSidebar"] { background: var(--color-sidebar); }
.st-key-es-topbar { background: rgba(15, 23, 42, 0.7); border-radius: 0 0 18px 18px; padding-left: 12px; padding-right: 12px; }
.stButton button, .stFormSubmitButton button, .stDownloadButton button { border-color: var(--color-border-strong); color: var(--color-text); }
.stButton button p, .stFormSubmitButton button p, .stDownloadButton button p { color: var(--color-text); }
[data-testid="stSidebar"] [role="radiogroup"] label[data-selected="true"] { background: rgba(34, 197, 94, 0.14); }
.st-key-period [data-baseweb="select"] * { color: #86efac !important; }
@media (prefers-reduced-motion: reduce) {
  .es-card, .es-table-wrap, .es-banner, [data-testid="stSidebar"], .st-key-es-topbar { backdrop-filter: none; }
}
"""

# --- Tableau de bord : photo du site, liste des sites, cartes à pastilles (les deux modes) ---------------
# Inspiré d'un tableau de bord domotique : grande photo avec ses mesures en pastilles, liste à pastilles rondes,
# carte foncée mise en avant, pastilles de statistiques, anneau. Aucune commande décorative : tout bouton agit.
_DASHBOARD_CSS = """
.es-panel {
  background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-card);
  box-shadow: var(--shadow-card); padding: 24px; min-height: 330px; display: flex; flex-direction: column; gap: 16px;
}
.es-panel-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.es-panel-title { font-size: 20px; font-weight: 700; color: var(--color-text); letter-spacing: -0.01em; }
.es-panel-sub { font-size: 14px; color: var(--color-muted); margin-top: 2px; }
.es-panel-value {
  font-size: 40px; font-weight: 700; line-height: 1.05; color: var(--color-text); letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums; font-family: 'Fira Code', ui-monospace, monospace;
}
.es-panel-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.es-panel .es-spark { height: 84px; margin-top: auto; }
/* Carte mise en avant : encre (ardoise en mode clair, blanc en mode sombre), sous-carte contrastée en bas. */
.es-panel.es-ink { background: var(--color-ink); border-color: var(--color-ink); }
.es-panel.es-ink .es-panel-title, .es-panel.es-ink .es-panel-value { color: var(--color-on-ink); }
.es-panel.es-ink .es-panel-sub, .es-panel.es-ink .es-kpi-note { color: var(--color-on-ink-muted); }
.es-subcard {
  margin-top: auto; display: flex; align-items: center; gap: 12px; padding: 14px 16px; border-radius: 20px;
  background: var(--color-surface-2); color: var(--color-text);
}
.es-panel.es-ink .es-subcard { background: var(--color-on-ink); color: var(--color-ink); }
.es-subcard-text { flex: 1; min-width: 72px; }
.es-subcard-end { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
.es-subcard-title { font-size: 15px; font-weight: 700; }
.es-subcard-sub { font-size: 13px; opacity: 0.78; }
.es-subcard-value { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }

/* Pastilles de statistiques : rond foncé + icône, valeur, libellé. */
.es-stats { display: flex; flex-wrap: wrap; gap: 10px; }
.es-stat {
  display: inline-flex; align-items: center; gap: 10px; padding: 6px 16px 6px 6px; border-radius: 999px;
  background: var(--color-surface-2); border: 1px solid var(--color-border);
}
.es-stat-icon {
  width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center; flex-shrink: 0;
  background: var(--color-icon-bg); color: var(--color-icon);
}
.es-stat-icon svg { width: 18px; height: 18px; }
.es-stat-value { display: block; font-size: 14px; font-weight: 700; color: var(--color-text); font-variant-numeric: tabular-nums; }
.es-stat-label { display: block; font-size: 12px; color: var(--color-muted); }

/* Photo du site en tête du tableau de bord, mesures en pastilles. */
.es-site-hero {
  position: relative; overflow: hidden; border-radius: var(--radius-card); min-height: 400px;
  background: var(--color-surface-3); box-shadow: var(--shadow-card);
}
.es-site-hero > img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; border: 0; border-radius: 0; }
.es-site-hero::after {
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(180deg, rgba(15,23,42,0.38) 0%, rgba(15,23,42,0) 32%, rgba(15,23,42,0) 52%, rgba(15,23,42,0.78) 100%);
}
.es-site-hero-top {
  position: absolute; z-index: 1; top: 18px; left: 18px; right: 18px;
  display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap;
}
.es-chips { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
.es-chip {
  display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 999px;
  background: rgba(255,255,255,0.86); color: #0f172a; font-size: 13px; font-weight: 600; white-space: nowrap;
  font-variant-numeric: tabular-nums; backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
}
.es-chip svg { width: 16px; height: 16px; color: #0f172a; }
.es-chip.es-chip-fresh::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: #16a34a; }
.es-site-hero-bottom { position: absolute; z-index: 1; left: 24px; right: 24px; bottom: 22px; }
.es-site-hero-title { font-size: 28px; font-weight: 700; color: #ffffff; letter-spacing: -0.02em; text-shadow: 0 1px 12px rgba(15,23,42,0.5); }
.es-site-hero-sub { font-size: 14px; color: #e2e8f0; margin-top: 4px; text-shadow: 0 1px 8px rgba(15,23,42,0.6); }

/* Liste des sites : rangées arrondies, pastille ronde, flèche ; le site affiché est en encre. */
.st-key-es-sites {
  background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-card);
  box-shadow: var(--shadow-card); padding: 22px 18px 18px; min-height: 400px; gap: 10px;
}
.st-key-es-sites > div:has(.st-key-es-add-site) { margin-top: auto; }
.st-key-es-sites .es-panel-head { padding-bottom: 14px; }

/* Listes déroulantes (Streamlit 1.64 : composant react-aria, « role=group ») : pilules, comme les champs. */
[data-testid="stSelectbox"] [role="group"], [data-testid="stMultiSelect"] [role="group"] {
  background: var(--color-surface-2) !important; border: 1px solid var(--color-border-strong); border-radius: 999px;
  min-height: 44px; padding-left: 10px;
}
[data-testid="stSelectbox"] [role="group"] input { color: var(--color-text); -webkit-text-fill-color: var(--color-text); }
[data-testid="stSelectbox"] [role="group"] button svg { color: var(--color-muted); }
.st-key-es_search [role="group"], .st-key-org_id [role="group"] { min-height: 52px; }
.st-key-org_id [role="group"] { background: var(--color-surface) !important; border-color: var(--color-border); }
.st-key-org_id [role="group"] input { font-weight: 700; }
.st-key-period [role="group"] { min-height: 52px; background: transparent !important; border: 2px solid var(--color-primary); }
.st-key-period [role="group"] input { color: var(--color-violet); -webkit-text-fill-color: var(--color-violet); font-weight: 500; }
[class*="st-key-es-siterow-"] {
  background: var(--color-surface-2); border: 1px solid var(--color-border); border-radius: 22px;
  padding: 8px 8px 8px 10px; gap: 12px; flex-wrap: nowrap !important;
}
[class*="st-key-es-siterow-on-"] { background: var(--color-ink); border-color: var(--color-ink); }
[class*="st-key-es-siterow-"] > div:first-child { flex: 1 1 auto !important; min-width: 0; }
.es-site-row { display: flex; align-items: center; gap: 12px; min-width: 0; }
.es-site-avatar {
  width: 46px; height: 46px; border-radius: 50%; display: grid; place-items: center; flex-shrink: 0;
  background: var(--color-surface); color: var(--color-text); border: 1px solid var(--color-border);
}
.es-site-avatar svg { width: 20px; height: 20px; }
.es-site-name { font-size: 15px; font-weight: 700; color: var(--color-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.es-site-meta { font-size: 13px; color: var(--color-muted); }
[class*="st-key-es-siterow-on-"] .es-site-name { color: var(--color-on-ink); }
[class*="st-key-es-siterow-on-"] .es-site-meta { color: var(--color-on-ink-muted); }
[class*="st-key-es-siterow-on-"] .es-site-avatar { background: var(--color-on-ink); color: var(--color-ink); border-color: var(--color-on-ink); }
[class*="st-key-es-siterow-"] .stButton button {
  min-width: 44px; min-height: 44px; padding: 0; border: none; background: transparent; border-radius: 999px;
}
[class*="st-key-es-siterow-"] .stButton button:hover { background: var(--color-surface); }
[class*="st-key-es-siterow-on-"] .stButton button [data-testid="stIconMaterial"] { color: var(--color-on-ink); }
[class*="st-key-es-siterow-"] .stButton button p {
  position: absolute !important; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap;
}
.st-key-es-add-site .stButton button { width: 100%; background: var(--color-ink); border-color: var(--color-ink); }
.st-key-es-add-site .stButton button p, .st-key-es-add-site .stButton button [data-testid="stIconMaterial"] { color: var(--color-on-ink); }
.st-key-es-add-site .stButton button:hover { background: var(--color-ink); opacity: 0.9; border-color: var(--color-ink); }

/* Anneau de répartition (électricité / gaz). */
.es-ring { position: relative; width: min(190px, 70%); margin: 4px auto 0; }
.es-ring svg { display: block; width: 100%; height: auto; }
.es-ring-center { position: absolute; inset: 0; display: grid; place-items: center; text-align: center; }
.es-ring-value { font-size: 30px; font-weight: 700; color: var(--color-text); font-variant-numeric: tabular-nums; }
.es-ring-label { font-size: 13px; color: var(--color-muted); }
.es-ring-legend { display: flex; justify-content: space-between; gap: 12px; margin-top: auto; }
.es-ring-legend span { font-size: 13px; color: var(--color-muted); }
.es-ring-legend b { display: block; font-size: 15px; color: var(--color-text); font-variant-numeric: tabular-nums; }
.es-swatch { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }

.es-sr { position: absolute !important; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

/* Écran étroit (téléphone) : la barre du haut passe sur plusieurs lignes au lieu de se chevaucher. */
@media (max-width: 640px) {
  .st-key-es-topbar-left, .st-key-es-topbar-right { flex-wrap: wrap !important; }
  .st-key-es-topbar > [data-testid="stLayoutWrapper"]:has(> .st-key-es-topbar-right) { flex: 1 1 100% !important; margin-left: 0; }
  .st-key-es-topbar-right { justify-content: flex-start !important; }
  .st-key-es-topbar-right > div { flex: 0 0 auto !important; width: auto !important; }
  .st-key-es-topbar-left > div:has(.st-key-es_search), .st-key-es_search { flex-basis: 100% !important; }
  .es-site-hero { min-height: 340px; }
  .es-site-hero-title { font-size: 22px; }
  .st-key-es-sites { min-height: 0; }
}

/* Navigation : icône devant chaque page. */
[data-testid="stSidebar"] [role="radiogroup"] label [data-testid="stIconMaterial"] { font-size: 20px; margin-right: 6px; opacity: 0.9; }
"""

# --- Mode clair : cadre blanc, cartes gris très clair arrondies, barre latérale ardoise ---------------
# Mêmes couleurs EffiSmart ; contrastes recalculés sur fond clair (texte ≥ 4,5:1, contours et séries ≥ 3:1).
# Streamlit reste configuré en sombre : ses composants natifs (champs, menus, onglets, alertes) sont repeints ici.
_LIGHT_CSS = """
:root {
  color-scheme: light;
  --color-bg: #ffffff;
  --color-frame: #ffffff;
  --color-sidebar: #0f172a;
  --color-surface: #f1f5f9;
  --color-surface-2: #ffffff;
  --color-surface-3: #e2e8f0;
  --color-border: #e2e8f0;
  --color-border-strong: #64748b;
  --color-text: #0f172a;
  --color-text-2: #1e293b;
  --color-muted: #475569;
  --color-label: #475569;
  --color-subtitle: #334155;
  --color-primary: #22c55e;
  --color-primary-hover: #16a34a;
  --color-on-primary: #0f172a;
  --color-accent: #16a34a;
  --color-amber: #0284c7;
  --color-focus: #0f172a;
  --color-mark: linear-gradient(135deg, #22c55e, #0ea5e9);
  --color-track: #e2e8f0;
  --color-icon: #22c55e; --color-icon-bg: #0f172a;
  --color-ink: #0f172a; --color-on-ink: #f8fafc; --color-on-ink-muted: #cbd5e1;
  --color-violet: #166534; --color-violet-bg: #dcfce7;
  --color-success: #166534; --color-success-bg: #dcfce7; --color-success-dot: #16a34a;
  --color-danger: #9f1239;  --color-danger-bg: #ffe4e6;  --color-danger-dot: #e11d48;
  --color-warning: #92400e; --color-warning-bg: #fef3c7; --color-warning-dot: #d97706;
  --shadow-card: 0 1px 2px rgba(15, 23, 42, 0.04), 0 12px 32px -16px rgba(15, 23, 42, 0.18);
  --radius-card: 28px;
}
html, body, .stApp, [data-testid="stAppViewContainer"] {
  background:
    radial-gradient(900px 600px at 10% -10%, rgba(34, 197, 94, 0.10), transparent 60%),
    radial-gradient(800px 600px at 95% 0%, rgba(14, 165, 233, 0.10), transparent 60%),
    #e7ecf2;
  background-attachment: fixed;
}
/* Cadre blanc arrondi autour du contenu, comme la référence. */
[data-testid="stMainBlockContainer"] {
  background: var(--color-frame); border-radius: 32px; margin-top: 14px; margin-bottom: 14px;
  padding-left: 32px; padding-right: 32px; padding-bottom: 48px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06), 0 24px 60px -30px rgba(15, 23, 42, 0.25);
}
.es-card, .es-table-wrap, .es-banner, [data-testid="stForm"], [data-testid="stExpander"] details,
[data-testid="stVerticalBlockBorderWrapper"], .st-key-es-topbar, .es-panel, .st-key-es-sites, .es-site-hero {
  backdrop-filter: none; -webkit-backdrop-filter: none;
}
.es-card, [data-testid="stVerticalBlockBorderWrapper"] { box-shadow: var(--shadow-card); }
.es-card, .es-table-wrap, .es-banner, [data-testid="stForm"], [data-testid="stExpander"] details,
[data-testid="stVerticalBlockBorderWrapper"] { background: var(--color-surface); border-color: var(--color-border) !important; }
.es-table-wrap { background: var(--color-surface-2); }
.es-banner { background: var(--color-surface); }
.st-key-es-topbar { background: var(--color-frame); border-radius: 0; padding-left: 0; padding-right: 0; }

/* Barre latérale ardoise, texte clair (le logo clair y reste lisible). */
[data-testid="stSidebar"] { background: var(--color-sidebar) !important; border-right: none; }
[data-testid="stSidebar"] [role="radiogroup"] label p,
[data-testid="stSidebar"] [role="radiogroup"] label [data-testid="stIconMaterial"] { color: #cbd5e1; }
[data-testid="stSidebar"] [role="radiogroup"] label:hover { background: rgba(255, 255, 255, 0.06); }
[data-testid="stSidebar"] [role="radiogroup"] label[data-selected="true"] { background: rgba(34, 197, 94, 0.18); }
[data-testid="stSidebar"] [role="radiogroup"] label[data-selected="true"] p,
[data-testid="stSidebar"] [role="radiogroup"] label[data-selected="true"] [data-testid="stIconMaterial"] { color: #ffffff; }
[data-testid="stSidebar"] .es-section, [data-testid="stSidebar"] [data-testid="stCaptionContainer"] p { color: #94a3b8 !important; }
[data-testid="stSidebar"] :focus-visible, [data-testid="stSidebar"] label:has(input:focus-visible) { outline-color: #ffffff !important; }
[data-testid="stSidebarCollapseButton"] button, [data-testid="stSidebarCollapseButton"] svg { color: #cbd5e1; }

/* Boutons : primaire vert à texte ardoise ; secondaire blanc à contour ardoise. */
.stButton button, .stFormSubmitButton button, .stDownloadButton button, [data-testid="stPopoverButton"] {
  background: var(--color-surface-2); border-color: var(--color-border-strong); color: var(--color-text);
}
.stButton button p, .stFormSubmitButton button p, .stDownloadButton button p, [data-testid="stPopoverButton"] p,
.stButton button [data-testid="stIconMaterial"], [data-testid="stPopoverButton"] [data-testid="stIconMaterial"] { color: var(--color-text); }
.stButton button:hover, .stFormSubmitButton button:hover, .stDownloadButton button:hover { background: var(--color-surface); color: var(--color-text); }
.stButton button[kind="primary"] p, .stFormSubmitButton button[kind="primaryFormSubmit"] p,
.stFormSubmitButton button[kind="primary"] p, .stButton button[kind="primary"] [data-testid="stIconMaterial"] { color: var(--color-on-primary); }
.st-key-es-bell button, .st-key-es-bell-on button, .st-key-es-help button, .st-key-es-avatar button, .st-key-es-mode button {
  background: var(--color-surface-2); border-color: var(--color-border);
}
.st-key-es-avatar button { background: var(--color-ink); border-color: var(--color-ink); }
.st-key-es-avatar button::before { color: var(--color-on-ink); }
.st-key-period [data-baseweb="select"] * { color: var(--color-violet) !important; }

/* Champs, listes déroulantes, menus. */
.stApp [data-baseweb="select"] div, .stApp [data-baseweb="select"] input, .stApp [data-baseweb="input"] input,
.stApp textarea { color: var(--color-text) !important; -webkit-text-fill-color: var(--color-text); }
.stApp [data-baseweb="select"] svg, .stApp [data-baseweb="input"] svg { color: var(--color-muted); fill: var(--color-muted); }
.stApp input::placeholder, .stApp textarea::placeholder { color: var(--color-muted) !important; -webkit-text-fill-color: var(--color-muted); }
.stTextArea [data-baseweb="textarea"], .stTextArea textarea {
  background: var(--color-surface-2) !important; border-color: var(--color-border-strong) !important;
}
[data-testid="stNumberInputStepUp"], [data-testid="stNumberInputStepDown"] { background: var(--color-surface-3) !important; }
.stApp [data-baseweb="input"] button { background: transparent !important; }
.stApp [data-baseweb="input"] button svg { color: var(--color-muted); fill: var(--color-muted); }
[data-testid="stNumberInputStepUp"] svg, [data-testid="stNumberInputStepDown"] svg { color: var(--color-text); fill: var(--color-text); }
[data-baseweb="popover"] [data-baseweb="menu"], [data-baseweb="popover"] ul[role="listbox"],
[data-testid="stSelectboxVirtualDropdown"], [data-baseweb="popover"] [role="listbox"] {
  background: #ffffff !important; border: 1px solid var(--color-border); border-radius: 16px;
}
[data-baseweb="popover"] [role="option"], [data-baseweb="popover"] [role="option"] * { color: var(--color-text) !important; background: transparent; }
[data-testid$="VirtualDropdown"] { background: #ffffff !important; border: 1px solid var(--color-border); border-radius: 16px; }
[data-testid$="VirtualDropdown"] [role="option"], [data-testid$="VirtualDropdown"] [role="option"] * { color: var(--color-text) !important; }
[data-testid$="VirtualDropdown"] [data-item-hl] { background: var(--color-surface) !important; }
[data-baseweb="popover"] [role="option"]:hover, [data-baseweb="popover"] [role="option"][aria-selected="true"] { background: var(--color-surface) !important; }
/* Les fenêtres surgissantes (popover, menus) sont hors de .stApp : texte foncé dès la racine. */
body { color: var(--color-text-2); }
[data-testid="stPopoverBody"] { background: #ffffff !important; border: 1px solid var(--color-border); color: var(--color-text); }
[data-testid="stPopoverBody"] p, [data-testid="stPopoverBody"] label, [data-testid="stPopoverBody"] li { color: var(--color-text-2); }
[data-testid="stFileUploaderDropzone"] button {
  background: var(--color-surface-2) !important; color: var(--color-text) !important; border: 1px solid var(--color-border-strong) !important;
}
[data-testid="stFileUploaderDropzone"] button * { color: var(--color-text) !important; }
[data-testid="stDateInputField"] {
  background: var(--color-surface-2) !important; border: 1px solid var(--color-border-strong) !important; border-radius: 999px;
}
[data-testid="stDateInputField"] [role="group"], [data-testid="stDateInputField"] [role="group"] *,
[data-testid="stDateInputField"] svg { color: var(--color-text) !important; }
[role="dialog"]:has([role="grid"]) { background: #ffffff !important; color: var(--color-text) !important; }
[role="dialog"]:has([role="grid"]) * { color: var(--color-text); }
[role="dialog"]:has([role="grid"]) [aria-selected="true"], [role="dialog"]:has([role="grid"]) [data-selected="true"] { color: var(--color-on-primary); }
[data-baseweb="calendar"], [data-baseweb="datepicker"], [data-baseweb="calendar"] > div { background: #ffffff !important; color: var(--color-text) !important; }
[data-baseweb="calendar"] * { color: var(--color-text); }
[data-baseweb="calendar"] [aria-selected="true"] * { color: var(--color-on-primary); }
[data-testid="stFileUploaderDropzone"] { background: var(--color-surface) !important; border: 1px dashed var(--color-border-strong); }
[data-testid="stFileUploaderDropzone"] span, [data-testid="stFileUploaderDropzone"] small,
[data-testid="stFileUploaderDropzoneInstructions"] * { color: var(--color-muted) !important; }
[data-testid="stFileUploaderFile"] *, [data-testid="stFileUploaderFileName"] { color: var(--color-text) !important; }
[data-testid="stSliderThumbValue"], [data-testid="stSliderTickBarMin"], [data-testid="stSliderTickBarMax"] { color: var(--color-text-2) !important; }
[data-testid="stTooltipIcon"] svg, [data-testid="stTooltipHoverTarget"] svg { color: var(--color-muted); stroke: var(--color-muted); }
[data-baseweb="checkbox"] span:first-child { border-color: var(--color-border-strong) !important; }
/* Boutons radio : anneau ardoise (vert une fois choisi), centre blanc. */
[data-testid="stRadioOption"] > div > div:first-child:not([data-testid]) { background: var(--color-border-strong) !important; }
[data-testid="stRadioOption"][data-selected="true"] > div > div:first-child:not([data-testid]) { background: var(--color-primary-hover) !important; }
[data-testid="stRadioOption"] > div > div:first-child:not([data-testid]) > div { background: #ffffff !important; }

/* Onglets, volets, séparateurs, code. */
[data-baseweb="tab-list"] button p { color: var(--color-muted); }
[data-baseweb="tab-list"] button[aria-selected="true"] p { color: var(--color-text); font-weight: 600; }
[data-baseweb="tab-border"] { background: var(--color-border) !important; }
[data-testid="stExpander"] summary, [data-testid="stExpander"] summary p, [data-testid="stExpander"] summary span,
[data-testid="stExpander"] summary svg { color: var(--color-text) !important; }
[data-testid="stExpander"] summary, [data-testid="stExpander"] summary:hover { background: transparent !important; }
/* Cases à cocher (react-aria) : case blanche à contour ardoise, verte une fois cochée. */
[data-testid="stCheckbox"] label > div:not([data-testid]) { background: #ffffff !important; border-color: var(--color-border-strong) !important; }
[data-testid="stCheckbox"] label[data-selected="true"] > div:not([data-testid]) {
  background: var(--color-primary-hover) !important; border-color: var(--color-primary-hover) !important;
}
.stApp hr { border-color: var(--color-border); }
.stApp code { background: var(--color-surface-3); color: var(--color-text); }

/* Alertes Streamlit : fonds teintés, texte foncé. */
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) { background: #e0f2fe; }
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) { background: #dcfce7; }
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) { background: #fef3c7; }
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) { background: #ffe4e6; }
[data-testid="stAlertContentInfo"], [data-testid="stAlertContentInfo"] * { color: #075985 !important; }
[data-testid="stAlertContentSuccess"], [data-testid="stAlertContentSuccess"] * { color: #166534 !important; }
[data-testid="stAlertContentWarning"], [data-testid="stAlertContentWarning"] * { color: #92400e !important; }
[data-testid="stAlertContentError"], [data-testid="stAlertContentError"] * { color: #9f1239 !important; }

/* Tableaux de données (dessinés par Streamlit en sombre) : inversés pour rester lisibles sur fond clair. */
[data-testid="stDataFrame"], [data-testid="stDataEditor"] { filter: invert(1) hue-rotate(180deg); border-radius: 12px; }

/* Légende du graphe physique : nœuds teintés lisibles sur fond clair. */
.es-legend .k-flow { background: #e2e8f0; border-color: #64748b; }
.es-legend .k-target { border-color: #d97706; }
"""

# Icônes au trait (style « lucide »), 20 px, couleur d'accent.
_ICON_PATHS = {
    "total": '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
    "elec": '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/>',
    "gas": '<path d="M12 2c1 4 5 6 5 11a5 5 0 0 1-10 0c0-3 2-4 2-7 2 1 3 3 3 5"/>',
    "cost": '<path d="M17 6.5A6.5 6.5 0 1 0 17 17.5"/><path d="M4 10h9M4 14h9"/>',
    "co2": '<path d="M11 20A7 7 0 0 1 4 13c0-6 7-9 16-9 0 9-3 16-9 16z"/><path d="M4 21c3-4 6-7 10-9"/>',
    "clients": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6 6 0 0 1 3.5 6"/>',
    "drifts": '<path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4M12 17h.01"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    "site": '<path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/><path d="M9 21v-4h6v4"/><path d="M9 10h.01M15 10h.01M9 14h.01M15 14h.01"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "surface": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/>',
    "points": '<circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>',
}

def e(text: object) -> str:
    return html.escape(str(text))


ASSETS = Path(__file__).resolve().parent / "assets"


@st.cache_data(show_spinner=False)
def _data_uri(name: str) -> str:
    return "data:image/png;base64," + base64.b64encode((ASSETS / name).read_bytes()).decode("ascii")


def artwork(name: str, alt: str, css_class: str = "es-art") -> str:
    """Visuel canvas-design (backend/assets) avec texte alternatif ; vide si le fichier manque."""
    if not (ASSETS / name).is_file():
        return ""
    return f'<img class="{css_class}" src="{_data_uri(name)}" alt="{e(alt)}">'


def empty_state(title: str, text: str) -> None:
    """État vide illustré (semaine ordinaire, sans exception)."""
    render(
        '<div class="es-empty">'
        + artwork("calme.png", "Illustration : une semaine de consommation régulière, sans anomalie.", "es-empty-art")
        + f'<div class="es-empty-title">{e(title)}</div><div class="es-empty-text">{e(text)}</div></div>'
    )


def inject_css() -> None:
    """Feuille de style de la session : thème sombre, composants du tableau de bord, puis mode clair s'il est choisi."""
    extra = _GLASS_CSS if THEME == "glass" else ""
    light = _LIGHT_CSS if is_light() else ""
    st.markdown(f"<style>{_CSS}{extra}{_DASHBOARD_CSS}{light}</style>", unsafe_allow_html=True)


def render(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def icon(name: str) -> str:
    path = _ICON_PATHS.get(name)
    if not path:
        return ""
    return (f'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{path}</svg>')


def initials(email: str) -> str:
    local = email.split("@")[0]
    parts = [p for p in local.replace(".", " ").replace("-", " ").split() if p]
    letters = "".join(p[0] for p in parts[:2]) if len(parts) > 1 else local[:2]
    return letters.upper()[:2]


def logo_html(large: bool = False, on_light: bool = False) -> str:
    """Logo officiel, avec « EffiSmart » comme texte alternatif : version claire sur fond sombre (barre latérale),
    version sombre sur fond clair (connexion en mode clair)."""
    name = "logo-sombre.png" if on_light else "logo-clair.png"
    return f'<div class="es-logo{" es-logo-large" if large else ""}">{artwork(name, "EffiSmart", "")}</div>'


ICON_PATH = str(ASSETS / "icone.png")  # icône d'onglet : les trois barres du logo


def user_html(email: str, role_label: str) -> str:
    return (
        f'<div class="es-user"><div class="es-avatar" aria-hidden="true">{e(initials(email))}</div>'
        f'<div class="es-user-text">{e(email)}<br><span>{e(role_label)}</span></div></div>'
    )


def section_label(text: str) -> None:
    render(f'<div class="es-section">{e(text)}</div>')


def page_header(eyebrow: str, title: str, subtitle: str = "") -> None:
    """Libellé de section (contexte) → titre blanc → sous-titre lavande ; le titre est un vrai <h1>."""
    sub = f'<div class="es-subtitle">{e(subtitle)}</div>' if subtitle else ""
    top = f'<div class="es-eyebrow">{e(eyebrow)}</div>' if eyebrow else ""
    render(f'<header class="es-header">{top}'
           f'<h1 class="es-title">{e(title)}</h1>{sub}</header>')


def banner(markup: str) -> None:
    render(f'<div class="es-banner" role="status">{markup}</div>')


def trend(pct: float | None, *, lower_is_better: bool = True) -> tuple[str, str]:
    """(HTML de la pilule de tendance, ton) — flèche + valeur, jamais la couleur seule."""
    if pct is None:
        return '<span class="es-badge neutral">—</span>', "neutral"
    if abs(pct) < 0.05:
        return '<span class="es-badge neutral">→ 0 %</span>', "neutral"
    up = pct > 0
    tone = "success" if (not up) == lower_is_better else "danger"
    value = f"{abs(pct):.1f}".replace(".", ",")
    return f'<span class="es-badge {tone}">{"↗" if up else "↘"} {value} %</span>', tone


def sparkline(values: Sequence[float], tone: str = "neutral", width: int = 300, height: int = 48) -> str:
    """Mini-courbe SVG pleine largeur avec dégradé (décorative : valeur et tendance sont écrites à côté)."""
    points = [v for v in values if v is not None]
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    step = width / (len(points) - 1)
    coords = [(i * step, height - 6 - (v - lo) / span * (height - 14)) for i, v in enumerate(points)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    colors = palette()
    color = {"success": colors["SUCCESS_LINE"], "danger": colors["DANGER_LINE"]}.get(tone, colors["PRIMARY"])
    gid = f"es-g{next(_ids)}"
    return (
        f'<svg class="es-spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{color}" stop-opacity="0.28"/><stop offset="1" stop-color="{color}" stop-opacity="0"/>'
        f'</linearGradient></defs>'
        f'<polygon points="0,{height} {line} {width},{height}" fill="url(#{gid})"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round" '
        f'vector-effect="non-scaling-stroke"/></svg>'
    )


def kpi_grid(cards: Sequence[dict]) -> None:
    """Cartes KPI : dict(label, value, [pct], lower_is_better=True, note="", spark=[], icon="").

    Sans clé `pct`, la carte n'affiche pas de pilule de tendance.
    """
    body = []
    for card in cards:
        if "pct" in card:
            badge_html, tone = trend(card["pct"], lower_is_better=card.get("lower_is_better", True))
        else:
            badge_html, tone = "", "neutral"
        icon_html = f'<span class="es-kpi-icon">{icon(card["icon"])}</span>' if card.get("icon") else ""
        body.append(
            f'<div class="es-card{" es-hero" if card.get("hero") else ""}"><div class="es-kpi-head">{icon_html}'
            f'<span class="es-kpi-label">{e(card["label"])}</span></div>'
            f'<div class="es-kpi-value">{e(card["value"])}</div>'
            f'<div class="es-kpi-trend">{badge_html}<span class="es-kpi-note">{e(card.get("note", ""))}</span></div>'
            f'{sparkline(card.get("spark", []), tone)}</div>'
        )
    render(f'<div class="es-kpis">{"".join(body)}</div>')


def badge(text: str, tone: str = "neutral") -> str:
    return f'<span class="es-badge {tone}">{e(text)}</span>'


def status(text: str, tone: str = "neutral") -> str:
    return f'<span class="es-status {tone}">{e(text)}</span>'


def progress(pct: float, label: str | None = None, amber: bool = False) -> str:
    pct = max(0.0, min(100.0, pct))
    shown = label if label is not None else f"{pct:.0f} %"
    return (
        f'<div class="es-progress" role="img" aria-label="{e(shown)}">'
        f'<div class="es-progress-track"><div class="es-progress-fill{" amber" if amber else ""}" '
        f'style="width:{pct:.1f}%"></div></div><span>{e(shown)}</span></div>'
    )


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], numeric: set[int] | None = None) -> None:
    """Tableau HTML ; les cellules sont du HTML déjà échappé par l'appelant."""
    numeric = numeric or set()
    head = "".join(f'<th class="{"num" if i in numeric else ""}">{e(h)}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if i in numeric else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>"
        for r in rows
    )
    render(f'<div class="es-table-wrap"><table class="es-table"><thead><tr>{head}</tr></thead>'
           f"<tbody>{body}</tbody></table></div>")


def gauge(pct: float, caption: str) -> None:
    """Jauge 180° : remplissage violet, valeur toujours écrite en clair."""
    pct = max(0.0, min(100.0, pct))
    r, cx, cy, width = 90, 110, 105, 20
    angle = math.pi * (1 - pct / 100)
    x, y = cx + r * math.cos(angle), cy - r * math.sin(angle)
    track = f"M {cx - r} {cy} A {r} {r} 0 0 1 {cx + r} {cy}"
    fill = f"M {cx - r} {cy} A {r} {r} 0 0 1 {x:.2f} {y:.2f}" if pct > 0 else ""
    value = f"{pct:.0f} %"
    render(
        f'<div class="es-gauge" role="img" aria-label="{e(caption)} : {e(value)}">'
        f'<svg viewBox="0 0 220 120" width="100%" style="max-width:320px">'
        f'<path d="{track}" fill="none" stroke="{palette()["TRACK"]}" stroke-width="{width}" stroke-linecap="round"/>'
        + (f'<path d="{fill}" fill="none" stroke="{palette()["PRIMARY"]}" stroke-width="{width}" stroke-linecap="round"/>'
           if fill else "")
        + f'</svg><div class="es-gauge-value">{e(value)}</div>'
        f'<div class="es-gauge-caption">{e(caption)}</div></div>'
    )


def feed(items: Sequence[tuple[str, str]]) -> None:
    """Liste chronologique : (quand, HTML du contenu)."""
    body = "".join(f'<li><span class="when">{e(when)}</span><span>{content}</span></li>' for when, content in items)
    render(f'<ul class="es-feed">{body}</ul>')


# --- Tableau de bord : composants ------------------------------------------------------------------------


def _hidden(text: str) -> str:
    """Texte lu par les lecteurs d'écran, invisible à l'écran."""
    return f'<span class="es-sr">{e(text)}</span>'


def chip(icon_name: str, text: str, label: str) -> str:
    """Pastille de mesure posée sur la photo : icône, valeur ; le libellé est lu, et affiché au survol."""
    return f'<span class="es-chip" title="{e(label)}">{icon(icon_name)}{_hidden(label + " : ")}{e(text)}</span>'


def site_hero(image_uri: str | None, alt: str, title: str, subtitle: str, fresh: str, chips: Sequence[str]) -> None:
    """Photo du site (à défaut, l'illustration de la courbe de charge), mesures en pastilles, nom du site."""
    if image_uri:
        image = f'<img src="{e(image_uri)}" alt="{e(alt)}">'
    else:
        image = artwork("connexion.png", "Illustration : courbe de charge d'un bâtiment sur plusieurs semaines.", "")
    render(
        f'<section class="es-site-hero" aria-label="{e(title)}">{image}'
        f'<div class="es-site-hero-top"><span class="es-chip es-chip-fresh">{e(fresh)}</span>'
        f'<div class="es-chips">{"".join(chips)}</div></div>'
        f'<div class="es-site-hero-bottom"><div class="es-site-hero-title">{e(title)}</div>'
        f'<div class="es-site-hero-sub">{e(subtitle)}</div></div></section>'
    )


def site_row(name: str, meta: str) -> str:
    return (f'<div class="es-site-row"><span class="es-site-avatar" aria-hidden="true">{icon("site")}</span>'
            f'<div style="min-width:0"><div class="es-site-name">{e(name)}</div>'
            f'<div class="es-site-meta">{e(meta)}</div></div></div>')


def stat_pills(items: Sequence[tuple[str, str, str]]) -> str:
    """Pastilles (icône, valeur, libellé), comme le bas des cartes de la référence."""
    return '<div class="es-stats">' + "".join(
        f'<span class="es-stat"><span class="es-stat-icon">{icon(name)}</span>'
        f'<span><span class="es-stat-value">{e(value)}</span><span class="es-stat-label">{e(label)}</span></span></span>'
        for name, value, label in items
    ) + "</div>"


def panel(title: str, subtitle: str, body: str, *, ink: bool = False, aside: str = "") -> None:
    """Carte du tableau de bord : titre, sous-titre, élément à droite (pilule de tendance…), contenu HTML."""
    render(
        f'<section class="es-panel{" es-ink" if ink else ""}"><div class="es-panel-head"><div>'
        f'<div class="es-panel-title">{e(title)}</div><div class="es-panel-sub">{e(subtitle)}</div></div>'
        f'{aside}</div>{body}</section>'
    )


def ring(share_pct: float, legend: Sequence[tuple[str, str, str]]) -> str:
    """Anneau de répartition à deux énergies : la première en vert, le reste en bleu ; valeurs écrites dessous."""
    share = max(0.0, min(100.0, share_pct))
    colors = palette()
    r, c = 52, 2 * math.pi * 52
    first = c * share / 100
    rest = (f'<circle cx="60" cy="60" r="{r}" fill="none" stroke="{colors["SERIES_2"]}" stroke-width="12" '
            f'stroke-dasharray="{c - first:.2f} {c:.2f}" stroke-dashoffset="{-first:.2f}" '
            'transform="rotate(-90 60 60)"/>') if share < 100 else ""
    head = (f'<circle cx="60" cy="60" r="{r}" fill="none" stroke="{colors["PRIMARY"]}" stroke-width="12" '
            f'stroke-dasharray="{first:.2f} {c:.2f}" transform="rotate(-90 60 60)"/>') if share > 0 else ""
    swatches = [colors["PRIMARY"], colors["SERIES_2"]]
    items = "".join(
        f'<span><span class="es-swatch" style="background:{swatches[i % 2]}"></span>{e(label)}<b>{e(value)}</b></span>'
        for i, (label, value, _) in enumerate(legend)
    )
    aria = ", ".join(f"{label} {value}" for label, value, _ in legend)
    return (
        f'<div class="es-ring" role="img" aria-label="{e(aria)}">'
        f'<svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="{r}" fill="none" stroke="{colors["TRACK"]}" '
        f'stroke-width="12"/>{rest}{head}</svg>'
        f'<div class="es-ring-center"><div><div class="es-ring-value">{share:.0f} %</div>'
        f'<div class="es-ring-label">{e(legend[0][2])}</div></div></div></div>'
        f'<div class="es-ring-legend">{items}</div>'
    )

