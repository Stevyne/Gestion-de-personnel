(function () {
    'use strict';

    function initRegisterForm(root) {
        root = root || document;
        const form = root.querySelector ? root.querySelector('#registerForm') : null;
        if (!form || form.dataset.registerInitialized === '1') return;
        form.dataset.registerInitialized = '1';

        const password = form.querySelector('#password');
        const confirmation = form.querySelector('#confirm_password');
        const fill = form.querySelector('#strengthFill');
        const label = form.querySelector('#strengthLabel');
        const match = form.querySelector('#matchMessage');

        function updateStrength() {
            if (!password || !fill || !label) return;
            const value = password.value;
            let score = 0;
            if (value.length >= 8) score += 25;
            if (value.length >= 12) score += 20;
            if (/[A-Z]/.test(value)) score += 15;
            if (/[0-9]/.test(value)) score += 15;
            if (/[^A-Za-z0-9]/.test(value)) score += 25;

            const bounded = Math.min(score, 100);
            fill.style.width = bounded + '%';
            let text = value ? 'Faible' : 'Force du mot de passe';
            let color = value ? '#ef4444' : '#94a3b8';
            if (score > 70) { text = 'Excellent'; color = '#16a34a'; }
            else if (score > 50) { text = 'Bon'; color = '#22c55e'; }
            else if (score > 30) { text = 'Moyen'; color = '#f59e0b'; }
            fill.style.backgroundColor = color;
            label.textContent = text;
            label.style.color = color;
        }

        function updateMatch() {
            if (!password || !confirmation || !match) return;
            const hasValue = confirmation.value.length > 0;
            const matches = password.value === confirmation.value;
            confirmation.setCustomValidity(hasValue && !matches
                ? 'Les mots de passe ne correspondent pas.' : '');
            if (!hasValue) {
                match.textContent = '';
                match.className = 'match-message';
            } else if (matches) {
                match.textContent = '✓ Les mots de passe correspondent';
                match.className = 'match-message match';
            } else {
                match.textContent = '✗ Les mots de passe ne correspondent pas';
                match.className = 'match-message no-match';
            }
        }

        if (password) {
            password.addEventListener('input', function () {
                updateStrength();
                updateMatch();
            });
        }
        if (confirmation) confirmation.addEventListener('input', updateMatch);

        form.querySelectorAll('.register-password-toggle').forEach(function (button) {
            button.addEventListener('click', function () {
                const input = form.querySelector('#' + button.dataset.target);
                if (!input) return;
                const show = input.type === 'password';
                input.type = show ? 'text' : 'password';
                button.textContent = show ? '🙈' : '👁️';
                button.setAttribute('aria-label', show
                    ? 'Masquer le mot de passe' : 'Afficher le mot de passe');
                input.focus();
            });
        });

        // En page complète, afficher l'état de chargement. Dans la popup, le
        // gestionnaire AJAX générique de base.html prend lui-même ce rôle.
        form.addEventListener('submit', function (event) {
            updateMatch();
            if (!form.checkValidity()) {
                event.preventDefault();
                form.reportValidity();
                return;
            }
            if (form.closest('#formModalBody')) return;
            const button = form.querySelector('#submitBtn');
            const text = form.querySelector('#btnText');
            const spinner = form.querySelector('#btnSpinner');
            if (button) button.disabled = true;
            if (text) text.textContent = 'Création…';
            if (spinner) spinner.hidden = false;
        });

        updateStrength();
        updateMatch();
    }

    window.initRegisterForm = initRegisterForm;
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            initRegisterForm(document);
        });
    } else {
        initRegisterForm(document);
    }
})();
