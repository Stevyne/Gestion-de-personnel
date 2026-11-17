(() => {
    'use strict';

    const bar = document.getElementById('appLoading');
    const status = document.getElementById('appLoadingStatus');
    if (!bar || !status) return;

    // Évite le clignotement des opérations rapides, sans retarder les requêtes.
    const SHOW_DELAY = 120;
    const MIN_VISIBLE = 280;
    const NAVIGATION_TIMEOUT = 30000;
    const pending = new Set();
    let showTimer = null;
    let hideTimer = null;
    let visibleSince = 0;
    let navigationDone = null;
    let navigationTimer = null;

    const card = document.querySelector('.login-card');
    if (card) card.prepend(bar);

    function hide() {
        bar.hidden = true;
        status.textContent = '';
    }

    // Chaque opération reçoit sa propre fonction de fin, idempotente : une
    // ancienne réponse/annulation ne peut pas arrêter une requête plus récente.
    function start() {
        const token = {};
        pending.add(token);
        window.clearTimeout(hideTimer);
        if (bar.hidden && showTimer === null) {
            showTimer = window.setTimeout(() => {
                showTimer = null;
                if (!pending.size) return;
                visibleSince = Date.now();
                bar.hidden = false;
                status.textContent = 'Chargement en cours…';
            }, SHOW_DELAY);
        }
        return () => {
            if (!pending.delete(token) || pending.size) return;
            window.clearTimeout(showTimer);
            showTimer = null;
            const remaining = bar.hidden ? 0 : MIN_VISIBLE - (Date.now() - visibleSince);
            hideTimer = window.setTimeout(() => {
                if (!pending.size) hide();
            }, Math.max(0, remaining));
        };
    }

    function stopNavigation() {
        window.clearTimeout(navigationTimer);
        if (navigationDone) navigationDone();
        navigationDone = null;
    }

    function startNavigation() {
        if (navigationDone) return;
        navigationDone = start();
        // Le navigateur n'expose pas de promesse pour une navigation native.
        // Ce garde-fou évite une barre infinie si celle-ci est abandonnée.
        navigationTimer = window.setTimeout(stopNavigation, NAVIGATION_TIMEOUT);
    }

    function navigate(url) {
        startNavigation();
        try {
            window.location.assign(url);
        } catch (error) {
            stopNavigation();
            throw error;
        }
    }

    function reset() {
        pending.clear();
        window.clearTimeout(showTimer);
        window.clearTimeout(hideTimer);
        window.clearTimeout(navigationTimer);
        showTimer = hideTimer = navigationTimer = null;
        navigationDone = null;
        hide();
    }

    function sameWindow(target) {
        const baseTarget = document.querySelector('base[target]')?.getAttribute('target');
        return !(target || baseTarget) || (target || baseTarget).toLowerCase() === '_self';
    }

    function internalUrl(value) {
        try {
            const url = new URL(value, document.baseURI);
            return ['http:', 'https:'].includes(url.protocol)
                && url.origin === window.location.origin ? url : null;
        } catch (error) {
            return null;
        }
    }

    // La capture voit aussi les liens du tiroir mobile (stopPropagation).
    // Une nouvelle tâche, plutôt qu'une microtâche, laisse TOUS les handlers
    // terminer : une popup, une validation ou un preventDefault n'allume pas
    // par erreur le témoin de navigation.
    document.addEventListener('click', event => {
        if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        const link = event.target.closest('a[href]');
        if (!link) return;
        window.setTimeout(() => {
            if (event.defaultPrevented || link.closest('[data-no-loading]')
                || link.hasAttribute('download') || !sameWindow(link.getAttribute('target'))) return;
            const href = (link.getAttribute('href') || '').trim();
            if (!href || href.startsWith('#')) return;
            const url = internalUrl(href);
            if (!url) return;
            if (url.pathname === window.location.pathname
                && url.search === window.location.search && url.hash) return;
            startNavigation();
        }, 0);
    }, true);

    document.addEventListener('submit', event => {
        const form = event.target;
        const button = event.submitter;
        window.setTimeout(() => {
            if (event.defaultPrevented || form.closest('[data-no-loading]')
                || button?.closest('[data-no-loading]')) return;
            const target = button?.getAttribute('formtarget') ?? form.getAttribute('target');
            const method = button?.getAttribute('formmethod') || form.getAttribute('method') || 'get';
            const action = button?.getAttribute('formaction') || form.getAttribute('action') || window.location.href;
            if (method.toLowerCase() === 'dialog' || !sameWindow(target) || !internalUrl(action)) return;
            startNavigation();
        }, 0);
    }, true);

    // Ne modifie ni window.fetch ni HTMLFormElement.prototype.submit.
    // Les requêtes d'arrière-plan restent silencieuses par défaut.
    window.AppLoading = Object.freeze({start, navigate});

    // Une page restaurée via Précédent/Suivant doit revenir au repos.
    window.addEventListener('pageshow', reset);
    if (document.readyState !== 'complete') {
        const done = start();
        window.addEventListener('load', done, {once: true});
    }
})();
