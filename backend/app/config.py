"""Configuration de l'application.

Toutes les valeurs sont surchargeables par variable d'environnement préfixée
``EFFISMART_`` (ex. ``EFFISMART_DUE_SOON_DAYS=90``).

Les valeurs par défaut des décisions ouvertes du brief (§7) sont regroupées
dans une section dédiée pour être triviales à modifier.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EFFISMART_", extra="ignore")

    # --- Infrastructure -------------------------------------------------
    database_url: str = "postgresql+psycopg://effismart:effismart@localhost:5432/effismart"
    jwt_secret: str = "dev-secret-a-changer-en-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 12 * 60
    enable_scheduler: bool = True
    daily_job_hour: int = 6
    export_dir: str = "./data/exports"
    timezone: str = "Europe/Paris"
    cors_origins: list[str] = ["http://localhost:5173"]

    # --- Données énergie ------------------------------------------------
    # Historique récupéré lors de l'octroi d'un consentement (~21 mois, dans la
    # limite des 24 mois Enedis) : couvre l'année civile N-1 (export OPERAT) et
    # la période N-1 nécessaire au détecteur « écart climatique ».
    backfill_days: int = 640
    # Au-delà de cette durée, la courbe élec. est agrégée au jour (lisibilité).
    load_curve_max_raw_days: int = 31

    # --- Décisions ouvertes (brief §7) ------------------------------------
    # D5 : 1 site facturé = 1 point de livraison principal (is_primary).
    billing_site_definition: str = "PRIMARY_DELIVERY_POINT"
    # D9 : ordre de production des exports (OPERAT d'abord : obligation légale).
    export_formats_priority: list[str] = ["OPERAT", "VSME"]
    # Prix moyens utilisés pour le « coût estimé » (hors abonnement, TTC indicatif).
    estimated_price_elec_eur_kwh: float = 0.20
    estimated_price_gas_eur_kwh: float = 0.11

    # --- F3 : suivi réglementaire ----------------------------------------
    due_soon_days: int = 60
    operat_due_month: int = 9
    operat_due_day: int = 30

    # --- F2a : détection de dérives --------------------------------------
    threshold_ratio_of_subscribed_power: float = 0.9
    climate_deviation_tolerance: float = 0.20
    climate_regression_window_days: int = 28
    baseload_tolerance: float = 0.40
    baseload_reference_days: int = 28
    inactive_night_start_hour: int = 22
    inactive_night_end_hour: int = 6
    dju_base_temperature: float = 18.0
    notify_clients_on_drift: bool = True
    # Nombre de jours analysés rétroactivement après un nouveau consentement / au seed.
    detection_backfill_days: int = 45

    # --- Mock -----------------------------------------------------------
    mock_anomalies_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
