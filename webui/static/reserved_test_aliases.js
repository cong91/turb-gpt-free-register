(function () {
  'use strict';

  const form = document.getElementById('reservedTestAliasForm');
  if (!form) return;

  const baseInput = document.getElementById('reservedTestAliasBase');
  const limitInput = document.getElementById('reservedTestAliasLimit');
  const domain1Input = document.getElementById('reservedTestAliasDomain1');
  const domain2Input = document.getElementById('reservedTestAliasDomain2');
  const generateButton = document.getElementById('btnGenerateReservedTestAliases');
  const copyAllButton = document.getElementById('btnCopyReservedTestAliases');
  const status = document.getElementById('reservedTestAliasStatus');
  const results = document.getElementById('reservedTestAliasResults');
  let aliases = [];

  function clearResults() {
    aliases = [];
    results.replaceChildren();
    results.classList.add('hidden');
    copyAllButton.disabled = true;
    status.textContent = '';
    status.classList.remove('is-error');
  }

  function setStatus(message, isError) {
    status.textContent = message;
    status.classList.toggle('is-error', Boolean(isError));
  }

  function renderAliases(items) {
    const fragment = document.createDocumentFragment();
    items.forEach((alias) => {
      const row = document.createElement('div');
      row.className = 'reserved-test-alias-row';

      const value = document.createElement('code');
      value.className = 'reserved-test-alias-value mono';
      value.textContent = alias;

      const copyButton = document.createElement('button');
      copyButton.type = 'button';
      copyButton.className = 'reserved-test-alias-copy';
      copyButton.title = '复制此别名';
      copyButton.setAttribute('aria-label', `复制 ${alias}`);
      copyButton.dataset.alias = alias;
      copyButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';

      row.append(value, copyButton);
      fragment.append(row);
    });
    results.replaceChildren(fragment);
    results.classList.remove('hidden');
  }

  form.addEventListener('input', clearResults);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    clearResults();
    generateButton.disabled = true;
    setStatus('正在生成…', false);

    const domains = [domain1Input.value, domain2Input.value]
      .map((value) => value.trim())
      .filter(Boolean);
    try {
      const response = await api('/api/tools/reserved-test-aliases/preview', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          base: baseInput.value,
          domains,
          limit: limitInput.value,
        }),
      });
      aliases = Array.isArray(response.aliases) ? response.aliases : [];
      renderAliases(aliases);
      copyAllButton.disabled = aliases.length === 0;
      setStatus(`已生成 ${aliases.length} 个测试别名`, false);
    } catch (error) {
      setStatus(error.message || 'Tạo thất bại', true);
    } finally {
      generateButton.disabled = false;
    }
  });

  results.addEventListener('click', (event) => {
    const button = event.target.closest('[data-alias]');
    if (!button) return;
    copyText(button.dataset.alias || '');
  });

  copyAllButton.addEventListener('click', () => {
    copyText(aliases.join('\n'));
  });
})();
