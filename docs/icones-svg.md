# Icônes SVG harmonisées

Tous les pictogrammes décoratifs codés dans l’interface utilisent des SVG locaux,
avec le style de la navbar : grille 24 × 24, trait `currentColor` de 1,9,
extrémités et jointures arrondies. Aucune police d’icônes, aucun CDN, aucun accès
réseau supplémentaire vers un service tiers.

## Catalogue unique

`templates/_icons.html` contient les tracés constants et deux macros :

```jinja
{% from "_icons.html" import icon %}
<button type="submit">{{ icon('save', '1em') }} Enregistrer</button>
<button type="button" aria-label="Fermer">{{ icon('x', 18) }}</button>
```

La taille vaut 18 par défaut ; `1em` suit la taille du texte (titres, cartes,
badges). Les dimensions numériques sont bornées entre 8 et 128. Un nom inconnu
renvoie l’icône `info`. `base.html` réexporte `icon` pour compatibilité ; les
fragments importent directement `_icons.html`, sans rendre tout le layout.

Les seuls tracés marqués `safe` viennent du dictionnaire constant du catalogue.
Ne jamais y placer de valeur issue d’un formulaire, d’un message ou d’un fichier.

`static/icons.css` assure l’alignement dans les textes et les contrôles sans
modifier les couleurs métier. Les dessins restent nets à toutes les tailles.

## États JavaScript et contenu AJAX

La macro `icon_bank()` de `base.html` rend **une seule** banque dans un élément
`<template id="appIconTemplates">` invisible. Elle ne contient que les icônes
nécessaires aux changements d’état, issues du même catalogue. `static/icons.js`
clone ces nœuds SVG, au lieu de réécrire des tracés ou de faire une conversion
Unicode globale :

```javascript
window.AppIcons.set(button, 'eye-off', 18);
window.AppIcons.withText(fileLabel, 'paperclip', file.name);
const svg = window.AppIcons.create('alert', 24);
```

`withText` emploie `document.createTextNode` ; les chevrons et autres caractères
d’un nom de fichier restent donc du texte. `set` remplace seulement le contenu
du conteneur ciblé : ne pas l’appeler sur un bouton comportant un libellé à
conserver ; placer alors un sous-conteneur réservé à l’icône.

Pour ajouter un nouvel état dynamique, ajouter le nom à la liste de
`icon_bank()` ; aucun nouveau tracé JavaScript n’est nécessaire. Les fragments
AJAX sont rendus avec la macro serveur et utilisent la banque déjà présente
dans la page.

Les attributs `data-confirm-icon` contiennent désormais un **nom** (`trash`,
`calendar`, `file`, `alert`…), pas un emoji. Un nom inconnu reçoit `info`.

## Portée et accessibilité

- Navbar mobile, titres, tableaux de bord RH/Parc/Direction, cartes, boutons,
  compétences, objectifs, matériel, recrutement, notifications et messagerie.
- Fermetures, confirmations, avatars système de groupe/annonce, état d’erreur,
  bascule de mot de passe et sélection de pièce jointe, même après chargement AJAX.
- Les SVG sont décoratifs : `aria-hidden="true"`, `focusable="false"` ; les
  boutons sans texte gardent ou reçoivent un nom accessible. Les bascules
  exposent aussi leur état via `aria-expanded` ou `aria-pressed`.
- Les options natives des listes déroulantes ne rendent pas les SVG : elles
  gardent un libellé textuel propre, sans emoji. Le code/identifiant métier
  (`informatique`, `atteint`, etc.) ne change pas.
- Les nouveaux titres de notification et sujets d’e-mail ne contiennent plus
  de préfixe emoji ; l’interface affiche l’icône selon le type. L’e-mail HTML de
  retard conserve son titre lisible même si un client de messagerie masque le SVG.
- Les textes saisis, noms de fichiers, photos, initiales et messages enregistrés
  ne sont **ni réécrits ni convertis en HTML**. Les anciens contenus enregistrés
  sont conservés : aucune migration de données.
- Les caractères textuels/mathématiques (©, ≈, ≤, multiplication du nombre de
  sessions, etc.) sont préservés. Ce ne sont pas des emojis décoratifs.
- Le chargement bloquant, les confirmations de 10 secondes, les erreurs
  persistantes et les réglages Gunicorn ne sont pas modifiés.

## Vérification

```bash
# Avec la base PostgreSQL de test séparée configurée.
python -m pytest -q
node --test tests/js/*.test.cjs
node --check static/icons.js
```

`tests/test_icons.py` couvre les tracés SVG, les importations Jinja, l’absence de
pictogrammes Unicode codés en dur, les libellés natifs, les pages/fragments et
la préservation de messages contenant des emojis ou du HTML échappé.
`tests/js/icons.test.cjs` vérifie le clonage, les tailles, les noms inconnus,
le remplacement d’état et les noms de fichiers affichés comme texte brut.
