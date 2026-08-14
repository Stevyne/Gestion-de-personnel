(() => {
    'use strict';

    const layer = document.getElementById('activityPanelLayer');
    const panel = document.getElementById('activityPanel');
    const body = document.getElementById('activityPanelBody');
    const closeButton = document.getElementById('activityPanelClose');
    const backdrop = document.getElementById('activityPanelBackdrop');
    const title = document.getElementById('activityPanelTitle');
    const icon = document.getElementById('activityPanelIcon');
    const fullLink = document.getElementById('activityPanelFullLink');
    if (!layer || !panel || !body) return;

    let activeTrigger = null;
    let currentKind = null;
    let currentUrl = null;
    let controller = null;

    function panelUrl(url) {
        const parsed = new URL(url, window.location.origin);
        parsed.searchParams.set('panel', '1');
        return parsed.toString();
    }

    function fullUrl(url) {
        const parsed = new URL(url, window.location.origin);
        parsed.searchParams.delete('panel');
        parsed.searchParams.delete('anchor');
        return parsed.pathname + parsed.search + parsed.hash;
    }

    function ensureMessageStyles() {
        if (document.querySelector('link[data-activity-message-css]')) return;
        const href = panel.dataset.messageCss;
        if (!href) return;
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = href;
        link.dataset.activityMessageCss = '1';
        document.head.appendChild(link);
    }

    function setHeading(kind) {
        const messages = kind === 'messages';
        title.textContent = messages ? 'Messages' : 'Notifications';
        icon.textContent = messages ? '💬' : '🔔';
        layer.classList.toggle('is-messages', messages);
        layer.classList.toggle('is-notifications', !messages);
        if (messages) ensureMessageStyles();
    }

    function setExpanded(value) {
        document.querySelectorAll('.js-activity-panel').forEach(trigger => {
            trigger.setAttribute('aria-expanded', String(value && trigger === activeTrigger));
        });
    }

    function openLayer() {
        layer.hidden = false;
        panel.setAttribute('aria-hidden', 'false');
        document.body.classList.add('activity-panel-open');
        requestAnimationFrame(() => layer.classList.add('is-open'));
        setExpanded(true);
        if (typeof window.closeDrawer === 'function') window.closeDrawer(true);
        setTimeout(() => closeButton && closeButton.focus({preventScroll: true}), 260);
    }

    function closeLayer() {
        if (controller) controller.abort();
        layer.classList.remove('is-open');
        panel.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('activity-panel-open');
        setExpanded(false);
        const restore = activeTrigger;
        setTimeout(() => {
            layer.hidden = true;
            body.innerHTML = '<div class="activity-panel-loader"><span></span><p>Chargement…</p></div>';
            if (restore) restore.focus({preventScroll: true});
            activeTrigger = null;
        }, 240);
    }

    function showLoader() {
        body.innerHTML = '<div class="activity-panel-loader"><span></span><p>Chargement…</p></div>';
    }

    function updateBadges(root) {
        const count = Number(root && root.dataset.unreadCount);
        if (!Number.isFinite(count)) return;
        document.querySelectorAll(`[data-panel-kind="${currentKind}"]`).forEach(trigger => {
            let badge = trigger.querySelector('.notif-badge, .menu-count');
            if (count <= 0) {
                if (badge) badge.hidden = true;
                return;
            }
            if (!badge && trigger.classList.contains('nav-notif')) {
                badge = document.createElement('span');
                badge.className = 'notif-badge';
                trigger.appendChild(badge);
            }
            if (badge) {
                badge.hidden = false;
                badge.textContent = count < 100 ? String(count) : '99+';
            }
        });
    }

    function normalize(value) {
        return (value || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    }

    function bindSearch(root, inputSelector, itemSelector, datasetName, emptySelector) {
        const input = root.querySelector(inputSelector);
        if (!input) return;
        input.addEventListener('input', () => {
            const term = normalize(input.value.trim());
            let visible = 0;
            root.querySelectorAll(itemSelector).forEach(item => {
                const text = normalize(item.dataset[datasetName]);
                item.hidden = Boolean(term && !text.includes(term));
                if (!item.hidden) visible += 1;
            });
            const empty = emptySelector && root.querySelector(emptySelector);
            if (empty) empty.hidden = visible !== 0;
        });
    }

    function initMessagePanel(root) {
        bindSearch(root, '#conversationSearch', '.msg-conversation',
                   'conversationSearch', '#conversationNoResult');
        bindSearch(root, '#recipientSearch', '.msg-recipient', 'recipientSearch', null);

        const recipients = Array.from(root.querySelectorAll('.msg-recipient'));
        const recipientCount = root.querySelector('#recipientCount');
        const updateRecipients = () => {
            let count = 0;
            recipients.forEach(item => {
                const input = item.querySelector('input[type="checkbox"]');
                item.classList.toggle('is-selected', Boolean(input && input.checked));
                if (input && input.checked) count += 1;
            });
            if (recipientCount) recipientCount.textContent = `${count} sélectionné${count > 1 ? 's' : ''}`;
        };
        recipients.forEach(item => item.querySelector('input')?.addEventListener('change', updateRecipients));
        updateRecipients();

        const compose = root.querySelector('#formNouveauMessage');
        const recipientField = root.querySelector('#champDestinataires');
        const targetField = root.querySelector('#champCible');
        const titleField = root.querySelector('#champTitre');
        const updateType = () => {
            if (!compose) return;
            const type = compose.querySelector('input[name="type"]:checked')?.value || 'prive';
            if (recipientField) recipientField.hidden = type === 'annonce';
            if (targetField) targetField.hidden = type !== 'annonce';
            if (titleField) titleField.hidden = type === 'prive';
        };
        compose?.querySelectorAll('input[name="type"]').forEach(input => input.addEventListener('change', updateType));
        updateType();

        [['#messageFileInput', '#messageFileName'], ['#newMessageFile', '#newMessageFileName']]
            .forEach(([inputSelector, outputSelector]) => {
                const input = root.querySelector(inputSelector);
                const output = root.querySelector(outputSelector);
                input?.addEventListener('change', () => {
                    const file = input.files && input.files[0];
                    if (output) { output.textContent = file ? `📎 ${file.name}` : ''; output.hidden = !file; }
                });
            });

        const stream = root.querySelector('#messageStream');
        if (stream) requestAnimationFrame(() => { stream.scrollTop = stream.scrollHeight; });
        const composer = root.querySelector('#messageComposer');
        const textarea = root.querySelector('#messageComposerText');
        const resize = () => {
            if (!textarea) return;
            textarea.style.height = 'auto';
            textarea.style.height = `${Math.min(textarea.scrollHeight, 120)}px`;
        };
        textarea?.addEventListener('input', resize);
        textarea?.addEventListener('keydown', event => {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                composer?.requestSubmit();
            }
        });
        resize();
    }

    async function load(url, kind = currentKind) {
        currentKind = kind || 'notifications';
        currentUrl = panelUrl(url);
        setHeading(currentKind);
        fullLink.href = fullUrl(url);
        showLoader();
        if (controller) controller.abort();
        controller = new AbortController();
        try {
            const response = await fetch(currentUrl, {
                credentials: 'same-origin',
                headers: {'X-Activity-Panel': '1', 'X-Requested-With': 'XMLHttpRequest'},
                signal: controller.signal,
            });
            if (response.redirected && new URL(response.url).pathname === '/login') {
                window.location.assign(response.url);
                return;
            }
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const html = await response.text();
            if (/<!doctype html/i.test(html)) {
                window.location.assign(response.url);
                return;
            }
            body.innerHTML = html;
            const root = body.querySelector('.activity-panel-view');
            updateBadges(root);
            if (currentKind === 'messages' && root) initMessagePanel(root);
            body.scrollTop = 0;
        } catch (error) {
            if (error.name === 'AbortError') return;
            body.innerHTML = '<div class="activity-panel-error"><span>⚠️</span><p>Impossible de charger ce panneau.</p><button type="button">Réessayer</button></div>';
            body.querySelector('button')?.addEventListener('click', () => load(currentUrl, currentKind));
        }
    }

    document.addEventListener('click', event => {
        const trigger = event.target.closest('.js-activity-panel');
        if (trigger) {
            event.preventDefault();
            activeTrigger = trigger;
            const kind = trigger.dataset.panelKind || 'notifications';
            setHeading(kind);
            openLayer();
            load(trigger.dataset.panelUrl || trigger.href, kind);
            return;
        }
        const link = event.target.closest('#activityPanelBody [data-panel-link]');
        if (link) {
            event.preventDefault();
            load(link.href, currentKind);
        }
    });

    body.addEventListener('submit', async event => {
        const form = event.target.closest('[data-panel-form]');
        if (!form) return;
        event.preventDefault();
        const button = form.querySelector('button[type="submit"], .msg-send-btn');
        if (button) button.disabled = true;
        try {
            const response = await fetch(form.action || currentUrl, {
                method: (form.method || 'POST').toUpperCase(),
                body: new FormData(form), credentials: 'same-origin',
                headers: {'X-Activity-Panel': '1', 'X-Requested-With': 'XMLHttpRequest'},
            });
            if (response.redirected && new URL(response.url).pathname === '/login') {
                window.location.assign(response.url);
                return;
            }
            // Compatibilité défensive avec le gestionnaire AJAX des popups :
            // un éventuel 204 indique la cible dans X-Redirect-To.
            if (response.status === 204) {
                const target = response.headers.get('X-Redirect-To');
                if (!target) throw new Error('Redirection de panneau sans cible');
                await load(target, currentKind);
                return;
            }
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const html = await response.text();
            if (/<!doctype html/i.test(html)) {
                window.location.assign(response.url);
                return;
            }
            currentUrl = response.url;
            fullLink.href = fullUrl(response.url);
            body.innerHTML = html;
            const root = body.querySelector('.activity-panel-view');
            updateBadges(root);
            if (currentKind === 'messages' && root) initMessagePanel(root);
        } catch (error) {
            if (button) button.disabled = false;
            const notice = document.createElement('div');
            notice.className = 'activity-panel-flash activity-panel-flash--danger';
            notice.textContent = "L'action n'a pas pu être effectuée. Réessayez.";
            form.prepend(notice);
        }
    });

    closeButton?.addEventListener('click', closeLayer);
    backdrop?.addEventListener('click', closeLayer);
    document.addEventListener('keydown', event => {
        if (!layer.classList.contains('is-open')) return;
        if (event.key === 'Escape') { event.preventDefault(); closeLayer(); return; }
        if (event.key !== 'Tab') return;
        const focusable = Array.from(panel.querySelectorAll(
            'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled])'
        )).filter(element => !element.hidden && element.offsetParent !== null);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
})();
