(() => {
    'use strict';

    const selector = '[data-flash-message]';
    const messages = new Map();
    let pageHidden = false;

    function forget(element) {
        const state = messages.get(element);
        if (!state) return;
        window.clearTimeout(state.timer);
        state.listeners.forEach(remove => remove());
        messages.delete(element);
        element.classList.remove('is-flash-ready', 'is-flash-paused');
    }

    function dismiss(element) {
        forget(element);
        element.remove();
    }

    function update(state) {
        const element = state.element;
        if (!element.isConnected) { forget(element); return; }
        window.clearTimeout(state.timer);
        const now = performance.now();
        if (!state.paused) state.remaining -= now - state.lastUpdate;
        state.lastUpdate = now;
        if (state.remaining <= 0) { dismiss(element); return; }

        state.paused = state.hovered || state.focused || document.hidden || pageHidden;
        element.classList.toggle('is-flash-paused', state.paused);
        if (state.counter) {
            const text = Math.ceil(state.remaining / 1000) + ' s' + (state.paused ? ' · pause' : '');
            if (state.counter.textContent !== text) state.counter.textContent = text;
        }
        if (!state.paused) {
            state.timer = window.setTimeout(() => update(state), Math.min(250, state.remaining));
        }
    }

    function init(element) {
        if (messages.has(element)) return;
        const duration = Number(element.getAttribute('data-dismiss-after'));
        const timed = Number.isFinite(duration) && duration > 0;
        const state = {
            element, remaining: duration, paused: true,
            hovered: element.matches(':hover'),
            focused: element.contains(document.activeElement),
            lastUpdate: performance.now(), timer: null, listeners: [],
            counter: element.querySelector('[data-flash-countdown]'),
        };
        messages.set(element, state);
        function on(target, event, listener) {
            target.addEventListener(event, listener);
            state.listeners.push(() => target.removeEventListener(event, listener));
        }

        const close = element.querySelector('[data-flash-dismiss]');
        if (close) {
            close.hidden = false;
            on(close, 'click', () => dismiss(element));
        }
        if (!timed) return;

        const progress = element.querySelector('[data-flash-timer]');
        if (progress) {
            progress.style.animationDuration = duration + 'ms';
            progress.hidden = false;
        }
        if (state.counter) state.counter.hidden = false;
        element.classList.add('is-flash-ready');
        on(element, 'pointerenter', event => {
            if (event.pointerType === 'touch') return;
            state.hovered = true;
            update(state);
        });
        on(element, 'pointerleave', () => { state.hovered = false; update(state); });
        on(element, 'focusin', () => { state.focused = true; update(state); });
        on(element, 'focusout', event => {
            state.focused = element.contains(event.relatedTarget);
            update(state);
        });
        state.timed = true;
        update(state);
    }

    function scan(root) {
        if (root.nodeType === 1 && root.matches(selector)) init(root);
        root.querySelectorAll?.(selector).forEach(init);
    }

    function updateAll() {
        messages.forEach(state => { if (state.timed) update(state); });
    }

    scan(document);
    // Les fragments de popup et de panneau sont injectés après le chargement
    // initial. Un seul observateur suffit ; aucun script dans les fragments.
    new MutationObserver(records => {
        records.forEach(record => record.addedNodes.forEach(scan));
        messages.forEach((_state, element) => {
            if (!element.isConnected) forget(element);
        });
    }).observe(document.body, {childList: true, subtree: true});

    document.addEventListener('visibilitychange', updateAll);
    // Conserver le temps de lecture restant dans le cache Précédent/Suivant.
    window.addEventListener('pagehide', () => { pageHidden = true; updateAll(); });
    window.addEventListener('pageshow', () => { pageHidden = false; updateAll(); });
})();
