// node --test tests/js/*.test.cjs — contrôleur pur, sans dépendance npm.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const source = readFileSync(join(__dirname, '../../static/dashboard-tabs.js'), 'utf8');

function setup({active='general', path='/', names=['general','rh','parc','direction'], broken=null, noRoot=false, historyError=false}={}) {
    const paths = {general:'/', rh:'/dashboard/rh', parc:'/dashboard/parc', direction:'/dashboard/direction'};
    const labels = {general:'Général', rh:'RH', parc:'Parc', direction:'Direction'};
    const document = {title:'initial', activeElement:null};
    class Element {
        constructor(attrs={}, dataset={}) {
            this.attrs={...attrs}; this.dataset={...dataset}; this.events={}; this.hidden=false; this.textContent=''; this.scrollLeft=0;
            this.classes=new Set((attrs.class || '').split(' ').filter(Boolean));
            this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};
            Object.defineProperty(this, 'innerHTML', {set:()=>{throw Error('Le HTML ne doit pas être interprété');}});
        }
        get id() {return this.attrs.id || '';}
        get href() {return this.attrs.href || '';}
        set href(value) {this.attrs.href=value;}
        get target() {return this.attrs.target || '';}
        setAttribute(name, value) {this.attrs[name]=String(value);}
        getAttribute(name) {return this.attrs[name] ?? null;}
        hasAttribute(name) {return Object.hasOwn(this.attrs,name);}
        removeAttribute(name) {delete this.attrs[name];}
        addEventListener(type, cb) {(this.events[type] ||= []).push(cb);}
        dispatchEvent(event) {(this.events[event.type] || []).forEach(cb=>cb(event));}
        closest(selector) {return selector==='a[data-dashboard-tab]' && this.dataset.dashboardTab ? this : null;}
        focus(options) {document.activeElement=this; this.focusOptions=options;}
        contains(element) {return element===this;}
        getBoundingClientRect() {return this.box || {left:0,right:100};}
    }
    const nav=new Element(); const root=new Element({}, {dashboardActive:active});
    const links=names.map(name=>{
        const link=new Element({id:'dashboard-tab-'+name, href:'https://app.test'+paths[name], 'aria-current':name===active?'page':'', class:name===active?'active':''},
            {dashboardTab:name,title:'Vue '+labels[name],documentTitle:'Vue '+labels[name]+' · App',description:name==='general'?'':'Description '+name});
        link.textContent=labels[name]; return link;
    });
    const panels=names.map(name=>{
        const panel=new Element({id:'dashboard-panel-'+name}, {dashboardPanel:name}); panel.hidden=name!==active; return panel;
    });
    if (broken==='panel') panels.pop();
    if (broken==='foreign') links.at(-1).href='https://evil.test/dashboard/direction';
    if (broken==='invalid') links.at(-1).href='http://[';
    if (broken==='duplicate') links.at(-1).dataset.dashboardTab=names[0];
    const title=new Element(); const description=new Element(); const refresh=new Element();
    root.querySelector=selector=>({
        '[data-dashboard-nav]':broken==='nav'?null:nav,
        '[data-dashboard-title]':title, '[data-dashboard-description]':description, '[data-dashboard-refresh]':refresh,
    })[selector];
    root.querySelectorAll=()=>panels;
    nav.querySelectorAll=()=>links;
    nav.contains=link=>links.includes(link);
    document.querySelectorAll=()=>noRoot?[]:[root];
    let location=new URL('https://app.test'+path); const pushed=[];
    const window={events:{}, get location(){return location;}, history:{state:{autre:'conservé'},pushState(state,unused,url){
        if(historyError) throw Error('Historique interdit');
        this.state=state; pushed.push({state,url}); location=new URL(url,location);
    }}, addEventListener(type, cb){(this.events[type] ||= []).push(cb);}};
    class CustomEvent {constructor(type,options) {this.type=type; Object.assign(this,options);}}
    const requests=[];
    const forbidden=(...args)=>{requests.push(args); throw Error('Aucun réseau autorisé');};
    vm.runInNewContext(source, {document,window,URL,CustomEvent,fetch:forbidden,XMLHttpRequest:forbidden});
    const events=[]; root.addEventListener('dashboard:tabchange',event=>events.push(event.detail.tab));
    function send(type,name,extra={}) {
        const event={type,button:0,target:links.find(link=>link.dataset.dashboardTab===name),defaultPrevented:false,
            preventDefault(){this.defaultPrevented=true;},...extra};
        nav.dispatchEvent(event); return event;
    }
    function restore(path,type='popstate') {location=new URL(path,location); (window.events[type]||[]).forEach(cb=>cb({type}));}
    return {root,nav,links,panels,title,description,refresh,document,window,pushed,events,requests,send,restore};
}

