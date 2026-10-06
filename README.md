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
l'interface Streamlit sur http://localhost:8501. Il démarre aussi l'API partenaires sur
http://localhost:8000/api/v1 (documentation interactive : http://localhost:8000/docs). Les données sont
stockées dans `backend/data/` (base SQLite, exports, documents déposés et clé de chiffrement
`secret.key`) ; supprimer ce dossier réinitialise la démonstration.

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
| `energie@clinique-du-parc.demo` | `demo1234` | Responsable énergie : valide ou écarte les sorties de la plateforme |
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
  app/providers/    EnergyDataProvider (contrat), Mock, Enedis Data Connect, GRDF ADICT, connecteur générique,
                    WeatherProvider (DJU : Mock ou Open-Meteo)
  app/services/     consentement, ingestion, dashboard, dérives, réglementaire, exports, documents, intégrations,
                    validation (principe P1), explications, recommandations, prévisions, graphe des équipements
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
  et refus global de toute méthode non-GET pour un `CLIENT_VIEWER` dans `get_current_user`. Seule exception :
  le responsable énergie peut valider ou écarter (`PATCH`) une anomalie, une recommandation ou une prévision
  de sa propre organisation (voir « Principe 1 »).
- **Dépôt de documents** (page « Documents ») : seule écriture ouverte au client — déposer des factures et des
  relevés pour sa propre organisation, et retirer un dépôt non encore traité. L'auditeur télécharge, marque
  « traité » ou « refusé » (motif obligatoire, visible par le client) ; chaque dépôt et chaque décision
  notifient l'autre partie. Contrôles au niveau service (`app/services/documents.py`) : périmètre via
  `TenantRepository`, formats PDF / PNG / JPEG / CSV / XLSX vérifiés par signature de contenu, 10 Mo par
  fichier (`EFFISMART_DOCUMENT_MAX_MB`), stockage sous nom aléatoire dans `backend/data/documents/`,
  doublons refusés (SHA-256). Disponible dans l'interface Python ; pas encore exposé par l'API REST.
- **Consentement** : vérifié au niveau service (`require_active_consent`) avant tout appel fournisseur ou
  lecture de courbe ; les agrégats n'incluent que les points consentis.
- **Choix du fournisseur** : `get_energy_provider(provider, fluid, delivery_point)` dans
  `app/providers/registry.py` renvoie le Mock, Enedis, GRDF ou le connecteur du point selon
  `DeliveryPoint.provider` ; le reste du code (ingestion, dérives, exports) ne change pas. Voir
  « Intégrations d'API » ci-dessous.

### Données mock

`MockDataProvider` est déterministe (même point + même jour ⇒ mêmes valeurs) : occupation 8 h–18 h 30 en semaine,
creux du week-end, talon de nuit, chauffage selon la température du `MockWeatherProvider` (sinusoïde calée sur
les normales de Paris-Montsouris, rayonnement solaire simulé).

Les points de démonstration ont une physique de bâtiment réaliste (`DEMO_BEHAVIOURS` dans
`app/providers/mock.py`), pour que le moteur de prévision ait quelque chose à découvrir :

