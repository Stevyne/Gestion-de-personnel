# 👥 Gestion du Personnel

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
- Photo de profil affichée dans la liste et sur la fiche détaillée (à défaut : initiales)

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
- Dépôt self-service d'un justificatif PDF/PNG/JPEG (8 Mo), validé par magic-bytes et stocké en PostgreSQL (`BYTEA`)
- Statuts : `non_justifiee` → `justificatif_depose` → `acceptee` ou `refusee`; un refus RH exige un motif
- Un justificatif accepté crée automatiquement un **congé maladie approuvé** d'un jour, sans consommer le solde de congés payés
- Confidentialité : justificatif téléchargeable uniquement par son propriétaire et les rôles `admin`/`rh` (pas par le manager)
- Enregistrement manuel possible (`/absences/add`) ; suppression réservée à `admin`/`rh` et interdite après requalification

### 📦 Gestion des matériels (par département)
- Stock de fournitures et d'équipements rattaché à un **département** (papiers, stylos, classeurs, cartouches, mobilier, informatique...)
- **Traçabilité complète** : chaque entrée / sortie est enregistrée dans `materiels_mouvements` (quantité, motif, employé concerné, auteur, horodatage). Le stock n'est jamais édité à la main, il découle des mouvements
- **Attribution durable** à un employé (PC, téléphone, clés) : l'article sort du stock puis y revient au retour (`materiels_attributions`)
- **Alerte de stock bas** : seuil configurable par article, badge dans la liste et notification interne aux admin/RH/managers. Anti-spam (une seule notif tant que le stock n'est pas réapprovisionné)
- Filtres par nom, département, catégorie et état (stock bas / rupture), avec pagination
- Rôles : `admin`, `rh` et `manager` gèrent le stock ; la suppression est réservée à `admin`/`rh` ; les autres rôles consultent
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

### 🔎 Recherche globale
- Barre de recherche dans l'en-tête, avec **aperçu instantané** groupé par type et page complète `/recherche`
- Couvre les **employés, départements, matériels, congés, absences, documents, comptes** — et les pages de l'application (façon palette de navigation)
- **Filtrée par rôle côté serveur** : les catégories interdites ne sont pas interrogées du tout (un employé ne voit ni les congés ni les comptes, y compris en appelant l'API directement)
- Raccourci **Ctrl+K** / Cmd+K, navigation aux flèches, `Échap` pour fermer
- Sur écran étroit, le champ laisse place à une entrée « Rechercher » dans le tiroir de navigation
- Routes : `/recherche` (page), `/api/recherche?q=` (JSON pour l'aperçu)

### 🙋 Espace personnel (`/mon-profil`)
- Chaque utilisateur consulte sa fiche, corrige ses coordonnées (nom, prénom, email, téléphone) et change son mot de passe
- **Poste, département, salaire et date d'embauche sont en lecture seule** : ce sont des données contractuelles, du ressort des RH
- **Photo de profil** : PNG/JPEG jusqu'à 8 Mo, automatiquement recadrée en carré et réduite à 512×512 (Pillow). Le ré-encodage supprime au passage les métadonnées EXIF (dont la géolocalisation)
- La photo est portée par le **compte** (`users.photo`), donc disponible même pour les comptes sans fiche employé
- Fichiers stockés dans `static/avatars/` sous un nom imprévisible ; l'ancien est effacé à chaque remplacement
- Accessible via « Mon espace » dans le menu du compte

### 📁 Documents & Rapports
- Upload de documents (PDF, Excel, images...) avec **validation du contenu réel** (magic-bytes), pas seulement de l'extension
- Contenu stocké en PostgreSQL (`BYTEA`) et notification immédiate de l'employé concerné
- Alertes internes et e-mails avant expiration puis après expiration
- Rapports avancés avec filtres (`/rapports`)
- Exports PDF (ReportLab) et Excel (Openpyxl) pour présences et congés

### 🔔 Notifications et e-mails
- Notifications persistantes en base, filtrées par `user_id`; badge et page `/notifications`
- Événements couverts : absences, présences, documents, arrivée d'un employé, congés, permissions, matériel et maintenance
- E-mails sur les actions à traiter, décisions, absences et expirations de documents
- **Outbox PostgreSQL** (`email_outbox`) : envoi asynchrone par lots, verrou `SKIP LOCKED`, reprise après arrêt, jusqu'à cinq tentatives avec délai exponentiel et clés anti-doublon
- Mode développement propre : `EMAIL_ENABLED=false` par défaut, donc aucun contact SMTP

### 🔐 Sécurité & Rôles
- Authentification par session (Werkzeug pour le hash des mots de passe)
- 4 rôles : `admin`, `rh`, `manager`, `employe`
- Self-service pour les employés (`/self-service` ou `/mon-espace`)
- Logs d'audit (`/audit`, réservé à `admin`/`rh`)
- Protection CSRF (Flask-WTF), rate limiting (Flask-Limiter), headers de sécurité (Flask-Talisman)
- Création de comptes réservée aux admin/RH (`/register`) dans une popup depuis la page Utilisateurs, avec repli en page complète sans JavaScript ; choix immédiat du rôle et de l'employé lié, seul un administrateur pouvant créer un autre administrateur

### 🖥️ Sessions actives & déconnexion forcée
- Registre des sessions ouvertes en base (`sessions_actives`) : les cookies signés Flask n'étant pas révocables, un identifiant de session est stocké côté serveur
- La page `/utilisateurs` affiche un **badge d'état par compte** : `En ligne` (activité < 5 min, configurable via `SESSION_ONLINE_WINDOW_MIN`), `Inactif` (session ouverte sans activité récente), `Hors ligne`
- Un **administrateur peut forcer la déconnexion** d'un compte connecté ; la révocation prend effet à la requête suivante et l'action est tracée dans l'audit (`FORCE_LOGOUT`)
- Purge automatique des sessions de plus de 30 jours (tâche planifiée à 03h00)

### 🪟 Interface
- **Navigation compacte** : une seule ligne (~63 px), 5 groupes déroulants, icônes SVG, menu du compte avec avatar. Paliers responsive : complet ≥ 1151 px, compressé 1071–1150 px, icônes seules 769–1070 px, tiroir latéral ≤ 768 px
- **Formulaires en popup** : créations et modifications s'ouvrent dans une fenêtre modale sans quitter la page courante (avec repli sur la page classique si JavaScript est absent)
- Graphiques du tableau de bord responsives ; Chart.js servi **en local** (compatible avec la politique CSP)

---

## 🛠️ Technologies

| Catégorie       | Choix                                              |
|-----------------|----------------------------------------------------|
| Backend         | Flask 3.0.3                                        |
| Base de données | PostgreSQL (psycopg2-binary, RealDictCursor)       |
| Sécurité        | Flask-WTF (CSRF), Flask-Limiter, Flask-Talisman, python-dotenv |
| Emails          | Flask-Mail + outbox PostgreSQL asynchrone          |
| Exports         | ReportLab (PDF), Openpyxl (Excel)                  |
| Images          | Pillow (redimensionnement des photos de profil)    |
| Planification   | APScheduler (absences, alertes, soldes, sessions, outbox) |
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
> - Le rate limiter utilise `storage_uri="memory://"` (compteur **par processus**). Avec `gunicorn -w 4`, les quotas ne sont **pas partagés** entre workers → passez sur Redis/Memcached dès que vous dépassez 1 worker.
> - `Talisman(force_https=False)` et `SESSION_COOKIE_SECURE` (défaut `false`) : passez-les à `True` en HTTPS.
> - `.env.example` livre `FLASK_DEBUG=true` / `FLASK_ENV=development` : mettez-les à `false` / `production` pour la prod.
> - Configurez SMTP puis passez `EMAIL_ENABLED=true`; sans cette activation explicite, aucun e-mail ne quitte l'application.
> - Les avatars, documents RH et justificatifs récents sont persistés en base (`BYTEA`). `static/avatars/` et `static/uploads/` ne servent plus que de cache/repli pour les anciens fichiers.

---

## 🚀 Lancement

```bash
python app.py
```

L'application démarre sur **http://0.0.0.0:5000**

> La première exécution crée automatiquement les tables et les utilisateurs par défaut. Les migrations de schéma (ajout de colonnes, nouvelles tables) sont **idempotentes** et s'appliquent au démarrage : aucune commande manuelle n'est nécessaire lors d'une mise à jour.

**En production**, ne pas utiliser le serveur de développement Flask :

```bash
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

---

## 👤 Utilisateurs par défaut

| Utilisateur | Mot de passe | Rôle     | Description              |
|-------------|--------------|----------|--------------------------|
| `admin`     | `admin123`   | admin    | Accès complet            |
| `rh`        | `rh123`      | rh       | Ressources Humaines      |
| `manager`   | `manager123` | manager  | Chef de projet           |
| `employe`   | `user123`    | employe  | Employé classique        |

> À changer immédiatement en environnement réel — ces identifiants sont créés automatiquement par `init_db()`.

---

## 📍 Routes principales

| Route                              | Description                      | Accès                  |
|------------------------------------|----------------------------------|------------------------|
| `/`                                | Tableau de bord                  | Connecté               |
| `/login`, `/logout`                | Authentification / déconnexion   | Public / Connecté      |
| `/register`                        | Création d'un compte et rattachement salarié | admin, rh (admin pour rôle admin) |
| `/recherche`, `/api/recherche`     | Recherche globale (page + JSON)  | Connecté (filtré par rôle) |
| `/employes`, `/employes/add`, ...  | Gestion des employés             | Tous / selon rôle      |
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

> Surchargez la base avec la variable `TEST_DATABASE_URL` si besoin (CI, autre machine). Le CSRF, le rate limiting et SMTP sont désactivés pendant les tests (`conftest.py`). La suite couvre notamment le parc matériel, le workflow de maintenance, les justificatifs d'absence, les notifications événementielles et l'outbox.

> 💡 Si vous testez manuellement en enchaînant beaucoup de requêtes, le rate limiter (50/heure par route) finit par renvoyer `429 Too Many Requests` — ce n'est pas un bug de l'application. Redémarrez le serveur (le compteur est en mémoire) ou désactivez le limiteur dans votre script de test.

---

## 📁 Structure du projet

```
Gestion-de-personnel/
├── app.py                  # Application historique et composition des services
├── blueprints/
│   └── absence_justifications.py  # Premier domaine extrait du monolithe
├── services/
│   └── email_outbox.py     # File SMTP persistante, indépendante de Flask
├── requirements.txt        # Dépendances production
├── requirements-dev.txt    # Dépendances dev (pytest)
├── pytest.ini
├── .env.example
├── .gitignore
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
- Les migrations de schéma sont idempotentes et appliquées au démarrage
- Les soldes de congés sont recalculés automatiquement (~2,08 jours acquis par mois, plafond annuel de 25 jours)
- Les retards sont calculés en minutes par rapport à `HEURE_ARRIVEE_ATTENDUE` (09:00 par défaut)
- Les exports incluent le calcul des retards
- Les uploads (documents et photos) sont validés sur leur **contenu réel** (magic-bytes), pas seulement sur l'extension
- Le stock des matériels n'est jamais édité à la main : il découle des mouvements, y compris des ajustements d'inventaire (traçabilité complète)
- Sécurité applicative activée (CSRF, rate limiting, headers HTTP via Talisman) ; `SECRET_KEY` et `FLASK_DEBUG` sont lus depuis l'environnement
- En production : prévoir un stockage de rate limiting partagé (Redis) et activer HTTPS (`force_https` + `SESSION_COOKIE_SECURE`)

### Tâches planifiées (APScheduler)

| Fréquence | Tâche                                           |
|-----------|-------------------------------------------------|
| 01h00     | Génération automatique des absences             |
| 01h30     | Alertes d'expiration des documents              |
| 02h00     | Recalcul des soldes de congés                   |
| 03h00     | Purge des sessions expirées (> 30 jours)        |
| 03h30     | Validation automatique des retours maintenance  |
| 60 s      | Traitement de l'outbox (si `EMAIL_ENABLED=true`) |

---

## 📄 Licence

Projet interne – 2026

---

**Développé avec ❤️ en Flask + PostgreSQL**

Pour toute question ou contribution, contactez l'administrateur système.
