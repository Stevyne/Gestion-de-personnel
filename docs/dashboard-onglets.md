# Tableau de bord : onglets locaux préchargés

## Comportement

Une seule réponse HTML contient les données et le contenu de **tous les onglets
que le compte peut consulter**. Cliquer sur Général, RH, Parc ou Direction
change uniquement le panneau visible : pas de `fetch`, d’appel AJAX, de
rechargement de document ni de voile de chargement pour cette bascule.

- Les URL historiques sont conservées : `/`, `/dashboard/rh`,
  `/dashboard/parc`, `/dashboard/direction`.
- Un lien direct ouvre le bon onglet et précharge les autres vues autorisées.
- `history.pushState` reflète l’onglet actif dans l’adresse sans requête ;
  Précédent/Suivant le restaure localement. Reclique sur l’onglet actif : aucune
  entrée d’historique en double. Un rechargement retrouve la vue de l’URL.
- Ctrl/Cmd+clic et clic du milieu gardent leur comportement natif d’ouverture
  dans un autre onglet navigateur.
- Le lien **Actualiser** recharge la page et ses statistiques ; il suit l’onglet
  actif. Cette opération, comme la navigation vers Présences ou une autre page,
  conserve le chargement bloquant normal.
- Sans JavaScript ou si le contrôleur ne charge pas, les liens fonctionnent en
  navigation serveur classique. Les rôles ARIA d’onglets et `data-no-loading`
  ne sont ajoutés qu’après validation du document par le contrôleur.

Les chiffres sont ceux calculés pendant le chargement de la page, **pas des
indicateurs temps réel**. Changer d’onglet ne les actualise pas ; utiliser
Actualiser ou recharger le navigateur. Les éventuels changements de droits en
base sont réévalués à la prochaine requête, comme dans le reste de l’application,
pas par une vérification réseau à chaque bascule locale.

## Droits et cloisonnement

Le service `services/dashboard_tabs.py` combine les droits des routes et
`services.acl.endpoint_autorise` **avant de lancer les requêtes spécialisées**.
Le rôle est celui relu en base par `get_department_scope`, pas uniquement une
ancienne valeur du cookie. Aucun cache de statistiques partagé entre comptes
n’est introduit. Les réponses dashboard portent `Cache-Control: private, no-store`.

| Rôle | Onglets reçus | Portée des données |
| --- | --- | --- |
| admin / rh | Général, RH, Parc, Direction | Globale |
| technicien | Général, Parc | Département de sa fiche employé |
| manager | Général | Département de sa fiche employé |
| employe | Général | Département de sa fiche employé |

**Le manager ne gagne pas un nouvel accès Parc.** Le décorateur historique de la
route cite le manager, mais l’ACL centrale le refuse déjà. Le préchargement
respecte cette restriction effective ; ni panneau Parc masqué ni données RH/
Direction ne sont envoyés à ce rôle. `services/acl.py` n’est pas modifié.

Sans département rattaché, les statistiques Général et Parc d’un technicien
restent vides : aucun repli vers une portée globale. Les requêtes générales
conservent leurs filtres SQL et les montants salariaux globaux restent réservés
aux comptes admin/RH. Les routes directes gardent leurs décorateurs et le
filet de contrôle central.

## Architecture

- `blueprints/dashboard.py` : rendu commun `rendre_dashboard`, requêtes générales
  cloisonnées inchangées, chargement des panneaux autorisés dans le même
  curseur, puis réponse privée.
- `blueprints/dashboards_roles.py` : mêmes routes et endpoints, délégation au
  rendu commun avec l’onglet initial demandé. Pas de redirection supplémentaire.
- `services/dashboard_tabs.py` : matrice d’accès et requêtes spécialisées issues
  des anciens contrôleurs, sans changement de définition des indicateurs.
