# 👥 Gestion du Personnel

[![Tests](https://github.com/Stevyne/Gestion-de-personnel/actions/workflows/tests.yml/badge.svg)](https://github.com/Stevyne/Gestion-de-personnel/actions/workflows/tests.yml)
[![Licence MIT](https://img.shields.io/badge/licence-MIT-green.svg)](LICENSE)
[![Python 3.12.7](https://img.shields.io/badge/Python-3.12.7-blue.svg)](.python-version)

**Application RH complète en Flask + PostgreSQL**

Système de gestion du personnel multi-utilisateur avec suivi des présences, congés, matériels, documents et notifications.

> Interface entièrement en français • Multi-utilisateur réel (isolation par `user_id`) • Sécurité CSRF / rate limiting / headers HTTP

---

## ✨ Fonctionnalités principales

### 👤 Gestion des employés
- CRUD complet des employés (`/employes`, ajout via `/employes/add` ou `/add_employee`)
- Affectation aux départements (CRUD départements via `/departements`)
- Historique des salaires et dates d'embauche
- Page `/historique` dédiée
- Workflow de départ strict (`/departs`) : préparation, restitution obligatoire, archivage de la fiche, désactivation des comptes et fermeture des sessions
- L'historique RH est conservé ; un trigger PostgreSQL interdit l'archivage tant qu'un matériel ou exemplaire reste détenu
- Photo de profil affichée dans la liste et sur la fiche détaillée (à défaut : initiales)

### 🧑‍💼 Recrutement
- Demandes de recrutement créées par les managers dans leur département, puis validées ou refusées par admin/RH
- Offres d'emploi versionnées par statut : brouillon, publiée, suspendue, fermée ou pourvue
- Fiches candidats indépendantes des employés, avec CV et lettre privés stockés en mode hybride PostgreSQL/S3
- Candidatures suivies de la réception à l'acceptation/refus, sans création prématurée d'un employé
- Grilles de critères pondérés totalisant 100 %, notes explicables et score dossier calculé automatiquement
- Entretiens planifiés avec évaluateur et grille Technique, Communication, Motivation, Travail en équipe, Adaptabilité
- Score global transparent : 40 % dossier + 60 % moyenne des entretiens ; il reste une aide, jamais une décision automatique
- Comparaison côte à côte et classement des candidats d'une offre
- Conversion transactionnelle candidat → employé, création optionnelle du contrat et compte utilisateur créé séparément
- Politique d'accès : manager limité aux demandes de son département ; admin/RH pilotent offres, candidats, évaluations et embauches
- Routes principales : `/recrutement`, `/recrutement/demandes`, `/recrutement/offres`, `/recrutement/candidats`

### 🕒 Gestion des présences
- Pointage entrée / sortie (`/presences/clock_in/<employe_id>`, `/presences/clock_out/<employe_id>`, en POST)
- Calcul automatique des retards (seuil configurable, `HEURE_ARRIVEE_ATTENDUE = "09:00"` dans `app.py`)
- Notification de l'employé lorsqu'une présence est saisie ou modifiée par un gestionnaire
- E-mail HTML en cas de retard via l'outbox persistante (aucun SMTP dans la requête web)

### 🏖️ Gestion des congés
- Demandes de congés en self-service (`/self-service/conges`)
- Approbation / refus par admin ou RH
- Soldes de congés (25 jours acquis par défaut, table `soldes_conges`, recalcul automatique)
- Calendrier des congés : vue mensuelle des congés **approuvés** (`/calendrier-conges`)

### 🧾 Gestion des permissions (module séparé)
- Les permissions fonctionnent **comme les congés** (demande → approbation / refus), mais vivent dans **un module totalement séparé** (`/permissions`, table `permissions`)
- **Indépendantes des congés** : une permission approuvée ne déduit **aucun jour** du solde de congés (`soldes_conges`)
- Une permission approuvée couvre le jour : il n'est alors pas compté comme une absence
- Routes : `/permissions`, `/permissions/add`, `/permissions/update/<id>`, `/permissions/delete/<id>`

### 🚫 Absences et circuit de justification
- **Tout jour ouvré (lun. → ven.) sans présence enregistrée est automatiquement enregistré comme une absence** dans la table `absences`
- Sont exclus : congés et permissions approuvés, jours avant l'embauche et jour en cours. Le calcul tourne à 01h00 et reste déclenchable via **🔄 Synchroniser** ; il est idempotent (`UNIQUE(employe_id, date)`)
- L'employé est informé et consulte ses dossiers sur `/self-service/absences`
- Dépôt self-service d'un justificatif PDF/PNG/JPEG (8 Mo), validé par magic-bytes et stocké de façon hybride (PostgreSQL `BYTEA` ou S3 privé selon le seuil)
- Statuts : `non_justifiee` → `justificatif_depose` → `acceptee` ou `refusee`; un refus RH exige un motif
- Un justificatif accepté crée automatiquement un **congé maladie approuvé** d'un jour, sans consommer le solde de congés payés
- Confidentialité : justificatif téléchargeable uniquement par son propriétaire et les rôles `admin`/`rh` (pas par le manager)
- Enregistrement manuel possible (`/absences/add`) ; suppression réservée à `admin`/`rh` et interdite après requalification

### 📦 Gestion des matériels (par département)
- Stock de fournitures et d'équipements rattaché à un **département** (papiers, stylos, classeurs, cartouches, mobilier, informatique...)
- **Traçabilité complète** : chaque entrée / sortie est enregistrée dans `materiels_mouvements` (quantité, motif, employé concerné, auteur, horodatage). Le stock n'est jamais édité à la main, il découle des mouvements
- **Attribution durable** à un employé (PC, téléphone, clés) : l'article sort du stock puis y revient au retour (`materiels_attributions`)
- Pour le parc suivi à l'unité, chaque attribution référence l'**exemplaire physique exact** ; le détenteur de l'exemplaire est synchronisé automatiquement et ne peut plus être modifié hors workflow
- Contraintes et triggers PostgreSQL empêchent un exemplaire d'un autre article, indisponible, déjà attribué ou associé à une quantité différente de 1
- **Alerte de stock bas** : seuil configurable par article, badge dans la liste et notification interne aux admin/RH/managers. Anti-spam (une seule notif tant que le stock n'est pas réapprovisionné)
- Filtres par nom, département, catégorie et état (stock bas / rupture), avec pagination
- Rôles : `admin`, `rh` et `manager` gèrent le stock ; la suppression est réservée à `admin`/`rh` ; les autres rôles consultent
- Rapports parc PDF/Excel (`/export/materiels/pdf`, `/export/materiels/excel`) filtrés par la portée départementale
- Maintenance priorisée avec SLA : critique 4 h, haute 1 jour, normale 3 jours, basse 7 jours
- Chaque panne reçoit un ticket transactionnel `MAINT-AAAA-xxx`, avec prise en charge, échéance, dépassement et résultat SLA
- Routes : `/materiels`, `/materiels/add`, `/materiels/edit/<id>`, `/materiels/<id>`, `/materiels/<id>/mouvement`, `/materiels/<id>/attribuer`, `/materiels/attribution/<id>/retour`, `/materiels/delete/<id>`

### 📋 Inventaire physique
- Le stock du module Matériels est un **stock théorique** : il découle des mouvements saisis. L'inventaire le confronte au terrain (« 10 en base, 9 trouvés → anomalie »)
- **Campagne par département** : l'ouverture fige la liste des articles et leur stock théorique à l'instant T ; on saisit ensuite les quantités réellement comptées
- Écart calculé par ligne : **conforme**, *n* **manquant(s)** ou **+n en trop**. Compteurs d'avancement (comptés / restants / écarts) et écart net de la campagne
- Une ligne **non comptée** est distincte d'un comptage à zéro : elle est **ignorée** à la clôture (son stock reste inchangé)
- **À la clôture, le stock est aligné** sur les quantités comptées via un mouvement d'ajustement tracé (`origine = 'inventaire'`), visible dans l'historique du matériel. L'ajustement part du **stock réel du moment**, ce qui reste correct si des mouvements ont eu lieu pendant la campagne
- Une seule campagne ouverte à la fois par département (évite deux ajustements concurrents). Une campagne peut aussi être **annulée** sans toucher au stock
- Rôles : `admin`, `rh` et `manager` saisissent les comptages ; la **clôture** (qui modifie le stock) est réservée à `admin`/`rh` ; les autres consultent
- Routes : `/inventaires`, `/inventaires/nouveau`, `/inventaires/<id>`, `/inventaires/<id>/compter`, `/inventaires/<id>/cloturer`, `/inventaires/<id>/annuler`
- Planches d'étiquettes QR imprimables en A4, trois colonnes de 60 × 35 mm, avec numéro d'inventaire, série et lien direct vers la fiche

### 🔎 Recherche globale
- Barre de recherche dans l'en-tête, avec **aperçu instantané** groupé par type et page complète `/recherche`
- Couvre les **employés, départements, matériels, exemplaires par numéro d'inventaire ou série, congés, absences, documents, comptes** — et les pages de l'application (façon palette de navigation)
- **Filtrée par rôle côté serveur** : les catégories interdites ne sont pas interrogées du tout (un employé ne voit ni les congés ni les comptes, y compris en appelant l'API directement)
- Raccourci **Ctrl+K** / Cmd+K, navigation aux flèches, `Échap` pour fermer
- Sur écran étroit, le champ laisse place à une entrée « Rechercher » dans le tiroir de navigation
- Routes : `/recherche` (page), `/api/recherche?q=` (JSON pour l'aperçu)

### 🙋 Espace personnel (`/mon-profil`)
- Chaque utilisateur consulte sa fiche, corrige ses coordonnées (nom, prénom, email, téléphone) et change son mot de passe
- **Poste, département, salaire et date d'embauche sont en lecture seule** : ce sont des données contractuelles, du ressort des RH
- **Photo de profil** : PNG/JPEG jusqu'à 8 Mo, automatiquement recadrée en carré et réduite à 512×512 (Pillow). Le ré-encodage supprime au passage les métadonnées EXIF (dont la géolocalisation)
- La photo est portée par le **compte** (`users.photo`), donc disponible même pour les comptes sans fiche employé
- Contenu persistant stocké dans PostgreSQL (`users.photo_contenu`) ; `static/avatars/` ne sert que de cache/repli pour les anciennes photos
- Accessible via « Mon espace » dans le menu du compte

### 📁 Documents & Rapports
- Upload de documents (PDF, Excel, images...) avec **validation du contenu réel** (magic-bytes), pas seulement de l'extension
- Contenu stocké en PostgreSQL (`BYTEA`) ou dans un Object Storage S3 privé, avec notification immédiate de l'employé concerné
- Alertes internes et e-mails avant expiration puis après expiration
- Rapports avancés avec filtres (`/rapports`)
- Exports PDF (ReportLab) et Excel (Openpyxl) pour présences et congés

### 📑 Contrats
- Module versionné pour CDI, CDD, stages, consultants et autres contrats
- Dates, référence, statut, notes et document signé stocké en mode hybride (`BYTEA` / S3 privé)
- Renouvellement créant une nouvelle version liée à l'ancien contrat ; résiliation motivée et auditée
- Accès confidentiel : admin/RH gèrent tous les contrats, chaque employé ne consulte que les siens
- Alertes idempotentes à J-30, J-7 et après expiration, par notification interne et e-mail via l'outbox

### 💬 Messagerie interne
- Interface responsive inspirée de Messenger : liste des discussions à gauche, fil actif à droite, bulles, avatars, recherche instantanée et zone de saisie fixe
- Sur mobile, navigation plein écran entre la liste et la conversation ; `Entrée` envoie et `Maj+Entrée` ajoute une ligne
- Conversations privées et groupes avec suivi lu/non-lu et badge dans la navigation
- Chaque ticket de maintenance possède automatiquement un groupe de discussion avec demandeur, assigné et gestionnaires concernés ; le lien ticket ↔ conversation est bidirectionnel
- Chargement initial limité à 50 messages, puis pagination progressive des messages précédents
- Pour manager/technicien/employé, le sélecteur et les identifiants forgés sont limités au département courant ; admin/RH peuvent contacter tous les comptes
- Conversations privées strictement réservées à leurs membres, y compris pour admin/RH ; contrôle identique sur les réponses et les pièces jointes
- Annonces globales ou ciblées par rôle (`admin`, `rh`, `manager`, `technicien`, `employe`) réservées à admin/RH
- Pièces jointes validées par extension, taille et magic-bytes, puis stockées en mode hybride (`BYTEA` / S3 privé)
- Notifications internes et e-mails via l'outbox pour les nouveaux messages et les mises à jour d'annonces
- Limites : 20 000 caractères par message, 200 pour le titre, 8 Mo par pièce jointe

### 🔔 Notifications et e-mails
- Notifications persistantes en base, filtrées par `user_id`; badge et page `/notifications`
- Affichage uniforme sur mobile, tablette et ordinateur : titre + message limités à deux lignes au total avec `…`, texte complet conservé dans `title`
- Événements couverts : absences, présences, documents, arrivée d'un employé, congés, permissions, matériel et maintenance
- E-mails sur les actions à traiter, décisions, absences et expirations de documents
- **Outbox PostgreSQL** (`email_outbox`) : envoi asynchrone par lots, verrou `SKIP LOCKED`, reprise après arrêt, jusqu'à cinq tentatives avec délai exponentiel et clés anti-doublon
- Mode développement propre : `EMAIL_ENABLED=false` par défaut, donc aucun contact SMTP

### 🔐 Sécurité & Rôles
- Authentification par session (Werkzeug pour le hash des mots de passe)
- 5 rôles officiels centralisés dans `services/roles.py` : `admin`, `rh`, `manager`, `technicien`, `employe`
- Une contrainte PostgreSQL interdit tout code de rôle inconnu, y compris via une écriture SQL directe
- Self-service pour les employés (`/self-service` ou `/mon-espace`)
- Logs d'audit (`/audit`, réservé à `admin`/`rh`)
- Protection CSRF (Flask-WTF), rate limiting partagé via Redis et limite dédiée aux tentatives de connexion (`5/min`, `20/h` par IP), headers de sécurité (Flask-Talisman)
- Création de comptes réservée aux admin/RH (`/register`) dans une popup depuis la page Utilisateurs, avec repli en page complète sans JavaScript ; choix immédiat du rôle et de l'employé lié, seul un administrateur pouvant créer un autre administrateur

### 🖥️ Sessions actives & déconnexion forcée
- Registre des sessions ouvertes en base (`sessions_actives`) : les cookies signés Flask n'étant pas révocables, un identifiant de session est stocké côté serveur
- La page `/utilisateurs` affiche un **badge d'état par compte** : `En ligne` (activité < 5 min, configurable via `SESSION_ONLINE_WINDOW_MIN`), `Inactif` (session ouverte sans activité récente), `Hors ligne`
- Un **administrateur peut forcer la déconnexion** d'un compte connecté ; la révocation prend effet à la requête suivante et l'action est tracée dans l'audit (`FORCE_LOGOUT`)
- Purge automatique des sessions de plus de 30 jours (tâche planifiée à 03h00)

### 🛡️ Cloisonnement départemental de toute l'application
- **Admin et RH** : portée globale ; **manager, technicien et employé** : uniquement le département de leur fiche employé
- Politique centrale « refus par défaut » : un compte non privilégié sans département obtient des listes vides et ne peut ouvrir ni modifier aucune ressource départementale
- Filtrage SQL des listes, tableaux de bord, rapports, calendriers, recherche globale/API, activités récentes et exports PDF/Excel
- Garde anti-contournement sur les URL de détail et les formulaires forgés : employés, pointages, congés, permissions, absences, documents, matériels, attributions, inventaires, exemplaires, maintenances, QR et avatars
- Les sélecteurs ne proposent que les employés, comptes et départements autorisés ; les notifications de stock et maintenance ne sont envoyées qu'aux managers concernés, plus admin/RH
- Les documents conservent une règle plus stricte : employé/technicien = ses propres documents, manager = son département, admin/RH = tous
- Les prestataires constituent un référentiel global sans département : leur gestion est donc réservée à admin/RH
- Le rôle est relu en base à chaque requête : une rétrogradation prend effet immédiatement, sans attendre une reconnexion

### 📊 Tableaux de bord spécialisés
- Tableau général conservé, complété par `/dashboard/rh`, `/dashboard/parc` et `/dashboard/direction`
- RH et Direction : admin/RH en vue globale ; Parc : admin/RH global, manager/technicien limités à leur département
- Couverture des modules exploitables : personnel, présences/temps, congés/soldes, permissions, absences/justificatifs, contrats, documents, matériels/parc, SLA maintenance et inventaires
- La vue globale ajoute les statistiques d'accès, sessions, audit, notifications et outbox e-mail
- Les données salariales et les coûts consolidés restent réservés aux vues admin/RH

### 🪟 Interface
- **Connexion responsive et accessible** : écran dédié TeamSphere, champs compatibles gestionnaires de mots de passe, affichage/masquage du secret, alerte Verr. Maj et état de chargement anti-double-clic
- **Navigation compacte et fixe** : reste visible en haut pendant le défilement, une seule ligne (~63 px), 5 groupes déroulants, icônes SVG, menu du compte avec avatar. Paliers responsive : complet ≥ 1151 px, compressé 1071–1150 px, icônes seules 769–1070 px, tiroir latéral ≤ 768 px
- **Formulaires en popup** : créations et modifications s'ouvrent dans une fenêtre modale sans quitter la page courante (avec repli sur la page classique si JavaScript est absent)
- Graphiques du tableau de bord responsives ; Chart.js servi **en local** (compatible avec la politique CSP)

---

## 🛠️ Technologies

| Catégorie       | Choix                                              |
|-----------------|----------------------------------------------------|
| Backend         | Flask 3.0.3                                        |
| Base de données | PostgreSQL 17, psycopg2, Alembic/Flask-Migrate    |
| Sécurité        | Flask-WTF, Flask-Limiter + Redis, Flask-Talisman   |
| Fichiers        | PostgreSQL `BYTEA` + Object Storage S3 compatible  |
| Emails          | Flask-Mail + outbox PostgreSQL asynchrone          |
| Exports         | ReportLab (PDF), Openpyxl (Excel)                  |
| Images          | Pillow (redimensionnement des photos de profil)    |
| Planification   | APScheduler dans un worker séparé du serveur web   |
| Observabilité   | Logs JSON, Sentry, live/readiness, heartbeat       |
| Frontend        | HTML + CSS responsive mobile-first (pas de framework JS) |
| Auth            | Werkzeug (hash des mots de passe)                  |

---

## 📦 Installation

### 1. Prérequis

```bash
# Python 3.10+
python3 --version

# PostgreSQL
sudo apt update
sudo apt install postgresql postgresql-contrib
```

### 2. Cloner et installer les dépendances

```bash
git clone https://github.com/Stevyne/Gestion-de-personnel.git
cd Gestion-de-personnel
pip install -r requirements.txt
```

### 3. Configuration de la base de données

```bash
sudo -u postgres psql
```

Dans psql :

```sql
CREATE DATABASE gestion_personnel;
CREATE USER postgres WITH PASSWORD 'postgres';
GRANT ALL PRIVILEGES ON DATABASE gestion_personnel TO postgres;
\q
```

### 4. Variables d'environnement

Copiez `.env.example` en `.env` et adaptez les valeurs :

```bash
cp .env.example .env
```

| Variable                  | Description |
|---------------------------|-------------|
| `SECRET_KEY`              | Clé secrète Flask — à générer avec `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL`            | Chaîne de connexion PostgreSQL |
| `AUTO_INIT_DB`, `REQUIRE_ALEMBIC_CURRENT` | Bootstrap historique local et exigence de révision Alembic en production |
| `REDIS_URL`               | Stockage Redis partagé du rate limiting multi-worker |
| `LOGIN_RATE_LIMIT`        | Limites des POST `/login` (`5 per minute;20 per hour` par défaut) |
| `OBJECT_STORAGE_ENABLED`, `OBJECT_STORAGE_REQUIRED` | Laisser `false` sans S3 ; activer après création d'un bucket privé |
| `OBJECT_STORAGE_THRESHOLD_BYTES` | Seuil d'externalisation progressive (1 Mio par défaut) |
| `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT_URL` | Variables optionnelles tant que S3 est désactivé |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Identifiants optionnels du futur bucket privé |
| `SCHEDULER_MODE`          | `embedded` en local, `disabled` sur le web, `worker` sur le service dédié |
| `SENTRY_DSN`, `LOG_FORMAT` | Monitoring d'erreurs et logs structurés JSON |
| `EMAIL_ENABLED`           | Active explicitement l'outbox et SMTP (`false` par défaut) |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS` | Config SMTP |
| `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER` | Identifiants et expéditeur SMTP |
| `EMAIL_POLL_SECONDS`, `EMAIL_BATCH_SIZE`, `EMAIL_MAX_ATTEMPTS` | Fréquence et limites de l'outbox |
| `ADMIN_EMAIL`             | Destinataire des alertes admin |
| `FLASK_ENV`, `FLASK_DEBUG` | Mode d'exécution |
| `RATELIMIT_ENABLED`       | Active les limites de requêtes (`true` par défaut ; `false` en tests) |
| `SESSION_COOKIE_SECURE`   | `true` en HTTPS (défaut `false`) |
| `PERMANENT_SESSION_LIFETIME` | Durée de vie d'une session, en secondes |
| `SESSION_ONLINE_WINDOW_MIN` | Fenêtre d'inactivité avant de passer un compte de « En ligne » à « Inactif » (défaut 5 min) |
| `LOG_LEVEL`               | Niveau de journalisation |

> `SESSION_COOKIE_HTTPONLY` et `SESSION_COOKIE_SAMESITE` sont fixés dans `app.py` (`True` / `Lax`) et ne se règlent pas par l'environnement.

> ✅ **Bonnes nouvelles** : `SECRET_KEY` et `FLASK_DEBUG` sont **déjà lus depuis l'environnement** (aucune valeur sensible codée en dur dans `app.py`). En production, l'absence de `SECRET_KEY` lève une erreur (`RuntimeError`), et `FLASK_DEBUG=true` combiné à `FLASK_ENV=production` est bloqué. En production, l'absence de `DATABASE_URL` est également bloquante ; en développement, un fallback local (`postgres/postgres`) est utilisé avec un avertissement dans les logs.
>
> ⚠️ **Points à vérifier avant mise en production** :
> - Le seul secret demandé par le Blueprint est `BOOTSTRAP_ADMIN_PASSWORD` sur le service web (12 caractères minimum pour une base neuve). Aucun secret GitHub ni DSN Sentry n'est obligatoire.
> - La commande Build peut rester `pip install -r requirements.txt` : ce fichier UTF-8 contient maintenant Flask-Migrate et toutes les dépendances Phase 4 nécessaires au démarrage.
> - Le Blueprint Render configure Redis, HTTPS, le worker scheduler et les migrations Alembic ; les déploiements partent automatiquement après une CI réussie.
> - **Aucun S3 n'est requis actuellement** : le Blueprint fixe `OBJECT_STORAGE_ENABLED=false` et conserve tous les fichiers en PostgreSQL `BYTEA`.
> - Configurez SMTP puis passez `EMAIL_ENABLED=true`; sans cette activation explicite, aucun e-mail ne quitte l'application.
> - `static/uploads/` n'est qu'un repli pour d'anciens fichiers ; les nouveaux fichiers restent persistants dans PostgreSQL.
> - Sans bucket S3, les sauvegardes/PITR gérées du plan PostgreSQL Render constituent la protection active.

---

## 🚀 Lancement

```bash
python app.py
```

L'application démarre sur **http://0.0.0.0:5000**

> En développement, `AUTO_INIT_DB=true` conserve le bootstrap historique. En production, le serveur web ne modifie plus le schéma au démarrage : Render exécute `flask bootstrap-db && flask db upgrade` dans `preDeployCommand`. Les évolutions futures doivent être ajoutées sous forme de révisions Alembic.

**En production**, ne pas utiliser le serveur de développement Flask :

```bash
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

---

## 🏭 Phase 4 — exploitation en production

### Migrations versionnées

```bash
# Première transition d'une base existante
flask bootstrap-db
flask db upgrade
flask db current

# Nouvelle migration SQL/SQLAlchemy
flask db revision -m "description"
```

La chaîne de migrations part de `20260813_phase4` et aboutit à
`20260814_recrutement`. Les downgrades refusent de supprimer un stockage encore
utilisé (fichiers S3 ou candidats déjà convertis en employés).

### Bascule progressive vers S3 — prête mais désactivée

Le déploiement actuel ne nécessite aucun compte S3 : `OBJECT_STORAGE_ENABLED`
et `OBJECT_STORAGE_REQUIRED` valent `false` dans `render.yaml`. Tous les
fichiers restent donc en PostgreSQL `BYTEA`, comme avant la Phase 4.

Après création d'un bucket privé, renseignez les variables `S3_*`, activez
`OBJECT_STORAGE_ENABLED=true`, puis contrôlez la migration progressive :

```bash
flask storage status
flask storage migrate --dry-run
flask storage migrate --batch-size 100
# Option de transition : --keep-source ; pour tout migrer : --all-files
```

Les nouveaux fichiers dépassant le seuil seront alors envoyés vers S3 avec un
checksum SHA-256. AWS S3, Cloudflare R2, Backblaze B2 et MinIO sont supportés.

### Services Render

`render.yaml` décrit trois composants actifs, sans dépendance S3 :

1. le web Gunicorn (aucun scheduler embarqué) ;
2. le worker `scheduler_worker.py` avec heartbeat PostgreSQL ;
3. Redis/Key Value pour les quotas partagés.

Le cron S3 n'est volontairement pas déclaré. `Dockerfile.backup` et les scripts
restent disponibles pour l'ajouter après création d'un bucket.

Les sondes sont publiques mais ne divulguent aucune donnée métier :

- `GET /health/live` : processus Flask vivant ;
- `GET /health/ready` : PostgreSQL, Redis, révision Alembic, état S3 optionnel et
  fraîcheur du scheduler. Le backup applicatif indique `managed_by_render` tant
  que le cron S3 est désactivé.

### Sauvegardes et restauration

Sans S3, utilisez les sauvegardes et le PITR gérés par le plan PostgreSQL Render
`basic-256mb`. Vérifiez leur activation et la politique de rétention dans le
Dashboard Render.

Les scripts de copie logique indépendante restent prêts pour plus tard. Après
création d'un bucket, ils produiront un dump custom, chiffré côté S3 et vérifié
par taille + SHA-256 :

```bash
python scripts/backup_postgres.py

# Restauration volontaire vers une URL cible séparée
RESTORE_DATABASE_URL=postgresql://... \
python scripts/restore_postgres.py --confirm RESTAURER-GESTION-PERSONNEL
```

Tester périodiquement la restauration sur une base isolée. Ne pointez jamais
`RESTORE_DATABASE_URL` vers la production pendant un exercice.

### CI/CD

`.github/workflows/tests.yml` exécute la suite complète **tous les 10 pushes**
sur `master`. Un contrôle léger compte chaque exécution `push` via l'API GitHub ;
au dixième, il lance PostgreSQL 17, Redis 8, Ruff, Alembic, `pytest -q` et la
construction de l'image de backup.

Les pull requests et les lancements manuels exécutent toujours la suite complète.
En cas d'erreur de l'API de comptage, le workflow choisit également de tester
(fonctionnement sécurisé par défaut). Le dixième test n'est pas annulé si un
onzième push arrive pendant son exécution.

Comme neuf commits sur dix n'ont pas de suite complète, les déploiements
automatiques Render sont désactivés (`autoDeployTrigger: off`). Déployez
manuellement uniquement un commit dont le job **Suite complète PostgreSQL /
Redis** est vert. Aucun deploy hook ni secret GitHub n'est requis.

---

## 👤 Utilisateurs de démonstration (développement/tests uniquement)

| Utilisateur | Mot de passe | Rôle     | Description              |
|-------------|--------------|----------|--------------------------|
| `admin`     | `admin123`   | admin    | Accès complet            |
| `rh`        | `rh123`      | rh       | Ressources Humaines      |
| `manager`   | `manager123` | manager  | Chef de projet           |
| `employe`   | `user123`    | employe  | Employé classique        |

> Ces comptes ne sont créés que si `SEED_DEMO_DATA=true` (défaut hors production). Le Blueprint Render fixe cette option à `false` et exige `BOOTSTRAP_ADMIN_PASSWORD` (12 caractères minimum) pour une base neuve.

---

## 📍 Routes principales

| Route                              | Description                      | Accès                  |
|------------------------------------|----------------------------------|------------------------|
| `/`                                | Tableau de bord                  | Connecté               |
| `/login`, `/logout`                | Authentification / déconnexion   | Public / Connecté      |
| `/register`                        | Création d'un compte et rattachement salarié | admin, rh (admin pour rôle admin) |
| `/recherche`, `/api/recherche`     | Recherche globale (page + JSON)  | Connecté (filtré par rôle) |
| `/employes`, `/employes/add`, ...  | Gestion des employés             | Tous / selon rôle      |
| `/recrutement`, `/recrutement/demandes` | Besoins et workflow de validation | manager (département), admin/RH |
| `/recrutement/offres`, `/recrutement/candidats` | Offres, candidatures, scores et entretiens | admin/RH |
| `/departements`                    | Gestion des départements         | Selon rôle             |
| `/materiels`, `/materiels/add`     | Matériels par département        | Tous / selon rôle      |
| `/inventaires`, `/inventaires/nouveau` | Inventaire physique          | Saisie : admin/rh/manager · clôture : admin/rh |
| `/presences`, `/presences/add`     | Pointages                        | Tous / selon rôle      |
| `/conges`, `/conges/add`           | Congés                           | Tous / selon rôle      |
| `/permissions`, `/permissions/add` | Permissions (module séparé)      | Tous / selon rôle      |
| `/absences`, `/absences/add`       | Absences et décisions sur justificatifs | admin, rh, manager |
| `/self-service/absences`           | Mes absences et dépôt d'un justificatif | Employé connecté |
| `/absences/<id>/justificatif`      | Téléchargement confidentiel      | Propriétaire, admin, rh |
| `/absences/<id>/decision`          | Acceptation/refus du justificatif | admin, rh              |
| `/calendrier-conges`               | Calendrier des congés            | Tous                   |
| `/rapports`                        | Rapports avancés + filtres       | Tous                   |
| `/documents`                       | Documents                        | Tous                   |
| `/contrats`                        | Contrats, versions et échéances  | Propriétaire, admin, rh |
| `/departs`                         | Workflow de départ et archivage  | admin, rh              |
| `/export/materiels/pdf`, `/export/materiels/excel` | Rapports du parc | Connecté, portée départementale |
| `/messages`, `/messages/nouveau`   | Messagerie privée, groupes et annonces | Connecté, portée départementale |
| `/historique`                      | Historique salaires / embauches  | Tous                   |
| `/notifications`                   | Centre de notifications          | Tous                   |
| `/mon-profil`                      | Espace personnel (infos, photo, mot de passe) | Connecté  |
| `/self-service`, `/mon-espace`     | Self-service employé             | Tous                   |
| `/utilisateurs`                    | Comptes, badges de connexion, déconnexion forcée | admin, rh |
| `/audit`                           | Logs d'audit                     | admin, rh              |
| `/export/presences/pdf`            | Exports                          | Connecté               |

> `/mon-profil` (fiche personnelle et photo) et `/mon-espace` (self-service : demandes de congés, pointages) sont deux pages distinctes.

---

## 🔄 Support multi-utilisateur (concurrent)

- `threaded=True` dans Flask
- Notifications stockées en base avec `user_id`, filtrage systématique `WHERE user_id = %s`
- Sessions Flask isolées par utilisateur, avec registre serveur révocable (`sessions_actives`)
- Pas de variable globale partagée pour les notifications

---

## 🧪 Tests

Les tests s'appuient sur une **vraie base PostgreSQL** de test (pas de mock), configurée dans `tests/conftest.py`.

```bash
# 1. Créer la base de test
createdb gestion_personnel_test

# 2. Installer les dépendances de développement
pip install -r requirements-dev.txt

# 3. Lancer les tests
pytest
```

> Surchargez la base avec la variable `TEST_DATABASE_URL` si besoin (CI, autre machine). Le CSRF, le rate limiting et SMTP sont désactivés pendant les tests (`conftest.py`). La suite couvre notamment le parc matériel, le workflow de maintenance, les justificatifs d'absence, la messagerie, les contraintes PostgreSQL, les notifications événementielles et l'outbox.

### Intégration continue

Le workflow `.github/workflows/tests.yml` lance automatiquement PostgreSQL 17,
compile les modules puis exécute `pytest -q` sur chaque push et pull request vers
`master`. Il peut également être déclenché manuellement depuis GitHub Actions.

> 💡 Les POST `/login` sont limités à 5/minute et 20/heure par IP ; les autres routes conservent les limites globales. En production, les compteurs Redis sont partagés entre workers et un redémarrage du web ne les efface pas.

---

## 🧩 Architecture par Blueprints

`app.py` est désormais principalement un point de composition. La configuration,
la sécurité HTTP, PostgreSQL, le schéma et les services partagés sont isolés dans
`services/`, tandis que les domaines autonomes vivent dans des Blueprints Flask :

- `parc.py` : matériels, mouvements, attributions, inventaires, exemplaires,
  maintenance, prestataires et étiquettes QR ;
- `documents.py` : dépôt, liste, suppression et téléchargement protégé ;
- `departements.py` : consultation et administration des départements ;
- `presences.py` : pointages, saisie des présences et historique du temps ;
- `utilisateurs.py` : comptes, rôles, sessions et création d'accès ;
- `auth.py` : connexion, déconnexion, profil, photo et mot de passe ;
- `departs.py` : préparation et finalisation des départs ;
- `contrats.py` : contrats, versions, fichiers et alertes ;
- `rapports_parc.py` : exports PDF/Excel du matériel ;
- `dashboard.py` et `dashboards_roles.py` : tableau général et tableaux RH, Parc, Direction ;
- `recherche.py` : recherche multi-domaine et palette de navigation ;
- `conges.py` : dépôt, avis manager, décision RH et annulation ;
- `absences.py` : consultation, saisie et synchronisation ;
- `notifications.py` : centre de notifications et marquage comme lu ;
- `recrutement.py` : demandes, offres, candidats, évaluations, entretiens et embauche ;
- `absence_justifications.py` : workflow confidentiel des justificatifs ;
- `messagerie.py` : messagerie interne.

Les dépendances communes sont injectées à l'enregistrement des Blueprints : il
n'existe aucun import circulaire vers `app.py`. Les URLs publiques restent
inchangées (`/materiels`, `/documents`, `/departements`, `/presences`, `/login`,
etc.) ; seuls les noms d'endpoints internes sont préfixés par leur domaine.
Un test structurel maintient désormais `app.py` sous 3 200 lignes.

## 📁 Structure du projet

```
Gestion-de-personnel/
├── app.py                  # Composition des extensions, services et Blueprints
├── blueprints/
│   ├── dashboard.py        # Tableau de bord général cloisonné
│   ├── recherche.py        # Recherche globale
│   ├── conges.py           # Workflow des congés
│   ├── absences.py         # Gestion des absences
│   ├── notifications.py    # Centre de notifications
│   ├── recrutement.py      # Workflow complet de recrutement
│   ├── parc.py             # Stock, inventaires, exemplaires et maintenance
│   ├── documents.py        # Documents RH et contrôle des téléchargements
│   ├── departements.py     # Gestion des départements
│   ├── presences.py        # Pointages et historique
│   ├── utilisateurs.py     # Comptes, rôles et sessions
│   ├── auth.py             # Authentification et profil
│   ├── departs.py          # Départs et archivage
│   ├── contrats.py         # Contrats et alertes
│   ├── rapports_parc.py    # Exports du parc
│   ├── dashboards_roles.py # Tableaux RH, Parc et Direction
│   ├── absence_justifications.py
│   └── messagerie.py
├── services/
│   ├── configuration.py    # Variables d'environnement, SQLAlchemy, SMTP, proxy
│   ├── security.py         # CSRF, Redis rate limit et Talisman
│   ├── database.py         # Connexions et context manager PostgreSQL
│   ├── migrations.py       # Initialisation Flask-Migrate/Alembic
│   ├── schema.py           # Bootstrap idempotent du schéma historique
│   ├── common.py           # Pagination, retards et calculs partagés
│   ├── notifications.py    # Persistance des notifications
│   ├── email_outbox.py     # File SMTP persistante, indépendante de Flask
│   ├── roles.py            # Référentiel officiel des rôles
│   └── phase*_schema.py    # Contraintes et migrations métier
├── migrations/             # Révisions Alembic / Flask-Migrate
├── .github/workflows/
│   └── tests.yml           # PostgreSQL 17 + pytest sur push/PR
├── requirements.txt        # Dépendances runtime directes, minimales
├── requirements-dev.txt    # Dépendances dev/CI (pytest, Ruff, git-filter-repo)
├── pytest.ini
├── .env.example
├── .gitignore
├── LICENSE                 # Licence MIT
├── render.yaml             # Déploiement Render
├── static/
│   ├── style.css
│   ├── js/
│   │   └── chart.umd.min.js  # Chart.js servi en local (compatible CSP)
│   ├── avatars/            # Photos de profil (données utilisateur, hors dépôt)
│   └── uploads/            # Fichiers uploadés (hors dépôt)
├── templates/              # Templates, tous en français
│   ├── base.html           # Navigation, recherche globale, modale générique
│   ├── _modal_layout.html  # Layout réduit pour les formulaires en popup
│   ├── index.html / detail.html
│   ├── dashboard.html
│   ├── recherche.html      # Page de résultats de la recherche globale
│   ├── mon_profil.html     # Espace personnel
│   ├── utilisateurs.html   # Comptes + badges de connexion
│   ├── presences.html / conges.html / absences.html / permissions.html
│   ├── materiels.html / materiel_detail.html / materiel_form.html
│   ├── inventaires.html / inventaire_detail.html / inventaire_form.html
│   ├── calendrier_conges.html / soldes_conges.html
│   ├── departements.html / documents.html / historique.html
│   ├── rapports.html / notifications.html / audit.html
│   ├── register.html / login.html
│   ├── self_service*.html
│   └── emails/
├── tests/                  # Tests pytest (conftest + test_*.py)
└── README.md
```

---

## 📌 Notes importantes

- Base de données exclusivement PostgreSQL
- Les migrations de production sont versionnées par Alembic et appliquées dans le `preDeployCommand`, jamais par les workers web
- Les contraintes `CHECK ... NOT VALID` protègent immédiatement les nouvelles écritures sans bloquer une base historique ; les index uniques protègent les workflows concurrents
- Les soldes de congés sont recalculés automatiquement (~2,08 jours acquis par mois, plafond annuel de 25 jours)
- Les retards sont calculés en minutes par rapport à `HEURE_ARRIVEE_ATTENDUE` (09:00 par défaut)
- Les exports incluent le calcul des retards
- Les uploads (documents et photos) sont validés sur leur **contenu réel** (magic-bytes), pas seulement sur l'extension
- Le stock des matériels n'est jamais édité à la main : il découle des mouvements, y compris des ajustements d'inventaire (traçabilité complète)
- Sécurité applicative activée (CSRF, rate limiting, headers HTTP via Talisman) ; `SECRET_KEY` et `FLASK_DEBUG` sont lus depuis l'environnement
- En production, Redis partage les quotas entre workers et Talisman force HTTPS avec des cookies de session sécurisés

### Tâches planifiées (APScheduler)

| Fréquence | Tâche                                           |
|-----------|-------------------------------------------------|
| 01h00     | Génération automatique des absences             |
| 01h30     | Alertes d'expiration des documents              |
| 02h00     | Recalcul des soldes de congés                   |
| 02h30     | Alertes contrats à J-30, J-7 et expiration      |
| 03h00     | Purge des sessions expirées (> 30 jours)        |
| 03h30     | Validation automatique des retours maintenance  |
| 60 s      | Traitement de l'outbox (si `EMAIL_ENABLED=true`) |

---

## 📄 Licence

Ce projet est distribué sous licence **MIT**. Vous pouvez l'utiliser, le copier,
le modifier et le redistribuer, y compris dans un contexte commercial, à
condition de conserver la notice de copyright et la licence.

Copyright © 2026 Stevyne. Consultez le fichier [LICENSE](LICENSE) pour le texte
juridique complet.

---

**Développé avec ❤️ en Flask + PostgreSQL**

Pour toute question ou contribution, contactez l'administrateur système.
