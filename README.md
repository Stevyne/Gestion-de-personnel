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
- Calendrier des congés (`/calendrier-conges`)

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

> ⚠️ **À corriger avant mise en production** : `app.secret_key` est actuellement codé en dur dans `app.py` (ligne 29) et n'est pas lu depuis `SECRET_KEY` malgré le `.env.example`. De même, `app.run(debug=True, ...)` est en dur en fin de fichier. Les deux doivent être basculés sur les variables d'environnement avant tout déploiement.

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

## 📁 Structure du projet

```
Gestion-de-personnel/
├── app.py                  # Application principale (~1900 lignes)
├── requirements.txt
├── .env.example
├── static/
│   └── style.css
├── templates/               # 22 templates, tous en français
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
└── README.md
```

---

## 📌 Notes importantes

- Base de données exclusivement PostgreSQL
- Les soldes de congés sont recalculés automatiquement (25 jours acquis par défaut)
- Les retards sont calculés en minutes par rapport à `HEURE_ARRIVEE_ATTENDUE` (09:00 par défaut)
- Les exports incluent le calcul des retards
- Sécurité applicative activée (CSRF, rate limiting, headers) mais **clé secrète et mode debug à externaliser avant tout déploiement**

---

## 📄 Licence

Projet interne – 2026

---

**Développé avec ❤️ en Flask + PostgreSQL**
Pour toute question ou contribution, contactez l'administrateur système.