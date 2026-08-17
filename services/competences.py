"""Logique métier du module Compétences.

Les fonctions pures (sans Flask) pour faciliter les tests unitaires
et la réutilisation par les prochains modules (évaluations, formations…).
"""

NIVEAU_LABELS = [
    (0, 20, 'Débutant', 'not-acquired'),
    (20, 40, 'Notions', 'beginner'),
    (40, 60, 'Intermédiaire', 'intermediate'),
    (60, 80, 'Confirmé', 'confirmed'),
    (80, 101, 'Expert', 'expert'),
]


def libelle_niveau(niveau):
    """Renvoie (label, classe_css) pour un niveau 0..100."""
    try:
        n = int(niveau)
    except (TypeError, ValueError):
        return ('—', '')
    n = max(0, min(100, n))
    for lo, hi, label, cls in NIVEAU_LABELS:
        if lo <= n < hi:
            return (label, cls)
    return ('Expert', 'expert')


def lignes_texte(texte):
    """Split un champ texte multi-lignes (utile pour les notes libres)."""
    if not texte:
        return []
    return [ligne.strip() for ligne in str(texte).splitlines() if ligne.strip()]
