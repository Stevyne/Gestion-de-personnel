/* Politique de navigation testable sans DOM ; aucune décision d'autorisation. */
(function (root) {
    'use strict';
    const nativeKeys = new Set(['modal', 'panel', 'print', 'download', 'attachment']);
    function create(config, base) {
        const routes = config.routes.map(rule => new RegExp(rule));
        return value => {
            let url;
            try { url = new URL(value, base); } catch (_error) { return null; }
            const origin = new URL(base).origin;
            if (!config.enabled || !['http:', 'https:'].includes(url.protocol)
                || url.origin !== origin || url.username || url.password
                || [...nativeKeys].some(key => url.searchParams.has(key))
                || !routes.some(rule => rule.test(url.pathname))) return null;
            return url;
        };
    }
    const api = Object.freeze({create});
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.AppNavigationPolicy = api;
})(typeof window === 'undefined' ? globalThis : window);
