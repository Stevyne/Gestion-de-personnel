# Animation de chargement

Barre linéaire indéterminée inspirée de la connexion Google/Material, intégrée
au front-end Flask/Jinja existant. Elle ne simule pas un pourcentage de progression
et n'ajoute ni CDN ni dépendance de production.

## Comportement

- **Connexion :** barre de 4 px au sommet de la carte, avec ses coins arrondis.
  L'état du bouton et les protections existantes restent en place.
- **Autres pages :** barre fixée tout en haut, au-dessus de la navigation et des
  popups, sans overlay et sans bloquer les interactions.
- **Déclencheurs :** ressources de la page après l'analyse HTML, navigation
  interne, soumission native, filtres, confirmations, chargement/enregistrement
  des popups, panneaux de notifications/messages et recherche globale.
- **Pas de clignotement :** affichage après 120 ms, durée visible minimale de
  280 ms. Ces délais ne retardent jamais une requête ni une navigation.
- **Requêtes simultanées :** chaque opération possède sa fonction de fin
  idempotente ; la barre attend la dernière opération suivie.
- **Erreurs et annulations AJAX :** fin dans `finally`, annulation à la fermeture
  des panneaux/popups, réponses obsolètes ignorées.
- **Accessibilité :** `progressbar` indéterminé, annonce discrète en français,
  barre statique avec `prefers-reduced-motion`, aucun indicateur à l'impression.
- **Sans JavaScript :** les formulaires et liens classiques restent utilisables,
  et la barre reste masquée.

Le polling du compteur de messages, les téléchargements, les ancres, les liens
externes et les ouvertures dans un autre onglet n'activent pas la barre.
Les liens de téléchargement existants portent explicitement `data-no-loading`.

### Limite du navigateur

Une navigation native n'expose pas de promesse d'achèvement à l'ancien document.
La barre disparaît au changement de page ; `pageshow` remet son état à zéro au
retour arrière. Un garde-fou de 30 secondes arrête le témoin d'une navigation
abandonnée. Il ne coupe pas les requêtes AJAX et n'annule rien sur le serveur.
La barre ne peut pas apparaître avant la réception du premier HTML d'une visite.
Fermer une popup interrompt la lecture côté navigateur, pas nécessairement une
écriture déjà reçue par le serveur : aucune resoumission automatique n'est faite.

## Fichiers principaux

- `static/loading.css` : couleurs, animations, placement et mouvement réduit.
- `static/loading.js` : compteur d'opérations et suivi des navigations/formulaires.
- `templates/base.html` : un seul indicateur et intégration des popups/recherches.
- `static/activity-panel.js` : suivi explicite des requêtes des panneaux.
- Autres templates : exclusions des téléchargements et remplacement des appels
  directs à `submit()` par `requestSubmit()` dans les filtres.

Le contrôleur ne remplace ni `window.fetch` ni le prototype des formulaires.
Le backend, les autorisations, CSRF, la base et ses migrations sont inchangés.

## Ajouter un chargement AJAX

Inclure toute la lecture et la mise à jour de l'interface dans le bloc suivi :

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

Personnaliser `--loading-color` et `--loading-track` dans `static/loading.css`.

## Tests de régression inclus

```sh
# Environnement Python/PostgreSQL de test décrit dans le README du projet.
pytest -q

# Node >= 18, aucun npm install requis ; Node 22 configuré dans la CI.
node --test tests/js/loading.test.cjs
```

Les tests Python vérifient l'intégration HTML et les exclusions. Les tests Node
couvrent délais, concurrence, fins répétées, restauration de page, navigations,
attributs des boutons et actions à ignorer. La CI exécute les deux suites.
