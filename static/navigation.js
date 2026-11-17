(() => {
    'use strict';
    function boot() {
        const settings = document.getElementById('appNavigationConfig');
        if (!settings || !window.AppPage || !window.AppNavigationPolicy || !window.fetch) return;
        let config;
        try { config = JSON.parse(settings.textContent); } catch (_error) { return; }
        if (!config.enabled || !document.getElementById('appMain')) return;
        const eligible = window.AppNavigationPolicy.create(config, window.location.href);
        const stateKey = '__teamSphereNavigation';
        const once = new Set(Array.from(document.querySelectorAll('script[data-app-once][src]'), script => script.src));
        let requestId = 0;
        let controller = null;
        let scrollFrame = null;
        let renderedKey = null;
        let renderedUrl = window.location.href;
        let mounting = false;

        const newKey = () => window.crypto?.randomUUID?.() || Date.now() + '-' + Math.random();
        const stateObject = () => {
            const state = window.history.state;
            return state && typeof state === 'object' && !Array.isArray(state) ? state : {};
        };
        function replaceEntry(key = renderedKey) {
            const state = stateObject();
            window.history.replaceState({...state, [stateKey]: {
                version: config.version, key, scroll: [window.scrollX, window.scrollY],
            }}, '', window.location.href);
        }
        renderedKey = newKey();
        replaceEntry();
        window.history.scrollRestoration = 'manual';

        function normalNavigation(url) {
            if (window.AppLoading) window.AppLoading.navigate(url);
            else window.location.assign(url);
        }
        function announce(text) {
            const output = document.getElementById('appNavigationAnnouncer');
            if (output) output.textContent = text;
        }
        function clearError() { document.getElementById('appNavigationError')?.remove(); }
        function showError(url) {
            clearError();
            const box = document.createElement('section');
            box.id = 'appNavigationError';
            box.className = 'navigation-error';
            box.setAttribute('role', 'alert');
            const message = document.createElement('p');
            message.textContent = 'La page n’a pas pu être chargée. Votre page actuelle est conservée.';
            const retry = document.createElement('button');
            retry.type = 'button'; retry.className = 'btn btn-secondary'; retry.textContent = 'Réessayer';
            retry.addEventListener('click', () => visit(url));
            const native = document.createElement('a');
            native.href = url; native.className = 'btn btn-secondary'; native.textContent = 'Ouvrir normalement';
            native.setAttribute('data-native-navigation', '');
            box.append(message, retry, native);
            document.getElementById('appMain').prepend(box);
        }
        function parseHTML(html) {
            const template = document.createElement('template');
            template.innerHTML = html;
            return template.content;
        }
        function scriptsIn(root) {
            return Array.from(root.querySelectorAll('script')).filter(script =>
                !script.type || ['text/javascript', 'application/javascript'].includes(script.type.toLowerCase()));
        }
        function validateScripts(root, baseUrl) {
            for (const script of root.querySelectorAll('script')) {
                if (script.type && !['text/javascript', 'application/javascript', 'application/json', 'application/ld+json'].includes(script.type.toLowerCase())) return false;
                if (script.src) {
                    const url = new URL(script.getAttribute('src'), baseUrl);
                    if (url.origin !== window.location.origin || !['http:', 'https:'].includes(url.protocol)) return false;
                }
            }
            return true;
        }
        function shellLinks(payload) {
            const links = Array.from(document.querySelectorAll('.nav-link, .nav-dropdown-link, .nav-notif'));
            if (!Array.isArray(payload.nav) || links.length !== payload.nav.length) return null;
            for (let i = 0; i < links.length; i++) {
                if (links[i].tagName.toLowerCase() !== payload.nav[i].tag
                    || links[i].getAttribute('href') !== payload.nav[i].href) return null;
            }
            return links;
        }
        async function prepareStyles(html, signal, baseUrl) {
            const start = document.querySelector('meta[name="app-page-styles"][content="start"]');
            const end = document.querySelector('meta[name="app-page-styles"][content="end"]');
            if (!start || !end) throw new Error('Emplacement des styles absent');
            const old = [];
            for (let node = start.nextSibling; node && node !== end; node = node.nextSibling) {
                if (node.nodeType === 1) old.push(node);
            }
            const desired = Array.from(parseHTML(html).children);
            const staged = [], used = new Set();
            try {
                for (const candidate of desired) {
                    if (!['LINK', 'STYLE'].includes(candidate.tagName)) throw new Error('Ressource de page non prise en charge');
                    const href = candidate.tagName === 'LINK' ? new URL(candidate.getAttribute('href'), baseUrl) : null;
                    if (href && (candidate.rel !== 'stylesheet' || href.origin !== window.location.origin
                        || !['http:', 'https:'].includes(href.protocol))) throw new Error('Style externe');
                    const previous = old.find(node => !used.has(node) && node.tagName === candidate.tagName
                        && (href ? node.href === href.href && node.media === candidate.media : node.textContent === candidate.textContent));
                    if (previous) { used.add(previous); continue; }
                    const node = candidate.cloneNode(true);
                    if (href) node.href = href.href;
                    staged.push(node);
                    if (node.tagName === 'LINK') {
                        await new Promise((resolve, reject) => {
                            const aborted = () => reject(new DOMException('Navigation annulée', 'AbortError'));
                            const finish = callback => () => { signal.removeEventListener('abort', aborted); callback(); };
                            node.onload = finish(resolve); node.onerror = finish(() => reject(new Error('Style indisponible')));
                            signal.addEventListener('abort', aborted, {once: true});
                            end.before(node);
                            if (signal.aborted) aborted();
                        });
                    } else end.before(node);
                }
                return {
                    commit() { old.filter(node => !used.has(node)).forEach(node => node.remove()); },
                    rollback() { staged.forEach(node => node.remove()); },
                };
            } catch (error) { staged.forEach(node => node.remove()); throw error; }
        }
        async function executeScript(original, destination, signal, baseUrl) {
            const src = original.hasAttribute('src') ? new URL(original.getAttribute('src'), baseUrl).href : null;
            if (src && original.hasAttribute('data-app-once') && once.has(src)) return;
            const script = document.createElement('script');
            for (const attr of original.attributes) {
                if (!['defer', 'async'].includes(attr.name)) script.setAttribute(attr.name, attr.value);
            }
            script.async = false;
            if (src) {
                script.src = src;
                await new Promise((resolve, reject) => {
                    const abort = () => { script.remove(); reject(new DOMException('Navigation annulée', 'AbortError')); };
                    const finish = callback => () => { signal.removeEventListener('abort', abort); callback(); };
                    script.onload = finish(resolve); script.onerror = finish(() => reject(new Error('Script indisponible')));
                    signal.addEventListener('abort', abort, {once: true});
                    destination.appendChild(script);
                    if (signal.aborted) abort();
                });
                if (original.hasAttribute('data-app-once')) once.add(src);
            } else {
                // Exécution normale d'un script de la vue serveur, pas d'eval
                // ni de fonction dynamique nécessitant unsafe-eval dans la CSP.
                script.textContent = original.textContent;
                let failure = null;
                const capture = event => { failure = event.error || new Error(event.message); };
                window.addEventListener('error', capture);
                try { destination.appendChild(script); } finally { window.removeEventListener('error', capture); }
                if (failure) throw failure;
            }
        }
        async function fetchPage(url, signal) {
            let target = url;
            for (let redirect = 0; redirect < 8; redirect++) {
                const response = await fetch(target, {
                    method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal,
                    headers: {'X-App-Navigation': '1', 'X-App-Identity': config.identity,
                              'X-App-Version': config.version, 'Accept': 'application/json'},
                });
                const native = response.headers.get('X-App-Native-Navigation');
                if (native) return {native: new URL(native, target).href};
                const next = response.headers.get('X-App-Redirect');
                if (next) {
                    const resolved = new URL(next, target);
                    if (!eligible(resolved.href)) return {native: resolved.href};
                    target = resolved.href;
                    continue;
                }
                if (!response.ok) throw new Error('HTTP ' + response.status);
                if (response.headers.get('X-App-Navigation') !== config.version
                    || !(response.headers.get('Content-Type') || '').includes('application/json')) return {native: target};
                const payload = await response.json();
                if (payload.version !== config.version || payload.identity !== config.identity) return {native: target};
                const resolved = new URL(payload.url, target);
                if (!eligible(resolved.href)) return {native: target};
                resolved.hash = new URL(target).hash;
                return {payload, url: resolved.href};
            }
            throw new Error('Trop de redirections');
        }
        async function visit(value, options = {}) {
            const url = eligible(value);
            if (!url) { normalNavigation(value); return false; }
            if (!options.pop) replaceEntry();
            const id = ++requestId;
            const previous = controller;
            const active = new AbortController();
            controller = active;
            const stopLoading = window.AppLoading ? window.AppLoading.start() : () => {};
            if (previous) previous.abort();
            const timer = window.setTimeout(() => active.abort('timeout'), 30000);
            let styles = null, committed = false, focus = null;
            let fallbackUrl = url.href;
            document.getElementById('appMain').setAttribute('aria-busy', 'true');
            clearError();
            try {
                const result = await fetchPage(url.href, active.signal);
                if (active.signal.aborted || id !== requestId) return false;
                if (result.native) { normalNavigation(result.native); return false; }
                const {payload} = result;
                fallbackUrl = result.url;
                if (typeof payload.main !== 'string' || typeof payload.styles !== 'string'
                    || typeof payload.scripts !== 'string' || typeof payload.title !== 'string') throw new Error('Réponse invalide');
                const fragment = parseHTML(payload.main);
                const mains = fragment.querySelectorAll('#appMain');
                const extra = parseHTML(payload.scripts);
                const links = shellLinks(payload);
                if (mains.length !== 1 || !links || !validateScripts(fragment, result.url) || !validateScripts(extra, result.url)) {
                    normalNavigation(result.url); return false;
                }
                const main = mains[0];
                const scripts = [...scriptsIn(main), ...scriptsIn(extra)];
                scripts.forEach(script => script.remove());
                styles = await prepareStyles(payload.styles, active.signal, result.url);
                if (active.signal.aborted || id !== requestId) { styles.rollback(); return false; }
                document.dispatchEvent(new CustomEvent('app:page-before-change'));
                window.AppPage.dispose();
                window.AppPage.begin();
                mounting = true;
                main.setAttribute('aria-busy', 'true');
                document.getElementById('appMain').replaceWith(main);
                committed = true;
                const destination = document.getElementById('appPageScripts');
                destination.replaceChildren();
                styles.commit();
                const locked = document.body.classList.contains('app-loading-active');
                document.body.className = payload.bodyClass || '';
                if (locked) document.body.classList.add('app-loading-active');
                document.title = payload.title;
                links.forEach((link, index) => {
                    link.classList.toggle('active', payload.nav[index].active);
                    if (payload.nav[index].active) link.setAttribute('aria-current', 'page');
                    else link.removeAttribute('aria-current');
                });
                if (payload.badges && window.AppActivityPanel) {
                    Object.entries(payload.badges).forEach(([kind, count]) => window.AppActivityPanel.updateBadges(kind, count));
                }
                const key = options.pop ? options.key : newKey();
                if (!options.pop) {
                    const state = {...stateObject(), [stateKey]: {version: config.version, key, scroll: [0, 0]}};
                    if (options.replace) window.history.replaceState(state, '', result.url);
                    else window.history.pushState(state, '', result.url);
                } else if (window.location.href !== result.url) {
                    const state = {...stateObject(), [stateKey]: {version: config.version, key, scroll: options.scroll || [0, 0]}};
                    window.history.replaceState(state, '', result.url);
                }
                renderedKey = key;
                renderedUrl = result.url;
                for (const script of scripts) {
                    if (active.signal.aborted || id !== requestId) return false;
                    await executeScript(script, destination, active.signal, result.url);
                }
                window.AppPage.finish();
                mounting = false;
                document.dispatchEvent(new CustomEvent('app:page-ready', {detail: {url: result.url, main}}));
                let anchor = null;
                try { anchor = new URL(result.url).hash && document.getElementById(decodeURIComponent(new URL(result.url).hash.slice(1))); } catch (_error) { /* ancre invalide */ }
                if (options.scroll) window.scrollTo(options.scroll[0], options.scroll[1]);
                else if (anchor) anchor.scrollIntoView();
                else window.scrollTo(0, 0);
                focus = options.focusId && document.getElementById(options.focusId) || main.querySelector('h1, h2') || main;
                if (focus === main || focus.matches('h1, h2')) focus.setAttribute('tabindex', '-1');
                announce(payload.title);
                replaceEntry();
                return true;
            } catch (error) {
                if (id !== requestId) return false;
                if (styles && !committed) styles.rollback();
                if (committed || mounting || options.pop) normalNavigation(fallbackUrl);
                else if (error.name !== 'AbortError' || active.signal.reason === 'timeout') showError(url.href);
                return false;
            } finally {
                window.clearTimeout(timer);
                stopLoading();
                if (controller === active) {
                    controller = null;
                    mounting = false;
                    document.getElementById('appMain')?.removeAttribute('aria-busy');
                    if (focus?.isConnected && !active.signal.aborted) focus.focus({preventScroll: true});
                }
            }
        }
        document.addEventListener('click', event => {
            if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            const link = event.target.closest('a[href]');
            if (!link || link.hasAttribute('download') || link.hasAttribute('onclick')
                || link.closest('[data-native-navigation], [data-no-loading], .js-modal-form, .js-activity-panel, #formModalBody, #activityPanelBody')
                || (link.target && link.target !== '_self') || document.querySelector('base[target]')) return;
            const raw = link.getAttribute('href').trim();
            if (!raw || raw.startsWith('#')) return;
            const url = eligible(link.href);
            if (!url || (url.pathname === window.location.pathname && url.search === window.location.search && url.hash)) return;
            event.preventDefault();
            // Capture nécessaire pour les liens du tiroir stopPropagation.
            // Le départ différé laisse les handlers locaux finir proprement.
            window.setTimeout(() => visit(url.href), 0);
        }, true);
        document.addEventListener('submit', event => {
            const form = event.target;
            const button = event.submitter;
            if (event.defaultPrevented || !(form instanceof HTMLFormElement)
                || form.closest('[data-native-navigation], [data-no-loading], #formModalBody, #activityPanelBody')
                || button?.closest('[data-native-navigation], [data-no-loading]')
                || document.querySelector('base[target]')) return;
            const method = button?.getAttribute('formmethod') || form.getAttribute('method') || 'get';
            const target = button?.getAttribute('formtarget') || form.getAttribute('target');
            if (method.toLowerCase() !== 'get' || (target && target !== '_self')) return;
            const url = new URL(button?.getAttribute('formaction') || form.getAttribute('action') || window.location.href, window.location.href);
            url.search = '';
            const fields = new FormData(form, button || undefined);
            for (const [name, value] of fields) url.searchParams.append(name, typeof value === 'string' ? value : value.name);
            if (!eligible(url.href)) return;
            event.preventDefault();
            visit(url.href, {focusId: document.activeElement?.id});
        });
        window.addEventListener('popstate', event => {
            const entry = event.state?.[stateKey];
            const previousUrl = new URL(renderedUrl);
            const samePage = previousUrl.pathname === window.location.pathname && previousUrl.search === window.location.search;
            if ((!entry && samePage) || (entry?.version === config.version && entry.key === renderedKey)) {
                if (mounting) { normalNavigation(window.location.href); return; }
                controller?.abort();
                renderedUrl = window.location.href;
                if (!entry) replaceEntry(); // une ancre native crée parfois un état nul
                else if (samePage && entry.scroll) {
                    const target = window.location.href;
                    window.requestAnimationFrame(() => {
                        if (!controller && window.location.href === target) window.scrollTo(entry.scroll[0], entry.scroll[1]);
                    });
                }
                return; // onglet local du dashboard ou ancre du même document
            }
            if (!entry || entry.version !== config.version || !eligible(window.location.href)) {
                normalNavigation(window.location.href); return;
            }
            visit(window.location.href, {pop: true, key: entry.key, scroll: entry.scroll});
        });
        window.addEventListener('scroll', () => {
            if (scrollFrame || controller) return;
            scrollFrame = window.requestAnimationFrame(() => { scrollFrame = null; if (!controller) replaceEntry(); });
        }, {passive: true});
        document.addEventListener('dashboard:tabchange', () => { renderedUrl = window.location.href; replaceEntry(); });
        window.addEventListener('pageshow', event => {
            if (!event.persisted) return;
            // Une restauration BFCache doit revalider la session côté serveur.
            visit(window.location.href, {replace: true});
        });
        window.AppNavigation = Object.freeze({visit, abort() { controller?.abort(); },
            get currentUrl() { return renderedUrl; }, get busy() { return Boolean(controller); }});
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once: true});
    else boot();
})();
