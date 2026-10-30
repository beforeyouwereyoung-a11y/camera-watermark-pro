/* ============================================================
   相机水印工具 Pro — 前端逻辑（原生 JS，无框架、无 CDN）

   模块划分（按出现顺序）：
     基础工具 → 主题 → 初始化 → 事件绑定 → 预设 → 模板 → Logo
     → 配置保存 → 文件树 → 拖拽 → 目录 → 预览 → 批量处理
     → 结果面板 → 大图查看 → 可视化编辑器 → 性能
   ============================================================ */
'use strict';

const TEMPLATE_DESC = {
  '标准水印': '经典 EXIF 信息条',
  '标准水印2': '信息条 + 圆角 + 阴影',
  '标准水印Pro': '白底展签 + 品牌/型号 + 参数行 + 右下 Logo',
  '画框Pro-中': '明信片白框 + 照片柔影 + 细描边',
  '背景模糊': '模糊背景，社交平台感强',
  '尼康专用背景模糊': '尼康风格 + 红色 Z 高亮',
  '右下角参数': '低干扰，右下角参数',
  '文件夹名+右下角参数': '文件夹名 + 参数，适合归档',
  'logo居中': '中心 Logo，强调品牌',
};

const state = {
  mode: 'template',
  templateName: '标准水印2',
  templates: [],
  crop: 'none',
  outputFormat: 'original',
  quality: 100,
  longEdge: 0,
  namePattern: '{name}_{template}',
  preserveExif: true,
  override: false,
  inputFolder: '',
  outputFolder: '',
  files: [],
  selected: new Set(),
  current: null,
  previewMode: 'after',
  simple: {
    text: '',
    position: 'bottom_right',
    height: 3.5,
    opacity: 85,
    angle: 0,
    color: '#ffffff',
    strokeColor: '#000000',
    strokeWidth: 0,
    tile: false,
  },
  jobId: null,
  running: false,
  nativeDrop: false,
  outputRoot: '',
  meta: {},
  // 预览几何：{width, height, photo_rect}，用于把对比模式的原图对齐到成片位置
  previewGeo: null,

  // 主题 / 预设 / Logo / 性能
  theme: 'light',
  presets: [],
  activePreset: null,
  logos: [],
  logo: { logo_name: '', logo_scale: 100, logo_opacity: 100, logo_corner: 'bottom_right' },
  perf: { workers: 'auto', exif_cache_size: 2048, preview_max_edge: 1400, cores: 0, max_workers: 8 },

  // 结果面板
  resultItems: [],
  resultOnlyOk: false,

  // 可视化编辑器
  processors: [],
  categories: {},
  pipeline: [],
  editorStep: -1,
  editorBaseline: '[]',
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

/* ---------------- 基础工具 ---------------- */
async function api(path, options) {
  const res = await fetch(path, options);
  const ct = res.headers.get('content-type') || '';
  if (!ct.includes('application/json')) {
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res;
  }
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

function toast(message, type) {
  const node = el('div', 'toast' + (type ? ' ' + type : ''), message);
  $('toasts').appendChild(node);
  setTimeout(() => {
    node.style.transition = 'opacity .25s';
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 260);
  }, type === 'err' ? 4200 : 2600);
}

function log(message, cls) {
  const body = $('logBody');
  const stamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  body.appendChild(el('div', 'log-line ' + (cls || 'info'), `[${stamp}] ${message}`));
  body.scrollTop = body.scrollHeight;
}

function debounce(fn, wait) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function formatBytes(n) {
  if (!n && n !== 0) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function baseName(p) {
  return (p || '').split(/[\\/]/).pop() || '';
}

/* ---------------- 主题 ---------------- */
function isDarkNow(mode) {
  if (mode === 'dark') return true;
  if (mode === 'light') return false;
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
}

function applyTheme(mode, persist) {
  state.theme = mode || 'light';
  const dark = isDarkNow(state.theme);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  try { localStorage.setItem('cwp-theme', state.theme); } catch (e) { /* 忽略 */ }

  const box = $('themeSwitch');
  if (box) {
    [...box.children].forEach((b) => b.classList.toggle('is-active', b.dataset.theme === state.theme));
  }
  if (persist) saveConfig();
}

// 跟随系统模式下，系统切换深浅色时界面要跟着变
if (window.matchMedia) {
  try {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (state.theme === 'auto') applyTheme('auto', false);
    });
  } catch (e) { /* 老内核忽略 */ }
}

/* ---------------- 初始化 ---------------- */
async function boot() {
  bindEvents();
  applyTheme(localStorage.getItem('cwp-theme') || 'light', false);

  try {
    const meta = await api('/api/v1/meta');
    state.meta = meta;
    state.nativeDrop = !!meta.native_drop;
    state.logos = meta.logos || [];
    state.presets = meta.presets || [];
    $('appVersion').textContent = meta.version;
    $('modeLabel').textContent = meta.portable ? '便携模式' : '用户目录模式';

    const chip = $('chipExif');
    if (meta.exiftool_available) {
      chip.textContent = 'EXIF 引擎：exiftool 已就绪';
      chip.classList.add('ok');
    } else {
      chip.textContent = 'EXIF 引擎：Pillow 降级模式';
      chip.classList.add('warn');
    }
    const chipData = $('chipData');
    chipData.textContent = '数据目录：' + meta.data_root;
    chipData.title = meta.data_root;

    if (meta.theme) applyTheme(meta.theme, false);
  } catch (e) {
    toast('初始化失败：' + e.message, 'err');
  }

  try {
    const cfg = await api('/api/v1/config');
    state.templates = cfg.templates || [];
    state.templateName = cfg.template_name;
    state.crop = cfg.crop_preset || 'none';
    state.outputFormat = cfg.output_format || 'original';
    state.quality = cfg.quality;
    state.longEdge = cfg.long_edge;
    state.namePattern = cfg.name_pattern;
    state.preserveExif = cfg.preserve_exif;
    state.override = cfg.override_existed;
    state.inputFolder = cfg.input_folder;
    state.outputFolder = cfg.output_folder;
    state.logo = {
      logo_name: cfg.logo_name || '',
      logo_scale: cfg.logo_scale == null ? 100 : cfg.logo_scale,
      logo_opacity: cfg.logo_opacity == null ? 100 : cfg.logo_opacity,
      logo_corner: cfg.logo_corner || 'bottom_right',
    };
    if (cfg.perf) state.perf = Object.assign(state.perf, cfg.perf);

    $('inFolder').value = state.inputFolder;
    $('outFolder').value = state.outputFolder;
    $('outFormat').value = state.outputFormat;
    $('outQuality').value = state.quality;
    $('outQualityVal').textContent = state.quality;
    $('outLongEdge').value = state.longEdge;
    $('outName').value = state.namePattern;
    $('outExif').checked = state.preserveExif;
    $('outOverride').checked = state.override;
    $('advJson').value = cfg.template || '';

    renderTemplates();
    syncCropChips();
    renderPresets();
    syncLogoUI();
    syncPerfUI();
    updateOutFolderNote();
    log('配置已加载，模板 ' + state.templates.length + ' 个，预设 ' + state.presets.length + ' 个', 'ok');
  } catch (e) {
    toast('读取配置失败：' + e.message, 'err');
  }

  // 拖拽提示按实际能力显示，避免出现「支持拖拽却提示不支持」的矛盾
  const dzHint = document.querySelector('#dropzone .hint');
  if (dzHint) {
    dzHint.textContent = state.nativeDrop
      ? '支持拖拽文件夹或照片；也可以在上方选择输入文件夹后点「刷新」'
      : '请在上方选择输入文件夹，或点「浏览」';
  }

  await loadTree();
}

