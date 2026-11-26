'use strict';

// ── Theme management ────────────────────────────────────────────────────────

const THEMES = ['warm', 'dark', 'light', 'grey', 'green', 'nord', 'contrast'];
const THEME_ACCENTS = {
  dark: '#4f8ef7', light: '#2563eb', grey: '#98989d',
  green: '#00c800', warm: '#b5661a', nord: '#88c0d0', contrast: '#ffff00',
};

let currentThemeIdx = 0;

function applyTheme(name) {
  document.documentElement.setAttribute('data-theme', name);
  localStorage.setItem('selectedTheme', name);
  currentThemeIdx = THEMES.indexOf(name);
  if (currentThemeIdx < 0) currentThemeIdx = 0;
  document.querySelectorAll('.theme-dot').forEach((dot, i) => {
    dot.classList.toggle('active', i === currentThemeIdx);
  });
}

function buildThemePicker() {
  const picker = document.getElementById('theme-picker');
  THEMES.forEach((name) => {
    const dot = document.createElement('button');
    dot.className = 'theme-dot';
    dot.dataset.tip = `${name[0].toUpperCase()}${name.slice(1)} theme (T cycles)`;
    dot.setAttribute('aria-label', `${name} theme`);
    dot.style.background = THEME_ACCENTS[name];
    dot.addEventListener('click', () => applyTheme(name));
    picker.appendChild(dot);
  });
}

function cycleTheme() {
  currentThemeIdx = (currentThemeIdx + 1) % THEMES.length;
  applyTheme(THEMES[currentThemeIdx]);
}

// ── State ───────────────────────────────────────────────────────────────────

const PAGE_SIZE = 50;
let results = [];
let selectedIdx = -1;
let currentQuery = '';
let currentTransformedQuery = '';
let highlightsEnabled = localStorage.getItem('highlightsEnabled') !== 'false';
let currentBoxes = [];
let debounceTimer = null;
let searchAbort = null;
let nextOffset = 0;
let totalAvailable = 0;
let loadingMore = false;
let isRecentMode = false;
let folderFilter = localStorage.getItem('folderFilter') || '';   // '' = all folders
let folderOptions = [];                                          // [{path,label,count}] from last search

// ── DOM refs ────────────────────────────────────────────────────────────────

const searchInput    = document.getElementById('search-input');
const searchSpinner  = document.getElementById('search-spinner');
const searchError    = document.getElementById('search-error');
const resultsList    = document.getElementById('results-list');
const resultsLabel   = document.getElementById('results-label');
const previewEmpty   = document.getElementById('preview-empty');
const previewContent = document.getElementById('preview-content');
const previewImg     = document.getElementById('preview-img');
const hlCanvas       = document.getElementById('highlight-canvas');
const hlToast        = document.getElementById('highlight-toast');
const metaFilename   = document.getElementById('meta-filename');
const metaDatetime   = document.getElementById('meta-datetime');
const metaDims       = document.getElementById('meta-dims');
const metaSize       = document.getElementById('meta-size');
const metaPath       = document.getElementById('meta-path');
const btnOpenFinder  = document.getElementById('btn-open-finder');
const btnOpen        = document.getElementById('btn-open');
const btnCopy        = document.getElementById('btn-copy');
const btnCopyPath    = document.getElementById('btn-copy-path');
const btnDelete      = document.getElementById('btn-delete');
const helpOverlay    = document.getElementById('help-overlay');
const statsOverlay   = document.getElementById('stats-overlay');
const statsContent   = document.getElementById('stats-content');

const ctx2d = hlCanvas.getContext('2d');

// ── Query transformation ────────────────────────────────────────────────────
// Default = phrase. Multi-token treated as adjacent. 3+ char tokens get prefix.
// If user types AND/OR/NOT or quotes/asterisks, we pass through as advanced FTS5.

