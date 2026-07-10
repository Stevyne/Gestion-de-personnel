# 👥 Gestion du Personnel

**Application RH complète en Flask + PostgreSQL**
Système de gestion du personnel multi-utilisateur avec suivi des présences, congés, documents et notifications.

> Interface entièrement en français • Multi-utilisateur réel (isolation par `user_id`) • Sécurité CSRF / rate limiting / headers HTTP

---

## ✨ Fonctionnalités principales

### 👤 Gestion des employés
- CRUD complet des employés (`/employes`, ajout via `/employes/add` ou `/add_employee`)
- Affectation aux départements (CRUD départements via `/departements`)
- Historique des salaires et dates d'embauche
- Page `/historique` dédiée

### 🕒 Gestion des présences
- Pointage entrée / sortie (`/presences/clock_in`, `/presences/clock_out`)
- Calcul automatique des retards (seuil configurable, `HEURE_ARRIVEE_ATTENDUE = "09:00"` dans `app.py`)
- Envoi automatique d'emails HTML en cas de retard (mode démo si pas de credentials mail)

### 🏖️ Gestion des congés
- Demandes de congés en self-service (`/self-service/conges`)
- Approbation / refus par admin ou RH
- Soldes de congés (25 jours acquis par défaut, table `soldes_conges`, recalcul automatique)
- Calendrier des congés : vue mensuelle des congés **approuvés** (`/calendrier-conges`)

### 📁 Documents & Rapports
- Upload de documents (PDF, Excel, images...)
- Rapports avancés avec filtres (`/rapports`)
- Exports PDF (ReportLab) et Excel (Openpyxl) pour présences et congés

### 🔔 Notifications
- Notifications persistantes en base, filtrées par `user_id`
- Badge de notifications non lues, page dédiée `/notifications`

### 🔐 Sécurité & Rôles
- Authentification par session (Werkzeug pour le hash des mots de passe)
- 4 rôles : `admin`, `rh`, `manager`, `employe`
- Self-service pour les employés (`/self-service` ou `/mon-espace`)
- Logs d'audit (`/audit`, réservé à `admin`/`rh`)
- Protection CSRF (Flask-WTF), rate limiting (Flask-Limiter), headers de sécurité (Flask-Talisman)
- Formulaire d'inscription (`/register`)

---

## 🛠️ Technologies

| Catégorie      | Choix                                              |
|----------------|-----------------------------------------------------|
| Backend        | Flask 3.0.3                                          |
| Base de données| PostgreSQL (psycopg2-binary, RealDictCursor)         |
| Sécurité       | Flask-WTF (CSRF), Flask-Limiter, Flask-Talisman, python-dotenv |
| Emails         | Flask-Mail                                           |
| Exports        | ReportLab (PDF), Openpyxl (Excel)                    |
| Frontend       | HTML + CSS responsive mobile-first (pas de framework JS) |
| Auth           | Werkzeug (hash des mots de passe)                    |

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

| Variable | Description |
|---|---|
| `SECRET_KEY` | Clé secrète Flask — à générer avec `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL` | Chaîne de connexion PostgreSQL |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS` | Config SMTP |
| `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER` | Identifiants email (optionnel, mode démo sinon) |
| `ADMIN_EMAIL` | Destinataire des alertes admin |
| `FLASK_ENV`, `FLASK_DEBUG` | Mode d'exécution |
| `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_HTTPONLY`, `SESSION_COOKIE_SAMESITE` | Sécurité des cookies de session |

> ✅ **Bonnes nouvelles** : `SECRET_KEY` et `FLASK_DEBUG` sont **déjà lus depuis l'environnement** (aucune valeur sensible codée en dur dans `app.py`). En production, l'absence de `SECRET_KEY` lève une erreur (`RuntimeError`), et `FLASK_DEBUG=true` combiné à `FLASK_ENV=production` est bloqué.
>
> ⚠️ **Points à vérifier avant mise en production** :
> - `DATABASE_URL` a un fallback avec un mot de passe en clair (`Stevyne123`) : ne vous y fiez jamais, renseignez toujours la variable d'environnement.
> - Le rate limiter utilise `storage_uri="memory://"` (compteur **par processus**). Avec `gunicorn -w 4`, les quotas ne sont **pas partagés** entre workers → passez sur Redis/Memcached dès que vous dépassez 1 worker.
> - `Talisman(force_https=False)` et `SESSION_COOKIE_SECURE` (défaut `false`) : passez-les à `True` en HTTPS.
> - `.env.example` livre `FLASK_DEBUG=true` / `FLASK_ENV=development` : mettez-les à `false` / `production` pour la prod.
> - Les uploads ne valident que l'extension de fichier : ajoutez une vérification du type MIME côté serveur.

---

## 🚀 Lancement

```bash
python app.py
```

L'application démarre sur **http://0.0.0.0:5000**

> La première exécution crée automatiquement les tables et les utilisateurs par défaut.

**En production**, ne pas utiliser le serveur de développement Flask :
```bash
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

