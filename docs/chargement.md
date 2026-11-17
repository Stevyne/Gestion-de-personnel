# Chargement bloquant et écran assombri

Barre linéaire indéterminée inspirée de Google/Material, dans une petite fenêtre
centrée sur un **voile sombre couvrant tout l'écran**. L'ensemble utilise le
front-end Flask/Jinja existant, sans CDN ni dépendance de production et sans
pourcentage de progression fictif.

## Comportement

- **Toutes les pages, y compris la connexion :** voile bleu-noir à 68 %
  d'opacité, au-dessus de la navigation, des popups et des panneaux d'activité.
  La barre animée de 4 px et « Chargement en cours… » restent lisibles au centre.
- **Verrouillage immédiat :** liens, champs, boutons, fermetures de popup,
  raccourcis de la page, clics, gestes tactiles et défilement du fond sont
  inaccessibles pendant une opération suivie. Tab, Entrée et Échap ne permettent
  pas de contourner le verrouillage. Le voile n'a pas de bouton de fermeture.
- **Vraie modale native :** `dialog.showModal()` utilise la couche supérieure du
  navigateur et rend le reste du document inerte, y compris les éléments ajoutés
  par AJAX. Ce n'est pas seulement un `z-index` ou un bouton désactivé.
  Les événements sont également interceptés en capture avant les gestionnaires
  de la page. Les commandes propres au navigateur (retour, rechargement…)
  restent disponibles.
- **Déblocage automatique :** dès que la dernière opération se termine, la
  fenêtre et le voile disparaissent et le défilement est rétabli. Le focus revient
  à l'élément précédent, ou au formulaire/panneau qui vient d'être affiché.
  Les verrouillages propres aux popups et panneaux ouverts restent en place.
- **Aucun délai artificiel :** contrairement à la première barre non bloquante,
  l'affichage est immédiat, sans attente de 120 ms ni durée minimale de 280 ms.
  Une opération terminée ne garde donc pas l'interface verrouillée pour une
  animation. Les requêtes et les actions ne sont jamais temporisées.
- **Requêtes simultanées :** chaque opération possède sa fonction de fin
  idempotente ; finir ou annuler l'une ne libère pas une autre encore en cours.
- **Erreurs et annulations AJAX :** fin dans `finally`, réponses obsolètes
  ignorées. Une annulation programmée libère également le voile ; les boutons
  « Fermer » et la touche Échap ne sont volontairement plus utilisables pendant
  le chargement. Une erreur ne supprime pas les messages de validation.
- **Déclencheurs inchangés :** ressources après l'analyse HTML, navigation
  interne, soumissions natives, filtres, confirmations, chargement/enregistrement
  des popups, panneaux de notifications/messages et recherche globale.
- **Accessibilité :** dialogue nommé et décrit en français, fond inerte et focus
  maintenu dans le chargement ; `progressbar` indéterminé et annonce discrète ;
  barre statique avec `prefers-reduced-motion`, voile masqué à l'impression.
- **Sans JavaScript :** le dialogue fermé reste masqué. Les liens et formulaires
  classiques continuent de fonctionner normalement.

Le polling du compteur de messages, les téléchargements, les ancres, les liens
externes et les ouvertures dans un autre onglet restent silencieux. Les liens de
téléchargement existants portent explicitement `data-no-loading`.
Les confirmations de succès/d'information conservent leur durée de **10 secondes**
(documentation : [messages-confirmation.md](messages-confirmation.md)).

### Limites du navigateur

L'interface ne peut être verrouillée qu'après l'exécution de son JavaScript :
le voile ne précède pas la réception du premier HTML d'une visite.
Le dialogue modal natif nécessite un navigateur moderne prenant en charge
`HTMLDialogElement.showModal()` (versions actuelles de Chrome/Edge, Firefox et
Safari, y compris leurs versions mobiles compatibles).

Une navigation native n'expose pas de promesse d'achèvement à l'ancien document.
Le voile disparaît au changement de page ; `pageshow` remet son état à zéro au
retour arrière. Un garde-fou de **30 secondes** libère une navigation abandonnée,
sans libérer une opération AJAX encore en cours. Il n'annule aucune action sur
le serveur. Une annulation côté navigateur n'annule pas nécessairement une
écriture déjà reçue ; aucune resoumission automatique n'est faite.

## Fichiers principaux

- `static/loading.css` : voile sombre, fenêtre, blocage du défilement, animation,
  mobile, mouvement réduit et impression.
- `static/loading.js` : modale, interception des interactions, restitution du
  focus, compteur d'opérations et suivi des navigations/formulaires.
- `templates/base.html` : un seul dialogue de chargement, partagé par les pages.
  Les fragments AJAX n'ajoutent pas de deuxième voile.
- `static/activity-panel.js` : suivi explicite des requêtes des panneaux.
- Autres templates : exclusions des téléchargements et utilisation de
  `requestSubmit()` dans les filtres.

Le contrôleur ne remplace ni `window.fetch` ni le prototype des formulaires.
Le backend, les autorisations, CSRF, la base et ses migrations sont inchangés.
Le réglage Gunicorn n'est pas modifié par cette mise à jour.

## Ajouter un chargement AJAX

L'API existante reste la même. Inclure la lecture et la mise à jour de l'interface
dans le bloc suivi ; **toujours** terminer dans `finally`, même en cas d'erreur :

```javascript
const termine = window.AppLoading.start();
try {
    const reponse = await fetch('/mon-endpoint');
    if (!reponse.ok) throw new Error('HTTP ' + reponse.status);
    const donnees = await reponse.json();
    afficher(donnees);
} finally {
    termine();
}
```

Pour une redirection JavaScript : `window.AppLoading.navigate('/destination')`.
Pour une soumission programmée : `form.requestSubmit()` ; `form.submit()` ne
produit pas d'événement `submit` et contourne le suivi automatique.
Pour exclure une action : `<a href="/mon-export" data-no-loading>Exporter</a>`.
L'attribut peut aussi être placé sur un formulaire, son bouton ou un conteneur.
Ne pas démarrer de verrouillage bloquant pour un simple polling en arrière-plan.

### Personnalisation

Dans `static/loading.css` :

- `.app-loading-overlay::backdrop` → `background: rgb(8 15 28 / .68)`.
  Augmenter `.68` pour assombrir davantage ; le diminuer pour éclaircir le fond.
- `.app-loading` → `--loading-color` et `--loading-track` pour la barre.

## Tests de régression inclus

```sh
# Environnement Python/PostgreSQL de test décrit dans le README du projet.
pytest -q

# Node >= 18, aucun npm install requis ; Node 22 configuré dans la CI.
node --test tests/js/*.test.cjs
```

Les tests Python vérifient le dialogue unique, l'intégration HTML, les exclusions
et l'absence de duplication dans les fragments. Les tests Node couvrent le
verrouillage/déblocage immédiat, les événements, le focus, la concurrence, les
fins répétées, `pageshow`, la navigation et son garde-fou, les attributs des
boutons et les actions à ignorer. Les confirmations temporisées sont également
couvertes par leur suite de régression. La CI exécute ces suites.
