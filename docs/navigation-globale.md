# Navigation rapide entre les pages

## Ce que le correctif apporte

Un clic normal vers une page de lecture de l’application conserve l’en-tête,
le tiroir mobile, les fenêtres de confirmation et le panneau d’activité.
Seul le contenu de `<main id="appMain">` est remplacé après un GET authentifié.
Cela évite de reconstruire toute l’interface et de réinitialiser son polling.
Les filtres GET, la pagination, Précédent/Suivant, les titres et le focus sont suivis.

**Ce n’est pas un préchargement de toute la base.** Chaque nouvelle page demande
ses données au serveur. Les requêtes SQL et les contrôles métier restent ceux
des routes existantes ; ce correctif ne promet donc pas un affichage instantané,
un fonctionnement hors ligne, ni une diminution du temps SQL. Le serveur rend
encore le template HTML complet, puis en extrait la partie utile. Le gain visé
concerne surtout le transfert et la reconstruction du document côté navigateur.

Les onglets **Général / RH / Parc / Direction** sont différents : leurs panneaux
autorisés sont déjà dans le DOM du dashboard et continuent à basculer sans réseau
ni voile. [Détails du dashboard](dashboard-onglets.md).

## Périmètre

La liste blanche couvre les **83 endpoints GET de lecture** du projet :

- Employés et fiches, départements, contrats, départs ;
- présences, historique, absences et justificatifs ;
- congés, permissions, soldes, calendrier, espace personnel ;
- documents, recherche et rapports HTML ;
- matériels, exemplaires, inventaires, maintenance, prestataires ;
- compétences, objectifs et leurs formulaires ;
- recrutement : demandes, offres, candidats, candidatures, entretiens, évaluations ;
- utilisateurs, inscription, profil, messagerie et notifications en page complète ;
- les quatre URL d’entrée du dashboard.

Un endpoint éligible n’est **pas** une permission : chaque rôle ne voit et
n’atteint que les écrans déjà autorisés. Les identifiants de détail continuent
à être vérifiés côté serveur. Le journal d’audit reste invisible hors admin.

Les effets de **consultation** préexistants sont conservés : ouvrir une
conversation la marque comme lue ; ouvrir l’aperçu d’une fiche de paie consigne
sa génération dans l’audit. Ces GET ne créent ni paiement ni fiche enregistrée.
Ils restent éligibles car aucune consultation n’est anticipée : un clic, un
retour dans l’historique ou un réessai refait réellement le GET, comme un
rechargement. Annuler le fetch n’annule pas un effet déjà exécuté sur le serveur.

### Ce qui reste volontairement natif

- **POST et actions métier** : création, modification, suppression, pointage,
  décisions, uploads, changements de mot de passe, etc. Aucun POST n’est rejoué
  par ce contrôleur. Les popups et le panneau gardent leur gestionnaire AJAX.
- Connexion et déconnexion ; exports PDF/Excel, pièces jointes, avatars,
  API JSON, étiquettes et impressions.
- URLs comportant `modal`, `panel`, `print`, `download` ou `attachment`.
- Liens externes, `target`, téléchargements, clics Ctrl/Cmd/Maj/Alt, ancres
  internes, liens dotés d’un gestionnaire `onclick` ou marqués comme natifs.
- Un formulaire envoyé directement avec `form.submit()` conserve son comportement
  natif ; les formulaires GET avec soumission normale ou `requestSubmit()` sont suivis.

Sans JavaScript, avec le contrôleur absent ou une vue non compatible, les vrais
liens et les routes Flask restent utilisables. Les formulaires et exports ne
sont pas convertis en fausses pages locales.

## Sécurité et données

`services/navigation.py` ne remplace aucune vue ni aucun décorateur. L’ordre est :
contrôles habituels de session/rôle/département → garde de navigation → vue →
masquage ACL du HTML → extraction du fragment. Comme Flask exécute les
`after_request` dans l’ordre inverse, `configurer_navigation(app)` doit rester
**enregistré avant le masqueur de liens**.

La requête rapide porte :

- `X-App-Navigation: 1` ;
- `X-App-Identity: identifiant-utilisateur:rôle` ;
- `X-App-Version: empreinte-de-la-coque`.

L’identité est un repère de cohérence, **pas un jeton d’authentification**.
Le cookie signé et les vérifications serveur restent obligatoires. Le rôle est
relu en base avant la garde, comme auparavant. Session expirée, compte changé
dans un autre onglet, rôle modifié ou version différente provoquent un véritable
rechargement, sans injection du nouveau contenu privé dans l’ancienne coque.
Une révocation de session conserve le contrôle existant du registre de sessions.

Les réponses de navigation, et le HTML natif des pages éligibles, sont
`Cache-Control: private, no-store` avec `Vary` sur les en-têtes de navigation et
le cookie. Aucun cache applicatif de pages/données privées, stockage local,
Service Worker, préchargement au survol ou cache partagé de statistiques n’est ajouté.
Le navigateur peut réutiliser les **ressources statiques** comme auparavant.
Seule la liste des motifs de routes, dépourvue de données métier, est mémoïsée
côté serveur par rôle.

La version est calculée au démarrage à partir des templates, JS et CSS. Déployer
les fichiers ensemble et redémarrer **tous les workers** pour éviter un mélange
d’anciennes/nouvelles coques. La CSP reste inchangée : aucun `unsafe-eval`, CDN,
`eval`, `new Function` ou remplacement des prototypes natifs.

