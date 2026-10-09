# EffiSmart — V1

SaaS de suivi énergétique et de conformité réglementaire pour PME/ETI, piloté par l'auditeur énergétique :
**suivi J+1 sur données réseau certifiées** (courbe de charge au pas de 30 min, détection quotidienne des dérives).
Version 1 : le brief technique et le périmètre F1–F5 de l'étude de marché : F1 (tableau de bord),
F2 (alertes à deux étages : F2a seuils et dérive, F2b anomalies contextualisées), F3 (réglementaire),
F4 (données énergie RSE : OPERAT + VSME), F5 (espace client en lecture seule). Version 2 : F6 à F12 (suivi
tri-axe, IPE ISO 50001, plan 2D, simulateur, rapport trimestriel assisté, prédiction et trajectoire Décret
Tertiaire, optimisation tarifaire en mode conseil). Le tout sur données **mock** réalistes.

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

### Traitement quotidien

Le scheduler (APScheduler, dans le processus API) exécute chaque jour à **17 h** (Europe/Paris), après la
publication par Enedis de la courbe de charge de la veille (entre 12 h et 16 h) :
- **Ingestion** : la veille pour l'électricité ; pour le gaz, publié à J+1 ou J+2, la veille et l'avant-veille,
  relues pour compléter un jour encore absent la veille.
- **Détection quotidienne des dérives**, sur la veille et l'avant-veille (sans doublon).
- **Échéances**, puis projections, trajectoires, décalages de charge, données RSE et rapports trimestriels.

Lancement manuel : `docker compose exec backend python -m app.scheduler`. Horaire réglable
(`EFFISMART_DAILY_JOB_HOUR`).

### Vocabulaire : la donnée la plus fraîche est celle de la veille

La courbe de charge de la veille est disponible chaque jour entre 12 h et 16 h ; le gaz à J+1 ou J+2. Un suivi à
la seconde suppose des capteurs sur site (IoT) ; le pas de 30 min suffit à tout ce que promet la V1. Parler de
« suivi J+1 sur données réseau certifiées » est plus crédible face à un jury technique.

| À bannir | À employer |
|---|---|
| « supervision en temps réel » | « suivi quotidien » / « données J+1 » |
| « alertes en temps réel » | « détection quotidienne des dérives » |
| « pilotage instantané » | « analyse de la courbe de charge au pas 30 min » |
| « pilotage automatique » (décision D3) | « recommandation de décalage de charge » |

