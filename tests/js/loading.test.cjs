// node --test tests/js/*.test.cjs — aucune dépendance npm.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const source = readFileSync(join(__dirname, '../../static/loading.js'), 'utf8');

function setup({readyState = 'complete'} = {}) {
    let now = 0;
    let sequence = 0;
    const timers = new Map();
    const docEvents = {};
    const winEvents = {};
    const overlayEvents = {};
    const navigations = [];
    const document = {readyState, baseURI:'https://rh.example/accueil'};
    function element(attrs = {}) {
        const classes = new Set();
        const el = {
            isConnected:true, disabled:false, style:{}, children:[], hidden:false,
            getAttribute:name => attrs[name] ?? null,
            hasAttribute:name => Object.hasOwn(attrs,name),
            closest:selector => Object.hasOwn(attrs,selector.slice(1,-1)) ? {} : null,
            contains(node) {return node === el || el.children.some(child => child.contains(node));},
            getClientRects:() => [{width:100,height:40}],
            querySelectorAll:() => [],
            classList:{add:(...names) => names.forEach(n => classes.add(n)),remove:(...names) => names.forEach(n => classes.delete(n)),contains:n => classes.has(n)},
            focus() {document.activeElement = el;},
        };
        return el;
    }
    document.body = element();
    document.documentElement = element();
    document.activeElement = document.body;
    let surface = null;
    let autofocus = null;
    let invalidPassword = null;
    const bar = element(); bar.hidden = true;
    const status = {textContent:''};
    const overlay = element();
    overlay.children = [bar];
    overlay.open = false;
    overlay.showCount = 0;
    overlay.showModal = () => {overlay.open = true; overlay.showCount++;};
    overlay.close = () => {overlay.open = false;document.activeElement = document.body;};
    overlay.addEventListener = (name, listener) => {overlayEvents[name] = listener;};
    document.getElementById = id => ({appLoading:bar,appLoadingStatus:status,appLoadingOverlay:overlay})[id];
    document.querySelector = selector => {
        if (selector === '#formModal.show') return surface;
        if (selector === '[autofocus]') return autofocus;
        if (selector === 'input[type="password"][aria-invalid="true"]') return invalidPassword;
        return null;
    };
    document.addEventListener = (name, listener) => {docEvents[name] = listener;};
    const location = new URL(document.baseURI);
    location.assign = url => navigations.push(url);
    const window = {
        location,
        setTimeout(fn, delay = 0) {const id = ++sequence;timers.set(id,{at:now+delay,fn});return id;},
        clearTimeout:id => timers.delete(id),
        addEventListener:(name, listener) => {winEvents[name] = listener;},
    };
    vm.runInNewContext(source,{document,window,URL,Date:{now:() => now}});
    function advance(ms) {
        const until = now + ms;
        while (true) {
            const next = [...timers].filter(([,t]) => t.at <= until).sort((a,b) => a[1].at-b[1].at)[0];
            if (!next) break;
            now = next[1].at;timers.delete(next[0]);next[1].fn();
        }
        now = until;
    }
    function event(type, data = {}) {
        return {type,defaultPrevented:false,stopped:false,preventDefault(){this.defaultPrevented=true;},stopImmediatePropagation(){this.stopped=true;},...data};
    }
    function click(attrs, options = {}) {
        const link = element(attrs);
        const e = event('click',{button:0,target:{closest:() => link},...options});
        winEvents.click?.(e);
        if (!e.stopped) docEvents.click(e);
        return e;
    }
    function submit(attrs = {}, options = {}, buttonAttrs) {
        const e = event('submit',{target:element(attrs),...options});
        if (buttonAttrs) e.submitter=element(buttonAttrs);
        winEvents.submit?.(e);
        if (!e.stopped) docEvents.submit(e);
        return e;
    }
    return {api:window.AppLoading,document,bar,status,overlay,advance,click,submit,winEvents,overlayEvents,navigations,element,event,
        setSurface:el => {surface=el;},setAutofocus:el => {autofocus=el;},setInvalidPassword:el => {invalidPassword=el;}};
}

test('le voile bloque immédiatement et prend le focus', () => {
    const h=setup();h.api.start();
    assert.equal(h.overlay.open,true);assert.equal(h.bar.hidden,false);
    assert.equal(h.document.activeElement,h.overlay);
    assert.equal(h.status.textContent,'Chargement en cours…');
    assert.equal(h.document.body.classList.contains('app-loading-active'),true);
});

test('la dernière fin libère immédiatement, même avant 120 ms', () => {
    const h=setup();const done=h.api.start();h.advance(15);done();
    assert.equal(h.overlay.open,false);assert.equal(h.bar.hidden,true);
    assert.equal(h.status.textContent,'');
    assert.equal(h.document.body.classList.contains('app-loading-active'),false);
    assert.equal(h.document.documentElement.classList.contains('app-loading-active'),false);
});

test('deux requêtes : une fin répétée ne libère pas la seconde', () => {
    const h=setup();const first=h.api.start();const second=h.api.start();
    assert.equal(h.overlay.showCount,1);
    first();first();assert.equal(h.overlay.open,true);
    second();assert.equal(h.overlay.open,false);
});

test('une nouvelle opération peut réouvrir le voile après la fin', () => {
    const h=setup();h.api.start()();const done=h.api.start();
    assert.equal(h.overlay.open,true);assert.equal(h.overlay.showCount,2);done();
});

test('pageshow remet à zéro sans rendre dangereuse une ancienne fin', () => {
    const h=setup();const old=h.api.start();h.winEvents.pageshow();
    assert.equal(h.overlay.open,false);const current=h.api.start();old();
    assert.equal(h.overlay.open,true);current();assert.equal(h.overlay.open,false);
});

