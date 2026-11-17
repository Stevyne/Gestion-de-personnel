(() => {
    'use strict';

    // Ces SVG sont rendus par la macro Jinja commune à la navbar. Ni CDN,
    // ni interprétation HTML des messages, noms de fichiers ou autres saisies.
    const bank = document.getElementById('appIconTemplates');
    const templates = new Map();
    bank?.content.querySelectorAll('svg[data-icon]').forEach(svg => {
        templates.set(svg.getAttribute('data-icon'), svg);
    });

    function dimension(size) {
        if (size === '1em') return size;
        const value = Number(size);
        return Number.isFinite(value) && value >= 8 && value <= 128 ? value : 18;
    }

    function create(name, size = 18) {
        const source = templates.get(name) || templates.get('info');
        if (!source) return null;
        const svg = source.cloneNode(true);
        const value = String(dimension(size));
        svg.setAttribute('width', value);
        svg.setAttribute('height', value);
        return svg;
    }

    function set(target, name, size = 18) {
        if (!target) return;
        const svg = create(name, size);
        if (svg) target.replaceChildren(svg);
    }

    function withText(target, name, text, size = '1em') {
        if (!target) return;
        const svg = create(name, size);
        const value = document.createTextNode(String(text ?? ''));
        if (svg) target.replaceChildren(svg, document.createTextNode(' '), value);
        else target.replaceChildren(value);
    }

    window.AppIcons = Object.freeze({create, set, withText});
})();
