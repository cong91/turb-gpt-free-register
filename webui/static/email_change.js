(() => {
  const form = document.getElementById('emailChangeForm');
  if (!form) return;

  const modeButtons = [...document.querySelectorAll('[data-personal-mode]')];
  const panels = [...document.querySelectorAll('[data-personal-panel]')];
  const emailSubmit = document.getElementById('submit');
  const twofaSubmit = document.getElementById('twofaSubmit');
  const exportButton = document.getElementById('exportChangedAccounts');
  const resultPanel = document.getElementById('personalInfoResults');
  const resultBody = document.getElementById('personalResultBody');
  const resultEmpty = document.getElementById('personalResultEmpty');
  const status = document.getElementById('status');
  const submitted = document.getElementById('personalSubmitted');
  const succeeded = document.getElementById('personalSucceeded');
  const failed = document.getElementById('personalFailed');
  let activeMode = 'email';
  let exportBatchId = '';
  let exportableCount = 0;

  const escapeHtml = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');

  const setMode = (mode) => {
    activeMode = mode === 'twofa' ? 'twofa' : 'email';
    modeButtons.forEach((button) => {
      const active = button.dataset.personalMode === activeMode;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', String(active));
    });
    panels.forEach((panel) => {
      const active = panel.dataset.personalPanel === activeMode;
      panel.classList.toggle('is-hidden', !active);
      panel.setAttribute('aria-hidden', String(!active));
    });
  };

  const setStatus = (message, isError = false) => {
    status.className = `personal-info-status${isError ? ' is-error' : ''}`;
    status.textContent = message;
  };

  const resultStatus = (result) => {
    if (result && result.change_status === 'success') {
      return { label: 'Thành công', className: 'is-success' };
    }
    if (result && result.change_status === 'partial_failure') {
      return { label: 'Một phần', className: 'is-partial' };
    }
    return { label: 'Lỗi', className: 'is-failed' };
  };

  const renderResults = (payload, mode) => {
    const resultList = Array.isArray(payload.results) ? payload.results : [];
    const successfulResults = resultList.filter((result) => result.change_status === 'success');
    exportBatchId = String(payload.change_batch_id || '');
    exportableCount = Number.isInteger(Number(payload.exportable_count))
      ? Math.max(0, Number(payload.exportable_count))
      : successfulResults.length;

    submitted.textContent = String(payload.submitted ?? resultList.length);
    succeeded.textContent = String(payload.succeeded ?? successfulResults.length);
    failed.textContent = String(payload.failed ?? Math.max(0, resultList.length - successfulResults.length));
    resultBody.innerHTML = resultList.map((result) => {
      const state = resultStatus(result);
      const account = mode === 'email'
        ? `${result.old_email || result.email || '-'} -> ${result.new_email || '-'}`
        : (result.email || '-');
      const detail = result.error || result.warning || (state.className === 'is-success' ? 'Đã cập nhật dữ liệu tài khoản.' : 'Không hoàn tất.');
      const action = mode === 'email' ? 'Đổi email' : 'Đổi 2FA';
      return `<tr><td>${escapeHtml(account)}</td><td>${action}</td><td><span class="personal-info-result-status ${state.className}">${state.label}</span></td><td>${escapeHtml(detail)}</td></tr>`;
    }).join('');
    resultEmpty.hidden = resultList.length > 0;
    resultPanel.hidden = false;
    exportButton.disabled = !exportBatchId || exportableCount === 0;
  };

  const requestChange = async (mode) => {
    const isTwofa = mode === 'twofa';
    const activeSubmit = isTwofa ? twofaSubmit : emailSubmit;
    const credentials = document.getElementById(isTwofa ? 'twofaCredentials' : 'credentials').value.trim();
    if (!credentials) {
      setStatus(isTwofa ? 'Hãy nhập danh sách tài khoản cần đổi 2FA.' : 'Hãy nhập danh sách tài khoản hiện tại.', true);
      return;
    }

    activeSubmit.disabled = true;
    exportBatchId = '';
    exportableCount = 0;
    exportButton.disabled = true;
    resultPanel.hidden = true;
    setStatus(isTwofa ? 'Đang đổi 2FA theo từng tài khoản...' : 'Đang xử lý đổi email...');
    try {
      const body = isTwofa
        ? { credentials, workers: Number(document.getElementById('twofaWorkers').value || 1) }
        : {
          credentials,
          gmail_api: document.getElementById('gmailApi').value,
          quota: Number(document.getElementById('quota').value || 1),
          workers: Number(document.getElementById('workers').value || 1),
        };
      const response = await fetch(isTwofa ? '/api/accounts/change-twofa' : '/api/accounts/change-email', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(body),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || 'Yêu cầu không hoàn tất');
      renderResults(payload, mode);
      const exportMessage = exportableCount
        ? ` Có thể xuất ${exportableCount} tài khoản đã cập nhật.`
        : '';
      setStatus(`Hoàn tất ${payload.succeeded}/${payload.submitted} tài khoản.${payload.failed ? ' Có tài khoản lỗi, xem chi tiết bên dưới.' : ''}${exportMessage}`, payload.failed > 0);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Không thể gửi yêu cầu', true);
    } finally {
      activeSubmit.disabled = false;
    }
  };

  modeButtons.forEach((button) => {
    button.addEventListener('click', () => setMode(button.dataset.personalMode));
  });

  exportButton.addEventListener('click', async () => {
    if (!exportBatchId || !exportableCount) return;
    exportButton.disabled = true;
    try {
      const response = await fetch('/api/accounts/personal-info/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ batch_id: exportBatchId }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || 'Không thể export tài khoản');
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'personal-info-updated-accounts.txt';
      document.body.appendChild(link);
      link.click();
      setTimeout(() => {
        link.remove();
        URL.revokeObjectURL(url);
      }, 800);
      setStatus(`Đã export ${exportableCount} tài khoản.`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Không thể export tài khoản', true);
    } finally {
      exportButton.disabled = !exportBatchId || exportableCount === 0;
    }
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    requestChange('email');
  });
  twofaSubmit.addEventListener('click', () => requestChange('twofa'));
  setMode(activeMode);
})();