function transformQuerySimple(raw) {
  const q = raw.trim();
  if (!q) return '';
  // Advanced mode: user used operators or explicit syntax — pass through verbatim.
  if (/\b(AND|OR|NOT|NEAR)\b/.test(q) || /["*^()]/.test(q)) return q;

  // Extract clean word tokens (Unicode letters + digits + underscore).
  // This silently drops syntax-significant characters that FTS5 would choke on
  // (".", "@", "/", "-", commas, brackets, ...) but that the tokenizer also
  // ignores when building the index, so the matches still line up.
  const words = q.match(/[\p{L}\p{N}_]+/gu) || [];
  if (!words.length) return '';

  if (words.length === 1) {
    const t = words[0];
    return t.length >= 3 ? `${t}*` : t;
  }

  // Multi-word: phrase, with prefix on last token if it's 3+ chars (FTS5: "foo ba*").
  const head = words.slice(0, -1).join(' ');
  const last = words[words.length - 1];
  if (last.length >= 3) return `"${head} ${last}*"`;
  return `"${head} ${last}"`;
}

// ── Search ──────────────────────────────────────────────────────────────────

function debounceSearch(query) {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => runSearch(query), 200);
}

function searchUrl(offset) {
  const f = folderFilter ? `&folder=${encodeURIComponent(folderFilter)}` : '';
  return `/api/search?q=${encodeURIComponent(currentTransformedQuery)}&limit=${PAGE_SIZE}&offset=${offset}${f}`;
}

// ── Folder filter ───────────────────────────────────────────────────────────

const folderFilterBar = document.getElementById('folder-filter');

function renderFolderFilter(data) {
  folderOptions = data.folders || [];
  // Forget a filter for a folder that is no longer watched.
  if (folderFilter && !folderOptions.some(f => f.path === folderFilter)) {
    setFolderFilter('', false);
  }
  if (folderOptions.length < 2) { folderFilterBar.hidden = true; return; }
  folderFilterBar.hidden = false;
  const btn = (path, label, count, tip) => `
    <button class="ff-btn${folderFilter === path ? ' active' : ''}${count ? '' : ' empty'}" role="tab"
            aria-selected="${folderFilter === path}" data-folder="${esc(path)}" data-tip="${esc(tip)}">
      ${esc(label)}<span class="ff-count">${fmt(count)}</span>
    </button>`;
  const what = isRecentMode ? 'screenshots' : 'matches';
  folderFilterBar.innerHTML = '<span class="ff-label">Folders</span>' +
    btn('', 'All folders', data.all_total ?? 0, `Show ${what} from every folder (F)`) +
    folderOptions.map(f => btn(f.path, f.label, f.count, `${f.path}<br>${fmt(f.count)} ${what}`)).join('');
}

function setFolderFilter(path, rerun = true) {
  folderFilter = path;
  try { localStorage.setItem('folderFilter', path); } catch {}
  if (rerun) runSearch(searchInput.value);
}

function cycleFolderFilter(delta) {
  if (folderOptions.length < 2) return;
  const paths = ['', ...folderOptions.map(f => f.path)];
  const i = (paths.indexOf(folderFilter) + delta + paths.length) % paths.length;
  setFolderFilter(paths[i]);
}

folderFilterBar.addEventListener('click', e => {
  const b = e.target.closest('.ff-btn');
  if (b) setFolderFilter(b.dataset.folder);
});

async function runSearch(query) {
  currentQuery = query;
  currentTransformedQuery = transformQuerySimple(query);
  nextOffset = 0;
  isRecentMode = !query.trim();
  searchError.classList.remove('visible');

  if (searchAbort) searchAbort.abort();
  searchAbort = new AbortController();

  searchSpinner.classList.add('active');
  try {
    const url = searchUrl(0);
    const resp = await fetch(url, { signal: searchAbort.signal });
    const data = await resp.json();

    if (data.error) {
      searchError.textContent = `Invalid query — ${data.error} (interpreted as: ${currentTransformedQuery || '∅'})`;
      searchError.classList.add('visible');
      renderResults([], 0, false);
      return;
    }
    totalAvailable = data.total || 0;
    nextOffset = (data.results || []).length;
    isRecentMode = data.recent === true;
    renderFolderFilter(data);
    renderResults(data.results || [], totalAvailable, data.recent === true);
  } catch (e) {
    if (e.name !== 'AbortError') console.error(e);
  } finally {
    searchSpinner.classList.remove('active');
  }
}

async function loadMore() {
  if (loadingMore) return;
  if (results.length >= totalAvailable) return;
  loadingMore = true;
  try {
    const url = searchUrl(nextOffset);
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.results && data.results.length) {
      appendResults(data.results);
      nextOffset += data.results.length;
    }
  } catch (e) {
    console.error(e);
  } finally {
    loadingMore = false;
  }
}

// ── Rendering ───────────────────────────────────────────────────────────────

function formatDate(mtime) {
  const d = new Date(mtime * 1000);
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
}

