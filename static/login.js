(() => {
    'use strict';

    const form = document.getElementById('loginForm');
    const username = document.getElementById('username');
    const password = document.getElementById('password');
    const toggle = document.getElementById('passwordToggle');
    const capsMessage = document.getElementById('capsLockMessage');
    const submit = document.getElementById('loginSubmit');

    if (!form || !username || !password || !toggle || !submit) return;

    function setPasswordVisible(visible) {
        password.type = visible ? 'text' : 'password';
        toggle.classList.toggle('is-visible', visible);
        toggle.setAttribute('aria-pressed', String(visible));
        toggle.setAttribute(
            'aria-label', visible ? 'Masquer le mot de passe' : 'Afficher le mot de passe'
        );
    }

    toggle.addEventListener('click', () => {
        setPasswordVisible(password.type === 'password');
        password.focus({preventScroll: true});
    });

    function updateCapsLock(event) {
        if (!capsMessage || typeof event.getModifierState !== 'function') return;
        capsMessage.hidden = !event.getModifierState('CapsLock');
    }

    password.addEventListener('keydown', updateCapsLock);
    password.addEventListener('keyup', updateCapsLock);
    password.addEventListener('blur', () => {
        if (capsMessage) capsMessage.hidden = true;
    });

    form.addEventListener('submit', event => {
        if (!form.checkValidity()) {
            event.preventDefault();
            form.reportValidity();
            return;
        }
        submit.disabled = true;
        submit.classList.add('is-loading');
        submit.setAttribute('aria-busy', 'true');
    });

    // Le navigateur peut restaurer une page depuis son cache après un retour :
    // le bouton doit alors redevenir utilisable.
    window.addEventListener('pageshow', () => {
        submit.disabled = false;
        submit.classList.remove('is-loading');
        submit.removeAttribute('aria-busy');
        setPasswordVisible(false);
    });

    if (username.value && username.getAttribute('aria-invalid') === 'true') {
        password.focus({preventScroll: true});
    }
})();
