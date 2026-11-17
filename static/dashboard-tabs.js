(() => {
    'use strict';

    document.querySelectorAll('[data-dashboard-tabs]').forEach(root => {
        const nav = root.querySelector('[data-dashboard-nav]');
        if (!nav) return;
        const panels = new Map(Array.from(root.querySelectorAll('[data-dashboard-panel]'),
            panel => [panel.dataset.dashboardPanel, panel]));
        const links = Array.from(nav.querySelectorAll('a[data-dashboard-tab]'));
        const tabs = new Map();
        for (const link of links) {
            const name = link.dataset.dashboardTab;
            const panel = panels.get(name);
            let url;
            try { url = new URL(link.href, window.location.href); } catch (_error) { return; }
            // Validation complète avant d'améliorer les liens : si le HTML est
            // incomplet, la navigation serveur habituelle reste disponible.
            if (!panel || tabs.has(name) || !panel.id || !link.id
                || url.origin !== window.location.origin) return;
            tabs.set(name, {link, panel, url});
        }
        if (!tabs.size) return;

        const title = root.querySelector('[data-dashboard-title]');
        const description = root.querySelector('[data-dashboard-description]');
        const refresh = root.querySelector('[data-dashboard-refresh]');
        const currentName = () => {
            for (const [name, tab] of tabs) {
                if (tab.url.pathname === window.location.pathname) return name;
            }
            return null;
        };

        function activate(name, {push = false, focus = false} = {}) {
            const selected = tabs.get(name);
            if (!selected) return false;
            const changed = root.dataset.dashboardActive !== name;
            const previousPanel = panels.get(root.dataset.dashboardActive);
            const restoreFocus = changed && previousPanel && previousPanel.contains(document.activeElement);
            tabs.forEach((tab, key) => {
                const active = key === name;
                tab.link.classList.toggle('active', active);
                tab.link.setAttribute('aria-selected', String(active));
                tab.link.tabIndex = active ? 0 : -1;
                tab.panel.hidden = !active;
            });
            root.dataset.dashboardActive = name;
            if (title) title.textContent = selected.link.dataset.title || selected.link.textContent.trim();
            if (description) {
                description.textContent = selected.link.dataset.description || '';
                description.hidden = !description.textContent;
            }
            document.title = selected.link.dataset.documentTitle || document.title;
            if (refresh) refresh.href = selected.url.pathname + selected.url.search;

            if (push && changed && currentName() !== name) {
                try {
                    // Ne pas remplacer les éventuelles données d'un autre
                    // contrôleur dans l'entrée d'historique courante.
                    const state = window.history.state;
                    const previous = state && typeof state === 'object' && !Array.isArray(state) ? state : {};
                    window.history.pushState({...previous, dashboardTab: name}, '',
                        selected.url.pathname + selected.url.search + selected.url.hash);
                } catch (_error) {
                    // Historique indisponible (p. ex. aperçu sandboxé) :
                    // conserver la bascule locale, sans lancer de requête.
                }
            }
            if (focus || restoreFocus) selected.link.focus({preventScroll: true});
            // Rendre visible l'onglet actif dans la barre mobile sans déplacer
            // verticalement la page ni ajouter d'animation à la bascule.
            const bar = nav.getBoundingClientRect();
            const item = selected.link.getBoundingClientRect();
            if (item.left < bar.left) nav.scrollLeft += item.left - bar.left - 4;
            else if (item.right > bar.right) nav.scrollLeft += item.right - bar.right + 4;
            if (changed) root.dispatchEvent(new CustomEvent('dashboard:tabchange', {
                bubbles: true, detail: {tab: name},
            }));
            return true;
        }

        nav.setAttribute('role', 'tablist');
        tabs.forEach(({link, panel}) => {
            link.setAttribute('role', 'tab');
            link.removeAttribute('aria-current');
            link.setAttribute('data-no-loading', '');
            panel.setAttribute('role', 'tabpanel');
            panel.tabIndex = 0;
        });
        activate(currentName() || root.dataset.dashboardActive || tabs.keys().next().value);

        nav.addEventListener('click', event => {
            if (event.defaultPrevented || event.button !== 0
                || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
            const link = event.target.closest('a[data-dashboard-tab]');
            if (!link || !nav.contains(link) || link.hasAttribute('download')
                || (link.target && link.target !== '_self') || !tabs.has(link.dataset.dashboardTab)) return;
            event.preventDefault();
            activate(link.dataset.dashboardTab, {push: true});
        });

        nav.addEventListener('keydown', event => {
            if (event.ctrlKey || event.metaKey || event.altKey) return;
            const link = event.target.closest('a[data-dashboard-tab]');
            const index = links.indexOf(link);
            if (index < 0) return;
            let next;
            if (event.key === 'ArrowRight') next = (index + 1) % links.length;
            else if (event.key === 'ArrowLeft') next = (index - 1 + links.length) % links.length;
            else if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = links.length - 1;
            else if (event.key === 'Enter' || event.key === ' ') next = index;
            else return;
            event.preventDefault();
            activate(links[next].dataset.dashboardTab, {push: true, focus: true});
        });

        const restore = () => {
            const name = currentName();
            if (name) activate(name);
        };
        window.addEventListener('popstate', restore);
        window.addEventListener('pageshow', restore);
        window.AppPage?.onCleanup(()=>{
            window.removeEventListener('popstate', restore);
            window.removeEventListener('pageshow', restore);
        });
    });
})();
