"""Masquage automatique des liens vers des pages interdites.

Les décorateurs ``role_required`` et le filet central ``endpoint_autorise``
empêchent déjà d'*atteindre* une page interdite : l'utilisateur est
redirigé avec un message d'erreur. Mais il doit d'abord cliquer pour le
découvrir — le bouton était là, bien visible, et il mène nulle part.

Ce module supprime le problème à la source : au moment d'envoyer une page
HTML, on retire du document tout lien dont la cible n'est pas autorisée
pour le rôle courant. Un manager ne voit donc plus les boutons « Modifier »
ou « Supprimer » d'un employé, ni les entrées de menu vers le recrutement
ou le stock — ils n'existent tout simplement pas dans la page qu'il reçoit.

Portée et limites, assumées :

* Seuls les rôles filtrés (``manager``, ``employe``) sont concernés.
  ``admin`` et ``rh`` ont un accès global : leur réponse n'est jamais
  réécrite, ce qui leur évite le coût de traitement.
* Ne sont touchés que les liens **internes** du site. Les ancres, les
  adresses mail, les numéros de téléphone et les URL absolues vers
  l'extérieur sont laissés intacts.
* La résolution se fait sur l'URL : ``/employes/12/modifier`` est
  reconnu comme l'endpoint ``edit_employee`` quel que soit l'identifiant.
  Une URL qu'on ne sait pas résoudre est conservée — on ne supprime jamais
  ce qu'on n'a pas compris.
* Le retrait porte sur l'élément ``<a>`` entier, contenu compris : un
  bouton interdit ne doit pas laisser son libellé à l'écran.
"""
import re

from werkzeug.exceptions import MethodNotAllowed

from services.acl import ROLES_FILTRES, endpoint_autorise

# Un élément <a> complet. Les <a> imbriqués étant interdits en HTML, la
# recherche non gourmande s'arrête à la bonne balise fermante.
_LIEN = re.compile(rb"<a\b[^>]*>.*?</a>", re.S | re.I)
_HREF = re.compile(rb"href\s*=\s*([\"'])(.*?)\1", re.S | re.I)

# Préfixes qui désignent autre chose qu'une page du site.
_SCHEME = re.compile(rb"^[a-z][a-z0-9+.\-]*:", re.I)
_EXCLUS = (b"#", b"mailto:", b"tel:", b"javascript:", b"data:", b"sms:")


def _extraire_chemin(href):
    """Ramène un href au chemin à résoudre, ou None s'il est hors de portée."""
    href = href.strip()
    if not href or href.startswith(_EXCLUS):
        return None
    if _SCHEME.match(href):
        return None                      # http:, https:, ftp:… : site tiers
    href = href.decode("utf-8", "replace")
    # La querystring et le fragment ne comptent pas pour trouver l'endpoint.
    for separateur in ("#", "?"):
        if separateur in href:
            href = href.split(separateur, 1)[0]
    if not href.startswith("/"):
        return None                      # chemin relatif : on ne devine pas
    return href


class MasqueurLiens:
    """Réécrit une réponse HTML en retirant les liens non autorisés."""

    def __init__(self, app):
        self._app = app
        # Le plan d'URL ne bouge pas à l'exécution : un chemin résolu une
        # fois l'est pour toute la durée du processus.
        self._cache = {}

    def _endpoint_du_chemin(self, chemin):
        if chemin in self._cache:
            return self._cache[chemin]
        endpoint = self._resoudre_chemin(chemin)
        self._cache[chemin] = endpoint
        return endpoint

    #: Méthodes essayées à la suite. Un bouton de suppression pointe vers
    #: une route qui n'accepte que le POST : se limiter au GET laisserait
    #: passer tous ces liens-là, ce qui est justement le cas intéressant.
    _METHODES = ("GET", "POST", "PUT", "PATCH", "DELETE")

    def _resoudre_chemin(self, chemin):
        """Nom de l'endpoint derrière un chemin, ou None si on l'ignore."""
        adaptateur = self._app.url_map.bind(
            self._app.config.get("SERVER_NAME") or "localhost")
        for methode in self._METHODES:
            try:
                endpoint, _ = adaptateur.match(chemin, method=methode)
                return endpoint
            except MethodNotAllowed:
                continue        # chemin connu, mais pas avec cette méthode
            except Exception:
                # Chemin inconnu : on ne sait pas de quoi il s'agit, donc
                # on ne touche à rien.
                return None
        return None

    def filtrer(self, html, role):
        """Retourne ``html`` sans les liens interdits pour ``role``."""
        # Aucun rôle filtré => tout est autorisé => rien à réécrire.
        if role not in ROLES_FILTRES or not html:
            return html

        def remplacer(match):
            href = _HREF.search(match.group(0))
            if href is None:
                return match.group(0)        # <a> sans href : ce n'est pas un lien
            chemin = _extraire_chemin(href.group(2))
            if chemin is None:
                return match.group(0)
            endpoint = self._endpoint_du_chemin(chemin)
            if endpoint is None or endpoint == "static":
                return match.group(0)
            if endpoint_autorise(role, endpoint):
                return match.group(0)
            return b""                       # lien interdit : retiré entièrement

        return _LIEN.sub(remplacer, html)
