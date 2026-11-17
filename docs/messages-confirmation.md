# Messages de confirmation : fermeture après 10 secondes

Les bannières de confirmation de connexion, déconnexion, enregistrement,
suppression, etc. se ferment automatiquement après **10 secondes de lecture**.
Cela ne retarde jamais l'action : la déconnexion, par exemple, reste immédiate.

## Périmètre

- Catégories Flask `success`, `info` et `message` : compte à rebours, barre de
  durée et fermeture automatique après 10 000 ms.
- Catégories `danger`, `error`, `warning` et catégories inconnues : aucune
  fermeture automatique, pour conserver les informations importantes.
- Tous les flashs disposent d'un bouton « Fermer le message » quand JavaScript
  est disponible.
- Même rendu et même durée en page complète, dans une popup et dans le panneau
  d'activité. Un `MutationObserver` initialise les fragments injectés par AJAX.
- Les notifications enregistrées en base, les conversations, les alertes métier
  permanentes et les fenêtres demandant de confirmer une action ne sont pas
  supprimées. Seules les bannières de retour d'action sont concernées.

## Lecture et accessibilité

Le timer s'interrompt au survol (souris/stylet), au focus clavier, dans un onglet
masqué et pendant le stockage de la page dans le cache Précédent/Suivant. Il
reprend avec le **temps restant**, pas avec dix nouvelles secondes.
Un contact tactile ne provoque pas de pause de survol permanente.

Le nombre de secondes porte `aria-hidden` pour ne pas être annoncé à chaque
seconde. Les confirmations utilisent `role="status"`, les erreurs/avertissements
`role="alert"`. La barre animée est masquée avec `prefers-reduced-motion` ; le
compteur textuel reste disponible. Les contrôles ne sont pas imprimés.

Sans JavaScript, le texte demeure visible et les contrôles inactifs sont cachés :
on ne perd pas une information simplement parce que le script est indisponible.

## Intégration

- `templates/_flash_message.html` : macro commune et délai `10000`.
- `templates/base.html`, `_modal_layout.html`, `_activity_panel_flashes.html` :
  utilisation de la même macro, sans dupliquer de script dans les fragments.
- `static/flash-messages.js` : temporisation indépendante de chaque message,
  pauses, fermeture manuelle et nettoyage des timers/écouteurs après retrait.
- `static/flash-messages.css` : compteur, barre de durée et styles accessibles.

Les attributs `data-flash-message` et `data-dismiss-after` ne sont ajoutés qu'aux
flashs concernés. Le contrôleur ne masque donc pas tous les éléments `.alert`
et ne touche ni à la base de données ni aux messages persistants.

Les éléments retirés d'une popup sont nettoyés. Une initialisation répétée ne
redémarre pas le compteur. Plusieurs bannières ont des durées indépendantes.
Aucune dépendance de production, aucun CDN et aucune migration ne sont ajoutés.

## Tests

```sh
pytest -q
node --test tests/js/*.test.cjs
```

Les tests Python vérifient les catégories, la déconnexion réelle, les erreurs,
les fragments AJAX et l'échappement HTML. Les tests JavaScript vérifient les dix
secondes, les pauses, la visibilité de l'onglet, les fermetures manuelles, les
messages simultanés, les insertions AJAX et le nettoyage.
