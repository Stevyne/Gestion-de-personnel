"""Conversion d'un montant en toutes lettres (français).

Sert à rédiger la mention légale des reçus et fiches de paie :
« je reconnais avoir reçu la somme de : quatre cent mille Ariary ».

La numération suivie est celle du français de référence — la même qu'à
Madagascar : 70 se dit « soixante-dix » et non « septante », 90
« quatre-vingt-dix » et non « nonante ».

Règles d'accord gérées, parce qu'une fiche de paie se relit :
    * « vingt » et « cent » ne prennent un « s » que lorsqu'ils terminent
      le nombre : 80 -> « quatre-vingts », 80 000 -> « quatre-vingt mille »,
      400 -> « quatre cents », 400 000 -> « quatre cent mille ».
    * « mille » est invariable et ne prend jamais de « un » : 1 000 se dit
      « mille », pas « un mille ».
    * « million » et « milliard » sont des noms : ils prennent la marque du
      pluriel (« deux millions ») et gardent leur article (« un million »).

Au-delà de 999 999 999 999 la fonction renvoie le nombre en chiffres plutôt
que d'inventer une forme : un montant pareil n'entre pas dans un reçu, et
une conversion fausse serait pire qu'une conversion absente.
"""
from decimal import Decimal, ROUND_HALF_UP

_UNITES = (
    "zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit",
    "neuf", "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize",
    "dix-sept", "dix-huit", "dix-neuf",
)

_DIZAINES = {
    2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante",
    7: "soixante", 8: "quatre-vingt", 9: "quatre-vingt",
}

# 10^3, 10^6, 10^9 — traités du plus grand au plus petit.
_ECHELLES = ((10 ** 9, "milliard"), (10 ** 6, "million"), (10 ** 3, "mille"))

_PLAFOND = 10 ** 12


def _centaine(nombre, terminal=True):
    """Convertit un entier de 0 à 999.

    `terminal` indique si ce morceau finit le nombre : c'est ce qui
    décide de l'accord de « vingt » et de « cent ».
    """
    if nombre < 20:
        return _UNITES[nombre]

    if nombre < 100:
        dizaine, unite = divmod(nombre, 10)
        mot = _DIZAINES[dizaine]
        if dizaine in (7, 9):
            # 70-79 se construit sur « soixante », 90-99 sur « quatre-vingt ».
            if unite == 0:
                return mot + "-dix"
            if unite == 1:
                return "soixante et onze" if dizaine == 7 else "quatre-vingt-onze"
            return "%s-%s" % (mot, _UNITES[10 + unite])
        if unite == 0:
            return mot + ("s" if dizaine == 8 and terminal else "")
        if unite == 1:
            return mot + ("-un" if dizaine == 8 else " et un")
        return "%s-%s" % (mot, _UNITES[unite])

    centaines, reste = divmod(nombre, 100)
    if centaines == 1:
        mot = "cent"
    else:
        mot = _UNITES[centaines] + " cent" + ("s" if reste == 0 and terminal else "")
    return mot if reste == 0 else "%s %s" % (mot, _centaine(reste, terminal))


def _groupes(nombre, terminal=True):
    """Découpe le nombre par milliers (milliards, millions, milles)."""
    for valeur, echelle in _ECHELLES:
        if nombre >= valeur:
            quotient, reste = divmod(nombre, valeur)
            if quotient == 1 and echelle == "mille":
                tete = "mille"
            else:
                tete = "%s %s" % (_groupes(quotient, terminal=False), echelle)
                if echelle in ("million", "milliard") and quotient > 1:
                    tete += "s"
            return tete if reste == 0 else "%s %s" % (tete, _groupes(reste, terminal))
    return _centaine(nombre, terminal)


def montant_en_lettres(montant, devise="Ariary", majuscule=False):
    """Convertit un montant en lettres, devise comprise.

    >>> montant_en_lettres(400000)
    'quatre cent mille Ariary'
    >>> montant_en_lettres(80)
    'quatre-vingts Ariary'
    >>> montant_en_lettres(1000)
    'mille Ariary'
    """
    try:
        nombre = int(Decimal(str(montant)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        return ""

    if nombre == 0:
        texte = "zéro"
    elif abs(nombre) >= _PLAFOND:
        texte = str(nombre)
    else:
        signe = "moins " if nombre < 0 else ""
        texte = signe + _groupes(abs(nombre))

    if devise:
        texte = "%s %s" % (texte, devise)
    return texte.upper() if majuscule else texte
