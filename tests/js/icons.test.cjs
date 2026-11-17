// node --test tests/js/*.test.cjs — sans dépendance npm.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {join}=require('node:path');
const vm=require('node:vm');
const source=readFileSync(join(__dirname,'../../static/icons.js'),'utf8');

function setup({missingBank=false}={}) {
    class Svg {
        constructor(name,attrs={}) {this.nodeName='svg';this.attrs={'data-icon':name,width:'18',height:'18','aria-hidden':'true',focusable:'false',...attrs};this.children=[{nodeName:'path',d:'M1 1h2'}];}
        getAttribute(name){return this.attrs[name] ?? null;}
        setAttribute(name,value){this.attrs[name]=String(value);}
        cloneNode(){const clone=new Svg(this.attrs['data-icon'],{...this.attrs});clone.children=this.children.map(n=>({...n}));return clone;}
    }
    const originals=['info','eye','eye-off','paperclip','menu','x','check'].map(name=>new Svg(name));
    const bank={content:{querySelectorAll:()=>originals}};
    const document={getElementById:()=>missingBank?null:bank,createTextNode:text=>({nodeName:'#text',textContent:text})};
    const fetch=()=>{};const window={fetch};
    vm.runInNewContext(source,{document,window});
    function target() {return {children:[],replaceChildren(...nodes){this.children=nodes;},set innerHTML(_){throw new Error('Ne jamais interpréter de HTML.');}};}
    return {api:window.AppIcons,originals,target,window,fetch};
}

test('API figée et aucune interception de fetch',()=>{
    const h=setup();assert.equal(Object.isFrozen(h.api),true);assert.equal(h.window.fetch,h.fetch);
});

test('create clone le SVG commun sans modifier le modèle',()=>{
    const h=setup();const a=h.api.create('eye',24);const b=h.api.create('eye',16);
    assert.equal(a.getAttribute('data-icon'),'eye');assert.equal(a.getAttribute('width'),'24');
    assert.equal(b.getAttribute('width'),'16');assert.notEqual(a,b);assert.notEqual(a.children[0],b.children[0]);
    assert.equal(h.originals[1].getAttribute('width'),'18');
    assert.equal(a.getAttribute('aria-hidden'),'true');assert.equal(a.getAttribute('focusable'),'false');
});

test('noms inconnus ou contenant du HTML : repli fermé sur info',()=>{
    const h=setup();for(const name of ['absent','<img src=x onerror=alert(1)>','__proto__','[data-icon="eye"]']){
        const svg=h.api.create(name);assert.equal(svg.getAttribute('data-icon'),'info');
        assert.equal(Object.values(svg.attrs).some(value=>String(value).includes('onerror')),false);
    }
});

test('tailles numériques et taille relative sont explicites',()=>{
    const h=setup();for(const size of [8,18,27.5,128,'24','1em']){
        const svg=h.api.create('eye',size);assert.equal(svg.getAttribute('width'),String(size));assert.equal(svg.getAttribute('height'),String(size));
    }
});

test('une taille invalide ne devient pas un attribut libre',()=>{
    const h=setup();for(const size of [-1,0,129,Infinity,NaN,'100vw','18" onload="evil',null]){
        assert.equal(h.api.create('eye',size).getAttribute('width'),'18');
    }
});

test('set remplace l’état précédent sans accumuler les icônes',()=>{
    const h=setup();const t=h.target();h.api.set(t,'eye');h.api.set(t,'eye-off',20);
    assert.equal(t.children.length,1);assert.equal(t.children[0].getAttribute('data-icon'),'eye-off');
    assert.equal(t.children[0].getAttribute('width'),'20');
});

test('withText conserve strictement un nom de fichier et ses caractères',()=>{
    const h=setup();const t=h.target();const value='<img src=x onerror=alert(1)> — équipe 🎉.pdf';
    h.api.withText(t,'paperclip',value);
    assert.equal(t.children.length,3);assert.equal(t.children[0].getAttribute('data-icon'),'paperclip');
    assert.equal(t.children[0].getAttribute('width'),'1em');
    assert.equal(t.children[1].textContent,' ');assert.equal(t.children[2].nodeName,'#text');assert.equal(t.children[2].textContent,value);
});

test('conteneur absent et texte nul sont gérés sans erreur',()=>{
    const h=setup();h.api.set(null,'eye');h.api.withText(null,'info','texte');
    const t=h.target();h.api.withText(t,'info',null);assert.equal(t.children.at(-1).textContent,'');
});

test('sans banque disponible, le libellé reste du texte et aucune erreur n’est levée',()=>{
    const h=setup({missingBank:true});assert.equal(h.api.create('eye'),null);
    const t=h.target();h.api.set(t,'eye');assert.equal(t.children.length,0);
    h.api.withText(t,'paperclip','fichier.pdf');assert.equal(t.children.length,1);assert.equal(t.children[0].nodeName,'#text');assert.equal(t.children[0].textContent,'fichier.pdf');
});
