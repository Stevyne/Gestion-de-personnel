(function () {
  'use strict';

  function normaliser(value) {
    return (value || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  }

  function brancherRecherche(input, elements, empty) {
    if (!input) return;
    input.addEventListener('input', function () {
      const terme = normaliser(input.value.trim());
      let visibles = 0;
      elements.forEach(function (element) {
        const texte = normaliser(element.dataset.conversationSearch || element.dataset.recipientSearch);
        const visible = !terme || texte.includes(terme);
        element.hidden = !visible;
        if (visible) visibles += 1;
      });
      if (empty) empty.hidden = visibles !== 0;
    });
  }

  const conversations = Array.from(document.querySelectorAll('.msg-conversation'));
  brancherRecherche(
    document.getElementById('conversationSearch'), conversations,
    document.getElementById('conversationNoResult')
  );
  const activeConversation = document.querySelector('.msg-conversation.is-active');
  if (activeConversation) activeConversation.scrollIntoView({ block: 'nearest' });

  const recipientElements = Array.from(document.querySelectorAll('.msg-recipient'));
  brancherRecherche(document.getElementById('recipientSearch'), recipientElements, null);
  const recipientCount = document.getElementById('recipientCount');
  function mettreAJourDestinataires() {
    let count = 0;
    recipientElements.forEach(function (element) {
      const input = element.querySelector('input[type="checkbox"]');
      const selected = Boolean(input && input.checked);
      element.classList.toggle('is-selected', selected);
      if (selected) count += 1;
    });
    if (recipientCount) recipientCount.textContent = count + ' sélectionné' + (count > 1 ? 's' : '');
  }
  recipientElements.forEach(function (element) {
    const input = element.querySelector('input[type="checkbox"]');
    if (input) input.addEventListener('change', mettreAJourDestinataires);
  });
  mettreAJourDestinataires();

  const composeForm = document.getElementById('formNouveauMessage');
  const recipientField = document.getElementById('champDestinataires');
  const targetField = document.getElementById('champCible');
  const titleField = document.getElementById('champTitre');
  function mettreAJourType() {
    if (!composeForm) return;
    const selected = composeForm.querySelector('input[name="type"]:checked');
    const type = selected ? selected.value : 'prive';
    if (recipientField) recipientField.hidden = type === 'annonce';
    if (targetField) targetField.hidden = type !== 'annonce';
    if (titleField) titleField.hidden = type === 'prive';
  }
  if (composeForm) {
    composeForm.querySelectorAll('input[name="type"]').forEach(function (input) {
      input.addEventListener('change', mettreAJourType);
    });
    mettreAJourType();
  }

  function brancherNomFichier(input, output) {
    if (!input || !output) return;
    input.addEventListener('change', function () {
      const file = input.files && input.files[0];
      output.textContent = file ? '📎 ' + file.name : '';
      output.hidden = !file;
    });
  }
  brancherNomFichier(document.getElementById('messageFileInput'), document.getElementById('messageFileName'));
  brancherNomFichier(document.getElementById('newMessageFile'), document.getElementById('newMessageFileName'));

  const messageStream = document.getElementById('messageStream');
  if (messageStream) {
    requestAnimationFrame(function () {
      const anchor = new URLSearchParams(window.location.search).get('anchor');
      const target = anchor && messageStream.querySelector('[data-message-id="' + anchor + '"]');
      if (target) target.scrollIntoView({ block: 'start' });
      else messageStream.scrollTop = messageStream.scrollHeight;
    });
  }

  const composer = document.getElementById('messageComposer');
  const composerText = document.getElementById('messageComposerText');
  const composerFile = document.getElementById('messageFileInput');
  function ajusterTextarea() {
    if (!composerText) return;
    composerText.style.height = 'auto';
    composerText.style.height = Math.min(composerText.scrollHeight, 120) + 'px';
  }
  if (composerText) {
    composerText.addEventListener('input', ajusterTextarea);
    composerText.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        if (composer) composer.requestSubmit();
      }
    });
    ajusterTextarea();
  }
  if (composer) {
    composer.addEventListener('submit', function (event) {
      const hasText = composerText && composerText.value.trim();
      const hasFile = composerFile && composerFile.files && composerFile.files.length;
      if (!hasText && !hasFile) {
        event.preventDefault();
        if (composerText) composerText.focus();
        return;
      }
      const button = composer.querySelector('.msg-send-btn');
      if (button) button.disabled = true;
    });
  }

  if (composeForm) {
    composeForm.addEventListener('submit', function (event) {
      const type = (composeForm.querySelector('input[name="type"]:checked') || {}).value || 'prive';
      const selectedRecipients = composeForm.querySelectorAll('input[name="destinataires"]:checked').length;
      const text = (document.getElementById('newMessageText') || {}).value || '';
      const fileInput = document.getElementById('newMessageFile');
      const hasFile = fileInput && fileInput.files && fileInput.files.length;
      if (type !== 'annonce' && selectedRecipients === 0) {
        event.preventDefault();
        if (recipientField) recipientField.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
      }
      if (!text.trim() && !hasFile) {
        event.preventDefault();
        const textarea = document.getElementById('newMessageText');
        if (textarea) textarea.focus();
        return;
      }
      const button = composeForm.querySelector('button[type="submit"]');
      if (button) { button.disabled = true; button.textContent = 'Envoi…'; }
    });
  }
})();