/* ---------------- 事件绑定 ---------------- */
function bindEvents() {
  // 主题
  $('themeSwitch').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    applyTheme(btn.dataset.theme, true);
  });

  // 模式切换
  $('modeSwitch').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    state.mode = btn.dataset.mode;
    [...$('modeSwitch').children].forEach((b) => b.classList.toggle('is-active', b === btn));
    $('blockTemplate').classList.toggle('hidden', state.mode !== 'template');
    $('blockSimple').classList.toggle('hidden', state.mode !== 'simple');
    schedulePreview();
  });

  // 预览模式
  $('previewMode').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    state.previewMode = btn.dataset.pview;
    [...$('previewMode').children].forEach((b) => b.classList.toggle('is-active', b === btn));
    applyPreviewMode();
  });

  // 自定义文字水印
  $('swText').addEventListener('input', () => { state.simple.text = $('swText').value; schedulePreview(); });
  $('swPosition').addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    state.simple.position = btn.dataset.pos;
    [...$('swPosition').children].forEach((b) => b.classList.toggle('is-active', b === btn));
    schedulePreview();
  });
  bindRange('swHeight', 'swHeightVal', (v) => v + '%', (v) => { state.simple.height = parseFloat(v); });
  bindRange('swOpacity', 'swOpacityVal', (v) => v + '%', (v) => { state.simple.opacity = parseInt(v, 10); });
  bindRange('swAngle', 'swAngleVal', (v) => v + '°', (v) => { state.simple.angle = parseInt(v, 10); });
  bindRange('swStrokeWidth', 'swStrokeVal', (v) => v + 'px', (v) => { state.simple.strokeWidth = parseInt(v, 10); });
  $('swColor').addEventListener('input', () => { state.simple.color = $('swColor').value; schedulePreview(); });
  $('swStroke').addEventListener('input', () => { state.simple.strokeColor = $('swStroke').value; schedulePreview(); });
  $('swTile').addEventListener('change', () => { state.simple.tile = $('swTile').checked; schedulePreview(); });

  // 裁切
  $('cropPresets').addEventListener('click', (e) => {
    const btn = e.target.closest('.chip-btn');
    if (!btn) return;
    state.crop = btn.dataset.crop;
    syncCropChips();
    schedulePreview();
    saveConfig();
  });

  // 输出规则
  $('outFormat').addEventListener('change', () => { state.outputFormat = $('outFormat').value; saveConfig(); });
  $('outLongEdge').addEventListener('change', () => { state.longEdge = parseInt($('outLongEdge').value, 10) || 0; saveConfig(); });
  bindRange('outQuality', 'outQualityVal', (v) => v, (v) => { state.quality = parseInt(v, 10); }, saveConfig);
  $('outName').addEventListener('change', () => { state.namePattern = $('outName').value || '{name}_{template}'; saveConfig(); });
  $('outExif').addEventListener('change', () => { state.preserveExif = $('outExif').checked; saveConfig(); });
  $('outOverride').addEventListener('change', () => { state.override = $('outOverride').checked; saveConfig(); });

  // 目录（目录改动必须立即落盘，不能等防抖）
  $('inFolder').addEventListener('change', async () => {
    state.inputFolder = $('inFolder').value.trim();
    updateOutFolderNote();
    await saveConfigNow();
    await loadTree();
  });
  $('outFolder').addEventListener('change', async () => {
    state.outputFolder = $('outFolder').value.trim();
    updateOutFolderNote();
    await saveConfigNow();
  });
  document.querySelectorAll('[data-pick]').forEach((btn) => {
    btn.addEventListener('click', () => pickFolder(btn.dataset.pick));
  });
  $('btnFixOutput').addEventListener('click', fixOutputFolder);
  $('btnOpenOutput').addEventListener('click', openOutputFolder);
  $('btnOpenOutputTop').addEventListener('click', openOutputFolder);

  // 预设
  $('btnPresetSave').addEventListener('click', savePresetAs);
  $('btnPresetRefresh').addEventListener('click', refreshPresets);
  $('presetStrip').addEventListener('click', (e) => {
    const card = e.target.closest('.preset-card');
    if (card) applyPresetByName(card.dataset.preset);
  });
  $('presetStrip').addEventListener('contextmenu', (e) => {
    const card = e.target.closest('.preset-card');
    if (!card) return;
    e.preventDefault();
    deletePreset(card.dataset.preset);
  });

  // 模板
  $('btnTplRefresh').addEventListener('click', refreshTemplates);
  $('btnTplSaveAs').addEventListener('click', saveAsTemplate);
  $('btnOpenEditor').addEventListener('click', () => openEditor($('advJson').value));
  $('btnJsonToEditor').addEventListener('click', () => openEditor($('advJson').value));

  // Logo
  $('btnLogoUpload').addEventListener('click', () => $('logoFile').click());
  $('logoFile').addEventListener('change', uploadLogo);
  $('btnLogoDelete').addEventListener('click', deleteLogo);
  $('logoSelect').addEventListener('change', () => {
    state.logo.logo_name = $('logoSelect').value;
    syncLogoUI();
    saveConfig();
    schedulePreview();
  });
  bindRange('logoScale', 'logoScaleVal', (v) => v + '%',
    (v) => { state.logo.logo_scale = parseInt(v, 10); }, saveConfig, schedulePreview);
  bindRange('logoOpacity', 'logoOpacityVal', (v) => v + '%',
    (v) => { state.logo.logo_opacity = parseInt(v, 10); }, saveConfig, schedulePreview);
  $('logoCorner').addEventListener('click', (e) => {
    const btn = e.target.closest('.chip-btn');
    if (!btn) return;
    state.logo.logo_corner = btn.dataset.corner;
    syncLogoUI();
    saveConfig();
    schedulePreview();
  });

  // 折叠面板：默认收起，展开状态记在本地，下次打开保持
  setupCollapse('logoToggle', 'logoBody');
  setupCollapse('cropToggle', 'cropBody');
  setupCollapse('outToggle', 'outBody');
  setupCollapse('perfToggle', 'perfBody');
  setupCollapse('advToggle', 'advBody');

  // 可拖动分隔条 + 预览放大
  setupSplitters();
  setupPreviewZoom();

  // 性能
  $('perfWorkers').addEventListener('change', () => {
    state.perf.workers = $('perfWorkers').value;
    syncPerfUI();
    saveConfig();
  });
  bindRange('perfCache', 'perfCacheVal', (v) => v,
    (v) => { state.perf.exif_cache_size = parseInt(v, 10); }, saveConfig);
  bindRange('perfPreview', 'perfPreviewVal', (v) => v,
    (v) => { state.perf.preview_max_edge = parseInt(v, 10); }, saveConfig, schedulePreview);
  $('btnPerfProbe').addEventListener('click', runPerfProbe);
  $('btnCacheClear').addEventListener('click', clearExifCache);

  // 文件操作
  $('btnSelectAll').addEventListener('click', () => {
    visibleFiles().forEach((f) => state.selected.add(f.value));
    renderFiles();
  });
  $('btnSelectNone').addEventListener('click', () => { state.selected.clear(); renderFiles(); });
  $('btnClearAll').addEventListener('click', clearAllFiles);
  $('btnRefresh').addEventListener('click', loadTree);
  $('fileSearch').addEventListener('input', renderFiles);

  // 执行
  $('btnStart').addEventListener('click', startProcess);
  $('btnCancel').addEventListener('click', cancelProcess);
  $('btnViewResult').addEventListener('click', openResultPanel);
  $('btnExportReport').addEventListener('click', exportReport);

  // 结果面板
  $('btnResultClose').addEventListener('click', () => $('resultModal').classList.add('hidden'));
  $('btnResultOpenFolder').addEventListener('click', openOutputFolder);
  $('btnResultExport').addEventListener('click', exportReport);
  $('btnResultOnlyOk').addEventListener('click', () => {
    state.resultOnlyOk = !state.resultOnlyOk;
    $('btnResultOnlyOk').classList.toggle('is-active', state.resultOnlyOk);
    renderResultPanel();
  });
  $('resultModal').addEventListener('click', (e) => {
    if (e.target === $('resultModal')) $('resultModal').classList.add('hidden');
  });

  // 大图查看
  $('btnLbClose').addEventListener('click', closeLightbox);
  $('btnLbCompare').addEventListener('click', () => {
    $('lightboxBefore').classList.toggle('hidden');
    $('btnLbCompare').classList.toggle('is-active', !$('lightboxBefore').classList.contains('hidden'));
  });
  $('btnLbReveal').addEventListener('click', () => {
    const p = $('lightboxImg').dataset.path;
    if (p) revealFile(p);
  });
  $('btnLbSaveAs').addEventListener('click', () => {
    const p = $('lightboxImg').dataset.path;
    if (p) saveAs(p);
  });
  $('lightbox').addEventListener('click', (e) => {
    if (e.target === $('lightbox')) closeLightbox();
  });

  // 可视化编辑器
  $('btnEditorClose').addEventListener('click', closeEditor);
  $('btnEditorSave').addEventListener('click', () => saveEditorPipeline(false));
  $('btnEditorSaveAs').addEventListener('click', () => saveEditorPipeline(true));
  $('btnEditorRevert').addEventListener('click', () => {
    try {
      state.pipeline = JSON.parse(state.editorBaseline);
      state.editorStep = -1;
      renderEditor();
      editorPreviewNow();
      toast('已还原为打开时的内容', 'ok');
    } catch (e) { /* 忽略 */ }
  });
  $('btnEditorPreview').addEventListener('click', editorPreviewNow);
  $('editorSearch').addEventListener('input', renderEditorLib);
  $('editorLibList').addEventListener('click', (e) => {
    const item = e.target.closest('.editor-lib-item');
    if (!item) return;
    addEditorStep(item.dataset.name);
  });
  $('editorPipeline').addEventListener('click', onEditorPipelineClick);
  $('editorModal').addEventListener('click', (e) => {
    if (e.target === $('editorModal')) closeEditor();
  });

  // 日志抽屉
  $('btnLog').addEventListener('click', () => $('logDrawer').classList.toggle('hidden'));
  $('btnLogClose').addEventListener('click', () => $('logDrawer').classList.add('hidden'));
  $('btnLogClear').addEventListener('click', () => { $('logBody').innerHTML = ''; });

  // 高级
  $('btnJsonValidate').addEventListener('click', async () => {
    const r = await api('/api/v1/template/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: $('advJson').value }),
    });
    if (r.valid) {
      $('jsonStatus').textContent = `JSON 合法，共 ${r.count} 个处理步骤`
        + (r.unknown && r.unknown.length ? `（未知处理器：${r.unknown.join('、')}）` : '');
    } else {
      $('jsonStatus').textContent = '错误：' + r.error;
    }
    $('jsonStatus').style.color = r.valid ? 'var(--ok)' : 'var(--err)';
  });
  $('btnJsonApply').addEventListener('click', async () => {
    await api('/api/v1/template', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ template_name: state.templateName, content: $('advJson').value }),
    });
    toast('模板「' + state.templateName + '」已更新', 'ok');
    log('已更新模板：' + state.templateName, 'ok');
    schedulePreview();
  });

  $('btnReset').addEventListener('click', () => location.reload());

  // 快捷键
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!$('lightbox').classList.contains('hidden')) { closeLightbox(); return; }
    if (!$('resultModal').classList.contains('hidden')) { $('resultModal').classList.add('hidden'); return; }
    if (!$('editorModal').classList.contains('hidden')) { closeEditor(); return; }
    $('logDrawer').classList.add('hidden');
  });

  // 拖拽
  setupDragDrop();

  // 对比滑块
  setupCompare();
}

function setupCollapse(headId, bodyId) {
  const head = $(headId);
  const body = $(bodyId);
  if (!head || !body) return;
  const key = 'cwp-collapse-' + headId;

  const setOpen = (open) => {
    body.classList.toggle('hidden', !open);
    head.classList.toggle('is-open', open);
  };

  // 默认收起；用户展开过就记住，下次打开保持
  let open = false;
  try { open = localStorage.getItem(key) === 'open'; } catch (e) { /* 忽略 */ }
  setOpen(open);

  head.addEventListener('click', () => {
    const nowOpen = body.classList.contains('hidden');
    setOpen(nowOpen);
    try { localStorage.setItem(key, nowOpen ? 'open' : 'closed'); } catch (e) { /* 忽略 */ }
  });
}

/* ---------------- 可拖动分隔条 ---------------- */
// 三栏布局的左/右栏宽度记在 localStorage 里（与主题记忆同一机制，零后端改动）。
const COL_MIN_SIDE = 240;   // 两侧栏最小宽度
const COL_MIN_MID = 300;    // 中间「待处理照片」最小宽度
const COL_SPLIT = 14;       // 单条分隔条宽度
const COL_DEFAULT = { settings: 350, preview: 420 };

function readCols() {
  try {
    const raw = JSON.parse(localStorage.getItem('cwp-layout-cols') || 'null');
    if (raw && typeof raw.settings === 'number' && typeof raw.preview === 'number') return raw;
  } catch (e) { /* 忽略 */ }
  return null;
}

function writeCols(cols) {
  try { localStorage.setItem('cwp-layout-cols', JSON.stringify(cols)); } catch (e) { /* 忽略 */ }
}

