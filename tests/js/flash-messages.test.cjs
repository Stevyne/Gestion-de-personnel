// Node >= 18, aucune dépendance npm : node --test tests/js/*.test.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const source = readFileSync(join(__dirname, '../../static/flash-messages.js'), 'utf8');

function setup(durations = [10000]) {
    let now = 0;
    let nextId = 0;
    let observer;
    const timers = new Map();
    class Element {
        constructor(attrs = {}) {
            this.attrs = attrs;
            this.nodeType = 1;
            this.children = [];
            this.parent = null;
            this.hidden = false;
            this.textContent = '';
            this.style = {};
            this.events = new Map();
            const classes = new Set();
            this.classList = {
                add: (...names) => names.forEach(n => classes.add(n)),
                remove: (...names) => names.forEach(n => classes.delete(n)),
                toggle: (name, active) => active ? classes.add(name) : classes.delete(name),
                contains: name => classes.has(name),
            };
        }
        get isConnected() { return this === document.body || !!this.parent?.isConnected; }
        append(child) { this.children.push(child); child.parent = this; return child; }
        remove() {
            if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);
            this.parent = null;
        }
        getAttribute(name) { return this.attrs[name] ?? null; }
        contains(node) { return !!node && (node === this || this.children.some(child => child.contains(node))); }
        matches(selector) { return selector.startsWith('[') && Object.hasOwn(this.attrs, selector.slice(1, -1)); }
        querySelectorAll(selector) {
            return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
        }
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
        addEventListener(name, fn) {
            if (!this.events.has(name)) this.events.set(name, new Set());
            this.events.get(name).add(fn);
        }
        removeEventListener(name, fn) { this.events.get(name)?.delete(fn); }
        emit(name, data = {}) { this.events.get(name)?.forEach(fn => fn({target: this, ...data})); }
    }
    const document = new Element();
    document.nodeType = 9;
    document.body = document.append(new Element());
    document.hidden = false;
    document.activeElement = document.body;
    function makeMessage(duration = 10000, marked = true) {
        const node = new Element(marked ? {'data-flash-message': '', ...(duration === null ? {} : {'data-dismiss-after': String(duration)})} : {});
        for (const name of ['countdown', 'dismiss', 'timer']) {
            const child = node.append(new Element({['data-flash-' + name]: ''}));
            child.hidden = true;
        }
        return document.body.append(node);
    }
    const initial = durations.map(duration => makeMessage(duration));
    const window = new Element();
    window.setTimeout = (fn, ms) => {const id = ++nextId; timers.set(id, {fn, at:now + ms}); return id;};
    window.clearTimeout = id => timers.delete(id);
    class MutationObserver {
        constructor(fn) { observer = fn; }
        observe() {}
    }
    vm.runInNewContext(source, {window, document, performance: {now: () => now}, MutationObserver});
    function advance(ms) {
        const until = now + ms;
        while (true) {
            const next = [...timers].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
            if (!next) break;
            now = next[1].at;
            timers.delete(next[0]);
            next[1].fn();
        }
        now = until;
    }
    function observe(nodes = []) { observer([{addedNodes: nodes}]); }
    return {document, window, initial, advance, observe, makeMessage, timers};
}

const count = node => node.querySelector('[data-flash-countdown]');
const close = node => node.querySelector('[data-flash-dismiss]');

test('une confirmation reste visible 10 secondes, puis est retirée exactement', () => {
    const h = setup();
    const message = h.initial[0];
    assert.equal(count(message).textContent, '10 s');
    h.advance(1000);
    assert.equal(count(message).textContent, '9 s');
    h.advance(8999);
    assert.equal(message.isConnected, true);
    h.advance(1);
    assert.equal(message.isConnected, false);
    assert.equal(h.timers.size, 0);
});

test('les messages sans délai (erreurs/avertissements) restent visibles', () => {
    const h = setup([null]);
    h.advance(60000);
    assert.equal(h.initial[0].isConnected, true);
    assert.equal(count(h.initial[0]).hidden, true);
    assert.equal(close(h.initial[0]).hidden, false);
    assert.equal(h.timers.size, 0);
});

test('la croix ferme immédiatement et annule le timer', () => {
    const h = setup();
    close(h.initial[0]).emit('click');
    assert.equal(h.initial[0].isConnected, false);
    assert.equal(h.timers.size, 0);
});

