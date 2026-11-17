(() => {
    'use strict';
    let mounting = false;
    let queue = [];
    let cleanups = [];

    function onCleanup(callback) {
        if (typeof callback === 'function') cleanups.push(callback);
    }
    function listen(target, type, callback, options) {
        target.addEventListener(type, callback, options);
        onCleanup(() => target.removeEventListener(type, callback, options));
    }
    function ready(callback) {
        if (mounting) queue.push(callback);
        else if (document.readyState === 'loading') listen(document, 'DOMContentLoaded', callback, {once: true});
        else callback();
    }
    function dispose() {
        mounting = false;
        queue = [];
        const previous = cleanups;
        cleanups = [];
        for (const callback of previous.reverse()) {
            try { callback(); } catch (error) { console.error('Nettoyage de page', error); }
        }
    }
    function begin() { mounting = true; queue = []; }
    function finish() {
        mounting = false;
        const callbacks = queue;
        queue = [];
        callbacks.forEach(callback => callback());
    }
    window.AppPage = Object.freeze({ready, onCleanup, listen, dispose, begin, finish});
})();
