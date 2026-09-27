# EffiSmart — V1

SaaS de suivi énergétique et de conformité réglementaire pour PME/ETI, piloté par l'auditeur énergétique.
Cette V1 implémente strictement le brief technique : F1 (dashboard), F2a (dérives), F3 (réglementaire),
F4 (exports OPERAT + VSME), F5 (espace client en lecture seule), sur données **mock** réalistes.

## Démarrage rapide : version 100 % Python (sans Docker)

Prérequis : Python 3.11 ou plus récent.

Double-cliquez sur **`Lancer-EffiSmart.bat`**, ou exécutez :

```bash
python lancer.py
```

Le script crée l'environnement `.venv`, installe les dépendances au premier lancement, puis ouvre
l'interface Streamlit sur http://localhost:8501. Les données sont stockées dans `backend/data/`
(base SQLite et exports) ; supprimer ce dossier réinitialise la démonstration.

Cette interface (`backend/effismart_ui.py`) appelle directement les services du backend : mêmes règles
métier, même isolation entre clients et mêmes rôles que l'API. Le job quotidien est remplacé par un
rattrapage automatique au démarrage et par le bouton « Mettre à jour les données ».

### Design de l'interface

- **Thème « Verre »** (par défaut) : design system généré par le skill UI/UX Pro Max pour un tableau de
  bord énergie SaaS (verre dépoli sombre, ardoise + vert, Fira Sans / Fira Code), contrastes vérifiés
  WCAG AA. Règles : `.claude/skills/design-system-effismart/SKILL.md` ; code : `backend/ui_theme.py`.
- **Thème « Nova »** (violet) toujours disponible : lancer avec `EFFISMART_THEME=nova`.
- **Visuels** (skill canvas-design) : affiche « Registre silencieux » et illustrations de l'interface,
  générées à partir des vraies données simulées — `docs/visuel/`. Pour les régénérer :
  `.venv\Scripts\python.exe docs\visuel\generer_visuels.py`.

## Démarrage : version complète (Docker)

Prérequis : Docker Desktop.

```bash
docker compose up --build
```

- Application : http://localhost:5173
- API (OpenAPI / Swagger) : http://localhost:8000/docs