test('pause au survol : conserver le temps restant, sans repartir de zéro', () => {
    const h = setup();
    const message = h.initial[0];
    h.advance(3000);
    message.emit('pointerenter', {pointerType:'mouse'});
    assert.equal(count(message).textContent, '7 s · pause');
    h.advance(20000);
    assert.equal(message.isConnected, true);
    message.emit('pointerleave');
    h.advance(6999);
    assert.equal(message.isConnected, true);
    h.advance(1);
    assert.equal(message.isConnected, false);
});

test('un contact tactile ne laisse pas une pause de survol permanente', () => {
    const h = setup();
    h.initial[0].emit('pointerenter', {pointerType:'touch'});
    h.advance(10000);
    assert.equal(h.initial[0].isConnected, false);
});

test('focus clavier et survol sont deux motifs de pause indépendants', () => {
    const h = setup();
    const message = h.initial[0];
    h.advance(2000);
    message.emit('pointerenter', {pointerType:'mouse'});
    message.emit('focusin');
    message.emit('pointerleave');
    h.advance(15000);
    assert.equal(message.isConnected, true);
    message.emit('focusout', {relatedTarget:h.document.body});
    h.advance(8000);
    assert.equal(message.isConnected, false);
});

test('déplacer le focus à l’intérieur du message conserve la pause', () => {
    const h = setup();
    const message = h.initial[0];
    message.emit('focusin');
    message.emit('focusout', {relatedTarget:close(message)});
    h.advance(12000);
    assert.equal(message.isConnected, true);
});

test('onglet masqué : le temps de lecture reprend au retour', () => {
    const h = setup();
    h.advance(4500);
    h.document.hidden = true;
    h.document.emit('visibilitychange');
    h.advance(60000);
    assert.equal(h.initial[0].isConnected, true);
    h.document.hidden = false;
    h.document.emit('visibilitychange');
    h.advance(5499);
    assert.equal(h.initial[0].isConnected, true);
    h.advance(1);
    assert.equal(h.initial[0].isConnected, false);
});

test('plusieurs confirmations possèdent leurs propres timers', () => {
    const h = setup([10000, 10000]);
    h.advance(2000);
    h.initial[0].emit('pointerenter', {pointerType:'mouse'});
    h.advance(8000);
    assert.equal(h.initial[0].isConnected, true);
    assert.equal(h.initial[1].isConnected, false);
});

test('les confirmations injectées par AJAX sont initialisées une seule fois', () => {
    const h = setup([]);
    const message = h.makeMessage();
    h.observe([message]);
    h.advance(3000);
    h.observe([message]);
    assert.equal(count(message).textContent, '7 s');
    h.advance(7000);
    assert.equal(message.isConnected, false);
});

test('fermer une popup nettoie les timers de ses messages retirés', () => {
    const h = setup();
    h.initial[0].remove();
    h.observe();
    assert.equal(h.timers.size, 0);
});

test('une alerte métier non marquée est ignorée', () => {
    const h = setup([]);
    const message = h.makeMessage(10000, false);
    h.observe([message]);
    h.advance(60000);
    assert.equal(message.isConnected, true);
    assert.equal(close(message).hidden, true);
});

test('valeurs de durée invalides : ne jamais masquer par erreur', () => {
    for (const duration of ['abc', 0, -1, Infinity]) {
        const h = setup([duration]);
        h.advance(20000);
        assert.equal(h.initial[0].isConnected, true);
    }
});

test('cache Précédent/Suivant : conserver la durée restante', () => {
    const h = setup();
    h.advance(4000);
    h.window.emit('pagehide');
    h.advance(30000);
    h.window.emit('pageshow');
    assert.equal(count(h.initial[0]).textContent, '6 s');
    h.advance(6000);
    assert.equal(h.initial[0].isConnected, false);
});

test('un message détaché puis réinséré reçoit un nouveau timer propre', () => {
    const h = setup();
    const message = h.initial[0];
    message.remove();
    h.observe();
    h.document.body.append(message);
    h.observe([message]);
    h.advance(9999);
    assert.equal(message.isConnected, true);
    h.advance(1);
    assert.equal(message.isConnected, false);
});