test('navigation interne : verrouillage, puis garde-fou de 30 s', () => {
    const h=setup();h.click({href:'/employes'});h.advance(0);
    assert.equal(h.overlay.open,true);h.advance(30000);assert.equal(h.overlay.open,false);
});

test('preventDefault tardif est respecté sans bloquer par erreur', () => {
    const h=setup();const e=h.click({href:'/employes/add'});e.defaultPrevented=true;h.advance(500);
    assert.equal(h.overlay.open,false);
});

test('ancres, téléchargements, onglets et liens externes restent silencieux', () => {
    for (const [attrs,options] of [
        [{href:'#section'}],[{href:'/accueil#section'}],[{href:''}],
        [{href:'mailto:rh@example.test'}],[{href:'https://external.example/'}],
        [{href:'/fichier',download:''}],[{href:'/export','data-no-loading':''}],
        [{href:'/employes',target:'_blank'}],[{href:'/employes'},{ctrlKey:true}],
        [{href:'/employes'},{metaKey:true}],[{href:'/employes'},{shiftKey:true}],
        [{href:'/employes'},{altKey:true}],[{href:'/employes'},{button:1}],
    ]) {
        const h=setup();h.click(attrs,options);h.advance(500);assert.equal(h.overlay.open,false,JSON.stringify([attrs,options]));
    }
});

test('soumission native et attributs du submitter sont respectés', () => {
    const h=setup();h.submit({method:'post',action:'/login'});h.advance(0);assert.equal(h.overlay.open,true);
    for (const [attrs,options,button] of [
        [{method:'dialog'}],[{target:'_blank'}],[{'data-no-loading':''}],
        [{action:'/login'},{defaultPrevented:true}],[{action:'https://external.example/'}],
        [{action:'/login'},{},{formtarget:'_blank'}],[{action:'/login'},{},{'data-no-loading':''}],
        [{action:'/login'},{},{formmethod:'dialog'}],[{action:'/login'},{},{formaction:'https://external.example/'}],
    ]) {
        const ignored=setup();ignored.submit(attrs,options,button);ignored.advance(500);assert.equal(ignored.overlay.open,false);
    }
});

test('clics, tactile, molette, glisser-déposer et soumissions sont interceptés', () => {
    const h=setup();h.api.start();
    for (const type of ['click','dblclick','pointerdown','pointerup','mousedown','mouseup','touchstart','touchend','contextmenu','dragstart','dragover','drop','submit','wheel','touchmove']) {
        const e=h.event(type,{touches:[{}]});h.winEvents[type](e);
        assert.equal(e.defaultPrevented,true,type);assert.equal(e.stopped,true,type);
    }
    const e=h.click({href:'/autre-page'});assert.equal(e.stopped,true);
});

test('Échap, Tab et Entrée ne touchent ni le voile ni une popup derrière', () => {
    const h=setup();h.api.start();
    for (const key of ['Escape','Tab','Enter',' ','a']) {
        const e=h.event('keydown',{key});h.winEvents.keydown(e);
        assert.equal(e.defaultPrevented,true);assert.equal(e.stopped,true);
    }
    const cancel=h.event('cancel');h.overlayEvents.cancel(cancel);
    assert.equal(cancel.defaultPrevented,true);assert.equal(h.overlay.open,true);
});

test('les raccourcis du navigateur restent possibles, pas ceux de la page', () => {
    const h=setup();h.api.start();
    for (const data of [{key:'r',ctrlKey:true},{key:'ArrowLeft',altKey:true},{key:'F5'}]) {
        const e=h.event('keydown',data);h.winEvents.keydown(e);
        assert.equal(e.defaultPrevented,false);assert.equal(e.stopped,true);
    }
});

test('l’interface redevient interactive après la fin', () => {
    const h=setup();h.api.start()();const e=h.event('click');h.winEvents.click(e);
    assert.equal(e.defaultPrevented,false);assert.equal(e.stopped,false);
});

test('le garde-fou de navigation ne libère pas une opération AJAX en cours', () => {
    const h=setup();h.click({href:'/employes'});h.advance(0);const done=h.api.start();
    h.advance(31000);assert.equal(h.overlay.open,true);done();assert.equal(h.overlay.open,false);
});

test('redirection JavaScript : même suivi et même libération', () => {
    const h=setup();h.api.navigate('/employes');assert.deepEqual(h.navigations,['/employes']);
    assert.equal(h.overlay.open,true);h.advance(30000);assert.equal(h.overlay.open,false);
});

test('le focus initial et le verrouillage de scroll préexistant sont restaurés', () => {
    const h=setup();const input=h.element();h.document.activeElement=input;h.document.body.style.overflow='hidden';
    const done=h.api.start();done();
    assert.equal(h.document.activeElement,input);assert.equal(h.document.body.style.overflow,'hidden');
});

test('un formulaire remplacé par AJAX reçoit le focus dans sa popup', () => {
    const h=setup();const old=h.element();h.document.activeElement=old;const done=h.api.start();old.isConnected=false;
    const field=h.element();const surface=h.element();surface.children=[field];surface.querySelectorAll=() => [field];h.setSurface(surface);
    done();assert.equal(h.document.activeElement,field);
});

test('un échec de connexion conserve le focus sur le mot de passe invalide', () => {
    const h=setup();const password=h.element();const username=h.element();h.setInvalidPassword(password);h.setAutofocus(username);
    h.document.activeElement=username;
    h.api.start()();assert.equal(h.document.activeElement,password);
});

test('le chargement initial se termine à load puis pageshow', () => {
    const h=setup({readyState:'interactive'});assert.equal(h.overlay.open,true);
    h.winEvents.load();assert.equal(h.overlay.open,false);h.winEvents.pageshow();assert.equal(h.overlay.open,false);
});