function selected(h,name) {
    assert.equal(h.root.dataset.dashboardActive,name);
    for (const link of h.links) {
        const active=link.dataset.dashboardTab===name;
        assert.equal(link.getAttribute('aria-selected'),String(active));
        assert.equal(link.tabIndex,active?0:-1);
        assert.equal(link.classes.has('active'),active);
    }
    for (const panel of h.panels) assert.equal(panel.hidden,panel.dataset.dashboardPanel!==name);
    assert.equal(h.requests.length,0);
}

test('amélioration ARIA uniquement après validation et un seul onglet au tabulateur',()=>{
    const h=setup(); selected(h,'general');
    assert.equal(h.nav.getAttribute('role'),'tablist');
    for(const link of h.links){assert.equal(link.getAttribute('role'),'tab');assert.equal(link.hasAttribute('aria-current'),false);assert.equal(link.hasAttribute('data-no-loading'),true);}
    for(const panel of h.panels){assert.equal(panel.getAttribute('role'),'tabpanel');assert.equal(panel.tabIndex,0);}
    assert.equal(h.description.hidden,true);assert.equal(h.pushed.length,0);
});

test('clic simple : bascule locale, titre, actualisation et historique cohérents',()=>{
    const h=setup(); const event=h.send('click','rh'); selected(h,'rh'); assert.equal(event.defaultPrevented,true);
    assert.equal(h.title.textContent,'Vue RH');assert.equal(h.document.title,'Vue RH · App');
    assert.equal(h.description.textContent,'Description rh');assert.equal(h.description.hidden,false);
    assert.equal(h.refresh.href,'/dashboard/rh');assert.equal(h.window.location.pathname,'/dashboard/rh');
    assert.equal(h.pushed.length,1);assert.equal(h.pushed[0].state.autre,'conservé');assert.equal(h.pushed[0].state.dashboardTab,'rh');
    assert.deepEqual(h.events,['rh']);
});

test('clic répété sur l’onglet actif : ni événement ni entrée en double',()=>{
    const h=setup();h.send('click','general');assert.equal(h.pushed.length,0);assert.deepEqual(h.events,[]);
    h.send('click','parc');h.send('click','parc');selected(h,'parc');assert.equal(h.pushed.length,1);assert.deepEqual(h.events,['parc']);
});

test('une URL spécialisée sélectionne la bonne vue à l’initialisation',()=>{
    const h=setup({active:'direction',path:'/dashboard/direction'});selected(h,'direction');
    assert.equal(h.refresh.href,'/dashboard/direction');assert.equal(h.pushed.length,0);
});

test('flèches, Début et Fin bouclent, activent et gardent le focus dans la barre',()=>{
    const h=setup();
    assert.equal(h.send('keydown','general',{key:'ArrowLeft'}).defaultPrevented,true);selected(h,'direction');
    assert.equal(h.document.activeElement,h.links[3]);assert.equal(h.document.activeElement.focusOptions.preventScroll,true);
    h.send('keydown','direction',{key:'ArrowRight'});selected(h,'general');
    h.send('keydown','general',{key:'End'});selected(h,'direction');
    h.send('keydown','direction',{key:'Home'});selected(h,'general');
});

