"""Logique métier du module Objectifs.

Constantes et helpers purs (sans dépendance Flask).
"""

# --- Statuts et workflow ---
STATUT_BROUILLON = 'brouillon'
STATUT_EN_COURS = 'en_cours'
STATUT_ATTEINT = 'atteint'           # marqué comme terminé par l'employé, à valider
STATUT_NON_ATTEINT = 'non_atteint'
STATUT_ANNULE = 'annule'

STATUT_LABELS = {
    STATUT_BROUILLON: 'Brouillon',
    STATUT_EN_COURS: 'En cours',
    STATUT_ATTEINT: '✅ Atteint (à valider)',
    STATUT_NON_ATTEINT: '❌ Non atteint',
    STATUT_ANNULE: 'Annulé',
}
STATUT_BADGES = {
    STATUT_BROUILLON: 'brouillon',
    STATUT_EN_COURS: 'en_cours',
    STATUT_ATTEINT: 'atteint',
    STATUT_NON_ATTEINT: 'non_atteint',
    STATUT_ANNULE: 'annule',
}

# Priorités
PRIORITES = ('basse', 'normale', 'haute', 'critique')
PRIORITE_LABELS = {
    'basse': 'Basse',
    'normale': 'Normale',
    'haute': 'Haute',
    'critique': 'Critique',
}

# Catégories d'objectifs proposées par défaut (le champ est libre, mais on
# propose ces valeurs dans le datalist du formulaire).
CATEGORIES_DEFAUT = [
    'Performance', 'Projet', 'Formation', 'Qualité',
    'Management', 'Innovation', 'Client', 'Sécurité',
]

# Qui peut faire quoi selon le rôle et le statut de l'objectif.
# Le créateur initial d'un brouillon peut le modifier même s'il est l'employé ;
# un manager peut transformer n'importe quel brouillon visible en en_cours.


def progression_couleur(progression, statut):
    if statut == STATUT_ATTEINT:
        return 'atteint'
    if statut == STATUT_NON_ATTEINT:
        return 'echec'
    if statut == STATUT_ANNULE:
        return 'annule'
    if progression >= 100:
        return 'atteint'
    if progression >= 70:
        return 'confirme'
    if progression >= 40:
        return 'intermediaire'
    if progression >= 10:
        return 'debute'
    return 'not-started'


def statut_final(statut):
    return statut in (STATUT_ATTEINT, STATUT_NON_ATTEINT, STATUT_ANNULE)
