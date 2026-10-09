"""Mémoire des calculs coûteux : chaque clic de l'interface ne recalcule pas ce qui n'a pas changé.

Streamlit réexécute toute la page à chaque interaction. Les calculs lourds (chiffrage au pas 30 min, agrégats
journaliers, courbes de puissance) sont donc mémorisés, repérés par leurs arguments ET par une empreinte des
données qu'ils lisent : nombre de mesures, dernière mesure et somme des valeurs, factures, contrats, consentement.
Une nouvelle mesure, une correction, une facture ou un contrat change l'empreinte : le calcul est refait. Une valeur
mémorisée n'est donc jamais périmée ; la mémoire ne fait qu'éviter de refaire un calcul identique.

Mémoire en processus, par espace (une taille par espace, la plus ancienne entrée sortant en premier), protégée par un
verrou : les sessions Streamlit s'exécutent dans des fils différents.
"""
from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import date
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DeclaredConsumption, Measurement, SupplyContract
from app.timeutils import local_day_bounds

T = TypeVar("T")
SIZES = {"costing": 256, "daily": 256, "powers": 24, "forecast": 64}  # courbes 30 min et modèles : peu d'entrées
_stores: dict[str, OrderedDict] = {name: OrderedDict() for name in SIZES}
_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0}


def cached(space: str, key: tuple, compute: Callable[[], T], *, copy_result: bool = True) -> T:
    """Valeur mémorisée pour `key` dans l'espace `space`, sinon calculée puis mémorisée. Le résultat est copié : un
    appelant qui le modifie n'altère pas la mémoire."""
    store = _stores[space]
    with _lock:
        if key in store:
            store.move_to_end(key)
            _stats["hits"] += 1
            value = store[key]
            return copy.deepcopy(value) if copy_result else value
        _stats["misses"] += 1
    value = compute()  # hors verrou : deux calculs simultanés identiques sont sans conséquence
    with _lock:
        store[key] = value
        store.move_to_end(key)
        while len(store) > SIZES[space]:
            store.popitem(last=False)
    return copy.deepcopy(value) if copy_result else value


def clear() -> None:
    with _lock:
        for store in _stores.values():
            store.clear()
        _stats.update(hits=0, misses=0)


def stats() -> dict[str, Any]:
    with _lock:
        return {**_stats, **{space: len(store) for space, store in _stores.items()}}


# --- Empreintes des données ------------------------------------------------------------------------------------


def measurement_stamp(db: Session, delivery_point_id: int, start: date, end: date) -> tuple:
    """Mesures d'un point sur une période : nombre, dernier horodatage, somme (une correction de valeur la change)."""
    t0, t1 = local_day_bounds(start, end)
    count, last, total = db.execute(select(func.count(), func.max(Measurement.time), func.sum(Measurement.value_kwh))
                                    .where(Measurement.delivery_point_id == delivery_point_id,
                                           Measurement.time >= t0, Measurement.time < t1)).one()
    return int(count or 0), str(last), round(float(total or 0.0), 3)


def declared_stamp(db: Session, delivery_point_id: int) -> tuple:
    count, last, total = db.execute(select(func.count(), func.max(DeclaredConsumption.id),
                                           func.sum(DeclaredConsumption.kwh))
                                    .where(DeclaredConsumption.delivery_point_id == delivery_point_id)).one()
    return int(count or 0), last, round(float(total or 0.0), 3)


def contracts_stamp(contracts: list[SupplyContract]) -> tuple:
    """Contenu complet des grilles tarifaires d'un point (peu de lignes) : toute modification change l'empreinte."""
    return tuple((c.id, c.valid_from, c.option, c.subscription_eur_month, c.price_base, c.price_hp, c.price_hc,
                  c.offpeak_start_hour, c.offpeak_end_hour, repr(sorted((c.tempo_prices or {}).items())),
                  c.dynamic_margin_eur_kwh) for c in contracts)