| Site | Comportement simulé |
|---|---|
| Atelier central (Boulangeries Martin) | fours : chauffage seulement sous 15 °C ; inertie ~1 jour ; gaz : production le samedi, fermé le dimanche |
| Boutique Bellecour | vitrine plein sud (forts apports solaires), construction légère, fermée le lundi |
| Clinique du Parc | béton lourd (inertie ~3 jours), besoin de chauffage non linéaire, groupe froid au-delà de 21 °C |
| Entrepôt Genas | bardage métallique (peu d'inertie), chauffé hors gel sous 12 °C, réassort du lundi |
| Bureaux Part-Dieu | façades vitrées, pompe à chaleur réversible (climatisation au-delà de 22 °C), vendredi en télétravail |

Les autres points (créés par l'utilisateur, tests) gardent le comportement standard : DJU base 18 °C du jour.
Anomalies récurrentes de la démo (`DEFAULT_ANOMALIES`) :

| Point | Anomalie | Dérive attendue |
|---|---|---|
| 30001000000003 (Clinique) | groupe froid allumé le week-end (semaines ISO multiples de 3) | Talon anormal |
| 30001000000005 (Logistique) | pic de puissance l'après-midi (1 jour sur 11) | Dépassement de seuil |
| 30001000000006 (Logistique) | surconsommation en occupation (semaines multiples de 7) | Écart climatique |
| 21000000000008 (Clinique, gaz) | chaudière déréglée (semaines multiples de 8) | Écart climatique |

Le point 30001000000004 est volontairement **sans consentement** pour illustrer l'étape d'onboarding
(onglet « Patrimoine & consentements »).

## Principe 1 : la plateforme propose, un humain décide

Toute sortie algorithmique (anomalie, recommandation d'optimisation, prévision) est présentée avec son
**raisonnement**, son **gain estimé** et son **niveau de confiance**, et attend la **validation d'un humain**
avant d'être appliquée : l'auditeur partenaire ou le responsable énergie du client, jamais la plateforme
seule. L'auditeur vend son expertise ; l'outil l'outille sans décider à sa place, ce qui cadre aussi
l'exposition en responsabilité. Code : `app/services/validation.py`.

| Sortie | Produite par | Raisonnement | Gain estimé |
|---|---|---|---|
| Anomalie (dérive) | détecteurs F2a | donnée analysée, référence et méthode, écart, récurrence, équipements du compteur | excès du jour × occurrences par an (rythme observé sur 90 jours) |
| Recommandation | une anomalie **validée** + le graphe physique | équipements candidats, exclusions, correspondance de puissance, chaîne physique | gain de l'anomalie × part attribuable à l'équipement |
| Prévision | moteur v2 : modèles mis en concurrence, validés hors échantillon (voir « Moteur de prévision ») | classes de fonctionnement, modèles comparés, inertie, sol-air, mesuré, projeté, intervalle, comparaison à N-1 | écart projeté à l'année précédente |

- **Confiance** : départ à 50 points, chaque facteur l'ajuste d'un montant affiché (netteté de l'écart,
  complétude des données, stabilité de la référence, récurrence, correspondance de puissance, météo simulée…),
  borné entre 5 et 95 : jamais 100 %. Élevée ≥ 75, moyenne ≥ 50, faible en dessous.
- **Qui valide** : l'auditeur lié au client et le responsable énergie du client (case « Responsable énergie »
  dans « Patrimoine & consentements »). L'administrateur de la plateforme voit tout mais ne valide rien.
  Écarter exige un motif, conservé. Pas de validation en masse : chaque sortie se valide une à une.
- **Avant validation**, une sortie n'est vue que de ses valideurs : pas d'affichage aux autres comptes du
  client, pas de notification au client, pas de webhook, pas d'API partenaires. Le filtre est appliqué dans
  `TenantRepository` (donc dans chaque requête SQL), comme l'isolation entre clients.
- **Application** : la plateforme ne pilote aucun équipement. Une recommandation validée se déclare
  « appliquée » par un humain, après l'intervention réelle. Une anomalie validée déclenche une recommandation
  *proposée*, elle-même à valider ; si l'anomalie est ensuite écartée, sa recommandation encore non validée
  est retirée. Une prévision plus récente remplace une prévision non validée, jamais une prévision validée.
- **Interface** : page « À valider » (file triée par gain × confiance), pages « Dérives »,
  « Recommandations » et « Prévisions » ; le volet « Raisonnement, niveau de confiance et hypothèses » de
  chaque carte détaille le calcul et l'algorithme utilisé (nom et version, pour la traçabilité).
- **API** : `PATCH /api/drifts/{id}`, `/api/recommendations/{id}`, `/api/predictions/{id}` (auditeur ou
  responsable énergie) ; les réponses incluent `reasoning`, `confidence`, `confidence_factors`, `gain_*`.

### Moteur de prévision (v2)

Code : `app/services/forecasting.py`, appelé par `app/services/predictions.py`. Il s'appuie sur deux
références :

- **Catalina, Virgone, Blanco (2009)**, *Création de modèles de régression pour prédire les consommations
  d'énergie des bâtiments à partir de simulations numériques*, CIFQ, Lille (hal-00985331). Les degrés-jours
  seuls surestiment le besoin car ils ignorent soleil et inertie ; température **sol-air**
  T + 0,6 × rayonnement / 23 ; modèle **d'ordre 2** ; validation sur des cas non appris.
- **Paudel (2016)**, *Méthodologie pour estimer la consommation d'énergie dans les bâtiments en utilisant des
  techniques d'intelligence artificielle*, thèse, École des Mines de Nantes (tel-01382882). **Inertie** :
  le climat des 1 à 3 jours précédents compte ; **classes de fonctionnement** déduites des données ;
  **jours pertinents** : apprendre sur les 7 à 14 jours passés les plus semblables au jour à prévoir.

Fonctionnement, pour chaque site et énergie, sur les 365 derniers jours :

1. Classes de fonctionnement : niveau de chaque jour de la semaine rapporté à la moyenne de sa semaine,
   regroupement des jours à moins de 8 % d'écart (ex. « lundi–vendredi 118 % ; samedi, dimanche 55 % »).
2. Candidats : signature linéaire v1 (référence) ; signature d'ordre 2 (chauffage H et H², refroidissement
   pour l'électricité) ; jours pertinents (7, 10 ou 14 jours analogues, régression locale pondérée). Chacun
   avec une inertie de 0 à 3 jours et avec ou sans sol-air (si le rayonnement est disponible).
3. Validation hors échantillon : l'historique est coupé en 6 périodes d'environ deux mois ; chaque période
   est retirée de l'apprentissage puis prédite. Critère : erreur journalière CV(RMSE) ; à 2 % près, le
   modèle le plus simple l'emporte.
4. Projection du reste de l'année avec la météo normale ; intervalle = erreur de validation (plancher 3 %)
   et sensibilité au climat (normales ± 1,5 °C), combinées quadratiquement.

La carte de chaque prévision affiche le tableau des modèles testés et le modèle retenu. Les modèles restent
explicables (régressions, jours analogues) plutôt qu'un réseau de neurones ou un SVM : chaque prévision
doit présenter son raisonnement (principe 1). La thèse observe d'ailleurs que, sur le bâtiment réel étudié,
SVM linéaire et moindres carrés donnent des poids similaires.

**Sur les données de démonstration** (simulateur réaliste, voir « Données mock »), erreur journalière hors
échantillon :

| Site | Méthode v1 | Moteur v2 | Ce qu'il retient |
|---|---|---|---|
| Boutique Bellecour | 26 % | 0,6 % | sol-air (vitrine), fermeture du lundi |
| Atelier central, gaz | 35 % | 10 % | inertie 1 jour, samedi et dimanche distincts |
| Clinique, gaz | 30 % | 19 % | inertie 3 jours, sol-air |
| Bureaux Part-Dieu | 17 % | 14 % | inertie 2 jours, sol-air, vendredi distinct |
| Entrepôt Genas | 7 % | 6 % | inertie 1 jour |

L'électricité de la clinique reste à ~19 % : l'anomalie récurrente du groupe froid (week-ends) est
imprévisible par nature, et c'est au détecteur de dérives de la signaler. Les tests
`tests/test_forecasting.py` vérifient sur des données synthétiques que le moteur retrouve l'inertie, l'effet
solaire, les classes de fonctionnement ou une régulation en tout ou rien.

Le détecteur « écart climatique » des dérives utilise lui aussi les classes de fonctionnement apprises : un
samedi de production n'est plus comparé à un dimanche de fermeture (avant : +61 % et une fausse alerte chaque
samedi à l'atelier).

Rayonnement solaire : Open-Meteo (`shortwave_radiation_sum`) en production, valeurs simulées en démonstration.

## Principe 2 : un graphe physique des équipements

Page « Équipements » : les relations physiques réelles entre éléments d'un site, et non une arborescence
de rangement. Code : `app/services/assets.py`.

```
Compteur → alimente → Chaudière → produit → Eau chaude → alimente → CTA → dessert → Zone → accueille → Usage
```

- **Éléments** : compteur (créé avec chaque point de livraison), équipement (puissance nominale), fluide
  produit, zone (surface, occupée 24 h/24 ou non), usage.
- **Relations autorisées** : compteur → alimente → équipement (ou sous-compteur) ; équipement → produit →
  fluide ; fluide → alimente → équipement ; équipement → dessert → zone ; zone → accueille → usage. Toute
  autre relation est refusée comme physiquement incohérente, de même que les liens entre deux sites.
- **Un graphe, pas un arbre** : une CTA peut recevoir de l'eau chaude et de l'eau glacée, un usage peut être
  partagé par plusieurs zones, une boucle de récupération de chaleur est tolérée.
- **Pertinence physique** : une chaîne ne mène qu'aux usages que chacun de ses équipements peut servir (un
  groupe froid ne mène pas au chauffage, une CTA ne produit pas d'eau chaude sanitaire).
- **Usage par les recommandations** : l'équipement visé est choisi dans le graphe. Un talon anormal de
  52 kW le week-end désigne le groupe froid de 55 kW ; la salle serveurs, qui fonctionne en continu, est
  écartée, comme tout équipement qui ne dessert que des zones occupées 24 h/24. Pour un pic de puissance,
  c'est la hausse par rapport au pic habituel qui est comparée aux puissances nominales. Les maillons
  manquants sont signalés, car ils empêchent de localiser une anomalie.
- **Qui modifie** : l'auditeur ; l'espace client consulte. Démo : graphes de la Clinique du Parc et des deux
  sites de Logistique Rhône ; les Boulangeries Martin n'en ont pas, pour montrer une recommandation
  « identifier l'équipement en cause ». API : `GET /api/sites/{id}/assets`.

## Intégrations d'API

Page « Intégrations » de l'interface, en quatre onglets. Code : `app/services/integrations.py`,
`app/providers/`, `app/api/partner.py`.

### 1. Sources de données (administrateur)

| Source | Usage | Paramètres |
|---|---|---|
| Enedis Data Connect | courbe de charge élec. 30 min + contrat | client_id / client_secret, bac à sable ou production |
| GRDF ADICT | consommations gaz journalières + contrat | client_id / client_secret |
| Open-Meteo | températures réelles → DJU du détecteur climatique | latitude / longitude |

Seul l'administrateur active une source et saisit ses identifiants ; les auditeurs voient l'état en lecture
seule. Le bouton « Tester la connexion » vérifie une source sans l'activer. Un point de livraison utilise
Enedis ou GRDF quand on choisit cette source à sa création (« Patrimoine & consentements »).

- Les identifiants Enedis et GRDF s'obtiennent en signant les contrats partenaires de ces opérateurs.
  Les formats de réponse ont été codés d'après leur documentation publique, **sans compte réel** : à
  revalider dans le bac à sable Enedis et avec GRDF avant la production.
- **Ne pas activer Open-Meteo avec les données simulées** : les DJU réels ne correspondent pas à la météo
  simulée et produiraient de fausses dérives climatiques. Son offre gratuite est réservée à un usage non
  commercial ; prévoir l'offre payante en production.

### 2. Connecteurs (auditeur)

Pour toute autre API REST qui renvoie du JSON (GTB, compteur divisionnaire, plateforme IoT…).
L'auditeur décrit l'URL (`{ref}`, `{start}`, `{end}` remplacés à chaque appel), l'authentification (aucune,
clé dans un en-tête, Bearer, Basic), le chemin des relevés dans la réponse, les champs date et valeur,
l'unité (kWh, Wh, kW, W) et le pas (30 min ou jour). « Tester » affiche un aperçu des valeurs lues. Le
connecteur se choisit ensuite comme source d'un point de livraison ; la référence du point est alors
libre (32 caractères).

### 3. API partenaires (auditeur)

API en lecture seule pour les logiciels du cabinet ou de ses clients (ERP, BI…), sur
http://localhost:8000/api/v1 :

| Route | Contenu |
|---|---|
| `GET /organizations` | clients visibles par la clé |
| `GET /organizations/{id}/sites` | sites et points de livraison |
| `GET /organizations/{id}/consumption?start=&end=&granularity=day\|month` | kWh par période et par énergie (2 ans max.) |
| `GET /organizations/{id}/drifts?since=` | anomalies **validées**, avec raisonnement, gain et confiance |
| `GET /organizations/{id}/recommendations` | recommandations validées ou appliquées |
| `GET /organizations/{id}/predictions` | prévisions validées |

Authentification : en-tête `X-API-Key: esk_…` ou `Authorization: Bearer esk_…`. La clé est affichée une
seule fois à sa création ; seule son empreinte SHA-256 est enregistrée. Elle donne accès à tous les clients
du cabinet, ou à un seul si on la restreint, et se révoque à tout moment. Mêmes règles d'isolation que
l'application (`TenantRepository`) ; seuls les points consentis sont exposés ; 120 requêtes par minute et
par clé (`EFFISMART_PARTNER_API_RATE_LIMIT_PER_MINUTE`).

### 4. Webhooks (auditeur)

EffiSmart envoie un `POST` JSON vers l'URL choisie à chaque événement : `drift.validated`,
`recommendation.validated`, `recommendation.applied`, `prediction.validated` (principe 1 : seules les
sorties validées par un humain partent ; le rôle du valideur est transmis, jamais son identité) et
`document.deposited` (nouveau dépôt ; métadonnées seulement, jamais le fichier). Un abonnement à l'ancien
événement `drift.created` reçoit désormais `drift.validated`. En-têtes :
`X-EffiSmart-Event`, `X-EffiSmart-Delivery`, `X-EffiSmart-Timestamp` et
`X-EffiSmart-Signature: sha256=<HMAC-SHA256 de « horodatage.corps » avec le secret whsec_…>`. En cas
d'échec, nouvel essai après 1, 5, 30 puis 120 minutes (5 tentatives au total, `EFFISMART_WEBHOOK_MAX_ATTEMPTS`).
L'onglet affiche les derniers envois et un exemple de vérification de signature en Python.

### Sécurité

- Secrets (identifiants des sources, des connecteurs et des webhooks) chiffrés en base (Fernet). La clé vient
  de `EFFISMART_SECRET_KEY`, sinon du fichier `backend/data/secret.key` créé au premier lancement.
  **En production, définir `EFFISMART_SECRET_KEY`** et la sauvegarder : sans elle, les secrets enregistrés
  sont illisibles.
- Appels sortants : HTTPS uniquement, adresses publiques uniquement (pas de réseau interne ni de
  `localhost`), redirections refusées, délai maximal de 20 s (`EFFISMART_INTEGRATIONS_HTTP_TIMEOUT_S`).
- L'API partenaires tourne dans le serveur FastAPI, pas dans Streamlit : `lancer.py` la démarre à côté de
  l'interface (`python -m app.local_api` depuis `backend/`) ; en version Docker elle est servie par le backend.

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
9. **Exception à F5 (lecture seule client)** : ajout demandé après le brief — le client peut déposer des
   factures et des relevés (table `Document`, migration `0002_documents`). Les fichiers sont conservés tels
   quels : les relevés CSV/Excel ne sont pas encore importés comme mesures, l'auditeur les exploite à la main.
10. **Intégrations d'API** : ajout demandé après le brief (migration `0003_integrations`). Les sources réelles
    sont réglées pour toute la plateforme par l'administrateur ; connecteurs, clés d'API et webhooks
    appartiennent à chaque cabinet. Le consentement d'un point alimenté par un connecteur est celui recueilli
    dans EffiSmart (le connecteur n'a pas de mécanisme de consentement propre).
11. **Principe 1 (validation humaine)** : ajout demandé après le brief (migration `0004_validation_and_assets`).
    Les statuts d'une dérive gardent leurs valeurs (`OPEN` / `QUALIFIED` / `IGNORED`, compatibles avec l'API
    et le front React) mais s'affichent « À valider / Validée / Écartée ». Le responsable énergie est un
    compte client avec l'attribut `is_energy_manager`, et non un nouveau rôle : toutes les règles
    d'isolation restent inchangées. Les simples comptes client ne sont plus notifiés à la détection, mais à
    la validation. Les recommandations sont des règles d'expert ; les prévisions, un moteur de modèles
    explicables sélectionnés par validation (voir « Moteur de prévision »). La météo « normale » des
    prévisions (température, rayonnement) est simplifiée, à remplacer par des normales de station météo en
    production. Le front React n'affiche pas encore ces nouveautés.
12. **Principe 2 (graphe physique)** : les relations sont limitées à un même site ; une chaufferie commune à
    plusieurs bâtiments se modélise comme un site avec plusieurs zones. La correspondance entre équipement
    et usages (`RELEVANT_USAGES`) est une table d'expert, à compléter si de nouvelles catégories apparaissent.
13. **Moteur de prévision v2** (migration `0005_prediction_models`) : les classes de fonctionnement sont
    apprises sur tout l'historique avant la validation (choix structurel, peu sensible) ; les jours
    comportant une anomalie restent dans l'apprentissage (les exclure serait une amélioration ultérieure) ;
    le détecteur « écart climatique » garde sa régression N-1 sur les DJU du jour, mais par classe de
    fonctionnement apprise (sans inertie ni sol-air : une alerte doit rester simple à vérifier).
