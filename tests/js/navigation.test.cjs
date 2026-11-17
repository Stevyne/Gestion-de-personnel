// node --test tests/js/*.test.cjs — sans DOM externe, npm ni réseau.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const {create} = require('../../static/navigation-policy.js');
const root = join(__dirname, '../../static');
const routes = ['^/$', '^/employes$', '^/employes/\\d+$', '^/messages$', '^/materiels/\\d+$', '^/competences$'];
const eligible = create({enabled:true,routes}, 'https://app.test/employes?page=2');

test('liens et filtres internes de lecture sont éligibles, URL résolue conservée', () => {
    for (const value of ['/', '/employes', '/employes?search=a&page=2', '/employes/42#competences', 'https://app.test/messages']) {
        assert.equal(eligible(value).href, new URL(value, 'https://app.test/employes?page=2').href);
    }
});
test('liste blanche exacte : aucune déconnexion, écriture, impression, API ou export', () => {
    for (const value of ['/logout', '/login', '/employes/delete/4', '/messages/non-lus', '/api/recherche',
        '/export/presences/pdf', '/materiels/4/etiquettes', '/employes/1/fiche-de-paie/pdf',
        '/employes/abc', '/employes/12/edit', '/employes/', '/static/navigation.js']) assert.equal(eligible(value), null, value);
});
test('origines, protocoles, identifiants dans URL et adresses invalides restent natifs', () => {
    for (const value of ['https://other.test/employes', 'http://app.test/employes', '//evil.test/messages',
        'javascript:alert(1)', 'data:text/html,test', 'mailto:a@example.test', 'https://user:pass@app.test/employes',
        'https://app.test:444/employes', 'http://[']) assert.equal(eligible(value), null, value);
});
test('popup, panneau, téléchargement et impression sont exclus même avec valeur vide ou zéro', () => {
    for (const key of ['modal', 'panel', 'print', 'download', 'attachment']) {
        for (const value of ['', '0', '1']) assert.equal(eligible('/employes?'+key+'='+value), null);
    }
    assert.equal(eligible('/employes?panels=1').pathname, '/employes');
});
test('la politique ne préjuge pas des droits ; une route éligible doit encore passer le serveur', () => {
    const check = create({enabled:true,routes:['^/utilisateurs$']}, 'https://app.test/');
    assert.equal(check('/utilisateurs').pathname, '/utilisateurs');
    assert.equal(check('/employes'), null);
});
test('désactivation de l’amélioration progressive laisse tous les liens natifs', () => {
    const check = create({enabled:false,routes}, 'https://app.test/');
    for (const value of ['/', '/employes', '/messages']) assert.equal(check(value), null);
});
test('règles ancrées et encodage des filtres ne sont pas assouplis', () => {
    assert.equal(eligible('/employes?search=A%26B%3F').search, '?search=A%26B%3F');
    assert.equal(eligible('/employes/2/supprimer'), null);
    assert.equal(eligible('/employes-admin'), null);
});

function lifecycle(readyState='loading') {
    const document = new EventTarget();
    document.readyState = readyState;
    const window = {};
    const errors = [];
    vm.runInNewContext(readFileSync(join(root, 'page-lifecycle.js'), 'utf8'), {
        window, document, console:{error:(...args)=>errors.push(args)},
    });
    return {page:window.AppPage, document, errors};
}
test('chargement natif initial : ready attend DOMContentLoaded une seule fois', () => {
    const {page,document} = lifecycle();
    let calls=0;
    page.ready(()=>calls++);
    assert.equal(calls,0);
    document.dispatchEvent(new Event('DOMContentLoaded'));
    document.dispatchEvent(new Event('DOMContentLoaded'));
    assert.equal(calls,1);
});
test('page déjà chargée : ready fonctionne immédiatement', () => {
    const {page} = lifecycle('complete');
    let calls=0; page.ready(()=>calls++); assert.equal(calls,1);
});
test('fragment : tous les scripts peuvent se charger avant les initialisations de page', () => {
    const {page} = lifecycle('complete'); const calls=[];
    page.begin();
    page.ready(()=>calls.push('graphiques'));
    page.ready(()=>calls.push('filtres'));
    assert.deepEqual(calls,[]);
    page.finish(); assert.deepEqual(calls,['graphiques','filtres']);
    page.finish(); assert.equal(calls.length,2);
});
test('dispose nettoie dans l’ordre inverse, une fois, et accepte des callbacks facultatifs', () => {
    const {page} = lifecycle('complete'); const calls=[];
    page.onCleanup(null); page.onCleanup(()=>calls.push(1)); page.onCleanup(()=>calls.push(2));
    page.dispose(); page.dispose(); assert.deepEqual(calls,[2,1]);
});
test('les écouteurs globaux de la page précédente sont supprimés sans toucher ceux de la coque', () => {
    const {page,document} = lifecycle('complete');
    let shell=0,content=0;
    document.addEventListener('keydown',()=>shell++);
    for (let i=0;i<20;i++) {
        page.begin(); page.listen(document,'keydown',()=>content++); page.finish();
        document.dispatchEvent(new Event('keydown')); page.dispose();
    }
    document.dispatchEvent(new Event('keydown'));
    assert.equal(content,20); assert.equal(shell,21);
});
test('une erreur de nettoyage ne bloque ni les autres nettoyages ni la nouvelle page', () => {
    const {page,errors} = lifecycle('complete'); let cleaned=false, mounted=false;
    page.onCleanup(()=>cleaned=true); page.onCleanup(()=>{throw Error('test');});
    page.dispose(); assert.equal(cleaned,true); assert.equal(errors.length,1);
    page.begin(); page.ready(()=>mounted=true); page.finish(); assert.equal(mounted,true);
});
test('une navigation annulée ne rejoue pas les initialisations devenues obsolètes', () => {
    const {page} = lifecycle('complete'); const calls=[];
    page.begin(); page.ready(()=>calls.push('ancienne')); page.dispose();
    page.begin(); page.ready(()=>calls.push('courante')); page.finish();
    assert.deepEqual(calls,['courante']);
});
test('dispose avant DOMContentLoaded retire aussi les initialisations natives en attente', () => {
    const {page,document} = lifecycle(); let calls=0;
    page.ready(()=>calls++); page.dispose(); document.dispatchEvent(new Event('DOMContentLoaded'));
    assert.equal(calls,0);
});
test('API de cycle de vie gelée, sans remplacement des objets natifs', () => {
    const {page} = lifecycle(); assert.equal(Object.isFrozen(page),true);
    for (const file of ['navigation.js','navigation-policy.js','page-lifecycle.js']) {
        const source=readFileSync(join(root,file),'utf8');
        assert.doesNotMatch(source, /\beval\s*\(|new\s+Function\s*\(/);
        assert.doesNotMatch(source, /(?:XMLHttpRequest|HTMLFormElement|EventTarget)\.prototype\s*[.=]/);
        assert.doesNotMatch(source, /localStorage|sessionStorage|serviceWorker/);
    }
});