function setupSplitters() {
  const layout = document.querySelector('.layout');
  const leftPanel = document.querySelector('.panel-settings');
  const rightPanel = document.querySelector('.panel-preview');
  if (!layout || !leftPanel || !rightPanel) return;

  const apply = (cols) => {
    layout.style.setProperty('--col-settings', cols.settings + 'px');
    layout.style.setProperty('--col-preview', cols.preview + 'px');
  };
  const measure = () => ({
    settings: leftPanel.getBoundingClientRect().width,
    preview: rightPanel.getBoundingClientRect().width,
  });
  // 夹住范围：窗口变窄时优先压缩预览栏，再压缩左栏，保证中间栏始终可用
  const clamp = (cols) => {
    const cs = getComputedStyle(layout);
    const avail = layout.clientWidth
      - parseFloat(cs.paddingLeft || 0) - parseFloat(cs.paddingRight || 0)
      - COL_SPLIT * 2;
    let s = Math.max(COL_MIN_SIDE, cols.settings);
    let p = Math.max(COL_MIN_SIDE, cols.preview);
    let over = s + p + COL_MIN_MID - avail;
    if (over > 0) {
      const takeP = Math.min(over, p - COL_MIN_SIDE);
      p -= takeP;
      over -= takeP;
      if (over > 0) s = Math.max(COL_MIN_SIDE, s - over);
    }
    return { settings: s, preview: p };
  };
  const reset = () => { apply(clamp(COL_DEFAULT)); writeCols(measure()); };

  apply(clamp(readCols() || COL_DEFAULT));

  const bind = (el, axis) => {
    if (!el) return;
    let startX = 0;
    let base = null;

    el.addEventListener('pointerdown', (e) => {
      base = measure();
      startX = e.clientX;
      el.classList.add('is-dragging');
      try { el.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
      e.preventDefault();
    });
    el.addEventListener('pointermove', (e) => {
      if (!base) return;
      const dx = e.clientX - startX;
      // 右侧栏在分隔条右边：往左拖（dx<0）时它应该变宽，所以取负号
      apply(clamp(axis === 'settings'
        ? { ...base, settings: base.settings + dx }
        : { ...base, preview: base.preview - dx }));
    });
    const end = (e) => {
      if (!base) return;
      base = null;
      el.classList.remove('is-dragging');
      try { el.releasePointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
      writeCols(measure());
    };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);

    el.addEventListener('dblclick', reset);
    // 键盘也能调（聚焦分隔条后用左右方向键）
    el.addEventListener('keydown', (e) => {
      const step = e.shiftKey ? 40 : 12;
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      const delta = e.key === 'ArrowRight' ? step : -step;
      const cur = measure();
      apply(clamp(axis === 'settings'
        ? { ...cur, settings: cur.settings + delta }
        : { ...cur, preview: cur.preview - delta }));
      writeCols(measure());
      e.preventDefault();
    });
  };

  bind($('splitLeft'), 'settings');
  bind($('splitRight'), 'preview');
  window.addEventListener('resize', () => apply(clamp(measure())));
}

/* ---------------- 预览放大 ---------------- */
function setupPreviewZoom() {
  const stage = $('previewStage');
  if (!stage) return;

  // 预览区尺寸一变（拖分隔条、放大、窗口缩放）就重新贴合对比框
  if (window.ResizeObserver) {
    new ResizeObserver(() => fitCompareBox()).observe(stage);
  }

  stage.addEventListener('dblclick', (e) => {
    // 拖分割线时也会产生双击，别误触发放大
    if (e.target.closest('.compare-handle')) return;
    document.body.classList.toggle('preview-zoom');
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') document.body.classList.remove('preview-zoom');
  });
}

/* ---------------- 清空全部内容 ---------------- */
function clearAllFiles() {
  state.files = [];
  state.selected.clear();
  state.current = null;
  state.jobId = null;
  state.resultItems = [];
  state.resultOnlyOk = false;
  $('fileSearch').value = '';

  renderFiles();
  resetPreviewToEmpty();
  resetStats();
  $('runMessage').textContent = '就绪';
  $('btnViewResult').disabled = true;
  $('btnExportReport').disabled = true;
  $('btnResultOnlyOk').classList.remove('is-active');
  $('resultModal').classList.add('hidden');
  log('已清空照片列表与选择，回到初始状态', 'info');
}

function resetPreviewToEmpty() {
  state.previewGeo = null;
  $('previewEmpty').classList.remove('hidden');
  $('compareBox').classList.add('hidden');
  $('imgSingle').classList.add('hidden');
  $('previewLoading').classList.add('hidden');
  $('previewInfo').textContent = '—';
  ['imgAfter', 'imgBefore', 'imgSingle'].forEach((id) => {
    const node = $(id);
    if (node) node.removeAttribute('src');
  });
}


function bindRange(id, valId, fmt, onInput, onAfter) {
  const input = $(id);
  input.addEventListener('input', () => {
    $(valId).textContent = fmt(input.value);
    onInput(input.value);
    schedulePreview();
  });
  if (onAfter) input.addEventListener('change', onAfter);
}

/* ---------------- 预设方案 ---------------- */
function renderPresets() {
  const strip = $('presetStrip');
  strip.innerHTML = '';
  if (!state.presets.length) {
    strip.appendChild(el('span', 'hint', '还没有预设方案，点「存为预设」把当前设置保存下来。'));
    return;
  }
  state.presets.forEach((p) => {
    const card = el('button', 'preset-card' + (p.name === state.activePreset ? ' is-active' : ''));
    card.type = 'button';
    card.dataset.preset = p.name;
    card.title = (p.desc || '') + '\n（右键可删除）';
    card.appendChild(el('span', null, p.name));
    if (p.builtin) card.appendChild(el('span', 'preset-tag', '内置'));
    strip.appendChild(card);
  });
}

function currentPresetConfig() {
  return {
    mode: state.mode,
    template_name: state.templateName,
    crop_preset: state.crop,
    output_format: state.outputFormat,
    name_pattern: state.namePattern,
    quality: state.quality,
    long_edge: state.longEdge,
    preserve_exif: state.preserveExif,
    override_existed: state.override,
    simple: Object.assign({}, state.simple),
    logo: Object.assign({}, state.logo),
  };
}

async function applyPresetByName(name) {
  const preset = state.presets.find((p) => p.name === name);
  if (!preset) return;
  applyPresetConfig(preset.config || {});
  state.activePreset = name;
  renderPresets();
  await saveConfigNow();
  schedulePreview();
  log('已套用预设：' + name, 'ok');
  toast('已套用预设「' + name + '」', 'ok');
}

function applyPresetConfig(cfg) {
  if (cfg.mode) {
    state.mode = cfg.mode;
    [...$('modeSwitch').children].forEach((b) => b.classList.toggle('is-active', b.dataset.mode === state.mode));
    $('blockTemplate').classList.toggle('hidden', state.mode !== 'template');
    $('blockSimple').classList.toggle('hidden', state.mode !== 'simple');
  }
  if (cfg.template_name && state.templates.includes(cfg.template_name)) {
    state.templateName = cfg.template_name;
    renderTemplates();
    api('/api/v1/template/' + encodeURIComponent(cfg.template_name))
      .then((r) => { $('advJson').value = r.content; })
      .catch(() => { /* 忽略 */ });
  }
  if (cfg.crop_preset) { state.crop = cfg.crop_preset; syncCropChips(); }
  if (cfg.output_format) { state.outputFormat = cfg.output_format; $('outFormat').value = cfg.output_format; }
  if (cfg.name_pattern) { state.namePattern = cfg.name_pattern; $('outName').value = cfg.name_pattern; }
  if (cfg.quality != null) {
    state.quality = cfg.quality;
    $('outQuality').value = cfg.quality;
    $('outQualityVal').textContent = cfg.quality;
  }
  if (cfg.long_edge != null) { state.longEdge = cfg.long_edge; $('outLongEdge').value = cfg.long_edge; }
  if (cfg.preserve_exif != null) { state.preserveExif = !!cfg.preserve_exif; $('outExif').checked = state.preserveExif; }
  if (cfg.override_existed != null) { state.override = !!cfg.override_existed; $('outOverride').checked = state.override; }

  if (cfg.simple) {
    const s = cfg.simple;
    state.simple.text = s.text != null ? s.text : state.simple.text;
    state.simple.position = s.position || state.simple.position;
    if (s.height != null) state.simple.height = parseFloat(s.height);
    if (s.opacity != null) state.simple.opacity = Math.round(parseFloat(s.opacity) > 1
      ? parseFloat(s.opacity) : parseFloat(s.opacity) * 100);
    if (s.angle != null) state.simple.angle = parseInt(s.angle, 10);
    state.simple.color = s.color || state.simple.color;
    state.simple.strokeColor = s.strokeColor || s.stroke_color || state.simple.strokeColor;
    if (s.strokeWidth != null || s.stroke_width != null) {
      state.simple.strokeWidth = parseInt(s.strokeWidth != null ? s.strokeWidth : s.stroke_width, 10) || 0;
    }
    state.simple.tile = !!s.tile;
    syncSimpleUI();
  }

  if (cfg.logo) {
    state.logo = {
      logo_name: cfg.logo.logo_name != null ? cfg.logo.logo_name : state.logo.logo_name,
      logo_scale: cfg.logo.logo_scale != null ? cfg.logo.logo_scale : state.logo.logo_scale,
      logo_opacity: cfg.logo.logo_opacity != null ? cfg.logo.logo_opacity : state.logo.logo_opacity,
      logo_corner: cfg.logo.logo_corner || state.logo.logo_corner,
    };
    syncLogoUI();
  }
}

function syncSimpleUI() {
  const s = state.simple;
  $('swText').value = s.text || '';
  [...$('swPosition').children].forEach((b) => b.classList.toggle('is-active', b.dataset.pos === s.position));
  $('swHeight').value = s.height;
  $('swHeightVal').textContent = s.height + '%';
  $('swOpacity').value = s.opacity;
  $('swOpacityVal').textContent = s.opacity + '%';
  $('swAngle').value = s.angle;
  $('swAngleVal').textContent = s.angle + '°';
  $('swStrokeWidth').value = s.strokeWidth;
  $('swStrokeVal').textContent = s.strokeWidth + 'px';
  $('swColor').value = s.color;
  $('swStroke').value = s.strokeColor;
  $('swTile').checked = !!s.tile;
}

async function savePresetAs() {
  const name = prompt('预设方案名称：', state.activePreset || '我的方案');
  if (!name) return;
  const desc = prompt('一句话说明（可留空）：', '') || '';
  try {
    const r = await api('/api/v1/preset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim(), desc: desc.trim(), config: currentPresetConfig() }),
    });
    state.presets = r.presets || [];
    state.activePreset = r.preset ? r.preset.name : name.trim();
    renderPresets();
    toast('已保存预设「' + state.activePreset + '」', 'ok');
    log('已保存预设：' + state.activePreset, 'ok');
  } catch (e) {
    toast('保存预设失败：' + e.message, 'err');
  }
}

