(function () {
  'use strict';

  const tab = document.getElementById('tab-roxy-profiles');
  if (!tab) return;
  const $ = (selector) => tab.querySelector(selector);
  const state = {profiles: [], loading: false, page: 1, hasNext: false, search: '', profileState: '', selected: new Set()};
  const labels = {
    LOCAL_ONLY: 'Chưa đồng bộ', REMOTE_CREATING: 'Đang tạo', ACTIVE_STOPPED: 'Đã dừng', RUNNING: 'Đang chạy',
    SNAPSHOTTING: 'Đang tạo bản chụp', ARCHIVE_COMMITTED: 'Đã lưu trữ', SOFT_DELETE_PENDING: 'Đang lưu trữ',
    TRASHED: 'Trong thùng rác', RESTORE_REQUIRED: 'Cần khôi phục', OFFLINE_STAGING: 'Đang chuẩn bị cục bộ',
    OFFLINE_RUNNING: 'Cục bộ đang chạy', OFFLINE_STOPPED: 'Cục bộ đã dừng', OFFLINE_UNVERIFIED: 'Cục bộ cần kiểm tra',
    NEEDS_RECONCILIATION: 'Cần đồng bộ lại',
  };

  function esc(value) {
    return String(value == null ? '' : value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function setStatus(message, error = false) {
    const element = $('#roxyProfileStatus');
    if (!element) return;
    element.textContent = message || '';
    element.classList.toggle('is-error', Boolean(error));
  }
  async function call(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }
  function statusPill(value) {
    const status = String(value || '');
    return `<span class="roxy-profile-pill roxy-profile-pill--${esc(status.toLowerCase())}">${esc(labels[status] || status || '-')}</span>`;
  }
  function archiveCell(profile) {
    if (!profile.archive) return '<span class="muted">Chưa có</span>';
    const capabilities = profile.archive.capabilities || {};
    const full = Boolean(capabilities.detached_roxy_offline_open);
    return `<span class="roxy-profile-archive-state">${full ? 'Bản đầy đủ v2' : 'Siêu dữ liệu v1'}<br><small>${full ? 'Chỉ trạng thái trình duyệt' : 'Cần khôi phục Roxy trước khi mở'}</small></span>`;
  }
  function runtimeCell(profile) {
    const archive = profile.archive || {};
    const launch = profile.launch || {};
    const core = archive.source_core_version || '-';
    const backend = launch.backend || (['RUNNING', 'ACTIVE_STOPPED'].includes(profile.state) ? 'Roxy remote' : '-');
    const backendLabel = backend === 'Roxy remote' ? 'Roxy từ xa' : backend === '-' ? 'Chưa có' : backend;
    const cdp = launch.backend ? 'CDP đang hoạt động' : 'CDP không hoạt động';
    const parity = launch.fingerprint_status || 'unknown';
    const parityLabel = parity === 'unknown' ? 'chưa xác định' : parity;
    return `<span class="roxy-profile-runtime"><strong>${esc(backendLabel)}</strong><br><small>Core ${esc(core)} · ${esc(cdp)} · độ tương đồng ${esc(parityLabel)}</small></span>`;
  }
  function actions(profile) {
    const id = encodeURIComponent(profile.local_id);
    const stateName = profile.state;
    const canRemoteOpen = stateName === 'ACTIVE_STOPPED';
    const canRemoteClose = stateName === 'RUNNING';
    const canFullExport = stateName === 'ACTIVE_STOPPED' || stateName === 'ARCHIVE_COMMITTED';
    const canArchive = stateName === 'ACTIVE_STOPPED' || (
      stateName === 'ARCHIVE_COMMITTED'
      && profile.archive?.format_version === 'roxy-profile-folder-v2'
    );
    const canLocalOpen = ['TRASHED', 'RESTORE_REQUIRED', 'OFFLINE_STOPPED', 'OFFLINE_UNVERIFIED'].includes(stateName) && profile.capabilities?.offline_open;
    const canLocalClose = stateName === 'OFFLINE_RUNNING' || (
      stateName === 'OFFLINE_UNVERIFIED'
      && (Boolean(profile.launch) || profile.capabilities?.offline_recovery_staging)
    );
    return `<div class="roxy-profile-actions">
      <button type="button" class="roxy-profile-action" data-action="edit" data-id="${id}" title="Sửa profile Roxy" ${['ACTIVE_STOPPED', 'RUNNING'].includes(stateName) ? '' : 'disabled'}>✎</button>
      <button type="button" class="roxy-profile-action" data-action="remote-open" data-id="${id}" title="Mở Roxy chuẩn" ${canRemoteOpen ? '' : 'disabled'}>▶</button>
      <button type="button" class="roxy-profile-action" data-action="remote-close" data-id="${id}" title="Đóng Roxy chuẩn" ${canRemoteClose ? '' : 'disabled'}>■</button>
      <button type="button" class="roxy-profile-action" data-action="export-full" data-id="${id}" title="Xuất full-folder v2" ${canFullExport ? '' : 'disabled'}>⇩</button>
      <button type="button" class="roxy-profile-action" data-action="local-open" data-id="${id}" title="Mở bản cục bộ thử nghiệm: chỉ trạng thái trình duyệt" ${canLocalOpen ? '' : 'disabled'}>◫</button>
      <button type="button" class="roxy-profile-action" data-action="local-close" data-id="${id}" title="Đóng chế độ cục bộ và lưu điểm kiểm tra" ${canLocalClose ? '' : 'disabled'}>□</button>
      <button type="button" class="roxy-profile-action" data-action="download" data-id="${id}" title="Tải bản lưu trữ đã mã hóa" ${profile.archive ? '' : 'disabled'}>↓</button>
      <button type="button" class="roxy-profile-action roxy-profile-action--danger" data-action="archive" data-id="${id}" title="Xuất bản đầy đủ rồi chuyển profile vào thùng rác" ${canArchive ? '' : 'disabled'}>⌫</button>
    </div>`;
  }
  function updateBulkButtons() {
    const selected = state.profiles.filter((profile) => state.selected.has(profile.local_id));
    const allIn = (states) => selected.length > 0 && selected.every((profile) => states.includes(profile.state));
    const eligibility = {
      btnBulkOpenRoxyProfiles: allIn(['ACTIVE_STOPPED']),
      btnBulkMetadataRoxyProfiles: allIn(['ACTIVE_STOPPED']),
      btnBulkExportRoxyProfiles: allIn(['ACTIVE_STOPPED', 'ARCHIVE_COMMITTED']),
      btnBulkArchiveRoxyProfiles: selected.length > 0 && selected.every((profile) => (
        profile.state === 'ACTIVE_STOPPED'
        || (
          profile.state === 'ARCHIVE_COMMITTED'
          && profile.archive?.format_version === 'roxy-profile-folder-v2'
        )
      )),
      btnBulkCloseRoxyProfiles: allIn(['RUNNING']),
    };
    Object.entries(eligibility).forEach(([id, enabled]) => {
      const button = document.getElementById(id);
      if (button) button.disabled = !enabled;
    });
  }
  function render() {
    const body = $('#roxyProfilesBody');
    const empty = $('#roxyProfilesEmpty');
    if (!body || !empty) return;
    empty.classList.toggle('hidden', state.profiles.length > 0);
    body.innerHTML = state.profiles.map((profile) => `<tr><td><input class="roxy-profile-select" type="checkbox" data-id="${esc(profile.local_id)}" aria-label="Chọn ${esc(profile.name)}" ${state.selected.has(profile.local_id) ? 'checked' : ''}></td><td><strong>${esc(profile.name)}</strong><small class="roxy-profile-id">${esc(profile.dir_id || 'chỉ cục bộ')}</small></td><td>${statusPill(profile.state)}</td><td>${statusPill(profile.remote_state)}</td><td>${archiveCell(profile)}</td><td>${runtimeCell(profile)}</td><td class="muted">${esc(profile.updated_at || '-')}</td><td>${actions(profile)}</td></tr>`).join('');
    const pageLabel = $('#roxyProfilesPage');
    if (pageLabel) pageLabel.textContent = String(state.page);
    const previous = $('#btnRoxyProfilesPrev');
    const next = $('#btnRoxyProfilesNext');
    if (previous) previous.disabled = state.page <= 1;
    if (next) next.disabled = !state.hasNext;
    updateBulkButtons();
  }
  function renderStatus(status) {
    const summary = $('#roxyProfileSummary');
    const capabilities = $('#roxyProfileCapabilities');
    if (summary) summary.textContent = `${status.managed_count || 0} profile được quản lý · ${status.active_remote_count || 0} profile Roxy đang hoạt động`;
    if (capabilities) {
      capabilities.textContent = status.offline_open_supported ? 'Thử nghiệm cục bộ đã bật' : 'Thử nghiệm cục bộ đang tắt';
      capabilities.classList.toggle('is-warning', !status.offline_open_supported);
    }
  }
  async function refresh(reconcile = false) {
    if (state.loading) return;
    state.loading = true;
    setStatus(reconcile ? 'Đang đồng bộ với Roxy…' : 'Đang tải profile…');
    try {
      const params = new URLSearchParams({page: String(state.page), page_size: '50'});
      if (reconcile) params.set('reconcile', '1');
      if (state.search) params.set('search', state.search);
      if (state.profileState) params.set('state', state.profileState);
      const data = await call(`/api/roxy/profiles?${params}`);
      state.profiles = Array.isArray(data.profiles) ? data.profiles : [];
      state.selected.clear();
      state.hasNext = Boolean(data.has_next);
      renderStatus(data.status || {}); render();
      setStatus(`Đã tải ${state.profiles.length} profile`);
    } catch (error) { setStatus(error.message || 'Không thể tải profile', true); }
    finally { state.loading = false; }
  }
  function openModal() { $('#roxyProfileCreateModal')?.classList.remove('hidden'); $('#roxyProfileName')?.focus(); }
  function closeModal() { $('#roxyProfileCreateModal')?.classList.add('hidden'); $('#roxyProfileCreateForm')?.reset(); }
  async function openEditModal(profile) {
    let detail = profile;
    try {
      const response = await call(`/api/roxy/profiles/${encodeURIComponent(profile.local_id)}`);
      detail = response.profile || profile;
    } catch (error) {
      setStatus(error.message || 'Không thể tải cấu hình profile', true);
      return;
    }
    const config = detail.remote_config || {};
    $('#roxyProfileEditId').value = profile.local_id;
    $('#roxyProfileEditName').value = config.name || profile.name || '';
    $('#roxyProfileEditOs').value = config.os || 'Windows';
    $('#roxyProfileEditCoreVersion').value = config.coreVersion || '';
    $('#roxyProfileEditModal')?.classList.remove('hidden');
    $('#roxyProfileEditName')?.focus();
  }
  function closeEditModal() { $('#roxyProfileEditModal')?.classList.add('hidden'); $('#roxyProfileEditForm')?.reset(); }
  async function editProfile(event) {
    event.preventDefault();
    const localId = $('#roxyProfileEditId')?.value;
    const payload = {name: $('#roxyProfileEditName')?.value.trim(), os: $('#roxyProfileEditOs')?.value || 'Windows'};
    const coreVersion = $('#roxyProfileEditCoreVersion')?.value.trim();
    if (coreVersion) payload.coreVersion = coreVersion;
    try { await call(`/api/roxy/profiles/${encodeURIComponent(localId)}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)}); closeEditModal(); await refresh(false); }
    catch (error) { setStatus(error.message || 'Sửa profile thất bại', true); }
  }
  function openImportModal() { $('#roxyProfileImportModal')?.classList.remove('hidden'); $('#roxyProfileImportName')?.focus(); }
  function closeImportModal() { $('#roxyProfileImportModal')?.classList.add('hidden'); $('#roxyProfileImportForm')?.reset(); $('#roxyProfileImportFile').value = ''; }
  async function createProfile(event) {
    event.preventDefault();
    const button = $('#btnSubmitRoxyProfile'); const name = $('#roxyProfileName')?.value.trim(); const os = $('#roxyProfileOs')?.value || 'Windows';
    if (!name) return;
    const payload = {name, os}; const coreVersion = $('#roxyProfileCoreVersion')?.value.trim();
    if (coreVersion) payload.coreVersion = coreVersion;
    button.disabled = true;
    try { await call('/api/roxy/profiles', {method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': `ui-create-${Date.now()}`}, body: JSON.stringify(payload)}); closeModal(); await refresh(true); }
    catch (error) { setStatus(error.message || 'Tạo profile thất bại', true); }
    finally { button.disabled = false; }
  }
  async function importProfile(event) {
    event.preventDefault();
    const file = $('#roxyProfileImportFile')?.files?.[0]; const name = $('#roxyProfileImportName')?.value.trim();
    if (!file || !name) { setStatus('Chọn file .rpa2 và nhập tên profile', true); return; }
    const form = new FormData(); form.append('archive', file); form.append('name', name);
    try { await call('/api/roxy/profiles/import', {method: 'POST', body: form}); closeImportModal(); await refresh(false); }
    catch (error) { setStatus(error.message || 'Nhập thất bại', true); }
  }
  async function runBulk(action, confirmMessage = '') {
    if (!state.selected.size) return;
    if (confirmMessage && !window.confirm(confirmMessage)) return;
    try {
      const data = await call('/api/roxy/profiles/bulk', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action, local_ids: Array.from(state.selected)})});
      state.selected.clear();
      setStatus(`${data.result?.succeeded || 0}/${data.result?.requested || 0} profile thành công`);
      await refresh(false);
    } catch (error) { setStatus(error.message || 'Thao tác hàng loạt thất bại', true); }
  }
  function handleSelection(event) {
    const checkbox = event.target.closest('.roxy-profile-select');
    if (!checkbox) return;
    if (checkbox.checked) state.selected.add(checkbox.dataset.id); else state.selected.delete(checkbox.dataset.id);
    updateBulkButtons();
  }
  async function handleAction(event) {
    const button = event.target.closest('[data-action]'); if (!button || button.disabled) return;
    const action = button.dataset.action; const localId = decodeURIComponent(button.dataset.id || '');
    if (action === 'edit') {
      const profile = state.profiles.find((item) => item.local_id === localId);
      if (profile) openEditModal(profile);
      return;
    }
    if (action === 'download') { window.location.href = `/api/roxy/profiles/${encodeURIComponent(localId)}/archive/download`; return; }
    if (action === 'archive' && !window.confirm('Xuất bản đầy đủ rồi chuyển profile vào thùng rác Roxy?')) return;
    const endpoints = {'remote-open': 'open', 'remote-close': 'close', 'export-full': 'export-full', 'local-open': 'open-local', 'local-close': 'close-local', archive: 'archive'};
    const endpoint = endpoints[action]; if (!endpoint) return;
    button.disabled = true;
    try { await call(`/api/roxy/profiles/${encodeURIComponent(localId)}/${endpoint}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); await refresh(false); }
    catch (error) { setStatus(error.message || 'Thao tác profile thất bại', true); }
    finally { button.disabled = false; }
  }
  $('#btnCreateRoxyProfile')?.addEventListener('click', openModal); $('#btnCloseRoxyProfileModal')?.addEventListener('click', closeModal); $('#btnCancelRoxyProfileModal')?.addEventListener('click', closeModal); $('#roxyProfileCreateForm')?.addEventListener('submit', createProfile);
  $('#btnCloseRoxyProfileEditModal')?.addEventListener('click', closeEditModal); $('#btnCancelRoxyProfileEditModal')?.addEventListener('click', closeEditModal); $('#roxyProfileEditForm')?.addEventListener('submit', editProfile);
  $('#btnImportRoxyProfile')?.addEventListener('click', () => { openImportModal(); $('#roxyProfileImportFile')?.click(); }); $('#btnCloseRoxyProfileImportModal')?.addEventListener('click', closeImportModal); $('#btnCancelRoxyProfileImportModal')?.addEventListener('click', closeImportModal); $('#roxyProfileImportForm')?.addEventListener('submit', importProfile);
  $('#btnBulkOpenRoxyProfiles')?.addEventListener('click', () => runBulk('remote_open'));
  $('#btnBulkMetadataRoxyProfiles')?.addEventListener('click', () => runBulk('metadata_export'));
  $('#btnBulkExportRoxyProfiles')?.addEventListener('click', () => runBulk('full_export'));
  $('#btnBulkArchiveRoxyProfiles')?.addEventListener('click', () => runBulk('archive', 'Xuất bản đầy đủ rồi chuyển các profile đã chọn vào thùng rác?'));
  $('#btnBulkCloseRoxyProfiles')?.addEventListener('click', () => runBulk('remote_close'));
  $('#roxyProfilesSelectAll')?.addEventListener('change', (event) => { state.profiles.forEach((profile) => event.target.checked ? state.selected.add(profile.local_id) : state.selected.delete(profile.local_id)); render(); });
  $('#btnRefreshRoxyProfiles')?.addEventListener('click', () => refresh(true)); $('#roxyProfileSearch')?.addEventListener('input', (event) => { state.search = event.target.value.trim(); state.page = 1; refresh(false); }); $('#roxyProfileStateFilter')?.addEventListener('change', (event) => { state.profileState = event.target.value; state.page = 1; refresh(false); }); $('#btnRoxyProfilesPrev')?.addEventListener('click', () => { if (state.page > 1) { state.page -= 1; refresh(false); } }); $('#btnRoxyProfilesNext')?.addEventListener('click', () => { if (state.hasNext) { state.page += 1; refresh(false); } });
  $('#roxyProfilesBody')?.addEventListener('click', handleAction); $('#roxyProfilesBody')?.addEventListener('change', handleSelection);
  window.roxyProfileManagerRefresh = refresh;
  refresh(false);
})();
