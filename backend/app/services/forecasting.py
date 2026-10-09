"""Moteur de prévision de la consommation journalière (v2), fondé sur deux références.

1. Catalina, Virgone, Blanco (2009), « Création de modèles de régression pour prédire les consommations
   d'énergie des bâtiments à partir de simulations numériques », CIFQ, Lille (hal-00985331) :
   - les degrés-jours seuls surestiment le besoin, car ils ignorent les apports solaires et l'inertie ;
     d'où la température « sol-air » : T_sa = T_air + α·G / h_e, avec α = 0,6 et h_e = 23 W/m²·K ;
   - un modèle polynomial d'ordre 2 colle bien mieux à la physique qu'un modèle linéaire ;
   - un modèle se valide sur des cas qui n'ont pas servi à l'établir (écart moyen, écart maximal).
2. Paudel (2016), « Méthodologie pour estimer la consommation d'énergie dans les bâtiments en utilisant
   des techniques d'intelligence artificielle », thèse, École des Mines de Nantes (tel-01382882) :
   - l'inertie de l'enveloppe fait dépendre le besoin du climat des jours précédents : 1 à 2 jours
     pour un bâtiment conventionnel, 3 pour un bâtiment basse consommation (moyenne glissante) ;
   - les classes de fonctionnement du bâtiment (jours de la semaine qui se ressemblent) se déduisent
     des données ;
   - l'approche « jours pertinents » (n'apprendre que sur les 7 à 14 jours passés les plus semblables
     au jour à prévoir) est plus précise que l'apprentissage sur toutes les données.

Adaptation à EffiSmart :
- trois familles de modèles sont mises en concurrence : la signature linéaire v1 (référence), une
  signature d'ordre 2 et les jours pertinents, avec ou sans inertie (0 à 3 jours) et sol-air ;
- le choix se fait par **validation hors échantillon** : l'historique est découpé en périodes
  d'environ deux mois, chacune retirée de l'apprentissage puis prédite ; à précision équivalente
  (moins de 2 % d'écart), le modèle le plus simple l'emporte ;
- les modèles restent explicables (régressions, moyenne pondérée de jours analogues) plutôt qu'une
  boîte noire, car chaque prévision présente son raisonnement (principe P1). La thèse note d'ailleurs
  que, sur le bâtiment réel étudié, un SVM à noyau linéaire et les moindres carrés donnent des poids
  similaires.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cached_property

import numpy as np

from app.providers.weather import sol_air

COOLING_BASE = 22.0  # °C, au-delà : besoin de refroidissement (électricité)
INERTIA_CHOICES = (0, 1, 2, 3)  # jours précédents pris en compte, Paudel (2016)
NEIGHBOUR_CHOICES = (7, 10, 14)  # jours pertinents, Paudel (2016)
PARSIMONY = 1.02  # à 2 % près, le modèle le plus simple l'emporte
CLASS_TOLERANCE = 0.12  # deux jours de semaine à moins de 12 % de niveau forment une même classe
MIN_CLASS_DAYS = 20
MIN_SOLAR_COVERAGE = 0.9
WEEKDAY_NAMES = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
FAMILY_LABELS = {
    "v1": "signature linéaire v1 (DJU base 18 °C, ouvrés / week-end)",
    "poly2": "signature d'ordre 2 (Catalina et al., 2009)",
    "relevant": "jours pertinents (Paudel, 2016)",
}
FAMILY_RANK = {"v1": 0, "poly2": 1, "relevant": 2}


@dataclass(frozen=True)
class Config:
    family: str  # "v1" | "poly2" | "relevant"
    inertia: int = 0
    solar: bool = False
    neighbours: int = 0

    @property
    def complexity(self) -> tuple:
        return FAMILY_RANK[self.family], self.inertia, self.solar, -self.neighbours

    def describe(self) -> str:
        if self.family == "v1":
            return FAMILY_LABELS["v1"]
        parts = [FAMILY_LABELS[self.family],
                 f"inertie de {self.inertia} jour(s)" if self.inertia else "sans inertie",
                 "température sol-air" if self.solar else "température de l'air"]
        if self.family == "relevant":
            parts.append(f"{self.neighbours} jours analogues")
        return ", ".join(parts)


@dataclass
class Climate:
    """Températures (°C) et rayonnement global horizontal (W/m²) par jour."""

    temperature: dict[date, float]
    solar: dict[date, float] = field(default_factory=dict)
    # Profils déjà calculés : la validation croisée les redemande des centaines de milliers de fois. Un climat n'est
    # jamais modifié en place (`shifted` et les appelants en font une copie), la mémoire reste donc juste.
    _profiles: dict = field(default_factory=dict, repr=False, compare=False)
    _effective: dict = field(default_factory=dict, repr=False, compare=False)

    def air(self, day: date, solar: bool) -> float | None:
        t = self.temperature.get(day)
        if t is None or not solar:
            return t
        g = self.solar.get(day)
        # Température sol-air : effet combiné de l'air et du soleil sur l'enveloppe (Catalina et al., 2009).
        return None if g is None else sol_air(t, g)

    def profile(self, day: date, inertia: int, solar: bool) -> list[float] | None:
        """Le jour et ses `inertia` jours précédents (Paudel, 2016)."""
        key = (day, inertia, solar)
        if key not in self._profiles:
            values = [self.air(day - timedelta(days=k), solar) for k in range(inertia + 1)]
            self._profiles[key] = None if None in values else values
        return self._profiles[key]

    def effective(self, day: date, inertia: int, solar: bool) -> float | None:
        """Moyenne glissante de la température sur le jour et ses jours précédents (inertie)."""
        key = (day, inertia, solar)
        if key not in self._effective:
            values = self.profile(day, inertia, solar)
            self._effective[key] = None if values is None else sum(values) / len(values)
        return self._effective[key]

    def shifted(self, days: list[date], delta: float) -> Climate:
        """Même climat, températures décalées de `delta` °C sur les jours donnés (sensibilité)."""
        temperature = dict(self.temperature)
        for day in days:
            if day in temperature:
                temperature[day] += delta
        return Climate(temperature, self.solar)


# --- Classes de fonctionnement (Paudel, 2016) -------------------------------------------------------------


@dataclass(frozen=True)
class OperationClasses:
    groups: tuple[tuple[int, ...], ...]  # jours de semaine (0 = lundi) regroupés
    levels: dict[int, float] = field(default_factory=dict)  # niveau relatif à la moyenne de la semaine
    learned: bool = False

    @cached_property
    def _by_weekday(self) -> tuple[int, ...]:
        return tuple(next(i for i, group in enumerate(self.groups) if weekday in group) for weekday in range(7))

    def of(self, day: date) -> int:
        return self._by_weekday[day.weekday()]

    @staticmethod
    def _names(group: tuple[int, ...]) -> str:
        if len(group) > 2 and group == tuple(range(group[0], group[-1] + 1)):
            return f"{WEEKDAY_NAMES[group[0]]}–{WEEKDAY_NAMES[group[-1]]}"
        return ", ".join(WEEKDAY_NAMES[w] for w in group)

    def label_of(self, day: date) -> str:
        """Jours de la semaine de la classe du jour, ex. « lundi–vendredi » ou « samedi »."""
        return self._names(self.groups[self.of(day)])

    def describe(self) -> str:
        parts = []
        for group in self.groups:
            names = self._names(group)
            if self.levels:
                level = statistics.fmean(self.levels[w] for w in group)
                names += f" ({round(level * 100)} % de la moyenne hebdomadaire)"
            parts.append(names)
        return " ; ".join(parts)


DEFAULT_CLASSES = OperationClasses(((0, 1, 2, 3, 4), (5, 6)))


def learn_operation_classes(consumption: dict[date, float], min_class_days: int = MIN_CLASS_DAYS) -> OperationClasses:
    """Regroupe les jours de la semaine de niveau semblable.

    Niveau d'un jour = consommation du jour / moyenne de la semaine centrée sur ce jour : la saison et
    la météo s'annulent, il reste le rythme d'occupation. Médiane par jour de semaine, puis regroupement
    des niveaux proches (moins de 12 % d'écart : une anomalie récurrente ne crée pas de classe à elle seule).
    """
    ratios: dict[int, list[float]] = {w: [] for w in range(7)}
    for day, value in consumption.items():
        window = [consumption.get(day + timedelta(days=k)) for k in range(-3, 4)]
        if None in window:
            continue
        mean = sum(window) / 7
        if mean > 0:
            ratios[day.weekday()].append(value / mean)
    if any(len(r) < 3 for r in ratios.values()):
        return DEFAULT_CLASSES
    levels = {w: statistics.median(r) for w, r in ratios.items()}
    order = sorted(levels, key=levels.get)
    groups = [[order[0]]]
    for weekday in order[1:]:
        reference = statistics.fmean(levels[w] for w in groups[-1])
        if abs(levels[weekday] - reference) / reference <= CLASS_TOLERANCE:
            groups[-1].append(weekday)
        else:
            groups.append([weekday])
    classes = OperationClasses(tuple(tuple(sorted(g)) for g in sorted(groups, key=min)), levels, True)
    counts = [sum(1 for d in consumption if d.weekday() in g) for g in classes.groups]
    return classes if min(counts) >= min_class_days else DEFAULT_CLASSES


# --- Modèles ------------------------------------------------------------------------------------------------


class _V1Model:
    """Méthode v1 : conso = a + b × DJU (base fixe), jours ouvrés et week-end séparés, b ≥ 0."""

    def __init__(self, days: list[date], consumption: dict[date, float], climate: Climate, base: float) -> None:
        self.base = base
        self.coefficients: dict[bool, tuple[float, float]] = {}
        for weekend in (False, True):
            points = [(max(0.0, base - climate.temperature[d]), consumption[d]) for d in days
                      if (d.weekday() >= 5) == weekend and d in climate.temperature]
            if len(points) < 5:
                raise ValueError("historique insuffisant")
            xs, ys = [x for x, _ in points], [y for _, y in points]
            mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
            var_x = sum((x - mean_x) ** 2 for x in xs)
            slope = max(0.0, sum((x - mean_x) * (y - mean_y) for x, y in points) / var_x) if var_x >= 1.0 else 0.0
            self.coefficients[weekend] = (mean_y - slope * mean_x, slope)

    def predict(self, day: date, climate: Climate) -> float | None:
        t = climate.temperature.get(day)
        if t is None:
            return None
        intercept, slope = self.coefficients[day.weekday() >= 5]
        return max(0.0, intercept + slope * max(0.0, self.base - t))


class _Poly2Model:
    """Signature d'ordre 2 par classe : conso = β0 + β1·H + β2·H² (+ β3·R), sur la température effective.

    H = max(0, base − T_eff) (chauffage), R = max(0, T_eff − 22 °C) (refroidissement, électricité).
    """

    def __init__(self, config: Config, classes: OperationClasses, days: list[date], consumption: dict[date, float],
                 climate: Climate, base: float, cooling: bool) -> None:
        self.config, self.classes, self.base, self.cooling = config, classes, base, cooling
        rows: dict[int, list[tuple[list[float], float]]] = {}
        for day in days:
            features = self._features(day, climate)
            if features is not None:
                rows.setdefault(classes.of(day), []).append((features, consumption[day]))
        self.coefficients: dict[int, np.ndarray] = {}
        for cls in range(len(classes.groups)):
            data = rows.get(cls, [])
            if len(data) < 8:
                raise ValueError("classe trop peu fournie")
            x = np.array([f for f, _ in data])
            y = np.array([v for _, v in data])
            self.coefficients[cls] = np.linalg.lstsq(x, y, rcond=None)[0]

    def _features(self, day: date, climate: Climate) -> list[float] | None:
        t = climate.effective(day, self.config.inertia, self.config.solar)
        if t is None:
            return None
        heating = max(0.0, self.base - t)
        row = [1.0, heating, heating * heating]
        if self.cooling:
            row.append(max(0.0, t - COOLING_BASE))
        return row

    def predict(self, day: date, climate: Climate) -> float | None:
        features = self._features(day, climate)
        if features is None:
            return None
        return max(0.0, float(np.dot(self.coefficients[self.classes.of(day)], features)))


class _RelevantDaysModel:
    """Jours pertinents (Paudel, 2016) : pour chaque jour à prévoir, les jours appris de la même classe dont
    le profil climatique (le jour et ses jours précédents) est le plus proche ; régression linéaire locale
    pondérée par la proximité, ou moyenne pondérée si leurs températures sont trop semblables."""

    def __init__(self, config: Config, classes: OperationClasses, days: list[date], consumption: dict[date, float],
                 climate: Climate) -> None:
        self.config, self.classes = config, classes
        data: dict[int, list[tuple[list[float], float, float]]] = {}
        for day in days:
            profile = climate.profile(day, config.inertia, config.solar)
            if profile is not None:
                data.setdefault(classes.of(day), []).append((profile, sum(profile) / len(profile), consumption[day]))
        self.tables: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        for cls in range(len(classes.groups)):
            rows = data.get(cls, [])
            if len(rows) < config.neighbours:
                raise ValueError("classe trop peu fournie")
            profiles = np.array([p for p, _, _ in rows])
            mean, std = profiles.mean(axis=0), np.maximum(profiles.std(axis=0), 1e-6)
            self.tables[cls] = ((profiles - mean) / std, np.array([e for _, e, _ in rows]),
                                np.array([v for _, _, v in rows]), mean, std)

    def predict(self, day: date, climate: Climate) -> float | None:
        profile = climate.profile(day, self.config.inertia, self.config.solar)
        if profile is None:
            return None
        scaled, effective, values, mean, std = self.tables[self.classes.of(day)]
        target = (np.array(profile) - mean) / std
        distances = np.sqrt(((scaled - target) ** 2).sum(axis=1))
        nearest = np.argsort(distances)[: self.config.neighbours]
        weights = 1.0 / (distances[nearest] + 0.1)
        x, y = effective[nearest], values[nearest]
        estimate = float(np.average(y, weights=weights))
        if np.ptp(x) >= 1.0:
            design = np.column_stack([np.ones_like(x), x]) * np.sqrt(weights)[:, None]
            intercept, slope = np.linalg.lstsq(design, y * np.sqrt(weights), rcond=None)[0]
            local = float(intercept + slope * (sum(profile) / len(profile)))
            # Garde-fou : une régression locale n'extrapole pas loin des jours analogues.
            estimate = min(max(local, 0.5 * float(y.min())), 1.5 * float(y.max()))
        return max(0.0, estimate)

    def predict_many(self, days: list[date], climate: Climate) -> list[float | None]:
        """Même calcul que `predict`, jour par jour, mais les distances, le tri des jours analogues et la moyenne
        pondérée sont faits pour tous les jours d'une classe à la fois (validation croisée bien plus rapide)."""
        out: list[float | None] = [None] * len(days)
        by_class: dict[int, list[tuple[int, list[float]]]] = {}
        for index, day in enumerate(days):
            profile = climate.profile(day, self.config.inertia, self.config.solar)
            if profile is not None:
                by_class.setdefault(self.classes.of(day), []).append((index, profile))
        k = self.config.neighbours
        for cls, items in by_class.items():
            scaled, effective, values, mean, std = self.tables[cls]
            targets = (np.array([p for _, p in items]) - mean) / std
            distances = np.sqrt(((scaled[None, :, :] - targets[:, None, :]) ** 2).sum(axis=2))
            nearest = np.argsort(distances, axis=1)[:, :k]
            for row, (index, profile) in enumerate(items):
                chosen = nearest[row]
                weights = 1.0 / (distances[row, chosen] + 0.1)
                x, y = effective[chosen], values[chosen]
                estimate = float(np.average(y, weights=weights))
                if np.ptp(x) >= 1.0:
                    design = np.column_stack([np.ones_like(x), x]) * np.sqrt(weights)[:, None]
                    intercept, slope = np.linalg.lstsq(design, y * np.sqrt(weights), rcond=None)[0]
                    local = float(intercept + slope * (sum(profile) / len(profile)))
                    estimate = min(max(local, 0.5 * float(y.min())), 1.5 * float(y.max()))
                out[index] = max(0.0, estimate)
        return out


def fit(config: Config, classes: OperationClasses, days: list[date], consumption: dict[date, float],
        climate: Climate, *, base: float, cooling: bool):
    """Modèle ajusté, ou None si les données ne le permettent pas."""
    try:
        if config.family == "v1":
            return _V1Model(days, consumption, climate, base)
        if config.family == "poly2":
            return _Poly2Model(config, classes, days, consumption, climate, base, cooling)
        return _RelevantDaysModel(config, classes, days, consumption, climate)
    except (ValueError, np.linalg.LinAlgError):
        return None


# --- Validation hors échantillon -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Evaluation:
    config: Config
    cv_rmse: float  # erreur quadratique journalière / consommation moyenne, sur des jours non appris
    block_errors: tuple[float, ...]  # écart relatif sur le total de chaque période retirée

    @property
    def mean_block_error(self) -> float:
        return statistics.fmean(self.block_errors)

    @property
    def max_block_error(self) -> float:
        return max(self.block_errors)


def folds(days: list[date]) -> list[list[date]]:
    """Périodes contiguës d'environ deux mois (au moins trois)."""
    count = 6 if len(days) >= 180 else max(3, len(days) // 30)
    size = math.ceil(len(days) / count)
    return [days[i:i + size] for i in range(0, len(days), size)]


def evaluate(config: Config, classes: OperationClasses, days: list[date], consumption: dict[date, float],
             climate: Climate, blocks: list[list[date]], *, base: float, cooling: bool) -> Evaluation | None:
    squared, actual_total, predicted_days = 0.0, 0.0, 0
    block_errors = []
    for block in blocks:
        held = set(block)
        model = fit(config, classes, [d for d in days if d not in held], consumption, climate, base=base,
                    cooling=cooling)
        if model is None:
            return None
        predicted, actual = 0.0, 0.0
        estimates = (model.predict_many(block, climate) if hasattr(model, "predict_many")
                     else [model.predict(day, climate) for day in block])
        for day, estimate in zip(block, estimates):
            if estimate is None:
                continue
            squared += (estimate - consumption[day]) ** 2
            predicted += estimate
            actual += consumption[day]
            predicted_days += 1
        if actual > 0:
            block_errors.append(abs(predicted - actual) / actual)
        actual_total += actual
    if predicted_days < 0.9 * len(days) or actual_total <= 0 or not block_errors:
        return None  # le modèle ne couvre pas assez de jours (météo manquante, par exemple)
    rmse = math.sqrt(squared / predicted_days)
    return Evaluation(config, rmse / (actual_total / predicted_days), tuple(block_errors))


@dataclass
class Forecast:
    chosen: Evaluation
    model: object
    classes: OperationClasses
    by_family: dict[str, Evaluation]
    solar_available: bool
    validation_blocks: int
    block_days: int

    def predict(self, day: date, climate: Climate) -> float | None:
        return self.model.predict(day, climate)


def build_forecast(consumption: dict[date, float], climate: Climate, *, base: float,
                   cooling: bool) -> Forecast | None:
    """Compare les modèles candidats hors échantillon et ajuste le retenu sur tout l'historique."""
    days = sorted(consumption)
    if len(days) < 60:
        return None
    classes = learn_operation_classes(consumption)
    covered = sum(1 for d in days if d in climate.solar)
    solar_available = covered >= MIN_SOLAR_COVERAGE * len(days)
    solar_options = (False, True) if solar_available else (False,)
    candidates = [Config("v1")]
    candidates += [Config("poly2", u, s) for u in INERTIA_CHOICES for s in solar_options]
    candidates += [Config("relevant", u, s, n) for u in INERTIA_CHOICES for s in solar_options
                   for n in NEIGHBOUR_CHOICES]
    blocks = folds(days)
    evaluations = [e for c in candidates
                   if (e := evaluate(c, classes, days, consumption, climate, blocks, base=base, cooling=cooling))]
    if not evaluations:
        return None
    best = min(e.cv_rmse for e in evaluations)
    chosen = min((e for e in evaluations if e.cv_rmse <= best * PARSIMONY), key=lambda e: e.config.complexity)
    model = fit(chosen.config, classes, days, consumption, climate, base=base, cooling=cooling)
    if model is None:
        return None
    by_family: dict[str, Evaluation] = {}
    for evaluation in evaluations:
        current = by_family.get(evaluation.config.family)
        if current is None or evaluation.cv_rmse < current.cv_rmse:
            by_family[evaluation.config.family] = evaluation
    return Forecast(chosen, model, classes, by_family, solar_available, len(blocks), len(blocks[0]))