async function deletePreset(name) {
  if (!name) return;
  if (!confirm(`确定删除预设「${name}」吗？`)) return;
  try {
    const r = await api('/api/v1/preset/' + encodeURIComponent(name), { method: 'DELETE' });
    state.presets = r.presets || [];
    if (state.activePreset === name) state.activePreset = null;
    renderPresets();
    toast('已删除预设「' + name + '」', 'ok');
  } catch (e) {
    toast('删除失败：' + e.message, 'err');
  }
}

async function refreshPresets() {
  try {
    const r = await api('/api/v1/presets');
    state.presets = r.presets || [];
    renderPresets();
    toast('预设列表已刷新', 'ok');
  } catch (e) {
    toast('刷新失败：' + e.message, 'err');
  }
}

/* ---------------- 模板 ---------------- */
function renderTemplates() {
  const grid = $('tplGrid');
  grid.innerHTML = '';
  state.templates.forEach((name) => {
    const card = el('button', 'tpl-card' + (name === state.templateName ? ' is-active' : ''));
    card.type = 'button';
    card.appendChild(el('div', 'tpl-card-name', name));
    card.appendChild(el('div', 'tpl-card-desc', TEMPLATE_DESC[name] || '自定义模板'));
    card.addEventListener('click', () => selectTemplate(name));
    grid.appendChild(card);
  });
}

async function selectTemplate(name) {
  state.templateName = name;
  renderTemplates();
  try {
    const r = await api('/api/v1/template/' + encodeURIComponent(name));
    $('advJson').value = r.content;
  } catch (e) { /* 忽略 */ }
  await saveConfig();
  schedulePreview();
  log('已切换模板：' + name, 'ok');
}

async function refreshTemplates() {
  const r = await api('/api/v1/templates');
  state.templates = r.templates || [];
  renderTemplates();
  toast('模板列表已刷新', 'ok');
}

async function saveAsTemplate() {
  const name = prompt('新模板名称：', state.templateName + '-副本');
  if (!name) return;
  try {
    await api('/api/v1/template', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ template_name: name.trim(), content: $('advJson').value || '[]' }),
    });
    await refreshTemplates();
    await selectTemplate(name.trim());
    toast('已创建模板：' + name.trim(), 'ok');
  } catch (e) {
    toast('创建失败：' + e.message, 'err');
  }
}

function syncCropChips() {
  [...$('cropPresets').children].forEach((b) => {
    b.classList.toggle('is-active', b.dataset.crop === state.crop);
  });
}

/* ---------------- 自定义 Logo ---------------- */
function renderLogoSelect() {
  const sel = $('logoSelect');
  sel.innerHTML = '';
  const none = el('option', null, '（不叠加 Logo）');
  none.value = '';
  sel.appendChild(none);
  (state.logos || []).forEach((name) => {
    const opt = el('option', null, name);
    opt.value = name;
    sel.appendChild(opt);
  });
  sel.value = state.logo.logo_name || '';
}

function syncLogoUI() {
  renderLogoSelect();
  $('logoScale').value = state.logo.logo_scale;
  $('logoScaleVal').textContent = state.logo.logo_scale + '%';
  $('logoOpacity').value = state.logo.logo_opacity;
  $('logoOpacityVal').textContent = state.logo.logo_opacity + '%';
  [...$('logoCorner').children].forEach((b) => {
    b.classList.toggle('is-active', b.dataset.corner === state.logo.logo_corner);
  });
  const hint = $('logoHint');
  if (state.logo.logo_name) {
    hint.textContent = `已选择「${state.logo.logo_name}」，会叠加在每张成片上。`
      + `100% 大小约等于画面高度的 8%。`;
  } else {
    hint.textContent = '未选择 Logo 时不会叠加任何内容。上传的图片会存到数据目录的 logos 文件夹。';
  }
}

async function uploadLogo() {
  const input = $('logoFile');
  const file = input.files && input.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  try {
    const r = await api('/api/v1/logo/upload', { method: 'POST', body: form });
    state.logos = r.logos || [];
    state.logo.logo_name = r.logo_name || '';
    syncLogoUI();
    schedulePreview();
    toast('已上传 Logo：' + r.logo_name, 'ok');
    log('已上传 Logo：' + r.logo_name, 'ok');
  } catch (e) {
    toast('上传失败：' + e.message, 'err');
  } finally {
    input.value = '';
  }
}

async function deleteLogo() {
  const name = state.logo.logo_name;
  if (!name) {
    toast('请先在下拉框里选择要删除的 Logo', 'warn');
    return;
  }
  if (!confirm(`确定删除 Logo「${name}」吗？`)) return;
  try {
    const r = await api('/api/v1/logo/' + encodeURIComponent(name), { method: 'DELETE' });
    state.logos = r.logos || [];
    state.logo.logo_name = '';
    syncLogoUI();
    schedulePreview();
    toast('已删除 Logo', 'ok');
  } catch (e) {
    toast('删除失败：' + e.message, 'err');
  }
}

/* ---------------- 性能 ---------------- */
function syncPerfUI() {
  $('perfWorkers').value = String(state.perf.workers);
  $('perfCache').value = state.perf.exif_cache_size;
  $('perfCacheVal').textContent = state.perf.exif_cache_size;
  $('perfPreview').value = state.perf.preview_max_edge;
  $('perfPreviewVal').textContent = state.perf.preview_max_edge;

  const cores = state.perf.cores || 0;
  const auto = Math.max(1, Math.min(cores - 1, state.perf.max_workers || 8));
  const hint = $('perfWorkersHint');
  if (hint) {
    hint.textContent = cores
      ? `本机 ${cores} 个逻辑核；自动模式会使用 ${auto} 个进程（留 1 个给系统）。`
        + '照片少于 4 张时会自动串行，避免进程启动开销反而更慢。'
      : '';
  }
  const chip = $('chipPerf');
  if (chip) {
    chip.textContent = state.perf.workers === 'serial'
      ? '并行：串行'
      : `并行：${state.perf.workers === 'auto' ? '自动' : state.perf.workers + ' 进程'}`;
  }
}

async function runPerfProbe() {
  const status = $('perfStatus');
  status.textContent = '正在启动子进程自检…';
  status.style.color = 'var(--text-dim)';
  try {
    const r = await api('/api/v1/perf/probe', { method: 'POST' });
    if (r.ok) {
      status.textContent = `多进程可用：子进程已加载 ${r.processors} 个处理器`
        + `（本机 ${r.cores} 核，建议 ${r.suggested_workers} 进程并行）。`;
      status.style.color = 'var(--ok)';
      log(`性能自检通过：多进程可用，子进程加载 ${r.processors} 个处理器`, 'ok');
    } else {
      status.textContent = `多进程不可用：${r.error}。批处理会自动串行，功能不受影响。`;
      status.style.color = 'var(--warn)';
      log('性能自检：多进程不可用 —— ' + r.error, 'warn');
    }
    if (r.settings) {
      state.perf = Object.assign(state.perf, r.settings);
      syncPerfUI();
    }
  } catch (e) {
    status.textContent = '自检失败：' + e.message;
    status.style.color = 'var(--err)';
  }
}

async function clearExifCache() {
  try {
    const r = await api('/api/v1/perf/cache', { method: 'DELETE' });
    toast(r.message, 'ok');
    log(r.message, 'ok');
  } catch (e) {
    toast('清空失败：' + e.message, 'err');
  }
}

/* ---------------- 配置保存 ---------------- */
function configPayload() {
  return {
    input_folder: state.inputFolder,
    output_folder: state.outputFolder,
    override_existed: state.override,
    quality: state.quality,
    output_format: state.outputFormat,
    long_edge: state.longEdge,
    name_pattern: state.namePattern,
    preserve_exif: state.preserveExif,
    crop_preset: state.crop,
    template_name: state.templateName,
    theme: state.theme,
    logo_name: state.logo.logo_name,
    logo_scale: state.logo.logo_scale,
    logo_opacity: state.logo.logo_opacity,
    logo_corner: state.logo.logo_corner,
    workers: state.perf.workers,
    exif_cache_size: state.perf.exif_cache_size,
    preview_max_edge: state.perf.preview_max_edge,
  };
}

async function postConfig() {
  try {
    await api('/api/v1/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(configPayload()),
    });
  } catch (e) {
    log('保存配置失败：' + e.message, 'err');
  }
}

// 防抖版本：用于滑杆、输入框等高频改动
const saveConfig = debounce(postConfig, 400);

// 立即版本：目录这类「改完马上就要用」的项必须真正等它落盘。
// 之前这里是防抖的，导致「选了输出目录 → 立刻点开始处理」时
// 后端还拿着旧目录，成片被写到别处，用户就以为程序没输出。
async function saveConfigNow() {
  await postConfig();
}

