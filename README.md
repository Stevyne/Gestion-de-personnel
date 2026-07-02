# 👥 Gestion du Personnel

**Application RH complète en Flask + PostgreSQL**  
Système de gestion du personnel moderne, multi-utilisateur et professionnel.

> **Interface entièrement en français** • Support réel de plusieurs utilisateurs simultanés • Notifications persistantes par utilisateur

---

## ✨ Fonctionnalités principales

### 👤 Gestion des employés
- CRUD complet des employés
- Affectation aux départements
- Historique des salaires et dates d'embauche

### 🕒 Gestion des présences
- Pointage entrée / sortie
- Calcul automatique des retards (basé sur 09:00)
- Historique détaillé
- Envoi automatique d'emails HTML en cas de retard

### 🏖️ Gestion des congés
- Demandes de congés (self-service)
- Approbation / refus par admin/RH
- **Soldes de congés** (25 jours par défaut, recalcul automatique)
- Calendrier des congés

### 📁 Documents & Rapports
- Upload de documents (PDF, Excel, images...)
- Rapports avancés avec filtres (présences / congés)
- Exports **PDF** et **Excel** (présences et congés)

### 🔔 Notifications
- Notifications persistantes en base de données
- **Support multi-utilisateur réel** (filtrage par `user_id`)
- Badge de notifications non lues dans la navigation
- Page dédiée `/notifications`

### 🔐 Sécurité & Rôles
- Authentification par session
- 4 rôles : `admin`, `rh`, `manager`, `employe`
- Self-service pour les employés (`/mon-espace`)
- Logs d'audit complets

### 📧 Emails
- Emails HTML pour les retards
- Configuration via variables d'environnement (Gmail, etc.)
- Mode démo si pas de credentials

---

## 🛠️ Technologies

- **Backend** : Flask 3.0
- **Base de données** : PostgreSQL (psycopg2 + RealDictCursor)
- **Exports** : ReportLab (PDF) + Openpyxl (Excel)
- **Emails** : Flask-Mail
- **Frontend** : HTML + CSS responsive (mobile-first)
- **Auth** : Werkzeug (hashage sécurisé)

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
cd /gestion_personnel

pip install -r requirements.txt
```

### 3. Configuration de la base de données

```bash
# Créer la base
sudo -u postgres psql
```

Dans psql :
```sql
CREATE DATABASE gestion_personnel;
CREATE USER postgres WITH PASSWORD 'postgres';
GRANT ALL PRIVILEGES ON DATABASE gestion_personnel TO postgres;
\q
```

### 4. Variables d'environnement (optionnel)

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/gestion_personnel"

# Emails (optionnel)
export MAIL_USERNAME="votre@gmail.com"
export MAIL_PASSWORD="votre_app_password"
export MAIL_DEFAULT_SENDER="gestion.personnel@entreprise.fr"
```

---

## 🚀 Lancement

```bash
cd /gestion_personnel

# Initialisation automatique de la base + données de démo
python app.py
```

L'application démarre sur : **http://0.0.0.0:5000**

> **Note** : La première exécution crée automatiquement les tables et les utilisateurs par défaut.

---

## 👤 Utilisateurs par défaut

| Utilisateur | Mot de passe | Rôle       | Description              |
|-------------|--------------|------------|--------------------------|
| `admin`     | `admin123`   | admin      | Accès complet            |
| `rh`        | `rh123`      | rh         | Ressources Humaines      |
| `manager`   | `manager123` | manager    | Chef de projet           |
| `employe`   | `user123`    | employe    | Employé classique        |

---

## 📍 Routes principales

| Route                        | Description                          | Accès          |
|-----------------------------|--------------------------------------|----------------|
| `/`                         | Tableau de bord                      | Connecté       |
| `/employes`                 | Liste des employés                   | Tous           |
| `/presences`                | Pointages + Clock in/out             | Tous           |
| `/conges`                   | Gestion des congés + soldes          | Tous           |
| `/rapports`                 | Rapports avancés + filtres           | Tous           |
| `/documents`                | Upload et gestion de documents       | Tous           |
| `/notifications`            | Centre de notifications              | Tous           |
| `/self-service` ou `/mon-espace` | Espace personnel (self-service) | Tous           |
| `/audit`                    | Logs d'audit                         | admin, rh      |
| `/export/.../pdf` et `/excel` | Exports                              | Connecté       |

---

## 🔄 Support Multi-Utilisateur (Concurrent)

L'application est conçue pour un usage **réel multi-utilisateur** :

- Utilisation de `threaded=True` dans Flask
- Toutes les notifications sont stockées en base avec un `user_id`
- Filtrage systématique : `WHERE user_id = %s`
- Sessions Flask isolées par utilisateur
- Pas de variables globales partagées (`NOTIFICATIONS = []` supprimé)

**Recommandation production** :
```bash
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

---

## 📁 Structure du projet

```
gestion_personnel/
├── app.py                  # Application principale (1436 lignes)
├── requirements.txt
├── static/
│   ├── style.css
│   └── uploads/            # Fichiers uploadés
├── templates/              # 22 templates (tous en français)
│   ├── base.html
│   ├── notifications.html
│   ├── conges.html
│   ├── rapports.html
│   ├── documents.html
│   └── ...
└── README.md
```

---

## 🧪 Tests & Qualité

- Toutes les fonctionnalités critiques sont testées via le client Flask
- Isolation complète des données par utilisateur
- Syntaxe Python validée
- Pas de données globales non thread-safe

---

## 📌 Notes importantes

- La base de données est **exclusivement PostgreSQL**
- Les soldes de congés sont recalculés automatiquement
- Les retards sont calculés en minutes à partir de 09:00
- Les exports incluent le calcul des retards
- Le projet est prêt pour un usage professionnel

---

## 📄 Licence

Projet interne – 2026

---

**Développé avec ❤️ en Flask + PostgreSQL**

Pour toute question ou contribution, contactez l'administrateur système.