- `templates/dashboard.html` : enveloppe unique, navigation, panneaux et données
  Chart.js. `templates/_dashboard_general.html` conserve le contenu général.
  `dashboard_role.html` est une enveloppe de compatibilité vers le template unifié.
- `static/dashboard-tabs.js` : activation DOM, historique, titres et accessibilité ;
  aucun accès réseau, `innerHTML` ou remplacement de `window.fetch`.
- `static/dashboard-tabs.css` : styles du dashboard extraits de l’ancien template,
  masquage prioritaire des panneaux inactifs et focus visible.
- `templates/base.html` : l’entrée Tableau de bord reste active sur les quatre URL.
- `app.py` : injection de `calculer_retard` dans le blueprint spécialisé pour
  permettre le rendu général depuis chacun des points d’entrée.

Aucune dépendance, migration PostgreSQL, configuration Gunicorn, tâche planifiée,
clé ou variable d’environnement n’est ajoutée. Le chargement bloquant, le timer
de confirmation de 10 secondes et les icônes SVG restent ceux du correctif précédent.

## Graphiques et accessibilité

Les jeux de données et Chart.js local sont chargés au départ. La création des
graphiques attend que Général soit visible pour éviter les canvas de dimensions
nulles lors d’une arrivée directe sur RH, Parc ou Direction. Les instances sont
réutilisées et redimensionnées aux retours suivants, sans rechargement de données.

Le contrôleur installe `tablist`, `tab`, `tabpanel`, `aria-selected` et un seul
onglet dans l’ordre de tabulation. Flèches gauche/droite avec bouclage, Début,
Fin, Entrée et Espace sont gérés. Les panneaux inactifs sont réellement `hidden`
(absents de l’arbre accessible et du flux d’impression). La barre mobile fait
apparaître l’onglet actif horizontalement sans déplacer verticalement la page.
Les titres et descriptions sont affectés via `textContent`.

## Performances : ce que cela accélère

Cette technique supprime les allers-retours, calculs SQL et rendus répétés
**entre les vues de ce dashboard**. Le premier chargement transporte davantage
de contenu et calcule toutes les vues autorisées, y compris celles encore
inactives. Un compte admin/RH charge les neuf requêtes spécialisées existantes
en plus du général ; un technicien, les quatre du Parc ; manager/employé,
aucune requête spécialisée. Il n’y a pas de calcul supplémentaire lors des bascules.

Ce module seul ne convertit pas l’application en SPA. L’amélioration distincte
[Navigation globale](navigation-globale.md) conserve désormais la coque lors des
GET entre Employés, Présences, Documents, etc. Ces pages récupèrent toujours
leurs données et leurs droits côté serveur, contrairement aux onglets locaux
déjà présents dans le DOM. Aucun gain SQL ni gain sous charge n’est garanti ;
les index, la pagination et les coûts serveur restent à mesurer séparément.

## Vérification reproductible

Avec une **base de test distincte de la production** et les variables de test
habituelles du projet :

```bash
python -m pytest -q
node --test tests/js/*.test.cjs
node --check static/dashboard-tabs.js
```

- `tests/test_dashboard_tabs.py` : 29 cas couvrant les quatre points d’entrée,
  panneaux présents/absents, compte restreint sans requête spécialisée, technicien
  cloisonné ou sans département, évolution des rôles en base, absence de cache
  partagé, authentification et politique de préchargement.
- `tests/js/dashboard-tabs.test.cjs` : 17 cas sans dépendance npm couvrant clics,
  clavier, ARIA, historique, HTML incomplet, titres textuels, défilement horizontal,
  conservation du focus et absence de réseau.
- Vérifications navigateur réalisées séparément : 18 scénarios Chromium,
  incluant réseau coupé après l’arrivée, liens directs, graphique initialement
  masqué, Actualiser, vraie navigation, repli sans JS, Ctrl+clic et largeurs
  320/390/768/1440 px. Les résultats et limites complets figurent dans le guide
  de la livraison.
