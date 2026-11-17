// Régressions du contrôleur avec l'API standard de Node, sans npm install.
// Exécution : node --test tests/js/loading.test.cjs (Node >= 18).
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

const source = readFileSync(join(__dirname, '../../static/loading.js'), 'utf8');

function element(attributes = {}) {
    return {
        getAttribute: name => attributes[name] ?? null,
        hasAttribute: name => Object.hasOwn(attributes, name),
        closest: selector => selector === '[data-no-loading]' && Object.hasOwn(attributes, 'data-no-loading') ? {} : null,
    };
}

function setup({login = false, readyState = 'complete'} = {}) {
    let now = 0;
    let sequence = 0;
    const timers = new Map();
    const docEvents = {};
    const winEvents = {};
    const bar = {hidden: true};
    const status = {textContent: ''};
    const moves = [];
    const navigations = [];
    const location = new URL('https://rh.example/accueil');
    location.assign = url => navigations.push(url);
    const document = {
        baseURI: location.href,
        readyState,
        getElementById: id => id === 'appLoading' ? bar : status,
        querySelector: selector => selector === '.login-card' && login ? {prepend: el => moves.push(el)} : null,
        addEventListener: (name, listener) => { docEvents[name] = listener; },
    };
    const window = {
        location,
        setTimeout(fn, delay = 0) {
            const id = ++sequence;
            timers.set(id, {at: now + delay, fn});
            return id;
        },
        clearTimeout: id => timers.delete(id),
        addEventListener: (name, listener) => { winEvents[name] = listener; },
    };
    vm.runInNewContext(source, {window, document, URL, Date: {now: () => now}});
    function advance(ms) {
        const until = now + ms;
        while (true) {
            const next = [...timers].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
            if (!next) break;
            now = next[1].at;
            timers.delete(next[0]);
            next[1].fn();
        }
        now = until;
    }
    function click(attrs, options = {}) {
        const link = element(attrs);
        const event = {button: 0, target: {closest: () => link}, ...options};
        docEvents.click(event);
        return event;
    }
    function submit(attrs = {}, options = {}, buttonAttrs) {
        const event = {target: element(attrs), ...options};
        if (buttonAttrs) event.submitter = element(buttonAttrs);
        docEvents.submit(event);
        return event;
    }
    return {api: window.AppLoading, bar, status, advance, click, submit, winEvents, moves, navigations};
}

test('les opérations < 120 ms ne font pas clignoter la barre', () => {
    const h = setup();
    const done = h.api.start();
    h.advance(119);
    assert.equal(h.bar.hidden, true);
    done();
    h.advance(1000);
    assert.equal(h.bar.hidden, true);
    assert.equal(h.status.textContent, '');
});

test('affichage après 120 ms et durée visible minimale de 280 ms', () => {
    const h = setup();
    const done = h.api.start();
    h.advance(120);
    assert.equal(h.bar.hidden, false);
    assert.equal(h.status.textContent, 'Chargement en cours…');
    done();
    h.advance(279);
    assert.equal(h.bar.hidden, false);
    h.advance(1);
    assert.equal(h.bar.hidden, true);
});

test('une fin répétée ne masque pas une autre requête', () => {
    const h = setup();
    const first = h.api.start();
    const second = h.api.start();
    h.advance(500);
    first();
    first();
    h.advance(500);
    assert.equal(h.bar.hidden, false);
    second();
    h.advance(0);
    assert.equal(h.bar.hidden, true);
});

test('une nouvelle requête reprend la barre pendant le délai de masquage', () => {
    const h = setup();
    const first = h.api.start();
    h.advance(120);
    first();
    const second = h.api.start();
    h.advance(500);
    assert.equal(h.bar.hidden, false);
    second();
    h.advance(0);
    assert.equal(h.bar.hidden, true);
});

test('pageshow remet à zéro et les anciennes fins sont inoffensives', () => {
    const h = setup();
    const old = h.api.start();
    h.advance(500);
    h.winEvents.pageshow();
    assert.equal(h.bar.hidden, true);
    const current = h.api.start();
    old();
    h.advance(500);
    assert.equal(h.bar.hidden, false);
    current();
    h.advance(0);
    assert.equal(h.bar.hidden, true);
});

test('un lien interne non intercepté démarre le chargement', () => {
    const h = setup();
    h.click({href: '/employes'});
    h.advance(120);
    assert.equal(h.bar.hidden, false);
});

test('preventDefault tardif reste respecté même avec la capture', () => {
    const h = setup();
    const event = h.click({href: '/employes/add'});
    event.defaultPrevented = true;
    h.advance(500);
    assert.equal(h.bar.hidden, true);
});

test('ancres, téléchargements, onglets et liens externes restent silencieux', () => {
    for (const [attrs, options] of [
        [{href: '#section'}], [{href: '/accueil#section'}], [{href: ''}],
        [{href: 'mailto:rh@example.test'}], [{href: 'https://external.example/'}],
        [{href: '/fichier', download: ''}], [{href: '/export/presences/pdf', 'data-no-loading': ''}],
        [{href: '/employes', target: '_blank'}], [{href: '/employes'}, {ctrlKey: true}],
        [{href: '/employes'}, {metaKey: true}], [{href: '/employes'}, {shiftKey: true}],
        [{href: '/employes'}, {altKey: true}], [{href: '/employes'}, {button: 1}],
    ]) {
        const h = setup();
        h.click(attrs, options);
        h.advance(500);
        assert.equal(h.bar.hidden, true, JSON.stringify([attrs, options]));
    }
});

test('soumission native et attributs du submitter sont respectés', () => {
    const h = setup();
    h.submit({method: 'post', action: '/login'});
    h.advance(120);
    assert.equal(h.bar.hidden, false);
    for (const [attrs, options, submitter] of [
        [{method: 'dialog'}], [{target: '_blank'}], [{'data-no-loading': ''}],
        [{action: '/login'}, {defaultPrevented: true}], [{action: 'https://external.example/'}],
        [{action: '/login'}, {}, {formtarget: '_blank'}],
        [{action: '/login'}, {}, {'data-no-loading': ''}],
        [{action: '/login'}, {}, {formmethod: 'dialog'}],
        [{action: '/login'}, {}, {formaction: 'https://external.example/'}],
    ]) {
        const ignored = setup();
        ignored.submit(attrs, options, submitter);
        ignored.advance(500);
        assert.equal(ignored.bar.hidden, true, JSON.stringify([attrs, options, submitter]));
    }
});

test('le garde-fou de navigation ne termine pas une requête AJAX active', () => {
    const h = setup();
    h.click({href: '/employes'});
    const done = h.api.start();
    h.advance(31000);
    assert.equal(h.bar.hidden, false);
    done();
    h.advance(0);
    assert.equal(h.bar.hidden, true);
});

test('une navigation abandonnée ne laisse pas une barre infinie', () => {
    const h = setup();
    h.api.navigate('/employes');
    assert.deepEqual(h.navigations, ['/employes']);
    h.advance(500);
    assert.equal(h.bar.hidden, false);
    h.advance(30000);
    assert.equal(h.bar.hidden, true);
});

test('connexion : déplacement dans la carte et chargement initial', () => {
    const h = setup({login: true, readyState: 'interactive'});
    assert.deepEqual(h.moves, [h.bar]);
    h.advance(120);
    assert.equal(h.bar.hidden, false);
    h.winEvents.load();
    h.winEvents.pageshow();
    assert.equal(h.bar.hidden, true);
});
