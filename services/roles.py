"""Référentiel unique des rôles applicatifs.

Toute validation Python, formulaire ou contrainte PostgreSQL doit utiliser ces
codes. Les libellés restent séparés des valeurs persistées en base.
"""

ROLE_ADMIN = 'admin'
ROLE_RH = 'rh'
ROLE_MANAGER = 'manager'
ROLE_TECHNICIEN = 'technicien'
ROLE_EMPLOYE = 'employe'

ROLE_CODES = (
    ROLE_ADMIN, ROLE_RH, ROLE_MANAGER, ROLE_TECHNICIEN, ROLE_EMPLOYE,
)
GLOBAL_DATA_ROLES = (ROLE_ADMIN, ROLE_RH)

ROLE_LABELS = {
    ROLE_ADMIN: 'Administrateur',
    ROLE_RH: 'Responsable RH',
    ROLE_MANAGER: 'Manager',
    ROLE_TECHNICIEN: 'Technicien',
    ROLE_EMPLOYE: 'Employé',
}

ROLE_CHOICES = tuple((code, ROLE_LABELS[code]) for code in ROLE_CODES)