Les textes de référence sont dans `backend/app/vocabulary.py`. `tests/test_vocabulary.py` fait échouer la suite
de tests si un texte de l'application (services, interface Streamlit, front React) emploie une expression à
bannir.

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
| Entrepôt Genas | bardage métallique (peu d'inertie), chauffé hors gel sous 12 °C, réassort du lundi ; éclairage resté allumé la nuit et le week-end du 3/11/2025 au 5/04/2026, corrigé le 6/04 (scénario « avant / après ») |
| Bureaux Part-Dieu | façades vitrées, pompe à chaleur réversible (climatisation au-delà de 22 °C), vendredi en télétravail |

Les autres points (créés par l'utilisateur, tests) gardent le comportement standard : DJU base 18 °C du jour.
Anomalies récurrentes de la démo (`DEFAULT_ANOMALIES`) :

| Point | Anomalie | Dérive attendue |
|---|---|---|
| 30001000000003 (Clinique) | groupe froid en marche le week-end de 6 h à 22 h (semaines ISO multiples de 3) | Consommation en inoccupation |
| 30001000000005 (Logistique) | pic de puissance l'après-midi (1 jour sur 11) | Dépassement de seuil |
| 30001000000006 (Logistique) | surconsommation en occupation (semaines multiples de 7) | Écart climatique |
| 30001000000006 (Logistique) | éclairage des bureaux resté allumé la nuit (semaines multiples de 5) | Talon de nuit anormal |
| 21000000000008 (Clinique, gaz) | chaudière déréglée (semaines multiples de 8) | Écart climatique |

Le point 30001000000004 est volontairement **sans consentement** pour illustrer l'étape d'onboarding
(onglet « Patrimoine & consentements »).

## Périmètre F1–F5 confirmé par l'étude de marché

| Code | Fonctionnalité | Bénéficiaire | Données | Dans EffiSmart |
|---|---|---|---|---|
| F1 | Tableau de bord de pilotage : consommations, indicateurs de performance, évolution avant / après recommandations | Auditeur + client | N1 (N0 dégradé) | « Tableau de bord » : KPI, courbe, mois ; intensité kWh/m², €/m², kgCO₂e/m² sur 12 mois ; section « Avant / après recommandations » |
| F2a | Alertes : seuils et dérive (V1), purement statistiques | Auditeur + client | N1 | détection quotidienne des dérives, 4 détecteurs ; alerte dans la cloche **et par e-mail** dès la détection (valideurs), puis à la validation (tout le client) |
| F2b | Anomalies contextualisées (V2) : qualification et propagation d'impact par le graphe P2 | Auditeur + client | N1 + graphe | équipement suspect, zones et usages potentiellement impactés, sur chaque anomalie, dans l'alerte et l'e-mail |
| F3 | Suivi réglementaire : calendrier, rappels d'échéances, historique des actions | Auditeur | N0 | « Réglementaire » ; **rappels automatiques** à J-60, J-30, J-7, le jour J, puis en retard (cloche et e-mail) |
| F4 | Données énergie pour les rapports RSE, produites sans ressaisie | Client | N0 / N1 | « Données RSE » : OPERAT et VSME B3 **produits automatiquement** (année écoulée, année en cours chaque mois) |
| F5 | Espace client : tableau de bord en lecture seule, suivi en autonomie | Client | N1 | espace client (lecture seule ; le responsable énergie valide en plus) |

EffiSmart fournit la **brique énergie** des rapports RSE ; il ne produit pas le rapport de durabilité complet,
dont les volets social et gouvernance sont hors de son périmètre. La génération le dit dans chaque jeu
(`app/services/exports.py`) :

- **fichiers** `donnees_energie_operat.json` / `.csv` et `donnees_energie_vsme_b3.json` / `.csv`, jamais
  « rapport » ;
- **périmètre** écrit dans chaque JSON, avec ce qui reste à l'entreprise : pour le VSME, le module B3 est
  fourni et les modules B1, B2, B4 à B7 (environnement hors énergie), B8 à B10 (social) et B11 (gouvernance)
  sont listés « à compléter par l'entreprise » ; pour OPERAT, les catégories d'activité et indicateurs
  d'intensité d'usage restent à saisir par l'assujetti sur la plateforme ;
- **LISEZMOI.txt** dans chaque archive : périmètre, fichiers, origine des données (N1 / N0), facteurs
  d'émission, ce qui reste à compléter ;
- **gabarit versionné** (`format_version`) : quand il évolue, les jeux automatiques sont produits de nouveau ;
  les versions remplacées restent conservées, seule la plus récente est proposée au téléchargement.

**Niveaux de données** (`app/services/energy_data.py`) :

- **N1** : compteurs communicants (Linky, Gazpar, connecteurs), courbe 30 min ou relevé journalier, avec
  consentement ;
- **N0** : factures et relevés d'index. L'auditeur les saisit, ou importe un tableur CSV
  (`point;debut;fin;kwh;montant`), depuis la page « Documents », souvent à partir d'un dépôt du client.

Jour par jour, la mesure N1 prime ; un jour sans mesure reçoit sa part des périodes N0, au prorata des jours.
En mode dégradé (N0 seul), le tableau de bord, les indicateurs et les données RSE fonctionnent, mais sans
courbe de charge, sans détection d'anomalies et sans prévision journalière. Chaque fichier RSE indique la part
issue des factures.

**Avant / après recommandations** (`app/services/savings.py`, méthode inspirée de l'IPMVP option C) :

- **période de référence** : depuis la première anomalie validée qui a motivé l'action ;
- **modèle de référence** : le moteur de prévision v2, appris sur cette seule période ;
- **suivi** : à partir du lendemain de l'application déclarée, au moins 14 jours ;
- **calcul** : économie = consommation attendue sans l'action (météo réelle) − consommation mesurée ;
- **contrôle** : si le suivi sort de la plage de températures de la référence, la plateforme signale
  l'extrapolation et baisse sa confiance.

Comme toute sortie algorithmique (principe 1), la mesure n'est visible du client qu'une fois validée, figée
telle que validée. Démo : éclairage de l'entrepôt de Genas, 56 MWh économisés en 6 mois (−14 %).

**F2 : alertes à deux étages**. « Alertes automatiques » et « alertes sur les anomalies » sont une même
fonctionnalité à deux niveaux de maturité.

*F2a, seuils et dérive* (`app/services/drift.py`) : quatre détecteurs purement statistiques, sans IA,
entièrement faisables en N1.

| Détecteur | Mesure | Référence |
|---|---|---|
| Dépassement de seuil | pic de puissance (élec.) ou consommation du jour (gaz) | 90 % de la puissance souscrite, ou seuil fixé par point |
| Écart à l'historique corrigé du climat | consommation du jour | même période N-1, régression sur les DJU, jours de la même classe de fonctionnement |
| Talon de nuit anormal | puissance moyenne de 22 h à 6 h | médiane des nuits des 28 jours précédents |
| Consommation en période d'inoccupation | puissance moyenne hors nuit : le week-end de 6 h à 22 h ; les jours ouvrés, avant 7 h et après 20 h | talon de nuit **du jour même** + écart habituel des jours comparables |

Partir du talon du jour évite de signaler deux fois un même excès : un équipement resté en marche nuit et
jour relève du talon de nuit ; un équipement qui ne tourne qu'en journée le week-end relève de l'inoccupation.
Horaires réglables (`EFFISMART_OCCUPANCY_START_HOUR`, `EFFISMART_OCCUPANCY_END_HOUR`, `EFFISMART_INACTIVE_NIGHT_*`).

*F2b, anomalies contextualisées* (`app/services/anomaly_context.py`) : l'anomalie est replacée dans le graphe
physique du site (principe 2).

1. **Qualification** : nature probable (équipement resté en marche la nuit, fonctionnement hors occupation,
   dérive de régulation, appel de puissance) et équipement suspect. Les règles physiques sont celles des
   recommandations : catégorie, puissance nominale comparée à l'excès, zones occupées 24 h/24, saison. La
   recommandation visera donc l'équipement suspect.
2. **Propagation d'impact** : depuis l'équipement suspect, la plateforme suit les relations en aval
   (produit → alimente → dessert → accueille) : « Dérive détectée sur « Chaudière » ; zones potentiellement
   impactées : Atelier 2, Bureaux R+1. »
3. **Anomalies à examiner ensemble** : deux anomalies du même site, à un jour près, qui touchent les mêmes
   zones ou le même équipement sont rapprochées (une cause commune est possible, pas certaine).

*Une cause, une alerte* (`app/services/alert_groups.py`). Une nouvelle anomalie se rattache à une alerte encore
à valider quand elles ont vraisemblablement la même cause :

| Règle | Exemple |
|---|---|
| répétition : même compteur, même type, à moins de 30 jours | talon de nuit sept nuits de suite : une alerte, sept occurrences |
| excès expliqué : même compteur, même jour, l'excès de la journée couvert à 50 % au moins par une anomalie de plage horaire | groupe froid en marche le samedi : l'écart climatique du samedi rejoint l'alerte « inoccupation » |
| même équipement suspect (F2b), à un jour près | deux compteurs dont l'anomalie désigne la même chaudière |

Seule la **plus ancienne** est signalée (alerte, e-mail, file de validation) ; le même jour, les détecteurs de
plage horaire passent avant celui de la journée, plus global. Les suivantes mettent à jour l'alerte, sans nouvel
e-mail, et l'alerte dit pourquoi elle regroupe : « 7 alertes regroupées en une seule, du 24/08 au 30/08/2026.
Pourquoi ce regroupement : le problème se répète (même compteur, même type d'anomalie) ; une seule décision vaut
pour tout le groupe. » Comme toute proposition de la plateforme (principe 1), le regroupement se contrôle : la
décision humaine porte sur tout le groupe, et un valideur peut détacher une anomalie sans rapport
(`POST /api/drifts/{id}/detach`). Une alerte déjà décidée ne reçoit plus d'occurrences : la suivante ouvre une
nouvelle alerte. Deux causes distinctes sur un même compteur restent séparées (bureaux : éclairage la nuit,
160 kWh, et pompe à chaleur déréglée le jour, 800 kWh).

Le contexte figure sur la carte de l'anomalie (avec le graphe surligné), dans son raisonnement, dans l'alerte
et l'e-mail, dans les webhooks et l'API (`context`, et `grouped_with_id`, `group_drift_ids` pour les groupes). C'est une hypothèse (principe 1) : elle suit le graphe tant
que l'anomalie est à valider, puis elle est figée telle que validée. Sans graphe derrière le compteur,
l'anomalie reste signalée par F2a mais « non localisée ».

**E-mails** (`app/services/mailer.py`) :

- **serveur** : SMTP réglé par l'administrateur (« Intégrations », « Envoi des e-mails ») ;
- **sans serveur** : en version locale, chaque e-mail est écrit dans `backend/data/outbox/` (fichier .eml) ;
- **préférence** : chaque utilisateur coupe ses e-mails dans « Mon compte », l'alerte reste dans la cloche ;
- **envoi** : file d'envoi avec réessais, traitée à chaque alerte et chaque minute par le planificateur.

## Version 2 : fonctionnalités avancées (F6 à F12)

| Code | Fonctionnalité | Données | Dans EffiSmart |
|---|---|---|---|
| F6 | Suivi mensuel tri-axe : consommations, émissions, coûts | N1 (+ N0) | page « Suivi mensuel » ; contrats saisis dans « Patrimoine & consentements » |
| F7 | IPE ISO 50001 : kWh/m², kWh/unité produite, kWh/ETP, kWh/DJU | N1 + variables du client | page « Performance (IPE) » |
| F8 | Visualisation spatiale : plan 2D (la maquette 3D est reportée en V3) | N1 + P2 | onglet « Plan 2D » de « Équipements », et carte de chaque anomalie |
| F9 | Simulateur d'économies | N1 | page « Simulateur » ; bibliothèque de gestes types |
| F10 | Générateur de rapport trimestriel assisté (D2, option A) | N1 | page « Rapports trimestriels » |
| F11 | Prédiction des consommations : modèle mathématique renforcé par l'apprentissage | N1, 12 à 24 mois | détecteur « écart au modèle » (F2) ; page « Trajectoire 2030 » ; simulateur |
| F12 | Optimisation tarifaire, mode conseil (D3) | N1 + contrat | recommandations « décalage de charge » |

**F6 — suivi mensuel tri-axe** (`app/services/tariffs.py`). Chaque point de livraison peut avoir un contrat de
fourniture, avec une grille tarifaire à prix complets (fourniture, acheminement, taxes) :
- **Base** : un prix unique ;
- **heures pleines / creuses** : plage creuse réglable ;
- **Tempo** : prix par couleur de jour et par plage ;
- **dynamique** : prix horaire du marché plus une marge.

Chaque demi-heure mesurée est chiffrée au prix de sa plage, et l'abonnement est réparti au prorata des jours.
Les factures (N0) sont chiffrées au prix moyen du contrat. Sans contrat, le prix indicatif est signalé comme tel.
Émissions : facteurs ADEME de la Base Empreinte (D6). Démonstration : couleurs Tempo simulées (les jours les
plus froids) et prix spot simulés ; en production, signal RTE et prix de marché à brancher.

**F7 — IPE ISO 50001** (`app/services/ipe.py`). Chaque indicateur est calculé sur 12 mois et comparé à la
situation énergétique de référence (SER) du site, une année choisie :
- **kWh/m²** : consommation rapportée à la surface ;
- **kWh par unité produite et kWh/ETP** : les variables d'ajustement mensuelles, saisies par le responsable
  énergie du client ou par l'auditeur ;
- **kWh/DJU** : la consommation liée au chauffage (mois de chauffe moins le niveau des mois d'été), rapportée
  aux degrés-jours.

**IPE personnalisés** (`app/services/ipe_definitions.py`). L'auditeur ou le responsable énergie crée ses
propres IPE :
- **énergie** : toutes énergies, électricité ou gaz ;
- **facteur** : surface, degrés-jours de chauffage ou de froid, jours ouvrés, production, effectif, ou une
  **variable personnalisée** du site (repas servis, nuitées, heures d'ouverture…) dont il saisit les valeurs
  mensuelles ;
- **forme** :
  - un **ratio** (kWh par unité) ;
  - un **IPE modélisé, base 100** (ISO 50006) : consommation mesurée ÷ consommation attendue par une régression
    sur un ou deux facteurs, apprise sur l'année de référence ; sous 100, la performance s'améliore.

Un IPE créé par un humain est suivi d'emblée. Un simple compte client consulte seulement.

**IPE proposés par l'IA**. Sur les 12 à 24 derniers mois, la plateforme fait trois choses :
- **Elle teste chaque facteur** par régression. Elle garde ceux qui expliquent la consommation : R² ≥ 0,5, effet
  positif, au moins 20 % de la consommation (sinon l'IPE resterait plat).
- **Elle choisit le meilleur modèle**, en essayant aussi les couples de facteurs. Le critère est l'erreur sur les
  mois retirés de l'apprentissage (validation croisée « un mois retiré »).
- **Elle choisit la forme** : un ratio si le talon est faible (≤ 15 %), un IPE modélisé sinon. Elle propose par
  énergie d'abord, et « toutes énergies » seulement à défaut.

Chaque proposition (principe 1) présente son raisonnement : facteurs retenus et écartés avec leur R², formule,
erreur, choix de la forme. Elle porte un niveau de confiance et attend une validation humaine, dans « À valider »
ou sur la page « Performance (IPE) ». Une proposition écartée n'est pas reproposée avant 180 jours. Analyse
lancée chaque jour, à la mise à niveau, ou par le bouton « Analyser les données ».

Démo : la variable « fournées » de l'Atelier central est déduite de sa consommation d'électricité (le simulateur
ne modélise pas la production), pour montrer une proposition fondée sur l'activité : « kWh électricité par fournée »,
R² = 0,89. Les autres propositions portent sur le gaz (degrés-jours). L'électricité des sites de démonstration
dépend trop peu du climat (talon de 90 % et plus) pour qu'un IPE climatique soit utile.

Passerelle ISO 50001 : une entreprise certifiée est exemptée de l'audit énergétique obligatoire tous les 4 ans.
Une fois la date de validité du certificat saisie, l'échéance d'audit apparaît « Exemptée » et ses rappels
s'arrêtent. C'est un argument commercial que l'étude de marché n'exploitait pas encore.

**F8 — plan 2D** (`app/services/site_plan.py`). Les zones du graphe P2 sont des rectangles, les équipements et
les compteurs des points, placés en % du plan ; une image (PNG ou JPEG) peut servir de fond. Les anomalies des
30 derniers jours y sont situées : l'équipement suspect clignote en rouge, les zones potentiellement impactées
sont surlignées. Le plan apparaît aussi sur la carte de l'anomalie. La maquette 3D, coûteuse à développer et à
alimenter, est reportée en V3 : un plan 2D branché sur le graphe est plus utile qu'une 3D vide.

**F9 — simulateur** (`app/services/simulator.py`).
1. **Consommation par usage** sur 12 mois :
   - chauffage et refroidissement : la part liée au climat, d'après le modèle de consommation (F11) ;
   - autres usages : puissance nominale × durée type de fonctionnement des équipements du graphe ;
   - le reste en « autres usages » ;
   - chaque part est ajustable.
2. **Actions** : les gestes types de la bibliothèque (ordres de grandeur indicatifs, que chaque cabinet
   complète) et les recommandations validées. Sur un même usage, les économies se cumulent sans double compte.
3. **Résultats** : kWh, € au prix payé (contrat F6), tCO₂e, investissement, temps de retour.

Le scénario retenu alimente la trajectoire « avec actions » (F11) et le rapport trimestriel. Démo, bureaux
Part-Dieu : 34 MWh par an (5 %), 6 300 €, retour en 3,8 ans.

**F10 — rapport trimestriel assisté** (`app/services/quarterly_reports.py`). Le rapport est un outil de
productivité pour l'auditeur, pas une IA autonome.
1. **Projet** : dès la fin du trimestre, la plateforme prépare un projet en six parties : synthèse des
   consommations, écarts au prévisionnel, dérives détectées, IPE, trajectoire, pistes d'action.
2. **Enrichissement** : l'auditeur le relit et ajoute son analyse (sa synthèse est obligatoire).
3. **Validation, puis livraison** : il le valide, puis le délivre sous l'identité de son cabinet.

Le client ne voit que les rapports délivrés. Le document (HTML, imprimable en PDF) ne mentionne pas
EffiSmart : EffiSmart ne délivre jamais d'analyse en direct au client final. Le régénérer conserve le texte de
l'auditeur.

**F11 — prédiction** (`app/services/consumption_model.py`). Le moteur de prévision v2 est appris sur les 12 à
24 derniers mois. Le meilleur modèle est choisi par validation hors échantillon ; la famille « jours
pertinents » apprend des jours analogues. Le modèle sert trois fois :
1. **Consommation attendue** : une anomalie devient un écart au modèle (seuil : 15 % au moins, et 2,5 fois
   l'erreur du modèle). Ce détecteur remplace l'écart N-1 dès que l'historique suffit (`app/services/drift.py`).
2. **Trajectoire Décret Tertiaire** (`app/services/trajectory.py`) :
   - la consommation est corrigée du climat (année de météo normale) ;
   - elle est comparée à la référence déclarée sur OPERAT ;
   - le rythme annuel moyen depuis cette référence est prolongé jusqu'en 2050, face aux jalons −40 / −50 / −60 %.

   Démo, bureaux Part-Dieu : « au rythme actuel, −30 % en 2030 au lieu des −40 % exigés ; avec le plan
   d'actions retenu, −34 % ». La trajectoire est une projection : elle se valide comme une prévision
   (principe P1).
3. **Simulateur** (F9) : part du chauffage et trajectoire avec actions.

Mode dégradé la première année, faute d'historique : pas de modèle, l'écart N-1 et les autres détecteurs
restent en place.

**F12 — optimisation tarifaire en mode conseil** (`app/services/load_shift.py`).
- **Calcul** : pour un équipement décalable du graphe (chargeurs, ballon d'eau chaude, ou toute durée de
  fonctionnement renseignée), la plateforme repère sa plage actuelle dans la courbe de charge. Elle cherche la
  plage la moins chère selon le contrat (heures creuses, Tempo, prix de marché).
- **Recommandation** : elle propose une « recommandation de décalage de charge », validée par un humain.
  L'exploitant applique le réglage lui-même : aucune écriture vers les équipements (N4 exclu en V1 et V2).
- **Gain** : il est financier, l'énergie consommée ne change pas ; il n'y a donc pas de mesure avant / après en
  kWh.

Démo, entrepôt de Genas : chargeurs déplacés de 11 h – 17 h vers 22 h – 4 h, environ 12 000 € par an.

## Module référent énergie (formation PRO-REFEI de l'ATEE)

Ce module reprend la démarche enseignée aux référents énergie par le programme PRO-REFEI (ATEE) :
- état des lieux ;
- plan de préconisations chiffré ;
- mesure et suivi ;
- argumentation ;
- management de l'énergie.

Elle s'appuie aussi sur les outils diffusés pendant la formation : énergieSIM, check-list énergie CHECK, guide
ComptIAA, protocole IPMVP, guides ADEME. Migration `0011_referent_energie` ; tests dans
`backend/tests/test_referent.py`.

| Code | Fonctionnalité | Module de la formation | Dans EffiSmart |
|---|---|---|---|
| R1 | Bilan énergétique : énergies, usages, Pareto et UES, poids sur le CA et l'EBE, puissance souscrite | SP2, SP3, SP5 | page « Bilan énergétique » |
| R2 | Plan d'actions chiffré, hiérarchisé, planifié (QQOQPCC) ; note pour la direction | SP3, SP4, SP5, SP8, énergieSIM | page « Plan d'actions » |
| R3 | Management de l'énergie : auto-évaluation sur 5 axes ; politique, équipe, objectifs SMART | SP1, SP5, énergie CHECK | page « Management de l'énergie » |
| R4 | Plan de mesure et vérification IPMVP en 13 points par action | SP5, IPMVP | onglet « Mesure et vérification » d'une action |
| R5 | Plan de comptage : niveau, recoupement des sous-compteurs, sous-compteurs à poser | SP5, ComptIAA | onglet « Plan de comptage » du bilan |
| R6 | Utilités industrielles : air comprimé, moteurs, vapeur ; cibles et seuils d'alerte des IPE | SP3, SP4, SP5, ADEME | graphe, simulateur, page « Performance (IPE) » |
| R7 | Kit de sensibilisation : messages préparés d'après les données, approuvés par un humain | SP6 | onglet « Sensibilisation » |

**R1 — bilan énergétique** (`app/services/energy_balance.py`).
- **Bilan par énergie** sur 12 mois :
  - consommation, coût en € HT (abonnements compris, au prix du contrat F6) et émissions ;
  - parts de chaque énergie dans la consommation et dans la facture ;
  - ratios kWh/m² et kWh (ou €) par unité produite.
- **Poids économique de l'énergie** :
  - facture / chiffre d'affaires, un indicateur à suivre à chaque exercice au-delà de 2 % ;
  - facture / EBE, qui traduit l'impact direct sur la marge : 10 % d'économie sur la facture se lit en points d'EBE.
- **Répartition par usage** :
  - chaque part dit sa provenance (norme NF EN 16247) : « calculé » (modèle de consommation et DJU), « estimé »
    (puissance × durée type) ou « par différence » ;
  - le Pareto propose comme **usages énergétiques significatifs** (UES, ISO 50001) les usages qui cumulent 80 %
    de la consommation. Le référent énergie confirme.
- **Puissance souscrite** : comparée au maximum atteint sur 12 mois, en moyenne 30 min, convertie en kVA avec
  cos φ = 0,93 (seuil de l'énergie réactive) et une marge de 15 % pour les pointes au pas 10 min.
  - Diagnostic : dépassements probables, marge faible, puissance surdimensionnée ou adaptée.
  - Le gain d'un ajustement se chiffre avec la part fixe de l'acheminement (€/kVA/an) lue sur le contrat.

**R2 — plan d'actions** (`app/services/action_plan.py`, `app/services/economics.py`).

Une action est saisie par l'auditeur ou par le responsable énergie : directement, depuis un geste du simulateur
(bouton « Inscrire ces actions au plan d'actions ») ou depuis une recommandation validée. Elle porte :
- **chiffrage** : économies par énergie (une surconsommation est négative), gain ou coût récurrent, investissement,
  situation de référence (rentabilité du seul surinvestissement, ex. moteur neuf contre rebobinage), durée de vie,
  CEE en kWh cumac et fiche de référence ;
- **classement** : famille (éclairage, air comprimé, froid…), nature (conception, technique,
  pilotage-maintenance, organisationnelle), horizon (court terme, moyen terme, pour mémoire), priorité
  (prioritaire, ambitieuse, très ambitieuse) ;
- **cotation sur 100** : faisabilité économique (suggérée d'après le retour), technique et risques notées de 1 à 4,
  plus les co-bénéfices (sécurité, maintenance, réglementation, productivité, environnement…). Le ROI n'est pas le
  seul critère ;
- **fiche QQOQPCC** : Qui (un seul responsable), Quoi, Où (lieu, équipement du graphe), Quand (une date, jamais
  « asap »), Pourquoi, Combien, Comment, contacts à prendre, besoins en formation, documentation, surveillance ;
- **avancement** : identifiée, planifiée, en cours, réalisée, vérifiée, abandonnée. Une action planifiée exige un
  responsable et une échéance ; une action vérifiée, un plan de mesure et vérification.

Rentabilité, comme énergieSIM :
- TRB = investissement / gain annuel net ;
- VAN sur la durée d'analyse (au plus la durée de vie), au taux d'actualisation choisi ;
- TRI et temps de retour actualisé ;
- chaque indicateur sans et avec CEE (déduits de l'investissement).

Paramètres par organisation : taux d'actualisation (10 % par défaut), durée d'analyse (10 ans), évolution du prix
de l'énergie (constant, +2 %/an, +5 %/an ou personnalisé), valorisation des CEE.

Synthèse du plan, comme le canevas de la formation :
- **court terme** et **moyen terme** : le moyen terme reprend le court terme, les actions « pour mémoire » restent
  hors plan ;
- MWh/an, € HT/an et part de la facture, investissement, CEE, retour, VAN et TRI ;
- un graphique des flux cumulés bruts et actualisés ;
- des alertes de revue : échéance dépassée, action à planifier, action réalisée sans plan de vérification ;
- une **note de synthèse pour la direction** (HTML imprimable) : chiffres du plan et arguments par enjeu.
  L'accord CEE avec l'obligé se signe avant toute commande (rôle actif et incitatif).

**R3 — management de l'énergie** (`app/services/energy_management.py`).
- **Auto-évaluation** : 38 questions, 12 thèmes, 5 axes PDCA de l'ISO 50001, démarche inspirée de la check-list
  énergie CHECK de l'ATEE (questions reformulées).
  - Réponses : tout à fait, partiellement, pas du tout, non applicable (exclue).
  - Pour chaque question, la plateforme montre ce que ses données en disent (« indices ») ; elle ne répond jamais
    à la place du référent.
  - Les évaluations s'additionnent pour suivre la progression ; les thèmes sous 50 % renvoient vers la page
    EffiSmart qui y répond.
- **Socle** :
  - politique énergétique et date de validation par la direction, périmètre ;
  - équipe énergie (rôles, missions) ;
  - objectifs SMART, chacun signalé s'il manque un indicateur, une cible, une échéance ou un responsable ;
  - périodicité de la revue énergétique, avec alerte de retard.

**R4 — mesure et vérification** (`action_plan.suggest_mv`). Plan IPMVP en 13 points ; la plateforme suggère, un
humain enregistre :
- **option C** (compteur général) si les économies attendues dépassent 10 % de la consommation du compteur ;
- sinon **option B** si l'équipement a son sous-compteur, **option A** autrement ;
- **budget de M&V** : moins de 10 % des économies annuelles ;
- **précision** : critère « 90/10 ».

Vérification d'une action réalisée : par la mesure avant / après validée de la recommandation d'origine (F1), ou
par l'IPE de vérification relié à l'action (12 mois avant, mois écoulés depuis).

**R5 — plan de comptage** (`app/services/metering_plan.py`).
- **Niveau** : 1, compteurs et factures ; 2, sous-comptage des postes clés ; 3, chaque usage significatif mesuré.
- **Données de chaque compteur** : courbe 30 min, index journaliers, factures ou rien.
- **Recoupement** : un sous-compteur est relié à son compteur général dans le graphe (« alimente »). La part non
  mesurée est un compteur virtuel « général − sous-compteurs » ; si les sous-compteurs dépassent le général,
  l'incohérence est signalée.
- **Sous-compteurs à poser** : usages significatifs sans comptage propre, avec un coût indicatif (cas de la
  formation : 10 sous-compteurs électriques, 9 000 € HT).
- **Les 4 étapes ComptIAA**, cochées d'après les données.

**R6 — utilités industrielles et IPE.**
- **Graphe des équipements** : compresseur d'air, pompes, ventilateurs et moteurs ; fluide et usage « air comprimé ».
- **Bibliothèque de gestes** : 14 gestes industriels, chacun avec sa famille, sa fiche CEE de référence et sa durée
  de vie ; ordres de grandeur ADEME diffusés par la formation. Exemples :
  - fuites d'air comprimé, souvent 40 à 50 % de la consommation, acceptable sous 15 % ;
  - baisse de pression ;
  - variateurs de vitesse sur pompes et ventilateurs (IND-UT-102) ;
  - calorifugeage des points singuliers (IND-UT-121) ;
  - économiseur ;
  - HP flottante ;
  - free-cooling ;
  - sous-comptage.
- **IPE validé** : un humain fixe sa **valeur cible** et un **seuil d'alerte** (+10 % par défaut). EffiSmart affiche
  « cible atteinte », « au-dessus de la cible » ou « seuil d'alerte dépassé », et compte les mois hors seuil.
- **Repères des utilités** affichés quand la variable s'y prête :
  - air comprimé : 110 à 125 Wh/Nm³ à 7 bars ;
  - vapeur : 950 kWh PCS/t ;
  - eau chaude : 95 kWh PCS/m³.

**R7 — sensibilisation** (`energy_management.generate_drafts`). Messages préparés d'après les données du site,
selon la communication engageante : un message, un chiffre, un geste.
- **Sources** :
  - consommation hors activité de l'électricité ;
  - économies mesurées et validées ;
  - action réalisée ;
  - IPE en amélioration ;
  - coût d'une fuite d'air de 1 mm à 7 bars.
- **Relecture** : un brouillon n'est visible que de l'auditeur et du responsable énergie, qui l'ajustent puis
  l'approuvent (principe P1). Un message approuvé s'imprime en affiche A4.

Démo :
- **Finances** : chiffre d'affaires et EBE des trois clients.
- **Plans d'actions** : Boulangeries Martin, Clinique du Parc (avec CEE), Logistique Rhône.
- **Management de l'énergie** : socle des Boulangeries et de la Clinique, et leurs auto-évaluations (deux pour la
  Clinique, pour montrer la progression).
- **Sensibilisation** : messages à relire pour l'Atelier central et le Bâtiment principal.
- **Mise à niveau** : idempotente, faite au démarrage de l'interface.

## Principe 1 : la plateforme propose, un humain décide

Toute sortie algorithmique (anomalie, recommandation d'optimisation, prévision) est présentée avec son
**raisonnement**, son **gain estimé** et son **niveau de confiance**, et attend la **validation d'un humain**
avant d'être appliquée : l'auditeur partenaire ou le responsable énergie du client, jamais la plateforme
seule. L'auditeur vend son expertise ; l'outil l'outille sans décider à sa place, ce qui cadre aussi
l'exposition en responsabilité. Code : `app/services/validation.py`.

| Sortie | Produite par | Raisonnement | Gain estimé |
|---|---|---|---|
| Anomalie (dérive) | détecteurs F2a, contexte F2b | donnée analysée, référence et méthode, écart, récurrence, équipement suspect et zones impactées | excès du jour × occurrences par an (rythme observé sur 90 jours) |
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
- **Usage par les anomalies (F2b) et les recommandations** : l'équipement suspect est choisi dans le graphe,
  puis l'impact est propagé aux zones et usages en aval. Un excès de 52 kW le week-end désigne le groupe
  froid de 55 kW ; la salle serveurs, qui fonctionne en continu, est
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
| Enedis SGE-Tiers | accès industriel, pour un grand parc (décision D10) | suivi du référencement : état, date de dépôt, notes ; collecte à brancher une fois référencé |
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

### Décision D10 : les deux fronts en parallèle dès le départ

Data Connect + GRDF ADICT **et** référencement SGE-Tiers sont lancés simultanément dès le démarrage. Data Connect
rend la plateforme opérationnelle en quelques semaines pour signer les premiers auditeurs ; pendant ce temps, le
référencement SGE-Tiers, qui prend plusieurs mois, mûrit en arrière-plan. Quand le parc de compteurs grossit,
l'accès industriel est déjà prêt : le délai n'est pas subi, il est masqué derrière la V1.

**Planning** : le référencement SGE-Tiers est un **jalon administratif à démarrer semaine 1**, indépendant du
développement logiciel. C'est le chemin critique le plus long du projet, plus long que le code de la V1 lui-même.
Son avancement se suit dans « Intégrations », carte « Enedis SGE-Tiers » (administrateur) : à lancer, dossier
déposé, en instruction, référencé.

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
14. **Périmètre F1–F5** (migration `0006_scope_f1_f5`) :
    - **N0 / N1** : les saisies N0 ne demandent pas de consentement Enedis / GRDF, puisque le client fournit
      lui-même ses factures.
    - **Indicateurs de performance** : bruts (kWh/m², €/m², kgCO₂e/m²) ; la correction climatique est
      réservée aux prévisions et à la mesure avant / après, validées par un humain.
    - **Estimation du gain d'une anomalie persistante** : elle est sous-évaluée, car le détecteur de talon ne
      signale qu'un changement. La mesure avant / après corrige l'ordre de grandeur (entrepôt : 3 MWh/an
      estimés, 112 MWh/an mesurés).
    - **Démo** : seule exception à « aucune décision simulée », signalée par « Historique de démonstration ».
