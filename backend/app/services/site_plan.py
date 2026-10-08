"""F8 — Visualisation spatiale : plan 2D du site, branché sur le graphe P2.

Le plan 2D suffit à situer une anomalie et coûte peu : les zones du graphe sont des rectangles, équipements et
compteurs des points, placés en % de la largeur et de la hauteur ; une image du plan (PNG ou JPEG) peut servir de
fond. La maquette 3D, coûteuse à développer et à alimenter, est reportée en V3 : un plan 2D branché sur le graphe
P2 est plus utile qu'une 3D vide.

Sur le plan, les anomalies récentes (F2b) sont situées : équipement suspect en rouge, zones potentiellement
impactées surlignées. Le plan est tenu par l'auditeur ; l'espace client le consulte.
"""
from __future__ import annotations

import base64
import html
import io
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AssetNode, AssetNodeKind, DeliveryPoint, Drift, DriftStatus, SitePlan, User
from app.repositories import TenantRepository
from app.services import assets
from app.services.drift import DRIFT_LABELS
from app.timeutils import today_local, utcnow

K = AssetNodeKind
ALLOWED_TYPES = {"image/png": ".png", "image/jpeg": ".jpg"}
MAX_PLAN_MB = 10
WIDTH = 1000
DEFAULT_ASPECT = 0.62
ANOMALY_WINDOW_DAYS = 30
ZONE_FILL = {"OFFICE": "#6366f1", "CARE": "#ec4899", "PRODUCTION": "#f97316", "STORAGE": "#a3a3a3",
             "RETAIL": "#14b8a6", "TECHNICAL": "#38bdf8", "OTHER": "#8b5cf6"}


class PlanError(ValueError):
    pass


def plan_dir() -> Path:
    path = Path(settings.document_dir) / "plans"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_plan(db: Session, site_id: int) -> SitePlan | None:
    return db.scalar(select(SitePlan).where(SitePlan.site_id == site_id))


def _content_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def _aspect(content: bytes, content_type: str) -> float:
    """Hauteur / largeur de l'image ; valeur par défaut si elle ne peut pas être lue."""
    if content_type == "image/png" and len(content) >= 24:
        width, height = int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")
        return height / width if width else DEFAULT_ASPECT
    try:
        from PIL import Image  # Pillow, livré avec l'interface Streamlit

        with Image.open(io.BytesIO(content)) as image:
            return image.height / image.width
    except Exception:  # image illisible ou Pillow absent : proportion par défaut
        return DEFAULT_ASPECT


def save_plan(db: Session, repo: TenantRepository, user: User, site_id: int, file_name: str, content: bytes) -> SitePlan:
    """Dépose (ou remplace) l'image du plan d'un site : PNG ou JPEG, 10 Mo au plus."""
    if not assets.can_edit(user):
        raise assets.AssetPermissionError("Le plan du site est tenu par l'auditeur ; l'espace client le consulte.")
    site = repo.get_site(site_id)
    if len(content) > MAX_PLAN_MB * 1024 * 1024:
        raise PlanError(f"Image trop lourde : {MAX_PLAN_MB} Mo au plus.")
    content_type = _content_type(content)
    if content_type is None:
        raise PlanError("Format refusé : image PNG ou JPEG uniquement.")
    plan = get_plan(db, site.id)
    if plan is not None:
        (plan_dir() / plan.stored_name).unlink(missing_ok=True)
    else:
        plan = SitePlan(organization_id=site.organization_id, site_id=site.id)
        db.add(plan)
    stored = uuid.uuid4().hex + ALLOWED_TYPES[content_type]
    (plan_dir() / stored).write_bytes(content)
    plan.file_name, plan.content_type, plan.stored_name = Path(file_name).name[:255], content_type, stored
    plan.aspect_ratio = min(2.0, max(0.25, _aspect(content, content_type)))
    plan.uploaded_by, plan.uploaded_at = user.id, utcnow()
    db.commit()
    return plan


def remove_plan(db: Session, repo: TenantRepository, user: User, site_id: int) -> None:
    if not assets.can_edit(user):
        raise assets.AssetPermissionError("Le plan du site est tenu par l'auditeur ; l'espace client le consulte.")
    plan = get_plan(db, repo.get_site(site_id).id)
    if plan is not None:
        (plan_dir() / plan.stored_name).unlink(missing_ok=True)
        db.delete(plan)
        db.commit()


