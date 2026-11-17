(() => {
    'use strict';

    const overlay = document.getElementById('appLoadingOverlay');
    const bar = document.getElementById('appLoading');
    const status = document.getElementById('appLoadingStatus');
    if (!overlay || !bar || !status) return;

    const NAVIGATION_TIMEOUT = 30000;
    const pending = new Set();
    let locked = false;
    let previousFocus = null;
    let navigationDone = null;
    let navigationTimer = null;

    function usable(element) {
        return element && element !== document.body && element !== document.documentElement
            && element.isConnected && !element.disabled && !element.closest('[inert]')
            && !overlay.contains(element) && element.getClientRects().length > 0;
    }

    function restoreFocus() {
        const surface = document.querySelector('#formModal.show')
            || document.querySelector('#genericConfirmModal.show, #deleteModal.show')
            || document.querySelector('.activity-panel-layer.is-open .activity-panel');
        let target = usable(previousFocus) ? previousFocus : null;
        if (surface && (!target || !surface.contains(target))) {
            const fields = 'input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled])';
            target = Array.from(surface.querySelectorAll(fields)).find(usable)
                || Array.from(surface.querySelectorAll('button:not([disabled]), a[href]')).find(usable);
        }
        if (!surface) {
            // Le script de connexion a pu demander ce focus alors que le
            // document était encore inerte pendant son chargement initial.
            target = document.querySelector('input[type="password"][aria-invalid="true"]')
                || target || document.querySelector('[autofocus]');
        }
        if (usable(target) && document.activeElement !== target) target.focus({preventScroll: true});
        previousFocus = null;
    }

    function lock() {
        if (locked) return;
        previousFocus = document.activeElement;
        locked = true;
        document.documentElement.classList.add('app-loading-active');
        document.body.classList.add('app-loading-active');
        bar.hidden = false;
        status.textContent = 'Chargement en cours…';
        // showModal place le voile dans la couche supérieure du navigateur et
        // rend tout le reste du document inerte, y compris les nouveaux nœuds.
        overlay.showModal();
        overlay.focus({preventScroll: true});
    }

    function hide() {
        const wasLocked = locked;
        locked = false;
        bar.hidden = true;
        status.textContent = '';
        if (overlay.open) overlay.close();
        document.documentElement.classList.remove('app-loading-active');
        document.body.classList.remove('app-loading-active');
        if (wasLocked) restoreFocus();
    }

    // Chaque opération possède sa fin idempotente. Ni délai d'affichage ni
    // durée minimale : blocage immédiat, libération dès la dernière fin.
    function start() {
        const token = {};
        pending.add(token);
        lock();
        return () => {
            if (!pending.delete(token) || pending.size) return;
            hide();
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
        // Une navigation native n'expose pas de promesse de fin.
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
        window.clearTimeout(navigationTimer);
        navigationTimer = null;
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

    function blockInteraction(event) {
        if (!locked) return;
        if (event.type.startsWith('key')) {
            // Les raccourcis du navigateur (recharger, retour…) restent
            // utilisables, mais aucun raccourci de la page ne reçoit l'événement.
            if (!event.ctrlKey && !event.metaKey && !event.altKey && !/^F\d+$/.test(event.key)) {
                event.preventDefault();
            }
        } else if (event.type !== 'touchmove' || event.touches.length < 2) {
            event.preventDefault();
        }
        event.stopImmediatePropagation();
    }

    ['click', 'dblclick', 'pointerdown', 'pointerup', 'mousedown', 'mouseup',
     'touchstart', 'touchend', 'contextmenu', 'dragstart', 'dragover', 'drop',
     'submit', 'keydown', 'keyup', 'keypress', 'wheel', 'touchmove'].forEach(type => {
        window.addEventListener(type, blockInteraction, {capture: true, passive: false});
    });
    // Échap ne doit fermer ni le voile ni une popup située derrière lui.
    overlay.addEventListener('cancel', event => event.preventDefault());

    // La capture voit aussi les liens du tiroir mobile (stopPropagation).
    // La tâche différée laisse les handlers terminer : un preventDefault
    // utilisé par une popup n'est pas pris pour une navigation native.
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

    // Les fetch non suivis (polling) restent silencieux. Aucun prototype natif
    // n'est remplacé ; l'API existante des popups et panneaux reste identique.
    window.AppLoading = Object.freeze({start, navigate});
    window.addEventListener('pageshow', reset);
    if (document.readyState !== 'complete') {
        const done = start();
        window.addEventListener('load', done, {once: true});
    }
})();