function normPath(p) {
  return (p || '').replace(/[\\/]+$/, '').replace(/\//g, '\\').toLowerCase();
}

/* ---------------- 同目录保护（前置提示） ---------------- */
function updateOutFolderNote() {
  const box = $('outFolderNote');
  const outEl = $('outFolder');
  const same = state.inputFolder && state.outputFolder
    && normPath(state.inputFolder) === normPath(state.outputFolder);
  if (same) {
    $('outFolderNoteText').textContent =
      '输出目录和输入目录相同：程序会自动输出到「输入目录\\水印输出」，不会覆盖原图。'
      + '也可以点右边按钮把目录直接改过去。';
    box.classList.remove('hidden');
    outEl.classList.add('is-error');
  } else {
    box.classList.add('hidden');
    outEl.classList.remove('is-error');
  }
}

async function fixOutputFolder() {
  if (!state.inputFolder) {
    toast('请先选择输入目录', 'warn');
    return;
  }
  const fixed = state.inputFolder.replace(/[\\/]+$/, '') + '\\水印输出';
  state.outputFolder = fixed;
  $('outFolder').value = fixed;
  updateOutFolderNote();
  await saveConfigNow();
  toast('输出目录已改为 ' + fixed, 'ok');
  log('输出目录已修正为：' + fixed, 'ok');
}

/* ---------------- 文件树 ---------------- */
function flatten(nodes, out) {
  (nodes || []).forEach((n) => {
    if (n.is_file) out.push(n);
    else flatten(n.children, out);
  });
  return out;
}

async function loadTree() {
  try {
    const tree = await api('/api/v1/file/tree');
    state.files = flatten(tree.input_files, []);
    const valid = new Set(state.files.map((f) => f.value));
    [...state.selected].forEach((p) => { if (!valid.has(p)) state.selected.delete(p); });
    // 自动把第一张照片设为预览对象，省去一次点击
    if (!state.current || !valid.has(state.current)) {
      state.current = state.files.length ? state.files[0].value : null;
    }
    renderFiles();
    if (state.current) updatePreviewNow();
    log(`扫描完成，共 ${state.files.length} 张照片`, 'ok');
  } catch (e) {
    toast('扫描目录失败：' + e.message, 'err');
  }
}

function visibleFiles() {
  const kw = ($('fileSearch').value || '').trim().toLowerCase();
  if (!kw) return state.files;
  return state.files.filter((f) => f.label.toLowerCase().includes(kw));
}

function renderFiles() {
  const grid = $('fileGrid');
  const list = visibleFiles();
  grid.innerHTML = '';

  $('selCounter').textContent = `已选 ${state.selected.size} / ${state.files.length}`;

  if (!list.length) {
    grid.appendChild(el('div', 'empty-tip',
      state.files.length ? '没有匹配的文件' : '输入目录里还没有照片'));
    return;
  }

  list.forEach((file) => {
    const card = el('div', 'file-card');
    if (state.selected.has(file.value)) card.classList.add('is-sel');
    if (state.current === file.value) card.classList.add('is-current');

    const img = el('img', 'file-thumb');
    img.loading = 'lazy';
    img.src = '/api/v1/thumb?path=' + encodeURIComponent(file.value) + '&size=300';
    img.alt = file.label;
    img.onerror = () => {
      const ph = el('div', 'file-thumb-ph', '无法预览');
      img.replaceWith(ph);
    };
    card.appendChild(img);

    const tick = el('div', 'tick', '✓');
    card.appendChild(tick);

    const ext = (file.label.split('.').pop() || '').toUpperCase();
    card.appendChild(el('div', 'file-badge', ext));

    const meta = el('div', 'file-meta');
    const nm = el('div', 'file-name', file.label);
    nm.title = file.value;
    meta.appendChild(nm);
    card.appendChild(meta);

    card.addEventListener('click', () => {
      if (state.selected.has(file.value)) state.selected.delete(file.value);
      else state.selected.add(file.value);
      state.current = file.value;
      renderFiles();
      updatePreviewNow();
    });

    grid.appendChild(card);
  });
}

/* ---------------- 拖拽 ---------------- */
// 桌面外壳（WebView2）里，网页是拿不到拖拽文件真实路径的，所以真实路径
// 由 Python 侧通过 pywebview 的 DOM drop 监听取到，再回调这里。
// 这里只负责：高亮提示 + 把路径交给后端解析成「输入目录 + 待选文件」。
function setupDragDrop() {
  const zone = $('dropzone');
  ['dragenter', 'dragover'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('is-over'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove('is-over'); }));

  // 浏览器里直接打开（非桌面窗口）时的降级处理
  zone.addEventListener('drop', async (e) => {
    if (state.nativeDrop) return;           // 桌面外壳会走 __onNativeDrop
    const items = e.dataTransfer && e.dataTransfer.files;
    if (!items || !items.length) return;
    const first = items[0];
    const path = first.path || first.fullPath || '';
    if (!path) {
      toast('当前窗口读不到拖拽文件的真实路径，请点「浏览」选择文件夹', 'warn');
      return;
    }
    await applyDroppedPaths([path]);
  });
}

// 由桌面外壳注入调用，paths 是真实的绝对路径数组
window.__onNativeDrop = async (paths) => {
  if (!Array.isArray(paths) || !paths.length) return;
  await applyDroppedPaths(paths);
};

async function applyDroppedPaths(paths) {
  try {
    const r = await api('/api/v1/drop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    });
    state.inputFolder = r.folder;
    $('inFolder').value = r.folder;
    updateOutFolderNote();
    await loadTree();

    if (r.selected && r.selected.length) {
      r.selected.forEach((p) => state.selected.add(p));
      state.current = r.selected[0];
      renderFiles();
      updatePreviewNow();
      toast(`已载入 ${r.selected.length} 张照片`, 'ok');
    } else {
      toast('输入目录已更新：' + r.folder, 'ok');
    }
    if (r.ignored && r.ignored.length) {
      log(`已忽略 ${r.ignored.length} 个不支持的文件：${r.ignored.slice(0, 5).join('、')}`, 'warn');
    }
  } catch (e) {
    toast('拖拽处理失败：' + e.message, 'err');
  }
}

/* ---------------- 目录选择 ---------------- */
async function pickFolder(target) {
  try {
    const r = await api('/api/v1/pick_folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initial: target === 'inFolder' ? state.inputFolder : state.outputFolder }),
    });
    if (!r.folder) return;
    if (target === 'inFolder') {
      state.inputFolder = r.folder;
      $('inFolder').value = r.folder;
      updateOutFolderNote();
      await saveConfigNow();
      await loadTree();
    } else {
      state.outputFolder = r.folder;
      $('outFolder').value = r.folder;
      updateOutFolderNote();
      await saveConfigNow();
    }
  } catch (e) {
    toast('选择目录失败：' + e.message, 'err');
  }
}

async function openOutputFolder() {
  const target = state.outputRoot || state.outputFolder;
  if (!target) {
    toast('还没有设置输出目录', 'warn');
    return;
  }
  try {
    await api('/api/v1/open_folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: target }),
    });
  } catch (e) {
    toast('打开目录失败：' + e.message, 'err');
  }
}

