"""Services communs sans dépendance Flask ni accès direct à la base."""

from datetime import date


TAUX_ACQUISITION_CONGES_PAR_MOIS = 25 / 12

# Types de congé — source unique de vérité. Le formulaire de congé, le
# circuit de justification d'absence et les filtres lisent cette liste.
#
# Elle était jusqu'ici répétée à la main dans un gabarit, et le circuit
# d'absence créait pour sa part « congé maladie » là où le formulaire
# proposait « maladie » : deux écritures du même type, qui se retrouvaient
# côte à côte dans les calendriers et les exportations. Une seule liste
# évite que l'écart ne se reproduise.
TYPES_CONGE = (
    'congé payé',
    'congé sans solde',
    'maladie',
    'maternité / paternité',
    'récupération',
    'autre',
)

# Type retenu quand personne n'en a proposé : la maladie reste le motif le
# plus fréquent derrière une pièce justificative.
TYPE_CONGE_DEFAUT = 'maladie'

# Accepter un justificatif sans rien créer dans les congés. L'absence est
# excusée, mais elle n'entre ni dans le calendrier ni dans les soldes —
# utile pour une absence régularisée qui ne doit rien consommer.
TYPE_CONGE_AUCUN = 'aucun'


def pagination_info(total, page, per_page=10):
    pages = max(1, (total + per_page - 1) // per_page) if per_page else 1
    page = max(1, min(page, pages))
    return {
        'page': page, 'per_page': per_page, 'total': total, 'pages': pages,
        'has_prev': page > 1, 'has_next': page < pages,
    }


def page_list(page, pages):
    if pages <= 9:
        return list(range(1, pages + 1))
    items = [1]
    lo, hi = max(2, page - 1), min(pages - 1, page + 1)
    if lo > 2:
        items.append('...')
    items += list(range(lo, hi + 1))
    if hi < pages - 1:
        items.append('...')
    items.append(pages)
    return items


def calculer_retard(heure, heure_attendue='09:00'):
    if not heure:
        return 0
    try:
        if isinstance(heure, str):
            hh, mm = map(int, heure.split(':')[:2])
        else:
            hh, mm = heure.hour, heure.minute
        ha, ma = map(int, heure_attendue.split(':'))
        return max(0, (hh * 60 + mm) - (ha * 60 + ma))
    except (TypeError, ValueError, AttributeError):
        return 0


def calculer_jours_acquis_prorata(date_embauche, annee):
    debut_annee = date(annee, 1, 1)
    fin_annee = date(annee, 12, 31)
    aujourd_hui = date.today()
    if date_embauche and date_embauche > debut_annee:
        debut_calcul = max(date_embauche.replace(day=1), debut_annee)
    else:
        debut_calcul = debut_annee
    fin_calcul = min(aujourd_hui, fin_annee)
    if fin_calcul < debut_calcul:
        return 0.0
    mois = (
        (fin_calcul.year - debut_calcul.year) * 12
        + fin_calcul.month - debut_calcul.month + 1
    )
    return round(
        max(0, min(12, mois)) * TAUX_ACQUISITION_CONGES_PAR_MOIS, 1
    )