Au premier démarrage, le backend applique les migrations, puis le seed génère ~21 mois de courbes
(compter 1 à 2 minutes avant que l'API réponde).

| Compte | Mot de passe | Rôle |
|---|---|---|
| `auditeur@effismart.demo` | `demo1234` | Auditeur (portefeuille de 3 clients) |
| `client@clinique-du-parc.demo` | `demo1234` | Espace client, lecture seule |
| `client@boulangeries-martin.demo` | `demo1234` | Espace client, lecture seule |
| `client@logistique-rhone.demo` | `demo1234` | Espace client, lecture seule |
| `admin@effismart.demo` | `demo1234` | Administrateur |

Réinitialiser la base de démo : `docker compose down -v` puis `docker compose up`.

### Tests

```bash
docker compose run --rm backend pytest
```

Les tests tournent sur SQLite en mémoire (aucune base à lancer). Ils couvrent en priorité
l'isolation multi-tenant (`test_isolation.py`) et les garde-fous de rôle (`test_roles.py`, qui parcourt
**toutes** les routes d'écriture : une nouvelle route non protégée fait échouer le test).

### Job quotidien

Le scheduler (APScheduler, dans le processus API) exécute chaque jour à 6 h (Europe/Paris) :
ingestion de la veille pour tous les points consentis → détection des dérives → statuts des échéances.
Lancement manuel : `docker compose exec backend python -m app.scheduler`.

## Architecture

```
backend/            FastAPI + SQLAlchemy 2 + Alembic
  app/providers/    EnergyDataProvider (contrat), MockDataProvider, stubs Enedis/GRDF, WeatherProvider (DJU)
  app/services/     consentement, ingestion, dashboard, dérives, réglementaire, exports
  app/repositories.py  TenantRepository : filtre d'isolation appliqué dans chaque requête SQL
  app/api/          routes HTTP (préfixe /api)
  alembic/          migrations (measurements = hypertable TimescaleDB)
  tests/
frontend/           React + TypeScript + Vite + Recharts (chaînes UI centralisées dans src/i18n)
docker/             Dockerfiles
```

- **Isolation** : toute lecture métier passe par `TenantRepository.org_clause()` ; une ressource hors périmètre
  répond 404 (indiscernable d'une ressource inexistante). L'accès d'un auditeur suit les liens datés
  `AuditorClientLink` en vigueur.
- **Lecture seule client** : double verrou côté API — dépendance `require_writer` sur chaque route d'écriture,
  et refus global de toute méthode non-GET pour un `CLIENT_VIEWER` dans `get_current_user`.
- **Consentement** : vérifié au niveau service (`require_active_consent`) avant tout appel fournisseur ou
  lecture de courbe ; les agrégats n'incluent que les points consentis.
- **Brancher Enedis / GRDF** (jalon 8) : implémenter `app/providers/enedis.py` / `grdf.py`, puis changer
  `DeliveryPoint.provider`. Aucun autre code ne change.

### Données mock

`MockDataProvider` est déterministe (même point + même jour ⇒ mêmes valeurs) : occupation 8 h–18 h 30 en semaine,
creux du week-end, talon de nuit, chauffage proportionnel aux DJU du `MockWeatherProvider` (sinusoïde calée sur
les normales de Paris-Montsouris, base 18 °C). Anomalies récurrentes de la démo (`DEFAULT_ANOMALIES`) :

| Point | Anomalie | Dérive attendue |
|---|---|---|
| 30001000000003 (Clinique) | groupe froid allumé le week-end (semaines ISO multiples de 3) | Talon anormal |
| 30001000000005 (Logistique) | pic de puissance l'après-midi (1 jour sur 11) | Dépassement de seuil |
| 30001000000006 (Logistique) | surconsommation en occupation (semaines multiples de 7) | Écart climatique |
| 21000000000008 (Clinique, gaz) | chaudière déréglée (semaines multiples de 8) | Écart climatique |

Le point 30001000000004 est volontairement **sans consentement** pour illustrer l'étape d'onboarding
(onglet « Patrimoine & consentements »).

## Décisions ouvertes : valeurs par défaut appliquées (brief §7)

Toutes sont dans `backend/app/config.py`, surchargeables par variable d'environnement `EFFISMART_*`.

| Sujet | Défaut V1 |
|---|---|
| Site facturable | 1 site = 1 PRM principal (`DeliveryPoint.is_primary`) ; aucune facturation codée |
| Ordre des exports | OPERAT puis VSME (`export_formats_priority`) |
| Source DJU | `MockWeatherProvider`, documenté, derrière `WeatherProvider` |
| Facteurs d'émission | Seed ADEME Base Empreinte horodaté (élec. 0,052 ; gaz 0,227 kgCO₂e/kWh), **à vérifier** |
| Passage en « échéance proche » | J-60 (`due_soon_days`) |
| Échéance OPERAT | 30 septembre (`operat_due_month/day`) |

## Points ambigus : choix faits et signalés

1. **Qui génère les exports ?** F4 est destinée au client, mais F5 interdit toute écriture au client.
   Choix : l'auditeur génère (création d'un `ExportJob`), le client télécharge. À confirmer.
2. **Authentification** : le brief place `password_hash` sur `Auditor` *et* sur `User`. Seul `User` porte les
   identifiants ; `Auditor` représente le cabinet (tenant).
3. **Historique du seed** : ~21 mois au lieu de 12 pour disposer de l'année civile N-1 (export OPERAT) et de la
   période N-1 nécessaire au détecteur climatique. Réglable via `EFFISMART_BACKFILL_DAYS`.
4. **Courbe sur 12 mois** : au-delà de 31 jours, la courbe élec. est agrégée au jour (lisibilité et volume) ;
   le pas 30 min est conservé sur 7 j / 30 j.
5. **Seuil de puissance par défaut** : 90 % de la puissance souscrite, avec l'hypothèse kVA ≈ kW (cos φ ≈ 1).
   Seuil personnalisable par point (`power_threshold_kw`, `daily_threshold_kwh` pour le gaz).
6. **Échéance dépassée** : le statut reste `DUE_SOON` (l'énumération du brief n'a pas d'état « en retard ») ;
   l'UI affiche « En retard ».
7. **Chiffrement au repos** : à assurer au niveau infrastructure (volume chiffré / base managée chiffrée),
   non géré par le code. Aucune valeur de consommation n'est écrite dans les logs applicatifs.
8. **Notifications** : table `Notification` ajoutée (non listée au §4) pour matérialiser « chaque dérive crée
   une notification » ; affichées dans l'application, pas d'envoi d'e-mail en V1.