Une page déjà affichée n’est pas effacée à distance lors d’une modification des
droits. Les nouvelles requêtes les revalident ; les onglets locaux du dashboard
restent la photographie autorisée à leur ouverture, jusqu’à Actualiser. Une
restauration BFCache tente une revalidation serveur.

## Chargement, erreurs et historique

Le voile bloquant existant démarre immédiatement pour un changement de page.
Sa fin idempotente est appelée après le chargement du contenu, de ses styles et
de ses scripts, y compris en cas d’échec ou d’annulation. Les onglets locaux du
dashboard restent exclus de ce voile.

- Avant remplacement du contenu : erreur réseau/HTTP/CSS ou délai dépassé →
  page et adresse courantes conservées, message persistant, boutons **Réessayer**
  et **Ouvrir normalement**. Délai maximal : 30 secondes.
- Annulation avant remplacement : page conservée, déblocage sans message d’erreur.
- Nouvelle navigation : l’ancienne réponse n’est pas injectée à sa place.
- Coque incompatible ou script de vue indisponible après remplacement : repli
  sur une vraie navigation serveur, plutôt qu’une interface partiellement montée.
- Précédent/Suivant entre pages refont un GET privé et restaurent le défilement.
  Les ancres et onglets déjà présents dans le même document restent locaux.
- Les redirections Flask deviennent des réponses 204 avec `X-App-Redirect` ; le
  client suit au maximum huit étapes. `X-App-Native-Navigation` demande un repli
  natif. Le serveur ne rend pas une page cible dans un fetch automatique qui
  consommerait prématurément ses messages flash.

Le titre de la page est annoncé via une région `aria-live`. Le focus va au titre
ou retrouve un filtre identifié ; les champs restent dans l’ordre de tabulation.
Les styles spécifiques sont remplacés sans accumulation. Chart.js est chargé une
fois par document ; les instances de graphiques sont détruites quand on quitte
le dashboard et recréées lorsqu’on y revient.

Le compteur silencieux de messages appartient à la coque. Il n’est pas recréé
à chaque visite. Son polling est suspendu pendant un chargement suivi et une
requête de compteur en cours est annulée : une réponse tardive ne doit pas
réécrire un ancien cookie et faire perdre le flash d’un POST récent. Cela corrige
le scénario POST/204 qui échouait dans la livraison précédente.

## Ajouter une page ou un script

1. Garder une route GET avec ses décorateurs et contrôles habituels.
2. Étendre `base.html` et utiliser les blocs `content`, `extra_styles`, `extra_scripts`.
3. N’ajouter l’endpoint à `READ_ENDPOINTS` qu’après audit : pas d’export ni de
   mutation métier sur GET ; examiner aussi ses éventuels effets de consultation.
   L’absence de la liste garde la navigation native.
4. Les scripts de vue doivent être réexécutables. Préférer une IIFE pour éviter
   les `const` et fonctions globales conservés d’une ancienne page.
5. Remplacer les initialisations uniquement liées à `DOMContentLoaded` par :

```javascript
window.AppPage.ready(() => {
    const root = document.getElementById('appMain');
    // Brancher les contrôles du nouveau contenu ici.
    window.AppPage.listen(document, 'keydown', event => {
        // Écouteur global de CETTE page, retiré à son départ.
    });
    window.AppPage.onCleanup(() => {
        // destroy() des graphiques, clearInterval(), disconnect(), etc.
    });
});
```

`ready` fonctionne au chargement natif et après injection. Le contrôleur ne
redéclenche **pas** un faux `DOMContentLoaded` global : cela doublerait les
écouteurs de la coque. `AppPage.dispose()` exécute les nettoyages dans l’ordre
inverse. Les scripts externes de vue sont chargés séquentiellement ; les
bibliothèques stables peuvent porter `data-app-once`, pas les initialiseurs
qui doivent se rebrancher sur le nouveau DOM.

Événements de coque : `app:page-before-change` avant démontage ; `app:page-ready`
après montage, avec `event.detail.main` et `event.detail.url`. Ne pas y réinstaller
la recherche globale, les boutons des modales persistantes ni le polling.

Pour exclure un lien, formulaire, bouton de soumission ou une section :

```html
<a href="/employes" data-native-navigation>Ouvrir avec une vraie navigation</a>
```

`data-no-loading` reste également respecté. L’API facultative
`AppNavigation.visit(url)` renvoie une promesse booléenne ; `abort()` annule la
requête courante ; `busy` indique son activité. Les liens HTML sont à privilégier
pour conserver le repli sans JS et les clics modifiés.

## Vérifications et limites

Tests unitaires et serveur livrés : `tests/test_navigation.py` et
`tests/js/navigation.test.cjs`. Ils s’ajoutent à la suite existante :

```bash
# UNIQUEMENT avec une base PostgreSQL de TEST distincte de vos données réelles.
pytest -q
node --test tests/js/*.test.cjs
```

Les fixtures pytest vident leurs tables : **ne jamais pointer DATABASE_URL ou
TEST_DATABASE_URL sur une base de production**. Les scénarios Chromium locaux
utilisent une autre base de démonstration, séparée des tests Python.

Le rapport `VERIFICATIONS.json` de la livraison précise les résultats effectifs,
la couverture des routes, les erreurs simulées et les contrôles d’installation.
Pas de validation distante, de test de charge, de Firefox/Safari ni de téléphone
physique. Les mesures locales de taille ou de durée ne sont pas un benchmark de
production. Gunicorn reste à **2 workers / 4 threads**, scheduler et ACL inchangés.
