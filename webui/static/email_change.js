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
  const running = document.getElementById('personalRunning');
  const pending = document.getElementById('personalPending');
  const succeeded = document.getElementById('personalSucceeded');
  const failed = document.getElementById('personalFailed');
  let activeMode = 'email';
  let twofaProgressBatchId = '';
  let exportBatchId = '';
  let exportableCount = 0;
  let progressTimer = null;

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
    exportButton.textContent = activeMode === 'twofa'
      ? 'Xuất ACCOUNT | PASS | 2FA'
      : 'Xuất tài khoản đã đổi';
  };

  const setStatus = (message, isError = false) => {
    status.className = `personal-info-status${isError ? ' is-error' : ''}`;
    status.textContent = message;
  };

  const resultStatus = (result) => {
    const value = result && (result.status || result.change_status);
    if (value === 'success') {
      return { label: 'Thành công', className: 'is-success' };
    }
    if (value === 'partial_failure') {
      return { label: 'Một phần', className: 'is-partial' };
    }
    if (value === 'queued') {
      return { label: 'Đang chờ', className: 'is-pending' };
    }
    if (value === 'running') {
      return { label: 'Đang xử lý', className: 'is-running' };
    }
    return { label: 'Lỗi', className: 'is-failed' };
  };

  const renderResults = (payload, mode) => {
    const resultList = Array.isArray(payload.results) ? payload.results : [];
    const successfulResults = resultList.filter((result) => (result.status || result.change_status) === 'success');
    const batchDone = mode !== 'twofa' || ['completed', 'failed'].includes(payload.status);
    exportBatchId = batchDone ? String(payload.change_batch_id || '') : '';
    exportableCount = batchDone
      ? (Number.isInteger(Number(payload.exportable_count))
        ? Math.max(0, Number(payload.exportable_count))
        : successfulResults.length)
      : 0;

    submitted.textContent = String(payload.submitted ?? resultList.length);
    if (running) running.textContent = String(payload.running ?? resultList.filter((result) => result.status === 'running').length);
    if (pending) pending.textContent = String(payload.pending ?? resultList.filter((result) => result.status === 'queued').length);
    succeeded.textContent = String(payload.succeeded ?? successfulResults.length);
    failed.textContent = String(payload.failed ?? resultList.filter((result) => ['failed', 'partial_failure'].includes(result.status || result.change_status)).length);
    resultBody.innerHTML = resultList.map((result, index) => {
      const state = resultStatus(result);
      const account = mode === 'email'
        ? `${result.old_email || result.email || '-'} -> ${result.new_email || '-'}`
        : (result.email || '-');
      const planQueued = mode === 'twofa' && result.plan_check && result.plan_check.accepted;
      const detail = result.detail || result.error || result.warning || (state.className === 'is-success'
        ? (planQueued ? 'Đã cập nhật dữ liệu tài khoản. Đang kiểm tra loại gói.' : 'Đã cập nhật dữ liệu tài khoản.')
        : state.className === 'is-pending' ? 'Đang chờ luồng xử lý.'
          : state.className === 'is-running' ? 'Đang xử lý.'
            : 'Không hoàn tất.');
      const action = mode === 'email' ? 'Đổi email' : 'Đổi 2FA';
      const retryAction = batchDone && mode === 'twofa' && result.retryable !== false
        && ['is-failed', 'is-partial'].includes(state.className)
        ? `<button type="button" class="personal-info-retry" data-twofa-retry-index="${index}">Thử lại</button>`
        : '<span class="personal-info-no-action">-</span>';
      return `<tr><td>${escapeHtml(account)}</td><td>${action}</td><td><span class="personal-info-result-status ${state.className}">${state.label}</span></td><td>${escapeHtml(detail)}</td><td>${retryAction}</td></tr>`;
    }).join('');
    resultEmpty.hidden = resultList.length > 0;
    resultPanel.hidden = false;
    exportButton.disabled = !exportBatchId || exportableCount === 0;
  };

  const showRequestError = (message) => {
    resultPanel.hidden = false;
    resultBody.innerHTML = '';
    resultEmpty.hidden = false;
    submitted.textContent = '0';
    if (running) running.textContent = '0';
    if (pending) pending.textContent = '0';
    succeeded.textContent = '0';
    failed.textContent = '0';
    exportBatchId = '';
    twofaProgressBatchId = '';
    exportableCount = 0;
    exportButton.disabled = true;
    setStatus(message, true);
  };

  const stopProgressPolling = () => {
    if (progressTimer) {
      clearTimeout(progressTimer);
      progressTimer = null;
    }
  };

  const pollTwofaProgress = async (batchId) => {
    twofaProgressBatchId = String(batchId || '');
    const poll = async () => {
      try {
        const response = await fetch(`/api/accounts/change-twofa-status?batch_id=${encodeURIComponent(batchId)}`, {
          credentials: 'same-origin',
          cache: 'no-store',
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          const error = new Error(payload.error || 'Không thể đọc tiến độ đổi 2FA');
          error.status = response.status;
          throw error;
        }
        if (twofaProgressBatchId !== String(batchId)) return;
        renderResults(payload, 'twofa');
        const done = payload.status === 'completed' || payload.status === 'failed';
        if (!done) {
          progressTimer = setTimeout(poll, 1000);
          return;
        }
        stopProgressPolling();
        if (payload.batch_error) {
          setStatus(`Batch đổi 2FA gặp lỗi: ${payload.batch_error}`, true);
          return;
        }
        const exportMessage = exportableCount ? ` Có thể xuất ${exportableCount} tài khoản đã cập nhật.` : '';
        setStatus(`Hoàn tất ${payload.succeeded || 0}/${payload.submitted || 0} tài khoản.${payload.failed ? ' Có tài khoản lỗi, xem chi tiết bên dưới.' : ''}${exportMessage}`, Boolean(payload.failed));
      } catch (error) {
        if (twofaProgressBatchId !== String(batchId)) return;
        const message = error instanceof Error ? error.message : 'Không thể đọc tiến độ đổi 2FA';
        const statusCode = Number(error && error.status);
        if (statusCode >= 400 && statusCode < 500) {
          stopProgressPolling();
          setStatus(message, true);
          return;
        }
        setStatus(`${message}. Sẽ tự động thử đọc lại...`, true);
        progressTimer = setTimeout(poll, 1500);
      }
    };
    await poll();
  };

  const retryTwofaRow = async (batchId, index, button) => {
    button.disabled = true;
    setStatus('Đang đăng nhập lại và thử lại đổi 2FA...');
    try {
      const credentials = document.getElementById('twofaCredentials').value.trim();
      if (!credentials) throw new Error('Hãy nhập lại danh sách tài khoản cần đổi 2FA để thử lại');
      const response = await fetch('/api/accounts/change-twofa-retry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ batch_id: batchId, index, credentials }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || 'Không thể thử lại đổi 2FA');
      renderResults(payload, 'twofa');
      await pollTwofaProgress(batchId);
    } catch (error) {
      button.disabled = false;
      setStatus(error instanceof Error ? error.message : 'Không thể thử lại đổi 2FA', true);
    }
  };

  const requestChange = async (mode) => {
    const isTwofa = mode === 'twofa';
    const activeSubmit = isTwofa ? twofaSubmit : emailSubmit;
    const credentials = document.getElementById(isTwofa ? 'twofaCredentials' : 'credentials').value.trim();
    if (!credentials) {
      showRequestError(isTwofa ? 'Hãy nhập danh sách tài khoản cần đổi 2FA.' : 'Hãy nhập danh sách tài khoản hiện tại.');
      return;
    }

    activeSubmit.disabled = true;
    stopProgressPolling();
    exportBatchId = '';
    twofaProgressBatchId = '';
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
      if (!response.ok) {
        if (isTwofa && payload.batch_id) {
          twofaProgressBatchId = String(payload.batch_id);
          await pollTwofaProgress(payload.batch_id);
          setStatus(payload.error || 'Batch đổi 2FA không khởi động được', true);
          return;
        }
        throw new Error(payload.error || 'Yêu cầu không hoàn tất');
      }
      renderResults(payload, mode);
      if (isTwofa) {
        twofaProgressBatchId = String(payload.batch_id || '');
        setStatus(`Đã nhận ${payload.submitted || 0} tài khoản. Đang theo dõi từng luồng xử lý...`);
        await pollTwofaProgress(payload.batch_id);
      } else {
        const exportMessage = exportableCount
          ? ` Có thể xuất ${exportableCount} tài khoản đã cập nhật.`
          : '';
        setStatus(`Hoàn tất ${payload.succeeded}/${payload.submitted} tài khoản.${payload.failed ? ' Có tài khoản lỗi, xem chi tiết bên dưới.' : ''}${exportMessage}`, payload.failed > 0);
      }
    } catch (error) {
      showRequestError(error instanceof Error ? error.message : 'Không thể gửi yêu cầu');
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
        throw new Error(payload.error || 'Không thể xuất tài khoản');
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
      setStatus(`Đã xuất ${exportableCount} tài khoản.`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Không thể xuất tài khoản', true);
    } finally {
      exportButton.disabled = !exportBatchId || exportableCount === 0;
    }
  });

  resultBody.addEventListener('click', (event) => {
    const button = event.target.closest('[data-twofa-retry-index]');
    if (!button || activeMode !== 'twofa' || !twofaProgressBatchId) return;
    const index = Number(button.dataset.twofaRetryIndex);
    if (!Number.isInteger(index) || index < 0) return;
    retryTwofaRow(twofaProgressBatchId, index, button);
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    requestChange('email');
  });
  twofaSubmit.addEventListener('click', () => requestChange('twofa'));
  setMode(activeMode);
})();