function formatDateLong(mtime) {
  const d = new Date(mtime * 1000);
  return d.toLocaleString('en-GB', {
    day: 'numeric', month: 'long', year: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function formatFileSize(bytes) {
  if (bytes == null) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let v = bytes, u = 0;
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
  return `${v.toFixed(u === 0 ? 0 : 1)} ${units[u]}`;
}

function buildCard(r, idx) {
  const card = document.createElement('div');
  card.className = 'result-card';
  card.setAttribute('role', 'option');
  card.setAttribute('tabindex', '0');
  card.setAttribute('aria-selected', 'false');
  card.title = r.relative_path || r.filename;
  card.dataset.idx = idx;

  const thumb = document.createElement('img');
  thumb.className = 'result-thumb';
  thumb.src = `/api/image/${r.id}/thumb`;
  thumb.alt = '';
  thumb.loading = 'lazy';

  const meta = document.createElement('div');
  meta.className = 'result-meta';

  const fname = document.createElement('div');
  fname.className = 'result-filename';
  fname.textContent = r.filename;

  const date = document.createElement('div');
  date.className = 'result-date';
  date.textContent = formatDate(r.file_mtime);
  if (r.folder_label && !folderFilter && folderOptions.length > 1) {
    const chip = document.createElement('span');
    chip.className = 'result-folder';
    chip.textContent = r.folder_label;
    date.appendChild(chip);
  }

  const excerpt = document.createElement('div');
  excerpt.className = 'result-excerpt';
  excerpt.innerHTML = r.excerpt || '';

  meta.append(fname, date, excerpt);
  card.append(thumb, meta);

  // Mouse hover no longer selects — only click + keyboard navigation do.
  card.addEventListener('click', () => selectResult(Number(card.dataset.idx)));
  card.addEventListener('keydown', e => {
    if (e.key === 'Enter') openFile(results[Number(card.dataset.idx)].id);
  });

  return card;
}

function updateResultsLabel() {
  const where = folderFilter ? ` in ${(folderOptions.find(f => f.path === folderFilter) || {}).label || 'folder'}` : '';
  if (isRecentMode) {
    resultsLabel.textContent = `Most recent${where} · showing ${fmt(results.length)} of ${fmt(totalAvailable)}`;
  } else {
    resultsLabel.textContent = `${fmt(totalAvailable)} result${totalAvailable !== 1 ? 's' : ''}${where} · showing ${fmt(results.length)}`;
  }
}

function renderResults(data, total, isRecent) {
  results = data;
  totalAvailable = total;
  isRecentMode = isRecent;
  resultsList.innerHTML = '';
  data.forEach((r, i) => resultsList.appendChild(buildCard(r, i)));
  updateResultsLabel();

  if (data.length > 0) {
    selectResult(0);
  } else {
    clearPreview();
  }
}

function appendResults(data) {
  const startIdx = results.length;
  data.forEach((r, i) => {
    results.push(r);
    resultsList.appendChild(buildCard(r, startIdx + i));
  });
  updateResultsLabel();
}

function clearPreview() {
  previewEmpty.style.display = 'flex';
  previewContent.classList.remove('visible');
  selectedIdx = -1;
}

function selectResult(idx) {
  if (idx < 0 || idx >= results.length) return;
  selectedIdx = idx;

  document.querySelectorAll('.result-card').forEach((c, i) => {
    const selected = i === idx;
    c.classList.toggle('selected', selected);
    c.setAttribute('aria-selected', selected ? 'true' : 'false');
  });

  showPreview(results[idx]);
}

function moveSelection(delta) {
  const next = selectedIdx + delta;
  if (next < 0 || next >= results.length) return;
  selectResult(next);
  const card = resultsList.children[next];
  if (card) card.scrollIntoView({ block: 'nearest' });
}

// ── Preview ─────────────────────────────────────────────────────────────────

function showPreview(r) {
  previewEmpty.style.display = 'none';
  previewContent.classList.add('visible');

  previewImg.src = `/api/image/${r.id}`;
  metaFilename.textContent = r.filename;
  metaDatetime.textContent = formatDateLong(r.file_mtime);
  metaDims.textContent = '—';
  metaSize.textContent = formatFileSize(r.file_size);
  document.getElementById('meta-folder').textContent = r.folder_label || '—';
  document.getElementById('meta-folder').title = r.folder || '';
  metaPath.textContent = r.path || '';
  metaPath.title = r.path || '';

  previewImg.onload = () => {
    metaDims.textContent = `${previewImg.naturalWidth} × ${previewImg.naturalHeight}`;
    resizeCanvas();
    loadAndDrawBoxes(r.id);
  };

  btnOpen.onclick = () => openFile(r.id);
  btnOpenFinder.onclick = () => openFinder(r.id);
  btnCopy.onclick = () => copyImage(r.id);
  btnCopyPath.onclick = () => copyPathToClipboard(r.path);
  btnDelete.onclick = () => deleteFile(r.id);
  previewImg.onclick = () => openFile(r.id);
}

async function copyPathToClipboard(path) {
  if (!path) return;
  try {
    await navigator.clipboard.writeText(path);
    showToast('Path copied');
  } catch {
    showToast('Path copy failed', true);
  }
}

async function openFile(id) {
  await fetch(`/api/open/${id}`);
}

async function openFinder(id) {
  await fetch(`/api/open-finder/${id}`);
}

async function copyImage(id) {
  try {
    const resp = await fetch(`/api/copy-image/${id}`, { method: 'POST' });
    const data = await resp.json();
    if (data.ok) showToast('Copied to clipboard');
    else showToast('Copy failed', true);
  } catch {
    showToast('Copy failed', true);
  }
}

async function deleteFile(id) {
  try {
    const resp = await fetch(`/api/file/${id}`, { method: 'DELETE' });
    if (!resp.ok) { showToast('Delete failed', true); return; }
    const idx = results.findIndex(r => r.id === id);
    if (idx < 0) return;
    results.splice(idx, 1);
    totalAvailable = Math.max(0, totalAvailable - 1);
    const card = resultsList.children[idx];
    if (card) card.remove();
    Array.from(resultsList.children).forEach((c, i) => { c.dataset.idx = i; });
    updateResultsLabel();
    if (results.length === 0) clearPreview();
    else selectResult(Math.min(idx, results.length - 1));
    showToast('Moved to Trash');
  } catch {
    showToast('Delete failed', true);
  }
}

function showToast(text, isError = false) {
  hlToast.textContent = text;
  hlToast.style.background = isError ? 'rgba(192,57,43,.85)' : 'rgba(0,0,0,.7)';
  hlToast.classList.add('show');
  setTimeout(() => hlToast.classList.remove('show'), 1500);
}

// ── Bounding box highlighting ────────────────────────────────────────────────

function resizeCanvas() {
  const rect = previewImg.getBoundingClientRect();
  hlCanvas.width  = rect.width;
  hlCanvas.height = rect.height;
  hlCanvas.style.width  = rect.width  + 'px';
  hlCanvas.style.height = rect.height + 'px';
  // The canvas is positioned in the padded wrapper; line it up with the image itself.
  hlCanvas.style.left = previewImg.offsetLeft + 'px';
  hlCanvas.style.top  = previewImg.offsetTop + 'px';
}

function parseQueryTerms(query) {
  return query
    .replace(/[*"^()]/g, ' ')
    .replace(/\b(NOT|OR|AND|NEAR)\b/g, ' ')
    .split(/\s+/)
    .map(t => t.toLowerCase().trim())
    .filter(Boolean);
}

function drawBoxes() {
  ctx2d.clearRect(0, 0, hlCanvas.width, hlCanvas.height);
  if (!highlightsEnabled || !currentQuery.trim() || !currentBoxes.length) return;

  const terms = parseQueryTerms(currentQuery);
  if (!terms.length) return;

  const w = hlCanvas.width;
  const h = hlCanvas.height;

  const overlayColor = getComputedStyle(document.documentElement)
    .getPropertyValue('--img-overlay').trim();
  const accentColor = getComputedStyle(document.documentElement)
    .getPropertyValue('--accent').trim();

  currentBoxes.forEach(box => {
    const textLow = box.text.toLowerCase();
    const matches = terms.some(t => textLow.startsWith(t));
    if (!matches) return;

    const cx = box.x * w;
    const cy = (1 - box.y - box.h) * h;
    const cw = box.w * w;
    const ch = box.h * h;

    ctx2d.fillStyle = overlayColor;
    ctx2d.fillRect(cx, cy, cw, ch);
    ctx2d.strokeStyle = accentColor;
    ctx2d.lineWidth = 1.5;
    ctx2d.strokeRect(cx, cy, cw, ch);
  });
}

async function loadAndDrawBoxes(id) {
  try {
    const resp = await fetch(`/api/image/${id}/boxes`);
    const data = await resp.json();
    currentBoxes = data.boxes || [];
  } catch {
    currentBoxes = [];
  }
  drawBoxes();
}

function toggleHighlights() {
  highlightsEnabled = !highlightsEnabled;
  localStorage.setItem('highlightsEnabled', highlightsEnabled);
  drawBoxes();
  showToast(`Highlights: ${highlightsEnabled ? 'ON' : 'OFF'}`);
}

window.addEventListener('resize', () => {
  resizeCanvas();
  drawBoxes();
});

// ── Infinite scroll ─────────────────────────────────────────────────────────

resultsList.addEventListener('scroll', () => {
  const nearBottom = resultsList.scrollTop + resultsList.clientHeight >= resultsList.scrollHeight - 200;
  if (nearBottom) loadMore();
});

// ── Settings overlay: watched folders + stats ────────────────────────────────

const statsBox      = document.getElementById('stats-box');
const foldersList   = document.getElementById('folders-list');
const foldersSummary = document.getElementById('folders-summary');
const folderMsg     = document.getElementById('folder-msg');
const folderInput   = document.getElementById('folder-path-input');
let folderPollTimer = null;

function esc(str) {
  return String(str).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const fmt = n => Number(n).toLocaleString();

// "2026-11-24 05:30" (server local time) → "5 min ago" / "Yesterday" / "12 Nov 2026"
function relTime(str) {
  if (!str) return 'never';
  const d = new Date(str.replace(' ', 'T'));
  if (isNaN(d)) return str;
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} h ago`;
  const days = Math.round(hrs / 24);
  if (days === 1) return 'yesterday';
  if (days < 14) return `${days} days ago`;
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
}

function splitPath(path) {
  const trimmed = path.replace(/\/+$/, '');
  const i = trimmed.lastIndexOf('/');
  return { name: trimmed.slice(i + 1) || trimmed, parent: trimmed.slice(0, i) || '/' };
}

function setFolderMsg(text, kind) {
  folderMsg.textContent = text || '';
  folderMsg.className = kind || '';
}

const TIP_REINDEX = '<b>Reindex</b> — quick catch-up. Reads only new or changed screenshots, plus earlier failures and files OneDrive has since downloaded. Takes seconds when nothing changed. New screenshots are picked up automatically, so you rarely need this.';

function estimate(n) {
  const mins = Math.round(n * 0.4 / 60);
  return mins < 1 ? 'under a minute' : `about ${fmt(mins)} min`;
}

function tipReocr(n) {
  return `<b>Re-OCR every file</b> — re-reads the text of every screenshot in this folder, even unchanged ones (${estimate(n)} here). Use it after changing OCR settings or if the text looks wrong. Search keeps working meanwhile. It can't fix damaged files or download cloud-only ones.`;
}

function folderStatus(f, progress) {
  if (f.access_denied) return { cls: 'bad', label: 'Blocked by macOS' };
  if (!f.exists)       return { cls: 'bad', label: 'Folder not found' };
  if (f.reindexing) {
    const here = progress && progress.current === f.path;
    return { cls: 'busy', label: here ? `Indexing ${fmt(progress.done)} / ${fmt(progress.total)}` : 'Queued' };
  }
  if (!f.watching)     return { cls: 'bad', label: 'Not watching' };
  return { cls: 'ok', label: 'Watching' };
}

function folderCard(f, progress, python) {
  const { name, parent } = splitPath(f.path);
  const st = folderStatus(f, progress);
  const bad = st.cls === 'bad';

  let bar = '';
  if (f.reindexing) {
    const here = progress && progress.current === f.path && progress.total;
    bar = here
      ? `<div class="f-progress"><i style="width:${Math.min(100, Math.round(progress.done / progress.total * 100))}%"></i></div>`
      : '<div class="f-progress indeterminate"><i></i></div>';
  }

  const alert = f.access_denied ? `
    <div class="f-alert">
      <strong>macOS is blocking access to this folder.</strong>
      <ol>
        <li>Click <b>Open settings &amp; show file</b>. System Settings → Full Disk Access opens, and Finder highlights the program.</li>
        <li>Drag that file onto the list (don't use the <b>+</b> button) and switch it on.</li>
        <li>Click <b>Restart ScreenFind</b>.</li>
      </ol>
      <div class="f-alert-row">
        <code>${esc(f.access_denied)}</code>
        <button class="btn small" data-act="copy" data-text="${esc(f.access_denied)}">Copy path</button>
        <button class="btn small primary" data-act="open-fda">Open settings &amp; show file</button>
        ${canRestart ? '<button class="btn small" data-act="restart">Restart ScreenFind</button>' : ''}
      </div>
    </div>` : '';

  return `
    <div class="f-card${bad ? ' is-bad' : ''}" data-path="${esc(f.path)}">
      <div class="f-top">
        <div class="f-icon">📁</div>
        <div class="f-main">
          <div class="f-name">${esc(name)}
            <span class="chip ${st.cls}"><span class="dot"></span>${esc(st.label)}</span>
            ${f.recursive ? '<span class="chip">+ subfolders</span>' : ''}
          </div>
          <div class="f-path" title="${esc(f.path)}">${esc(parent)}</div>
        </div>
        <div class="f-actions">
          <button class="btn" data-act="reindex" data-tip="${esc(TIP_REINDEX)}">Reindex</button>
          <details class="menu">
            <summary aria-label="More actions" title="More actions">⋯</summary>
            <div class="menu-pop">
              <button data-act="full" data-tip="${esc(tipReocr(f.indexed + f.errors))}">Re-OCR every file<small>Re-reads the text of all ${fmt(f.indexed + f.errors)} screenshots · ${esc(estimate(f.indexed + f.errors))}</small></button>
              <hr>
              <button class="danger" data-act="remove" data-tip="Stops watching this folder and removes its screenshots from search. The files on your disk are not touched.">Remove folder<small>Removes it from search. Your files are not deleted</small></button>
            </div>
          </details>
        </div>
      </div>
      ${bar}
      <div class="f-stats">
        <div class="f-stat"><b>${fmt(f.indexed)}</b><span>indexed</span></div>
        <div class="f-stat${f.errors ? ' bad' : ''}"><b>${fmt(f.errors)}</b><span>errors</span></div>
        <div class="f-stat"><b>${fmt(f.pending)}</b><span>cloud-only</span></div>
        <div class="f-stat" title="${esc(f.last_indexed_at || '')}"><b>${esc(relTime(f.last_indexed_at))}</b><span>last indexed</span></div>
      </div>
      ${alert}
    </div>`;
}

let canRestart = false;

function renderFolders(payload) {
  const { folders, progress, python } = payload;
  canRestart = !!payload.can_restart;
  const total = folders.reduce((n, f) => n + f.indexed, 0);
  foldersSummary.textContent = folders.length
    ? `${fmt(folders.length)} folder${folders.length === 1 ? '' : 's'} · ${fmt(total)} screenshots`
    : '';
  foldersList.innerHTML = folders.length
    ? folders.map(f => folderCard(f, progress, python)).join('')
    : '<div class="folder-empty">No folders yet.<br>Add one below to start indexing screenshots.</div>';
}

async function loadFolders() {
  try {
    renderFolders(await (await fetch('/api/folders')).json());
  } catch {
    foldersList.innerHTML = '<div class="folder-empty" style="color:#c0392b">Failed to load folders.</div>';
  }
}

async function loadStatsBlock() {
  try {
    const d = await (await fetch('/api/stats')).json();
    const last = d.last_indexed_at
      ? new Date(d.last_indexed_at).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
      : 'never';
    const tile = (value, label, cls = '') => `<div class="tile ${cls}"><b>${value}</b><span>${label}</span></div>`;
    statsContent.innerHTML = [
      tile(fmt(d.total_indexed), 'Indexed'),
      tile(fmt(d.total_errors), 'Errors', d.total_errors ? 'bad' : ''),
      tile(fmt(d.total_pending), 'Cloud-only'),
      tile(fmt(d.total_deleted), 'Deleted'),
      tile(`${d.db_size_mb} MB`, 'Database'),
      tile(last, 'Last indexed', 'small'),
    ].join('');
  } catch {
    statsContent.innerHTML = '<p style="color:#c0392b;font-size:13px">Failed to load stats.</p>';
  }
}

function showStats() {
  statsOverlay.classList.add('open');
  setFolderMsg('');
  loadFolders();
  loadStatsBlock();
  clearInterval(folderPollTimer);
  folderPollTimer = setInterval(() => {
    if (!statsOverlay.classList.contains('open')) { clearInterval(folderPollTimer); return; }
    // Don't rebuild the cards while a ⋯ menu is open — it would snap shut under the cursor.
    if (!foldersList.querySelector('details[open]')) loadFolders();
    loadStatsBlock();
  }, 2000);
}

async function restartServer() {
  setFolderMsg('Restarting ScreenFind…');
  await fetch('/api/restart', { method: 'POST' }).catch(() => {});
  await new Promise(r => setTimeout(r, 1500));
  for (let i = 0; i < 40; i++) {           // launchd may wait up to 10 s before relaunching
    try { if ((await fetch('/api/stats')).ok) break; } catch {}
    await new Promise(r => setTimeout(r, 500));
  }
  setFolderMsg('Restarted. Indexing will resume if macOS now allows access.', 'ok');
  loadFolders(); refreshStatusBar(); runSearch(searchInput.value);
}

async function addFolder() {
  const path = folderInput.value.trim();
  if (!path) { setFolderMsg('Paste a folder path or press Browse…', 'error'); folderInput.focus(); return; }
  const addBtn = document.getElementById('btn-folder-add');
  addBtn.disabled = true;
  setFolderMsg('Checking folder…');
  try {
    const res = await fetch('/api/folders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, recursive: document.getElementById('folder-recursive').checked }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { setFolderMsg(body.detail || 'Could not add folder.', 'error'); return; }
    folderInput.value = '';
    setFolderMsg('Folder added — indexing started.', 'ok');
    renderFolders(body);
    refreshStatusBar();
    runSearch(searchInput.value);   // show it in the folder filter
  } finally {
    addBtn.disabled = false;
  }
}

async function browseFolder() {
  const btn = document.getElementById('btn-folder-browse');
  btn.disabled = true;
  try {
    const body = await (await fetch('/api/pick-folder', { method: 'POST' })).json();
    if (body.path) { folderInput.value = body.path; folderInput.focus(); }
  } catch {
    setFolderMsg('Folder picker failed — paste the path instead.', 'error');
  } finally {
    btn.disabled = false;
  }
}

foldersList.addEventListener('click', async e => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  btn.closest('details')?.removeAttribute('open');
  const act = btn.dataset.act;

  if (act === 'copy') {
    try { await navigator.clipboard.writeText(btn.dataset.text); setFolderMsg('Path copied.', 'ok'); }
    catch { setFolderMsg('Could not copy — select the path and press ⌘C.', 'error'); }
    return;
  }
  if (act === 'restart') { restartServer(); return; }
  if (act === 'open-fda') {
    await fetch('/api/open-fda-settings', { method: 'POST' });
    setFolderMsg('Opened System Settings. Drag the highlighted file onto the list, switch it on, then restart ScreenFind.', 'ok');
    return;
  }

  const card = btn.closest('.f-card');
  const path = card.dataset.path;
  const q = encodeURIComponent(path);
  const count = card.querySelector('.f-stat b').textContent;

  if (act === 'remove') {
    if (!confirm(`Remove this folder from ScreenFind?\n\n${path}\n\n${count} indexed screenshots will no longer appear in search. The files themselves are not touched.`)) return;
    const res = await fetch(`/api/folders?path=${q}`, { method: 'DELETE' });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { setFolderMsg(body.detail || 'Could not remove folder.', 'error'); return; }
    setFolderMsg(`Folder removed. ${fmt(body.removed_from_index)} screenshots dropped from the index.`, 'ok');
    renderFolders(body);
    runSearch(searchInput.value);   // results may have contained that folder
    refreshStatusBar();
    return;
  }

  const full = act === 'full';
  if (full && !confirm('Re-run OCR on every file in this folder? This can take a while for large folders.')) return;
  await fetch(`/api/folders/reindex?path=${q}&full=${full}`, { method: 'POST' });
  setFolderMsg(full ? 'Full re-OCR queued.' : 'Reindex queued.', 'ok');
  loadFolders();
  refreshStatusBar();
});

// Close an open ⋯ menu when clicking anywhere else in the dialog.
statsBox.addEventListener('click', e => {
  statsBox.querySelectorAll('details.menu[open]').forEach(m => { if (!m.contains(e.target)) m.removeAttribute('open'); });
});

document.getElementById('btn-folder-add').addEventListener('click', addFolder);
document.getElementById('btn-folder-browse').addEventListener('click', browseFolder);
folderInput.addEventListener('keydown', e => { if (e.key === 'Enter') addFolder(); });

document.getElementById('btn-reindex').addEventListener('click', async () => {
  const btn = document.getElementById('btn-reindex');
  await fetch('/api/reindex', { method: 'POST' });
  btn.textContent = 'Reindex queued ✓';
  setTimeout(() => { btn.textContent = 'Reindex all folders'; }, 3000);
  loadFolders();
});

// ── Keyboard shortcuts ───────────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  const inInput = document.activeElement === searchInput;
  const overlayOpen = helpOverlay.classList.contains('open') || statsOverlay.classList.contains('open');

  if (e.key === 'Escape') {
    if (overlayOpen) {
      helpOverlay.classList.remove('open');
      statsOverlay.classList.remove('open');
      return;
    }
    searchInput.value = '';
    searchInput.focus();
    debounceSearch('');
    return;
  }

  if (overlayOpen) return;

  // Delete / Backspace (only outside the search input) → move to Trash, remove from index.
  if ((e.key === 'Delete' || e.key === 'Backspace') && !inInput && selectedIdx >= 0) {
    e.preventDefault();
    deleteFile(results[selectedIdx].id);
    return;
  }

  if (e.key === 'ArrowDown') { e.preventDefault(); moveSelection(1); return; }
  if (e.key === 'ArrowUp')   { e.preventDefault(); moveSelection(-1); return; }

  if (e.key === 'Enter' && !inInput && selectedIdx >= 0) {
    if (e.metaKey) openFinder(results[selectedIdx].id);
    else openFile(results[selectedIdx].id);
    return;
  }
  if (e.key === 'Enter' && inInput && e.metaKey && selectedIdx >= 0) {
    openFinder(results[selectedIdx].id);
    return;
  }

  if (e.key === '/' && !inInput) { e.preventDefault(); searchInput.focus(); return; }

  // C — copy selected image to clipboard
  if ((e.key === 'c' || e.key === 'C') && !inInput && !e.metaKey && !e.ctrlKey && selectedIdx >= 0) {
    copyImage(results[selectedIdx].id);
    return;
  }

  if ((e.key === 'h' || e.key === 'H') && !inInput) { toggleHighlights(); return; }
  if ((e.key === 't' || e.key === 'T') && !inInput) { cycleTheme(); return; }
  if (e.key === '?') { helpOverlay.classList.toggle('open'); return; }
  if ((e.key === 'f' || e.key === 'F') && !inInput && !e.metaKey && !e.ctrlKey && !e.altKey) {
    e.preventDefault(); cycleFolderFilter(e.shiftKey ? -1 : 1); return;
  }

  if (!inInput && e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
    searchInput.focus();
  }
});

// ── Event wiring ─────────────────────────────────────────────────────────────

searchInput.addEventListener('input', () => debounceSearch(searchInput.value));

document.getElementById('btn-help').addEventListener('click', () => helpOverlay.classList.toggle('open'));
document.getElementById('help-close').addEventListener('click', () => helpOverlay.classList.remove('open'));
helpOverlay.addEventListener('click', e => { if (e.target === helpOverlay) helpOverlay.classList.remove('open'); });

document.getElementById('btn-stats').addEventListener('click', showStats);
document.getElementById('stats-close').addEventListener('click', () => statsOverlay.classList.remove('open'));
statsOverlay.addEventListener('click', e => { if (e.target === statsOverlay) statsOverlay.classList.remove('open'); });

// ── Status bar ───────────────────────────────────────────────────────────────

const statusIndexed  = document.getElementById('status-indexed');
const statusErrors   = document.getElementById('status-errors');
const statusLast     = document.getElementById('status-last');
const statusReindex  = document.getElementById('status-reindex');

async function refreshStatusBar() {
  try {
    const data = await (await fetch('/api/stats')).json();

    statusIndexed.textContent = `Indexed: ${data.total_indexed.toLocaleString()}`;
    const blocked = Object.keys(data.access_denied || {}).length;
    statusErrors.textContent  = blocked
      ? `⚠ macOS is blocking folder access — click to fix`
      : (data.total_errors > 0 ? `Errors: ${data.total_errors}` : '');
    statusErrors.classList.toggle('warn', !!blocked);
    statusErrors.onclick = blocked ? showStats : null;
    document.getElementById('settings-badge').hidden = !blocked;

    if (data.last_indexed_at) {
      const d = new Date(data.last_indexed_at);
      statusLast.textContent = `Last indexed: ${d.toLocaleString('en-GB', {
        day: '2-digit', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
      })}`;
    } else {
      statusLast.textContent = 'Not yet indexed';
    }

    statusReindex.textContent = data.reindex_running ? '⟳ Indexing…' : '';
  } catch {
    statusIndexed.textContent = 'Index unavailable';
  }
}

setInterval(refreshStatusBar, 10_000);
refreshStatusBar();

// ── Tooltips ([data-tip], HTML allowed — only ever set from our own strings) ──

const tooltip = document.getElementById('tooltip');
let tipTimer = null, tipTarget = null;

function showTip(el) {
  tooltip.innerHTML = el.dataset.tip;
  tooltip.classList.add('show');
  const r = el.getBoundingClientRect(), t = tooltip.getBoundingClientRect();
  let top = r.bottom + 8;
  if (top + t.height > innerHeight - 8) top = r.top - t.height - 8;
  const left = Math.max(8, Math.min(r.left + r.width / 2 - t.width / 2, innerWidth - t.width - 8));
  tooltip.style.top = `${top}px`;
  tooltip.style.left = `${left}px`;
}
function hideTip() { clearTimeout(tipTimer); tipTarget = null; tooltip.classList.remove('show'); }

document.addEventListener('mouseover', e => {
  const el = e.target.closest('[data-tip]');
  if (el === tipTarget) return;
  hideTip();
  if (!el) return;
  tipTarget = el;
  tipTimer = setTimeout(() => showTip(el), 350);
});
document.addEventListener('focusin', e => {
  const el = e.target.closest?.('[data-tip]');
  if (el && e.target.matches(':focus-visible')) { tipTarget = el; showTip(el); }
});
document.addEventListener('focusout', hideTip);
document.addEventListener('mousedown', hideTip);
document.addEventListener('scroll', hideTip, true);

// ── Init ─────────────────────────────────────────────────────────────────────

buildThemePicker();
const savedTheme = localStorage.getItem('selectedTheme') || 'warm';
applyTheme(savedTheme);

searchInput.focus();

// If launched via ?q=…  (browser address-bar keyword search), pre-fill and run.
const urlQ = new URLSearchParams(location.search).get('q');
if (urlQ && urlQ.trim()) {
  searchInput.value = urlQ;
  runSearch(urlQ);
} else {
  runSearch('');  // load recent screenshots on start
}

// http://localhost:5123/#settings opens Settings directly.
if (location.hash === '#settings') showStats();