async function revealFile(path) {
  try {
    await api('/api/v1/reveal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
  } catch (e) {
    toast('打开位置失败：' + e.message, 'err');
  }
}

async function saveAs(path) {
  try {
    const r = await api('/api/v1/save_as', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    if (r.cancelled) return;
    toast(r.message || '已保存', 'ok');
  } catch (e) {
    toast('另存为失败：' + e.message, 'err');
  }
}

function exportReport() {
  if (state.jobId) window.location.href = '/api/v1/process/report/' + state.jobId;
  else toast('还没有可导出的任务', 'warn');
}

/* ---------------- 预览 ---------------- */
function buildPreviewUrl() {
  const params = new URLSearchParams();
  if (state.current) params.set('path', state.current);
  params.set('mode', state.mode);
  params.set('crop_preset', state.crop);
  params.set('logo_name', state.logo.logo_name || '');
  params.set('logo_scale', String(state.logo.logo_scale));
  params.set('logo_opacity', String(state.logo.logo_opacity));
  params.set('logo_corner', state.logo.logo_corner);
  if (state.mode === 'template') {
    params.set('template_name', state.templateName);
  } else {
    const s = state.simple;
    params.set('text', s.text || '');
    params.set('position', s.position);
    params.set('color', s.color);
    params.set('opacity', String(s.opacity / 100));
    params.set('angle', String(s.angle));
    params.set('height', String(Math.round(s.height * 10)));
    params.set('stroke_color', s.strokeColor);
    params.set('stroke_width', String(s.strokeWidth));
    params.set('tile', s.tile ? 'true' : 'false');
  }
  return '/api/v1/preview?' + params.toString();
}

const schedulePreview = debounce(updatePreviewNow, 260);

function updatePreviewNow() {
  const loading = $('previewLoading');
  loading.classList.remove('hidden');

  const after = $('imgAfter');
  const single = $('imgSingle');
  const stamp = Date.now();
  const url = buildPreviewUrl() + '&_=' + stamp;

  const onDone = () => {
    loading.classList.add('hidden');
    $('previewEmpty').classList.add('hidden');
    $('compareBox').classList.remove('hidden');
    $('imgSingle').classList.remove('hidden');
    applyPreviewMode();
    const name = state.current ? baseName(state.current) : '样张';
    const parts = [name];
    parts.push(state.mode === 'template' ? '模板：' + state.templateName : '自定义文字水印');
    if (state.crop !== 'none') parts.push('裁切 ' + state.crop);
    if (state.logo.logo_name) parts.push('Logo：' + state.logo.logo_name);
    $('previewInfo').textContent = parts.join(' · ');
  };
  const onFail = () => {
    loading.classList.add('hidden');
    $('previewEmpty').classList.remove('hidden');
    $('compareBox').classList.add('hidden');
    $('imgSingle').classList.add('hidden');
  };

  const img = new Image();
  img.onload = () => {
    after.src = url;
    single.src = url;
    onDone();
    refreshPreviewGeo(url);
  };
  img.onerror = onFail;
  img.src = url;

  if (state.current) {
    $('imgBefore').src = '/api/v1/file?path=' + encodeURIComponent(state.current)
      + '&max=' + (state.perf.preview_max_edge || 1400) + '&_=' + stamp;
  }
}

/* ---------------- 对比模式对齐 ---------------- */
// 成片画布和原图尺寸天生不同（模板的画框/留白/投影会把画布撑大），所以要知道
// 「照片在成片里的矩形」，才能把原图摆到与成片完全重合的位置。
// 探针算不出来（文字水印、拼接类模板）时退回整幅 contain，至少分割线左右分布正确。
async function refreshPreviewGeo(url) {
  const metaUrl = url.replace('/api/v1/preview?', '/api/v1/preview/meta?');
  try {
    const res = await fetch(metaUrl);
    state.previewGeo = res.ok ? await res.json() : null;
  } catch (e) {
    state.previewGeo = null;
  }
  applyCompareGeometry();
}

function applyCompareGeometry() {
  const before = $('imgBefore');
  if (!before) return;
  fitCompareBox();
  const geo = state.previewGeo;
  const rect = geo && geo.photo_rect;
  const W = geo && geo.width;
  const H = geo && geo.height;

  if (!rect || !W || !H) {
    before.style.left = '0%';
    before.style.top = '0%';
    before.style.width = '100%';
    before.style.height = '100%';
    before.style.objectFit = 'contain';
    return;
  }
  const pct = (v, total) => (v / total * 100).toFixed(4) + '%';
  before.style.left = pct(rect[0], W);
  before.style.top = pct(rect[1], H);
  before.style.width = pct(rect[2], W);
  before.style.height = pct(rect[3], H);
  // cover：矩形比例与原图不一致（模板里带裁切）时按居中裁切对齐，与本程序行为一致
  before.style.objectFit = 'cover';
}

// 把对比容器精确调成「效果图等比缩放后」的像素尺寸
function fitCompareBox() {
  const box = $('compareBox');
  const stage = $('previewStage');
  if (!box || !stage) return;
  const geo = state.previewGeo;
  const nw = geo && geo.width;
  const nh = geo && geo.height;
  if (!nw || !nh || box.classList.contains('hidden')) return;
  const scale = Math.min(stage.clientWidth / nw, stage.clientHeight / nh);
  if (!isFinite(scale) || scale <= 0) return;
  const w = Math.max(1, Math.round(nw * scale));
  const h = Math.max(1, Math.round(nh * scale));
  if (box.style.width !== w + 'px') box.style.width = w + 'px';
  if (box.style.height !== h + 'px') box.style.height = h + 'px';
}

function applyPreviewMode() {
  const compare = $('compareBox');
  const single = $('imgSingle');
  if (state.previewMode === 'compare') {
    compare.classList.remove('hidden');
    single.classList.add('hidden');
    applyCompareGeometry();
  } else if (state.previewMode === 'after') {
    compare.classList.add('hidden');
    single.classList.remove('hidden');
  } else {
    compare.classList.add('hidden');
    single.classList.remove('hidden');
    single.src = $('imgBefore').src || single.src;
  }
}

/* ---------------- 对比滑块 ---------------- */
function setupCompare() {
  const handle = $('compareHandle');
  const before = $('compareBefore');
  const stage = $('previewStage');
  let dragging = false;

  // 分割线按「图片实际渲染框」算百分比，而不是整个预览舞台 ——
  // 舞台比图片大（图片是等比缩放后居中放置的），用舞台算会让分割线和图片错位。
  const imageBox = () => {
    const rect = $('imgAfter').getBoundingClientRect();
    return rect.width > 0 ? rect : stage.getBoundingClientRect();
  };

  const move = (clientX) => {
    const rect = imageBox();
    let pct = ((clientX - rect.left) / rect.width) * 100;
    pct = Math.max(0, Math.min(100, pct));
    before.style.clipPath = `inset(0 ${100 - pct}% 0 0)`;
    handle.style.left = pct + '%';
  };

  handle.addEventListener('mousedown', (e) => { dragging = true; e.preventDefault(); });
  window.addEventListener('mouseup', () => { dragging = false; });
  window.addEventListener('mousemove', (e) => { if (dragging) move(e.clientX); });
  stage.addEventListener('click', (e) => {
    if (state.previewMode === 'compare' && !e.target.closest('.compare-handle')) move(e.clientX);
  });
}

/* ---------------- 批量处理 ---------------- */
async function startProcess() {
  const files = [...state.selected];
  if (!files.length) {
    toast('请先勾选要处理的照片', 'warn');
    return;
  }

  const payload = {
    files,
    mode: state.mode,
    template_name: state.templateName,
    crop_preset: state.crop,
    output_format: state.outputFormat,
    name_pattern: state.namePattern,
    quality: state.quality,
    long_edge: state.longEdge,
    preserve_exif: state.preserveExif,
    override_existed: state.override,
    workers: state.perf.workers,
    logo: state.logo,
    // 目录随请求一起发，任务不再依赖「配置保存」的时序
    input_folder: state.inputFolder,
    output_folder: state.outputFolder,
    simple: {
      text: state.simple.text,
      position: state.simple.position,
      color: state.simple.color,
      opacity: state.simple.opacity / 100,
      angle: state.simple.angle,
      height: Math.round(state.simple.height * 10),
      stroke_color: state.simple.strokeColor,
      stroke_width: state.simple.strokeWidth,
      tile: state.simple.tile,
    },
  };

  try {
    const r = await api('/api/v1/process/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.jobId = r.job_id;
    state.running = true;
    setRunning(true);
    resetStats();
    $('btnViewResult').disabled = true;

    // 后端可能把「输入=输出」自动改成子目录，这里同步显示，避免用户困惑
    if (r.output_root) {
      state.outputRoot = r.output_root;
      state.outputFolder = r.output_root;
      $('outFolder').value = r.output_root;
      updateOutFolderNote();
    }
    if (r.note) {
      toast(r.note, 'warn');
      log(r.note, 'warn');
    }
    log(`开始处理 ${r.total} 张照片（任务 ${r.job_id}）→ ${r.output_root || state.outputFolder}`, 'info');
    listenJob(r.job_id);
  } catch (e) {
    toast('启动失败：' + e.message, 'err');
    log('启动失败：' + e.message, 'err');
  }
}

function listenJob(jobId) {
  const source = new EventSource('/api/v1/process/stream/' + jobId);

  source.addEventListener('progress', (ev) => {
    const d = JSON.parse(ev.data);
    updateStats(d);
  });

  source.addEventListener('complete', (ev) => {
    const d = JSON.parse(ev.data);
    updateStats(d);
    source.close();
    state.running = false;
    setRunning(false);
    $('btnExportReport').disabled = false;
    $('btnViewResult').disabled = false;
    if (d.output_root) state.outputRoot = d.output_root;

    // 成功 0 却提示「完成」最容易被误读，这里按结果给出更明确的口径
    let type = 'ok';
    if (d.status === 'cancelled') type = 'warn';
    else if (d.failure) type = 'warn';
    else if (!d.success) type = 'warn';
    toast(d.message, type);
    log(d.message, type === 'ok' ? 'ok' : 'warn');
    if (d.engine === 'pool' || d.engine === 'pool-fallback') {
      log(`执行引擎：${d.engine === 'pool' ? '多进程' : '多进程失败已回退串行'}（${d.workers} 进程）`, 'info');
    }
    if (d.errors && d.errors.length) {
      d.errors.slice(0, 30).forEach((it) => log(`失败 · ${it.file}：${it.error}`, 'err'));
    }
    if (d.skipped && !d.success) {
      log('提示：跳过的原因是目标文件已存在。想覆盖原结果请勾选「覆盖已存在的输出文件」。', 'warn');
    }
    loadTree();

    // 有产出就自动把结果面板打开，让用户立刻确认「到底出了什么」
    if (d.processed > 0) {
      setTimeout(() => openResultPanel(), 260);
    }
  });

  source.onerror = () => {
    source.close();
    if (state.running) {
      state.running = false;
      setRunning(false);
      log('进度连接中断，请刷新查看结果', 'warn');
    }
  };
}

async function cancelProcess() {
  if (!state.jobId) return;
  try {
    await api('/api/v1/process/cancel/' + state.jobId, { method: 'POST' });
    log('已发送取消请求，正在结束当前文件…', 'warn');
  } catch (e) {
    toast('取消失败：' + e.message, 'err');
  }
}

function resetStats() {
  $('statDone').textContent = '0';
  $('statOk').textContent = '0';
  $('statSkip').textContent = '0';
  $('statFail').textContent = '0';
  $('progressBar').style.width = '0%';
  $('runMessage').textContent = '准备中…';
}

function updateStats(d) {
  $('statDone').textContent = d.processed;
  $('statOk').textContent = d.success;
  $('statSkip').textContent = d.skipped;
  $('statFail').textContent = d.failure;
  $('progressBar').style.width = (d.percent || 0) + '%';
  let msg = d.message || '';
  if (d.status === 'running' && d.eta != null && d.processed > 0) {
    msg += ` · 剩余约 ${formatDuration(d.eta)}`;
  }
  $('runMessage').textContent = msg;
}

function formatDuration(sec) {
  if (sec < 60) return sec + ' 秒';
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m} 分 ${s} 秒`;
}

function setRunning(running) {
  $('btnStart').classList.toggle('hidden', running);
  $('btnCancel').classList.toggle('hidden', !running);
  $('btnStart').disabled = running;
}

/* ---------------- 处理结果确认面板 ---------------- */
async function openResultPanel() {
  if (!state.jobId) {
    toast('还没有处理记录', 'warn');
    return;
  }
  $('resultModal').classList.remove('hidden');
  $('resultSummary').textContent = '加载中…';
  try {
    const r = await api('/api/v1/process/result/' + state.jobId);
    state.resultItems = r.items || [];
    state.resultSummary = r;
    const engine = r.engine === 'pool' ? `${r.workers} 进程并行`
      : (r.engine === 'pool-fallback' ? '多进程失败已回退串行' : '串行');
    $('resultSummary').textContent =
      `共 ${r.total} 张 · 成功 ${r.success} · 跳过 ${r.skipped} · 失败 ${r.failure} · ${engine}`
      + (r.output_root ? ` · ${r.output_root}` : '');
    renderResultPanel();
  } catch (e) {
    $('resultSummary').textContent = '读取失败：' + e.message;
  }
}

function renderResultPanel() {
  const grid = $('resultGrid');
  grid.innerHTML = '';
  const items = state.resultOnlyOk
    ? state.resultItems.filter((i) => i.status === 'success')
    : state.resultItems;

  $('resultEmpty').classList.toggle('hidden', items.length > 0);
  if (!items.length) {
    $('resultEmpty').textContent = state.resultOnlyOk
      ? '没有成功产出的文件。'
      : '本次任务没有产出文件。';
    return;
  }

  items.forEach((item) => {
    const card = el('div', 'result-card');
    const badge = el('span', 'result-status ' + item.status,
      { success: '成功', skipped: '跳过', failure: '失败' }[item.status] || item.status);
    card.appendChild(badge);

    if (item.status === 'success' && item.exists) {
      const img = el('img', 'result-thumb');
      img.loading = 'lazy';
      img.src = '/api/v1/thumb?path=' + encodeURIComponent(item.output) + '&size=360';
      img.alt = item.output_name || '';
      img.onerror = () => {
        img.replaceWith(el('div', 'result-thumb-ph', '无法预览'));
      };
      card.appendChild(img);
    } else {
      card.classList.add('is-bad');
      card.appendChild(el('div', 'result-thumb-ph',
        item.error ? item.error : '没有输出文件'));
    }

    const meta = el('div', 'result-meta');
    const nm = el('div', 'result-name', item.output_name || item.file || '—');
    nm.title = item.output || item.file || '';
    meta.appendChild(nm);
    const sub = el('div', 'result-sub',
      item.status === 'success'
        ? (formatBytes(item.size) + (item.source ? ' · 原图 ' + baseName(item.source) : ''))
        : (item.error || ''));
    sub.title = item.error || '';
    meta.appendChild(sub);
    card.appendChild(meta);

    if (item.status === 'success' && item.exists) {
      card.addEventListener('click', () => openLightbox(item.output, item.source));
    }
    grid.appendChild(card);
  });
}

/* ---------------- 大图查看 ---------------- */
function openLightbox(outputPath, sourcePath) {
  $('lightboxImg').src = '/api/v1/file?path=' + encodeURIComponent(outputPath)
    + '&max=2200&_=' + Date.now();
  $('lightboxImg').dataset.path = outputPath;
  $('lightboxTitle').textContent = baseName(outputPath) + (sourcePath ? '　←　原图：' + baseName(sourcePath) : '');

  const before = $('lightboxBefore');
  if (sourcePath) {
    before.src = '/api/v1/file?path=' + encodeURIComponent(sourcePath)
      + '&max=2200&_=' + Date.now();
    before.classList.add('hidden');
    $('btnLbCompare').disabled = false;
    $('btnLbCompare').classList.remove('is-active');
  } else {
    before.src = '';
    before.classList.add('hidden');
    $('btnLbCompare').disabled = true;
  }
  $('lightbox').classList.remove('hidden');
}

function closeLightbox() {
  $('lightbox').classList.add('hidden');
  // 用 removeAttribute 而不是 src=''：后者会被解析成当前页面 URL 并多发一次请求
  $('lightboxImg').removeAttribute('src');
  $('lightboxImg').removeAttribute('data-path');
  $('lightboxBefore').removeAttribute('src');
}

/* ---------------- 模板可视化编辑器 ---------------- */
async function openEditor(contentJson) {
  try {
    if (!state.processors.length) {
      const r = await api('/api/v1/processors');
      state.processors = r.processors || [];
      state.categories = r.categories || {};
    }
  } catch (e) {
    toast('读取处理器列表失败：' + e.message, 'err');
    return;
  }

  let parsed;
  const raw = contentJson || $('advJson').value || '[]';
  try {
    parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) throw new Error('顶层必须是数组');
  } catch (e) {
    toast('当前模板 JSON 无法解析，无法进入可视化编辑：' + e.message, 'err');
    return;
  }

  state.pipeline = parsed;
  state.editorBaseline = JSON.stringify(parsed, null, 2);
  state.editorStep = parsed.length ? 0 : -1;
  $('editorModal').classList.remove('hidden');
  $('editorSub').textContent = `当前模板：${state.templateName}`;
  $('editorSearch').value = '';
  renderEditor();
  editorPreviewNow();
  log('已打开可视化编辑器，共 ' + parsed.length + ' 个步骤', 'info');
}

function closeEditor() {
  $('editorModal').classList.add('hidden');
}

function schemaOf(name) {
  return state.processors.find((p) => p.name === name) || null;
}

function catMeta(cat) {
  return state.categories[cat] || { label: cat || '未知', color: 'var(--accent)', desc: '' };
}

function renderEditorLib() {
  const list = $('editorLibList');
  const kw = ($('editorSearch').value || '').trim().toLowerCase();
  list.innerHTML = '';

  const groups = {};
  state.processors.forEach((p) => {
    if (kw && !(p.name.toLowerCase().includes(kw)
      || (p.label || '').toLowerCase().includes(kw))) return;
    const cat = p.category || 'filter';
    (groups[cat] = groups[cat] || []).push(p);
  });

  const order = ['generator', 'filter', 'merger'];
  const keys = Object.keys(groups).sort((a, b) => order.indexOf(a) - order.indexOf(b));

  if (!keys.length) {
    list.appendChild(el('p', 'hint', '没有匹配的处理器'));
    return;
  }

  keys.forEach((cat) => {
    const group = el('div', 'editor-lib-group');
    group.appendChild(el('div', 'editor-lib-group-title', catMeta(cat).label));
    groups[cat].forEach((p) => {
      const item = el('button', 'editor-lib-item' + (p.available ? '' : ' is-unavailable'));
      item.type = 'button';
      item.dataset.name = p.name;
      item.title = p.desc || '';
      item.appendChild(el('span', null, p.label || p.name));
      item.appendChild(el('span', 'lib-cat', catMeta(p.category).label));
      if (!p.available) item.title = '当前构建未包含该处理器';
      group.appendChild(item);
    });
    list.appendChild(group);
  });
}

function renderEditor() {
  renderEditorLib();

  const box = $('editorPipeline');
  box.innerHTML = '';
  $('editorStepCount').textContent = state.pipeline.length + ' 步';

  if (!state.pipeline.length) {
    box.appendChild(el('div', 'editor-empty',
      '流程为空。点左边的处理器即可添加步骤。'));
  }

  state.pipeline.forEach((step, idx) => {
    const name = step.processor_name || '（未命名）';
    const schema = schemaOf(name);
    const cat = schema ? schema.category : 'filter';
    const meta = catMeta(cat);

    const card = el('div', 'step-card' + (idx === state.editorStep ? ' is-active' : ''));
    card.dataset.index = String(idx);
    card.style.setProperty('--cat-color', meta.color);

    const top = el('div', 'step-card-top');
    top.appendChild(el('span', 'step-index', String(idx + 1)));
    top.appendChild(el('span', 'step-name', schema ? schema.label : name));
    top.appendChild(el('span', 'step-cat', meta.label));

    const actions = el('div', 'step-actions');
    const mk = (label, act, title, disabled) => {
      const b = el('button', 'step-btn', label);
      b.type = 'button';
      b.dataset.act = act;
      b.dataset.index = String(idx);
      b.title = title;
      if (disabled) b.disabled = true;
      return b;
    };
    actions.appendChild(mk('↑', 'up', '上移（更早执行）', idx === 0));
    actions.appendChild(mk('↓', 'down', '下移（更晚执行）', idx === state.pipeline.length - 1));
    actions.appendChild(mk('复制', 'dup', '复制一份'));
    actions.appendChild(mk('删除', 'del', '删除此步骤'));
    top.appendChild(actions);
    card.appendChild(top);

    if (schema && schema.desc) card.appendChild(el('div', 'step-desc', schema.desc));
    card.appendChild(el('div', 'step-summary', summarizeStep(step, schema)));

    box.appendChild(card);
  });

  renderEditorParams();
}

function summarizeStep(step, schema) {
  const keys = Object.keys(step).filter((k) => k !== 'processor_name' && k !== 'save_buffer');
  if (!keys.length) return '（使用默认参数）';
  const parts = keys.slice(0, 3).map((k) => {
    let v = step[k];
    if (v && typeof v === 'object') v = Array.isArray(v) ? `[${v.length} 项]` : '{…}';
    v = String(v);
    if (v.length > 22) v = v.slice(0, 22) + '…';
    return `${k}=${v}`;
  });
  return parts.join('  ') + (keys.length > 3 ? `  …共 ${keys.length} 项` : '');
}

function onEditorPipelineClick(e) {
  const btn = e.target.closest('.step-btn');
  const card = e.target.closest('.step-card');
  if (!card) return;
  const idx = parseInt(card.dataset.index, 10);

  if (!btn) {
    state.editorStep = idx;
    renderEditor();
    return;
  }
  e.stopPropagation();
  const act = btn.dataset.act;
  if (act === 'up' && idx > 0) {
    const tmp = state.pipeline[idx - 1];
    state.pipeline[idx - 1] = state.pipeline[idx];
    state.pipeline[idx] = tmp;
    state.editorStep = idx - 1;
  } else if (act === 'down' && idx < state.pipeline.length - 1) {
    const tmp = state.pipeline[idx + 1];
    state.pipeline[idx + 1] = state.pipeline[idx];
    state.pipeline[idx] = tmp;
    state.editorStep = idx + 1;
  } else if (act === 'dup') {
    state.pipeline.splice(idx + 1, 0, JSON.parse(JSON.stringify(state.pipeline[idx])));
    state.editorStep = idx + 1;
  } else if (act === 'del') {
    state.pipeline.splice(idx, 1);
    state.editorStep = Math.min(idx, state.pipeline.length - 1);
  }
  renderEditor();
  editorPreviewDebounced();
}

function addEditorStep(name) {
  const schema = schemaOf(name);
  const step = { processor_name: name };
  if (schema) {
    (schema.params || []).forEach((p) => {
      if (p.advanced) return;
      if (p.default !== undefined && p.default !== '' && p.default !== null) {
        step[p.key] = p.default;
      }
    });
  }
  state.pipeline.push(step);
  state.editorStep = state.pipeline.length - 1;
  renderEditor();
  editorPreviewDebounced();
}

/* ---- 参数表单 ---- */
function renderEditorParams() {
  const body = $('editorParamBody');
  body.innerHTML = '';
  const idx = state.editorStep;
  const title = $('editorParamTitle');

  if (idx < 0 || !state.pipeline[idx]) {
    title.textContent = '参数';
    body.appendChild(el('p', 'hint', '在中间流程里点一个步骤，这里就会显示它的参数。'));
    return;
  }

  const step = state.pipeline[idx];
  const name = step.processor_name;
  const schema = schemaOf(name);
  title.textContent = `步骤 ${idx + 1}`;

  body.appendChild(el('p', 'editor-param-title', (schema && schema.label) || name));
  body.appendChild(el('p', 'editor-param-desc',
    (schema && schema.desc) || '未登记该处理器，参数请用下方 JSON 直接编辑。'));

  const acts = el('div', 'editor-param-actions');
  const btnRaw = el('button', 'btn btn-mini', '编辑原始 JSON');
  btnRaw.type = 'button';
  acts.appendChild(btnRaw);
  body.appendChild(acts);

  const rawBox = el('textarea', 'input code hidden');
  rawBox.rows = 10;
  rawBox.spellcheck = false;
  rawBox.value = JSON.stringify(step, null, 2);
  body.appendChild(rawBox);

  const form = el('div');
  body.appendChild(form);

  btnRaw.addEventListener('click', () => {
    rawBox.classList.toggle('hidden');
    form.classList.toggle('hidden');
    btnRaw.classList.toggle('is-active');
  });

  rawBox.addEventListener('change', () => {
    try {
      const parsed = JSON.parse(rawBox.value);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('必须是 JSON 对象');
      }
      parsed.processor_name = name;
      state.pipeline[idx] = parsed;
      renderEditor();
      editorPreviewDebounced();
      $('editorStatus').textContent = '已应用原始 JSON。';
    } catch (e) {
      $('editorStatus').textContent = 'JSON 解析失败：' + e.message;
    }
  });

  if (!schema || !(schema.params || []).length) {
    form.appendChild(el('p', 'hint', '该处理器没有可编辑的参数元数据，请用「编辑原始 JSON」。'));
    return;
  }

  const setValue = (key, value) => {
    if (value === undefined || value === '' || value === null) delete state.pipeline[idx][key];
    else state.pipeline[idx][key] = value;
    editorPreviewDebounced();
  };

  schema.params.forEach((param) => {
    form.appendChild(buildParamField(param, step, setValue));
  });
}

function buildParamField(param, step, setValue) {
  const wrap = el('div', 'field');
  const hasValue = Object.prototype.hasOwnProperty.call(step, param.key);
  const current = hasValue ? step[param.key] : undefined;

  const label = el('label');
  label.appendChild(el('span', null, param.label || param.key));
  if (param.tokens) label.appendChild(el('span', 'editor-token-hint', '支持 {{...}}'));
  wrap.appendChild(label);

  const type = param.type || 'text';

  if (type === 'bool') {
    const lab = el('label', 'check');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = current === undefined ? !!param.default : !!current;
    lab.appendChild(cb);
    lab.appendChild(el('span', null, param.label || param.key));
    wrap.innerHTML = '';
    wrap.appendChild(lab);
    cb.addEventListener('change', () => setValue(param.key, cb.checked));
    return wrap;
  }

  if (type === 'position') {
    const grid = el('div', 'pos-grid');
    const positions = ['top_left', 'top_center', 'top_right',
      'middle_left', 'center', 'middle_right',
      'bottom_left', 'bottom_center', 'bottom_right'];
    const value = current === undefined ? (param.default || 'bottom_right') : current;
    positions.forEach((pos) => {
      const b = el('button');
      b.type = 'button';
      b.dataset.pos = pos;
      if (pos === value) b.classList.add('is-active');
      b.addEventListener('click', () => {
        [...grid.children].forEach((c) => c.classList.toggle('is-active', c === b));
        setValue(param.key, pos);
      });
      grid.appendChild(b);
    });
    wrap.appendChild(grid);
    return wrap;
  }

  if (type === 'select') {
    const sel = el('select', 'input');
    (param.options || []).forEach((o) => {
      const opt = el('option', null, o.label);
      opt.value = o.value;
      sel.appendChild(opt);
    });
    if (current !== undefined) sel.value = String(current);
    else if (param.default !== undefined) sel.value = String(param.default);
    sel.addEventListener('change', () => setValue(param.key, sel.value));
    wrap.appendChild(sel);
    if (param.hint) wrap.appendChild(el('p', 'hint', param.hint));
    return wrap;
  }

  if (type === 'font') {
    const sel = el('select', 'input');
    const none = el('option', null, '（默认字体）');
    none.value = '';
    sel.appendChild(none);
    (state.meta.fonts || []).forEach((f) => {
      const opt = el('option', null, f);
      opt.value = f;
      sel.appendChild(opt);
    });
    sel.value = current === undefined ? (param.default || '') : String(current);
    sel.addEventListener('change', () => setValue(param.key, sel.value));
    wrap.appendChild(sel);
    return wrap;
  }

  if (type === 'logo') {
    const row = el('div', 'input-row');
    const sel = el('select', 'input');
    const auto = el('option', null, '{{auto_logo()}}（按品牌自动匹配）');
    auto.value = '{{auto_logo()}}';
    sel.appendChild(auto);
    const none = el('option', null, '（不使用）');
    none.value = '';
    sel.appendChild(none);
    (state.logos || []).forEach((n) => {
      const opt = el('option', null, n);
      opt.value = n;
      sel.appendChild(opt);
    });
    const raw = el('input', 'input input-mini');
    raw.placeholder = '或直接填写路径 / 表达式';
    raw.value = current === undefined ? (param.default || '') : String(current);

    const known = [...sel.options].some((o) => o.value === raw.value);
    sel.value = known ? raw.value : '';
    sel.style.flex = '1';
    row.appendChild(sel);
    row.appendChild(raw);
    wrap.appendChild(row);

    const push = () => setValue(param.key, raw.value);
    sel.addEventListener('change', () => { raw.value = sel.value; push(); });
    raw.addEventListener('change', push);
    if (param.hint) wrap.appendChild(el('p', 'hint', param.hint));
    return wrap;
  }

  if (type === 'color') {
    const row = el('div', 'input-row');
    const text = el('input', 'input');
    text.value = current === undefined ? (param.default || '') : String(current);
    const picker = el('input');
    picker.type = 'color';
    picker.style.width = '46px';
    picker.style.flex = '0 0 auto';
    if (/^#[0-9a-f]{6}$/i.test(text.value)) picker.value = text.value;
    row.appendChild(text);
    row.appendChild(picker);
    wrap.appendChild(row);
    picker.addEventListener('input', () => {
      text.value = picker.value;
      setValue(param.key, picker.value);
    });
    text.addEventListener('change', () => setValue(param.key, text.value));
    wrap.appendChild(el('p', 'hint', '支持颜色名（white/black）、#RRGGBB 与 #RRGGBBAA。'));
    return wrap;
  }

  if (type === 'json' || type === 'textarea') {
    const ta = el('textarea', 'input code');
    ta.rows = type === 'json' ? 6 : 3;
    ta.spellcheck = false;
    if (current !== undefined) {
      ta.value = typeof current === 'object' ? JSON.stringify(current, null, 2) : String(current);
    } else if (param.default !== undefined) {
      ta.value = String(param.default);
    }
    wrap.appendChild(ta);
    ta.addEventListener('change', () => {
      if (type === 'json') {
        try {
          setValue(param.key, JSON.parse(ta.value));
          $('editorStatus').textContent = `已更新「${param.label}」。`;
        } catch (e) {
          $('editorStatus').textContent = `「${param.label}」JSON 解析失败：${e.message}`;
          ta.classList.add('is-error');
          return;
        }
      } else {
        setValue(param.key, ta.value);
      }
      ta.classList.remove('is-error');
    });
    if (param.hint) wrap.appendChild(el('p', 'hint', param.hint));
    return wrap;
  }

  // range：仅当当前值是纯数字时才用滑杆，否则退化成文本框（模板里可能写 Jinja 表达式）
  const isPlainNumber = current !== undefined && !isNaN(parseFloat(current)) && isFinite(current);
  const canRange = type === 'range' && !param.tokens && (current === undefined || isPlainNumber);

  if (canRange) {
    const row = el('div', 'input-row');
    const rng = el('input');
    rng.type = 'range';
    rng.min = param.min != null ? param.min : 0;
    rng.max = param.max != null ? param.max : 100;
    rng.step = param.step != null ? param.step : 1;
    rng.value = current === undefined ? (param.default != null ? param.default : rng.min) : current;
    rng.style.flex = '1';
    const out = el('span', 'val');
    out.textContent = rng.value;
    out.style.minWidth = '48px';
    out.style.textAlign = 'right';
    row.appendChild(rng);
    row.appendChild(out);
    wrap.appendChild(row);
    rng.addEventListener('input', () => {
      out.textContent = rng.value;
      setValue(param.key, parseFloat(rng.value));
    });
    return wrap;
  }

  // 文本 / 数字（允许写 Jinja 表达式）
  const input = el('input', 'input');
  input.type = 'text';
  if (current !== undefined) input.value = String(current);
  else if (param.default !== undefined) input.value = String(param.default);
  wrap.appendChild(input);
  input.addEventListener('change', () => {
    const v = input.value;
    if (type === 'number' && !param.tokens && v !== '' && !isNaN(parseFloat(v)) && isFinite(v)) {
      setValue(param.key, parseFloat(v));
    } else {
      setValue(param.key, v);
    }
  });
  if (param.hint) wrap.appendChild(el('p', 'hint', param.hint));
  return wrap;
}

/* ---- 编辑器预览 ---- */
const editorPreviewDebounced = debounce(editorPreviewNow, 420);

function editorPreviewNow() {
  const box = $('editorPreview');
  const status = $('editorStatus');
  if ($('editorModal').classList.contains('hidden')) return;
  if (!state.pipeline.length) {
    box.innerHTML = '';
    box.appendChild(el('span', 'editor-preview-ph', '流程为空，添加步骤后即可预览'));
    status.textContent = '流程为空。';
    return;
  }
  box.innerHTML = '';
  box.appendChild(el('span', 'editor-preview-ph', '渲染中…'));
  status.textContent = '正在渲染预览…';

  const t0 = performance.now();
  fetch('/api/v1/preview/pipeline', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      pipeline: state.pipeline,
      path: state.current || null,
      crop_preset: state.crop,
      logo: state.logo,
    }),
  })
    .then(async (res) => {
      if (!res.ok) {
        let msg = 'HTTP ' + res.status;
        try { const j = await res.json(); msg = j.error || msg; } catch (e) { /* 忽略 */ }
        throw new Error(msg);
      }
      return res.blob();
    })
    .then((blob) => {
      const url = URL.createObjectURL(blob);
      box.innerHTML = '';
      const img = el('img');
      img.src = url;
      img.onload = () => URL.revokeObjectURL(url);
      box.appendChild(img);
      const cost = Math.round(performance.now() - t0);
      status.textContent = `预览已更新（${cost}ms）· 共 ${state.pipeline.length} 个步骤`
        + `　提示：步骤自上而下执行，顺序会影响结果。`;
    })
    .catch((e) => {
      box.innerHTML = '';
      box.appendChild(el('span', 'editor-preview-ph', '预览失败：' + e.message));
      status.textContent = '预览失败：' + e.message;
    });
}

async function saveEditorPipeline(asNew) {
  const content = JSON.stringify(state.pipeline, null, 2);
  try {
    if (asNew) {
      const name = prompt('另存为模板名称：', state.templateName + '-可视化');
      if (!name) return;
      await api('/api/v1/template', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ template_name: name.trim(), content }),
      });
      await refreshTemplates();
      await selectTemplate(name.trim());
      toast('已创建模板：' + name.trim(), 'ok');
    } else {
      await api('/api/v1/template', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ template_name: state.templateName, content }),
      });
      toast('模板「' + state.templateName + '」已保存', 'ok');
    }
    $('advJson').value = content;
    state.editorBaseline = content;
    schedulePreview();
    log('可视化编辑器已保存模板：' + state.templateName, 'ok');
  } catch (e) {
    toast('保存失败：' + e.message, 'err');
  }
}

document.addEventListener('DOMContentLoaded', boot);