def image_data_uri(plan: SitePlan | None) -> str | None:
    if plan is None:
        return None
    path = plan_dir() / plan.stored_name
    if not path.is_file():
        return None
    return f"data:{plan.content_type};base64,{base64.b64encode(path.read_bytes()).decode()}"


def set_position(db: Session, repo: TenantRepository, user: User, node_id: int, x: float | None, y: float | None,
                 w: float | None = None, h: float | None = None) -> AssetNode:
    """Place un élément (en % du plan) ; `x` vide le retire du plan. Une zone est un rectangle (largeur, hauteur)."""
    if not assets.can_edit(user):
        raise assets.AssetPermissionError("Le plan du site est tenu par l'auditeur ; l'espace client le consulte.")
    node = repo.get_asset_node(node_id)
    if x is None or y is None:
        node.plan_x = node.plan_y = node.plan_w = node.plan_h = None
        db.commit()
        return node
    if not (0 <= x <= 100 and 0 <= y <= 100):
        raise PlanError("Position hors du plan : de 0 à 100 % en largeur et en hauteur.")
    if node.kind == K.ZONE:
        if not w or not h or w <= 0 or h <= 0 or x + w > 100.5 or y + h > 100.5:
            raise PlanError("Une zone est un rectangle dans le plan : largeur et hauteur positives, sans déborder.")
        node.plan_w, node.plan_h = float(w), float(h)
    node.plan_x, node.plan_y = float(x), float(y)
    db.commit()
    return node


# --- Anomalies situées sur le plan ----------------------------------------------------------------


@dataclass
class PlanMarks:
    suspects: set[int] = field(default_factory=set)
    zones: set[int] = field(default_factory=set)
    notes: dict[int, list[str]] = field(default_factory=dict)  # nœud → anomalies qui le concernent


def marks_for(drifts: list[Drift]) -> PlanMarks:
    marks = PlanMarks()
    for drift in drifts:
        ctx = drift.context or {}
        if ctx.get("localisation") in (None, "absente"):
            continue
        text = f"{DRIFT_LABELS[drift.kind]} le {drift.day:%d/%m/%Y}"
        for suspect in ctx.get("suspects", []):
            if suspect.get("retained"):
                marks.suspects.add(suspect["id"])
                marks.notes.setdefault(suspect["id"], []).append(f"Suspect : {text}")
        for zone in ctx.get("impacted", {}).get("zones", []):
            marks.zones.add(zone["id"])
            marks.notes.setdefault(zone["id"], []).append(f"Zone potentiellement impactée : {text}")
    return marks


def recent_anomalies(db: Session, site_id: int, today: date | None = None,
                     statuses: tuple[DriftStatus, ...] = (DriftStatus.OPEN, DriftStatus.QUALIFIED)) -> list[Drift]:
    """Alertes du site dont une occurrence date des 30 derniers jours (une par groupe de même cause : l'alerte
    principale porte le contexte), à valider ou validées."""
    since = (today or today_local()) - timedelta(days=ANOMALY_WINDOW_DAYS)
    recent = db.scalars(select(Drift).join(DeliveryPoint, Drift.delivery_point_id == DeliveryPoint.id).where(
        DeliveryPoint.site_id == site_id, Drift.day >= since, Drift.status.in_(statuses)))
    lead_ids = {d.grouped_with_id or d.id for d in recent}
    if not lead_ids:
        return []
    return list(db.scalars(select(Drift).where(Drift.id.in_(lead_ids)).order_by(Drift.day.desc())))


