"""Règles transparentes de calcul et référentiels du recrutement."""

from decimal import Decimal, ROUND_HALF_UP


DEMANDE_STATUTS = {
    'en_attente': 'En attente', 'validee': 'Validée', 'refusee': 'Refusée',
    'annulee': 'Annulée', 'publiee': 'Offre créée',
}
OFFRE_STATUTS = {
    'brouillon': 'Brouillon', 'publiee': 'Publiée', 'fermee': 'Fermée',
    'suspendue': 'Suspendue', 'pourvue': 'Pourvue',
}
CANDIDATURE_STATUTS = {
    'recue': 'Reçue', 'preselectionnee': 'Présélectionnée',
    'entretien': 'Entretien', 'evaluation': 'Évaluation',
    'acceptee': 'Acceptée', 'refusee': 'Refusée', 'embauchee': 'Embauchée',
}
ENTRETIEN_STATUTS = {
    'planifie': 'Planifié', 'realise': 'Réalisé', 'annule': 'Annulé',
}
TYPE_CONTRATS = {
    'cdi': 'CDI', 'cdd': 'CDD', 'stage': 'Stage',
    'consultant': 'Consultant', 'autre': 'Autre',
}
CRITERES_ENTRETIEN = (
    'Technique', 'Communication', 'Motivation',
    'Travail en équipe', 'Adaptabilité',
)


def lignes_texte(valeur):
    """Normalise une liste saisie ligne par ligne ou séparée par des virgules."""
    if not valeur:
        return []
    morceaux = str(valeur).replace(',', '\n').splitlines()
    resultat = []
    for morceau in morceaux:
        propre = morceau.strip(' \t-•')
        if propre and propre.casefold() not in {x.casefold() for x in resultat}:
            resultat.append(propre[:150])
    return resultat


def calculer_score_pondere(criteres, notes):
    """Calcule une aide à la décision sans modifier le statut du candidat."""
    poids_total = Decimal('0')
    total = Decimal('0')
    for critere in criteres:
        note = notes.get(int(critere['id']))
        if note is None:
            continue
        poids = Decimal(str(critere['poids']))
        total += Decimal(str(note)) * poids
        poids_total += poids
    if poids_total == 0:
        return None
    return float((total / poids_total).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def calculer_score_global(score_dossier, scores_entretiens):
    """40 % dossier + 60 % moyenne entretiens ; dossier seul avant entretien."""
    scores = [float(x) for x in scores_entretiens if x is not None]
    if score_dossier is None and not scores:
        return None, None
    moyenne_entretien = round(sum(scores) / len(scores), 2) if scores else None
    if score_dossier is None:
        global_ = moyenne_entretien
    elif moyenne_entretien is None:
        global_ = float(score_dossier)
    else:
        global_ = round(float(score_dossier) * 0.4 + moyenne_entretien * 0.6, 2)
    return moyenne_entretien, global_


def recommandation(score):
    if score is None:
        return 'À évaluer', 'neutre'
    if float(score) >= 75:
        return 'Recommandé', 'positive'
    if float(score) >= 60:
        return 'À approfondir', 'attention'
    return 'Réserves', 'negative'