test('Entrée et Espace activent sans navigation ; les autres touches restent libres',()=>{
    const h=setup();h.send('keydown','rh',{key:'Enter'});selected(h,'rh');h.send('keydown','parc',{key:' '});selected(h,'parc');
    for(const key of ['Tab','Escape','ArrowDown'])assert.equal(h.send('keydown','parc',{key}).defaultPrevented,false);
    assert.equal(h.send('keydown','parc',{key:'Home',ctrlKey:true}).defaultPrevented,false);selected(h,'parc');
});

test('Précédent, Suivant et pageshow restaurent sans requête ni nouvel historique',()=>{
    const h=setup();h.send('click','rh');h.send('click','direction');
    h.restore('/dashboard/rh');selected(h,'rh');h.restore('/');selected(h,'general');h.restore('/dashboard/direction','pageshow');selected(h,'direction');
    assert.equal(h.pushed.length,2);assert.deepEqual(h.events,['rh','direction','rh','general','direction']);
    h.restore('/autre-page');selected(h,'direction');
});

test('les clics modifiés, boutons secondaires et événements déjà annulés restent natifs',()=>{
    for(const extra of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{defaultPrevented:true}]){
        const h=setup();const event=h.send('click','rh',extra);selected(h,'general');assert.equal(h.pushed.length,0);
        assert.equal(event.defaultPrevented,Boolean(extra.defaultPrevented));
    }
});

test('target et download ne sont pas interceptés',()=>{
    for(const attr of ['target','download']){
        const h=setup();h.links[1].setAttribute(attr,attr==='target'?'_blank':'');
        assert.equal(h.send('click','rh').defaultPrevented,false);selected(h,'general');
    }
});

test('sans permission d’historique la bascule reste locale et ne lance jamais de repli réseau',()=>{
    const h=setup({historyError:true});h.send('click','rh');selected(h,'rh');assert.equal(h.pushed.length,0);assert.deepEqual(h.events,['rh']);
});

test('un compte restreint ne peut activer aucun onglet absent de son HTML',()=>{
    const h=setup({names:['general','parc']});h.send('click','parc');selected(h,'parc');
    assert.equal(h.send('click','rh',{target:{closest:()=>null}}).defaultPrevented,false);selected(h,'parc');
});

test('un seul onglet reste fonctionnel au clavier sans dupliquer l’historique',()=>{
    const h=setup({names:['general']});h.send('keydown','general',{key:'ArrowRight'});selected(h,'general');assert.equal(h.pushed.length,0);
});

test('HTML incomplet, lien externe ou ambigu : aucun rôle ARIA partiellement installé',()=>{
    for(const broken of ['panel','foreign','invalid','duplicate','nav']){
        const h=setup({broken});assert.equal(h.nav.getAttribute('role'),null);
        assert.equal(h.links[0].getAttribute('role'),null);assert.equal(h.links[0].hasAttribute('data-no-loading'),false);
    }
});

test('titres et descriptions restent du texte et non du HTML exécutable',()=>{
    const h=setup();const raw='<img src=x onerror=alert(1)> 🎉';h.links[1].dataset.title=raw;h.links[1].dataset.description=raw;
    h.send('click','rh');assert.equal(h.title.textContent,raw);assert.equal(h.description.textContent,raw);selected(h,'rh');
});

test('le focus d’un panneau devenu caché revient sur l’onglet visible',()=>{
    const h=setup();h.document.activeElement=h.panels[0];h.restore('/dashboard/rh');selected(h,'rh');assert.equal(h.document.activeElement,h.links[1]);
});

test('barre mobile : révèle horizontalement l’onglet actif, sans scroll vertical',()=>{
    const h=setup();h.nav.box={left:0,right:200};h.links[3].box={left:220,right:300};
    h.send('keydown','general',{key:'End'});assert.equal(h.nav.scrollLeft,104);
    h.links[0].box={left:-104,right:-20};h.send('keydown','direction',{key:'Home'});assert.equal(h.nav.scrollLeft,-4);
});

test('aucun effet sur une page sans dashboard',()=>{
    const h=setup({noRoot:true});assert.equal(h.nav.getAttribute('role'),null);assert.equal(h.pushed.length,0);assert.equal(h.requests.length,0);
});