# --- Rendu ----------------------------------------------------------------------------------------------


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def render_svg(nodes: list[AssetNode], plan: SitePlan | None, marks: PlanMarks | None = None,
               image_uri: str | None = None) -> str:
    """Plan 2D en SVG : fond (image ou trame), zones, équipements, compteurs, anomalies situées."""
    marks = marks or PlanMarks()
    aspect = plan.aspect_ratio if plan else DEFAULT_ASPECT
    height = round(WIDTH * aspect)
    parts = [f'<svg viewBox="0 0 {WIDTH} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
             f'aria-label="Plan 2D du site" style="width:100%;height:auto;border-radius:12px;'
             f'border:1px solid var(--color-border, #1f1538)">',
             f'<rect width="{WIDTH}" height="{height}" fill="var(--color-surface-2, #180c32)"/>']
    if image_uri:
        parts.append(f'<image href="{image_uri}" x="0" y="0" width="{WIDTH}" height="{height}" '
                     'preserveAspectRatio="none" opacity="0.92"/>')
    else:
        for i in range(1, 10):
            parts.append(f'<line x1="{i * WIDTH / 10:.0f}" y1="0" x2="{i * WIDTH / 10:.0f}" y2="{height}" '
                         'stroke="var(--color-border, #1f1538)" stroke-width="1"/>')
            parts.append(f'<line x1="0" y1="{i * height / 10:.0f}" x2="{WIDTH}" y2="{i * height / 10:.0f}" '
                         'stroke="var(--color-border, #1f1538)" stroke-width="1"/>')

    def px(value: float, total: float) -> float:
        return value / 100 * total

    for node in nodes:
        if node.kind != K.ZONE or node.plan_x is None or node.plan_w is None:
            continue
        hit = node.id in marks.zones
        color = "#f59e0b" if hit else ZONE_FILL.get(node.category, "#8b5cf6")
        title = "\n".join([node.name, *marks.notes.get(node.id, [])])
        x, y, w, h = px(node.plan_x, WIDTH), px(node.plan_y, height), px(node.plan_w, WIDTH), px(node.plan_h, height)
        parts.append(f'<g><title>{_e(title)}</title><rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" '
                     f'rx="8" fill="{color}" fill-opacity="{0.32 if hit else 0.16}" stroke="{color}" '
                     f'stroke-width="{4 if hit else 1.5}"/>'
                     f'<text x="{x + 10:.0f}" y="{y + 24:.0f}" fill="var(--color-text, #fff)" font-size="18" '
                     f'font-weight="600">{_e(node.name)}</text>'
                     + (f'<text x="{x + 10:.0f}" y="{y + 46:.0f}" fill="#f59e0b" font-size="15">Zone potentiellement '
                        'impactée</text>' if hit else "") + "</g>")
    for node in nodes:
        if node.kind not in (K.EQUIPMENT, K.METER) or node.plan_x is None:
            continue
        x, y = px(node.plan_x, WIDTH), px(node.plan_y, height)
        suspect = node.id in marks.suspects
        title = "\n".join([f"{node.name} ({assets.category(node).label})", *marks.notes.get(node.id, [])])
        if node.kind == K.METER:
            shape = (f'<rect x="{x - 11:.0f}" y="{y - 11:.0f}" width="22" height="22" rx="4" fill="#0c4a6e" '
                     'stroke="#38bdf8" stroke-width="2"/>')
        else:
            fill, stroke = ("#ef4444", "#fecaca") if suspect else ("#14532d", "#22c55e")
            pulse = ('<circle cx="{x:.0f}" cy="{y:.0f}" r="16" fill="none" stroke="#ef4444" stroke-width="3">'
                     '<animate attributeName="r" values="16;30;16" dur="1.6s" repeatCount="indefinite"/>'
                     '<animate attributeName="opacity" values="1;0;1" dur="1.6s" repeatCount="indefinite"/>'
                     '</circle>').format(x=x, y=y) if suspect else ""
            shape = pulse + (f'<circle cx="{x:.0f}" cy="{y:.0f}" r="13" fill="{fill}" stroke="{stroke}" '
                             'stroke-width="2.5"/>')
        label_color = "#fca5a5" if suspect else "var(--color-text-2, #d5d1f6)"
        parts.append(f'<g><title>{_e(title)}</title>{shape}<text x="{x:.0f}" y="{y + 32:.0f}" text-anchor="middle" '
                     f'fill="{label_color}" font-size="15" font-weight="{700 if suspect else 500}">{_e(node.name)}'
                     "</text></g>")
    parts.append("</svg>")
    return "".join(parts)


def unplaced(nodes: list[AssetNode]) -> list[AssetNode]:
    return [n for n in nodes if n.kind in (K.ZONE, K.EQUIPMENT, K.METER) and n.plan_x is None]