---

## 👤 Utilisateurs par défaut

| Utilisateur | Mot de passe | Rôle    | Description         |
|-------------|---------------|---------|----------------------|
| `admin`     | `admin123`    | admin   | Accès complet        |
| `rh`        | `rh123`       | rh      | Ressources Humaines  |
| `manager`   | `manager123`  | manager | Chef de projet       |
| `employe`   | `user123`     | employe | Employé classique    |

> À changer immédiatement en environnement réel — ces identifiants sont créés automatiquement par `init_db()`.

---

## 📍 Routes principales

| Route | Description | Accès |
|---|---|---|
| `/` | Tableau de bord | Connecté |
| `/login`, `/logout`, `/register` | Authentification et inscription | Public / Connecté |
| `/employes`, `/employes/add`, `/employes/<id>`, `/employes/<id>/edit`, `/employes/<id>/delete` | Gestion des employés | Tous / selon rôle |
| `/departements`, `/departements/add`, `/departements/edit/<id>`, `/departements/delete/<id>` | Gestion des départements | Selon rôle |
| `/presences`, `/presences/clock_in/<id>`, `/presences/clock_out/<id>`, `/presences/add`, `/presences/delete/<id>` | Pointages | Tous / selon rôle |
| `/conges`, `/conges/add`, `/conges/update/<id>`, `/conges/delete/<id>` | Congés | Tous / selon rôle |
| `/calendrier-conges` | Calendrier des congés | Tous |
| `/rapports` | Rapports avancés + filtres | Tous |
| `/documents`, `/documents/delete/<id>` | Documents | Tous |
| `/historique` | Historique salaires / embauches | Tous |
| `/notifications`, `/notifications/mark-read` | Centre de notifications | Tous |
| `/self-service`, `/mon-espace`, `/self-service/presences`, `/self-service/conges` | Espace personnel | Tous |
| `/audit` | Logs d'audit | admin, rh |
| `/export/presences/pdf`, `/export/presences/excel`, `/export/conges/pdf`, `/export/conges/excel` | Exports | Connecté |

---

## 🔄 Support multi-utilisateur (concurrent)

- `threaded=True` dans Flask
- Notifications stockées en base avec `user_id`, filtrage systématique `WHERE user_id = %s`
- Sessions Flask isolées par utilisateur
- Pas de variable globale partagée pour les notifications

---

## 🧪 Tests

Les tests s'appuient sur une **vraie base PostgreSQL** de test (pas de mock), configurée dans `tests/conftest.py`.

```bash
# 1. Créer la base de test
createdb gestion_personnel_test

# 2. Installer les dépendances de dev
pip install -r requirements-dev.txt

# 3. Lancer les tests
pytest
```

> Surchargez la base avec la variable `TEST_DATABASE_URL` si besoin (CI, autre machine). Le CSRF et le rate limiting sont désactivés pendant les tests (`conftest.py`).

---

## 📁 Structure du projet

```
Gestion-de-personnel/
├── app.py                  # Application principale (routes, modèles, exports)
├── requirements.txt        # Dépendances production
├── requirements-dev.txt    # Dépendances dev (pytest)
├── pytest.ini
├── .env.example
├── static/
│   ├── style.css
│   └── uploads/            # Fichiers uploadés (ignorés par git)
├── templates/              # Templates, tous en français
│   ├── base.html
│   ├── index.html
│   ├── dashboard.html
│   ├── presences.html
│   ├── conges.html
│   ├── calendrier_conges.html
│   ├── departements.html
│   ├── documents.html
│   ├── historique.html
│   ├── rapports.html
│   ├── notifications.html
│   ├── audit.html
│   ├── register.html / login.html
│   ├── self_service*.html
│   └── emails/
├── tests/                  # Tests pytest (conftest + test_*.py)
└── README.md
```

---

## 📌 Notes importantes

- Base de données exclusivement PostgreSQL
- Les soldes de congés sont recalculés automatiquement (25 jours acquis par défaut)
- Les retards sont calculés en minutes par rapport à `HEURE_ARRIVEE_ATTENDUE` (09:00 par défaut)
- Les exports incluent le calcul des retards
- Sécurité applicative activée (CSRF, rate limiting, headers HTTP via Talisman) ; `SECRET_KEY` et `FLASK_DEBUG` sont lus depuis l'environnement
- En production : prévoir un stockage de rate limiting partagé (Redis), activer HTTPS (`force_https` + `SESSION_COOKIE_SECURE`) et valider le type MIME des uploads

---

## 📄 Licence

Projet interne – 2026

---

**Développé avec ❤️ en Flask + PostgreSQL**
Pour toute question ou contribution, contactez l'administrateur système.