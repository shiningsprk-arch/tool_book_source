/**
 * 书源引擎 —— 工具前端逻辑（原生 JS，无框架）。
 *
 * 上游对应物是 `app/pages/toolbox/book_source.vue`（Vuetify2 页面）。
 * 外部工具的前端跑在宿主 <iframe> 里，拿不到宿主的 Vue/Vuetify/Vuex，所以这里用最朴素的
 * DOM 操作 + 手动重渲染复刻同一套交互：搜索下载 / 书源管理两个标签页、六个对话框、
 * 两个轮询（异步搜索 1.5s、后台任务 2s）、结果分页。
 *
 * 与宿主的全部交互都经过 toolbox-bridge.js：
 *   bridge.fetch(path, options)  → /api/toolbox/tool/book_source/<path>
 *   bridge.notify(msg, level)    → 宿主 snackbar
 *   bridge.theme / onThemeChange → 深浅色
 *   bridge.locale / i18n 胶水    → 文案
 */
(function () {
  'use strict';

  var bridge = window.MyBooksToolBridge;
  var i18n = window.MyBooksToolI18n.create();
  var PAGE_SIZE = 50;
  var SEARCH_POLL_MS = 1500;
  var TASK_POLL_MS = 2000;

  // ── 图标：手写的极简线性 SVG ─────────────────────────────────────────────
  // 刻意不引 Material Design Icons 等第三方图标字体/资源：外部工具包要能离线自包含，
  // 也不想为了十几个图标背上一个图标库的许可证。
  var ICONS = {
    search: '<circle cx="7.2" cy="7.2" r="4.2"/><line x1="10.4" y1="10.4" x2="13.8" y2="13.8"/>',
    book: '<path d="M2.6 3.2h4.6a1.6 1.6 0 0 1 1.6 1.6v8H4.2a1.6 1.6 0 0 1-1.6-1.6z"/>' +
          '<path d="M9 4.8a1.6 1.6 0 0 1 1.6-1.6h2.8v8H10.6A1.6 1.6 0 0 0 9 12.8z"/>',
    refresh: '<path d="M13.2 7.2a5.2 5.2 0 1 0-1.6 3.8"/><polyline points="13.2,3.4 13.2,7.2 9.4,7.2"/>',
    plus: '<line x1="8" y1="3" x2="8" y2="13"/><line x1="3" y1="8" x2="13" y2="8"/>',
    zip: '<rect x="2.8" y="2.8" width="10.4" height="10.4" rx="1.6"/><line x1="8" y1="2.8" x2="8" y2="6.4"/>' +
         '<line x1="8" y1="7.6" x2="8" y2="8.6"/>',
    globe: '<circle cx="8" cy="8" r="5.4"/><ellipse cx="8" cy="8" rx="2.4" ry="5.4"/>' +
           '<line x1="2.6" y1="8" x2="13.4" y2="8"/>',
    cog: '<circle cx="8" cy="8" r="2.2"/><path d="M8 2.6v1.8M8 11.6v1.8M2.6 8h1.8M11.6 8h1.8' +
         'M4.2 4.2l1.3 1.3M10.5 10.5l1.3 1.3M11.8 4.2l-1.3 1.3M5.5 10.5l-1.3 1.3"/>',
    plug: '<circle cx="8" cy="8" r="5.4"/><line x1="8" y1="2.6" x2="8" y2="8"/>' +
          '<line x1="8" y1="8" x2="11" y2="10.6"/>',
    download: '<line x1="8" y1="2.6" x2="8" y2="9.6"/><polyline points="4.8,6.6 8,9.8 11.2,6.6"/>' +
              '<line x1="3.2" y1="13" x2="12.8" y2="13"/>',
    epub: '<path d="M3 3h6.4a1.6 1.6 0 0 1 1.6 1.6V13H4.6A1.6 1.6 0 0 1 3 11.4z"/>' +
          '<polyline points="5.4,9.4 7.6,7 9.8,9.4"/><line x1="7.6" y1="7" x2="7.6" y2="10.6"/>',
    check: '<polyline points="3.2,8.4 6.4,11.6 12.8,4.4"/>',
    x: '<line x1="4.2" y1="4.2" x2="11.8" y2="11.8"/><line x1="11.8" y1="4.2" x2="4.2" y2="11.8"/>',
    trash: '<polyline points="2.8,4.4 13.2,4.4"/><path d="M5.4 4.4V13h5.2V4.4"/>' +
           '<polyline points="6.4,2.6 9.6,2.6"/>',
    cancel: '<circle cx="8" cy="8" r="5.4"/><line x1="5.8" y1="5.8" x2="10.2" y2="10.2"/>' +
            '<line x1="10.2" y1="5.8" x2="5.8" y2="10.2"/>',
  };

  function iconSvg(name) {
    return '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" ' +
      'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
      (ICONS[name] || '') + '</svg>';
  }

  // ── 小工具 ───────────────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function t(key, params) { return i18n.t(key, params); }

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function show(node, visible) { node.hidden = !visible; }

  function notify(message, level) {
    if (bridge && bridge.notify) bridge.notify(message, level || 'info');
  }

  function bookKey(book) { return (book.bookUrl || '') + '|' + (book.sourceName || ''); }

  // bridge.fetch 不做 HTTP 状态判断（非 JSON 响应会原样返回字符串），这里统一成对象
  function api(path, options) {
    return bridge.fetch(path, options).then(function (rsp) {
      if (typeof rsp === 'string') return { err: 'bad_response', msg: rsp };
      return rsp || {};
    }, function (err) {
      return { err: 'network', msg: String(err && err.message ? err.message : err) };
    });
  }

  function postJson(path, body) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  function val(id) { return ($(id).value || '').trim(); }

  // ── 状态 ────────────────────────────────────────────────────────────────
  var state = {
    tab: 'search',
    keyword: '',
    searching: false,
    searchResults: [],
    pageNum: 1,

    tasks: [],
    activeTask: null,
    pollTimer: null,
    searchTimer: null,
    downloadingAll: false,
    downloadingMap: {},
    generatingMap: {},

    sources: [],
    sLoading: false,
    searchQuery: '',

    editingSource: null,
    saving: false,

    importUrlOpen: false,
    importingUrl: false,

    settingsTarget: null,
    deleteTarget: null,
    testing: '',
  };

  // ── 渲染：搜索结果 ───────────────────────────────────────────────────────
  function pageCount() {
    return Math.max(1, Math.ceil(state.searchResults.length / PAGE_SIZE));
  }

  function renderResults() {
    var total = state.searchResults.length;
    $('result-count').textContent = String(total);
    show($('results-card'), total > 0);
    if (total === 0) {
      $('results-body').innerHTML = '';
      show($('pager'), false);
      return;
    }

    var start = (state.pageNum - 1) * PAGE_SIZE;
    var page = state.searchResults.slice(start, start + PAGE_SIZE);
    var html = '';
    page.forEach(function (book) {
      var key = bookKey(book);
      var cover = book.coverUrl
        ? '<img class="bs-cover" src="' + esc(book.coverUrl) + '" alt="" loading="lazy" ' +
          'referrerpolicy="no-referrer" onerror="this.style.visibility=\'hidden\'" />'
        : '<span class="bs-cover bs-cover--empty">' + iconSvg('book') + '</span>';
      var dlLoading = state.downloadingMap[key];
      var genLoading = state.generatingMap[key];
      html += '<tr>' +
        '<td class="bs-c-center" data-label="' + esc(t('bookSource.colCover')) + '">' + cover + '</td>' +
        '<td class="bs-name" data-label="' + esc(t('bookSource.colName')) + '">' + esc(book.name) + '</td>' +
        '<td data-label="' + esc(t('bookSource.colAuthor')) + '">' + esc(book.author) + '</td>' +
        '<td data-label="' + esc(t('bookSource.colSource')) + '"><span class="mb-chip mb-chip--primary">' +
          esc(book.sourceName) + '</span></td>' +
        '<td class="bs-truncate" data-label="' + esc(t('bookSource.colLastChapter')) + '" title="' +
          esc(book.lastChapter) + '">' + esc(book.lastChapter) + '</td>' +
        '<td class="bs-c-center" data-label="' + esc(t('bookSource.colAction')) + '"><span class="bs-actions">' +
          '<button class="mb-btn mb-btn--small" type="button" data-act="download" data-key="' + esc(key) + '"' +
            (dlLoading ? ' disabled' : '') + '>' +
            (dlLoading ? '<span class="mb-spinner" style="width:12px;height:12px;border-width:2px"></span>'
                       : iconSvg('download')) +
            '<span>' + esc(t('bookSource.download')) + '</span></button>' +
          '<button class="mb-btn mb-btn--secondary mb-btn--small" type="button" data-act="generate" data-key="' +
            esc(key) + '"' + (genLoading ? ' disabled' : '') + '>' +
            (genLoading ? '<span class="mb-spinner" style="width:12px;height:12px;border-width:2px"></span>'
                        : iconSvg('epub')) +
            '<span>' + esc(t('bookSource.generate')) + '</span></button>' +
        '</span></td>' +
      '</tr>';
    });
    $('results-body').innerHTML = html;
    renderPager();
  }

  function renderPager() {
    var count = pageCount();
    if (count <= 1) { show($('pager'), false); return; }
    show($('pager'), true);

    var current = state.pageNum;
    var half = 2;
    var from = Math.max(1, current - half);
    var to = Math.min(count, from + 4);
    from = Math.max(1, to - 4);

    var html = '<button class="mb-btn mb-btn--secondary mb-btn--small" type="button" data-page="' +
      (current - 1) + '"' + (current <= 1 ? ' disabled' : '') + '>&lsaquo;</button>';
    for (var p = from; p <= to; p++) {
      html += '<button class="mb-btn mb-btn--secondary mb-btn--small" type="button" data-page="' + p +
        '" aria-current="' + (p === current) + '">' + p + '</button>';
    }
    html += '<button class="mb-btn mb-btn--secondary mb-btn--small" type="button" data-page="' +
      (current + 1) + '"' + (current >= count ? ' disabled' : '') + '>&rsaquo;</button>';
    $('pager').innerHTML = html;
  }

  // ── 渲染：任务列表 ───────────────────────────────────────────────────────
  function renderTasks() {
    show($('tasks-card'), state.tasks.length > 0);
    var html = '';
    state.tasks.forEach(function (task, idx) {
      var statusClass = task.status === 'done' ? 'mb-status--success'
        : task.status === 'error' ? 'mb-status--error' : 'mb-status--info';
      html += '<div class="bs-task">' +
        '<div class="bs-task-head">' +
          '<span class="bs-task-name">' + esc(task.name) + '</span>' +
          (task.source ? '<span class="mb-chip mb-chip--primary">' + esc(task.source) + '</span>' : '') +
          (task.kind === 'epub' ? '<span class="mb-chip">' + esc(t('bookSource.generate')) + '</span>' : '') +
          '<span class="bs-task-actions">' +
            (task.epubUrl
              ? '<a class="mb-btn mb-btn--secondary mb-btn--small" href="' + esc(task.epubUrl) +
                '" target="_blank" rel="noopener">' + iconSvg('download') +
                '<span>' + esc(t('bookSource.downloadFile')) + '</span></a>'
              : '') +
            (task === state.activeTask
              ? '<button class="bs-icon-btn" type="button" data-act="cancel" data-idx="' + idx +
                '" title="' + esc(t('bookSource.cancelTask')) + '">' + iconSvg('cancel') + '</button>'
              : '') +
            '<span class="' + statusClass + '" style="font-size:0.82em">' +
              esc(task.msg || task.status) + '</span>' +
          '</span>' +
        '</div>' +
        '<div class="mb-progress-linear"><div class="mb-progress-linear__bar" style="width:' +
          Math.max(0, Math.min(100, task.progress || 0)) + '%"></div></div>' +
      '</div>';
    });
    $('tasks-body').innerHTML = html;
  }

  // ── 渲染：书源列表 ───────────────────────────────────────────────────────
  function filteredSources() {
    var q = state.searchQuery.toLowerCase();
    if (!q) return state.sources;
    return state.sources.filter(function (s) {
      return (s.bookSourceName || '').toLowerCase().indexOf(q) !== -1
        || (s.bookSourceUrl || '').toLowerCase().indexOf(q) !== -1
        || (s.bookSourceGroup || '').toLowerCase().indexOf(q) !== -1;
    });
  }

  function renderSources() {
    $('source-count').textContent = String(state.sources.length);
    var list = filteredSources();
    var empty = $('source-empty');
    if (list.length === 0) {
      $('source-list').innerHTML = '';
      empty.textContent = state.sLoading ? t('bookSource.loading') : t('bookSource.noSources');
      show(empty, true);
      $('search-hint').textContent = t('bookSource.searchHint', { n: enabledCount() });
      return;
    }
    show(empty, false);

    var html = '';
    list.forEach(function (item, idx) {
      var name = item.bookSourceName || '';
      html += '<div class="bs-source-item" data-idx="' + idx + '">' +
        '<button class="mb-btn bs-toggle ' + (item.enabled ? 'bs-toggle--on' : 'bs-toggle--off') +
          '" type="button" data-act="toggle" data-idx="' + idx + '" title="' + esc(name) + '">' +
          iconSvg(item.enabled ? 'check' : 'x') + '</button>' +
        '<span class="bs-source-text">' +
          '<div class="bs-source-name">' + esc(name) + '</div>' +
          '<div class="bs-source-meta">' + esc(item.bookSourceGroup || t('bookSource.noGroup')) + '</div>' +
        '</span>' +
        (state.testing === name
          ? '<span class="mb-spinner" style="width:14px;height:14px;border-width:2px"></span>'
          : '<button class="bs-icon-btn" type="button" data-act="test" data-idx="' + idx +
            '" title="' + esc(t('bookSource.testResultTitle')) + '">' + iconSvg('plug') + '</button>') +
        '<button class="bs-icon-btn" type="button" data-act="settings" data-idx="' + idx +
          '" title="' + esc(t('bookSource.settings')) + '">' + iconSvg('cog') + '</button>' +
      '</div>';
    });
    $('source-list').innerHTML = html;
    $('search-hint').textContent = t('bookSource.searchHint', { n: enabledCount() });
  }

  function enabledCount() {
    return state.sources.filter(function (s) { return s.enabled; }).length;
  }

  // ── 书源 CRUD ───────────────────────────────────────────────────────────
  function loadSources() {
    state.sLoading = true;
    renderSources();
    return api('list').then(function (rsp) {
      state.sources = (rsp.err === 'ok' && Array.isArray(rsp.data)) ? rsp.data.map(function (s) {
        return Object.assign({}, s);
      }) : [];
      state.sLoading = false;
      renderSources();
    });
  }

  function sourceToJsonObj(item) {
    var obj = {
      bookSourceName: item.bookSourceName,
      bookSourceUrl: item.bookSourceUrl,
      bookSourceGroup: item.bookSourceGroup,
      bookSourceType: item.bookSourceType == null ? 0 : item.bookSourceType,
      searchUrl: item.searchUrl || '',
      header: item.header || {},
      jsLib: item.jsLib || '',
      ruleSearch: item.ruleSearch || {},
      ruleBookInfo: item.ruleBookInfo || {},
      ruleToc: item.ruleToc || {},
      ruleContent: item.ruleContent || {},
    };
    if (!obj.ruleContent.replaceRegex) obj.ruleContent.replaceRegex = [];
    return obj;
  }

  function openEdit(item) {
    state.editingSource = item || null;
    $('edit-title').textContent = item ? t('bookSource.editTitle') : t('bookSource.addTitle');
    $('edit-json').value = item ? JSON.stringify(sourceToJsonObj(item), null, 2) : '';
    show($('edit-error'), false);
    show($('dlg-edit'), true);
  }

  function saveSource() {
    show($('edit-error'), false);
    var raw = ($('edit-json').value || '').trim();
    function fail(msg) {
      var box = $('edit-error');
      box.textContent = t('bookSource.jsonFormatError') + ': ' + msg;
      show(box, true);
      state.saving = false;
      $('edit-save').disabled = false;
    }
    if (!raw) { fail('empty'); return; }
    var obj;
    try {
      obj = JSON.parse(raw);
    } catch (e) {
      fail(e.message);
      return;
    }
    if (!obj.bookSourceName || !obj.bookSourceUrl) {
      fail(t('bookSource.fieldName') + ' / bookSourceUrl');
      return;
    }
    obj.enabled = state.editingSource ? state.editingSource.enabled : true;
    obj.bookSourceUrl = String(obj.bookSourceUrl).replace(/\/+$/, '');

    state.saving = true;
    $('edit-save').disabled = true;
    postJson('save', { raw: obj }).then(function (rsp) {
      state.saving = false;
      $('edit-save').disabled = false;
      if (rsp.err === 'ok') {
        notify(t('bookSource.saveSuccess'), 'success');
        show($('dlg-edit'), false);
        loadSources();
      } else {
        notify(rsp.msg || t('bookSource.saveFailed'), 'error');
      }
    });
  }

  function toggleSource(item) {
    var was = item.enabled;
    item.enabled = !item.enabled;
    renderSources();
    postJson('toggle', { name: item.bookSourceName }).then(function (rsp) {
      if (rsp.err !== 'ok') {
        item.enabled = was;
        renderSources();
        notify(rsp.msg || 'toggle failed', 'error');
      }
    });
  }

  function openSettings(item) {
    state.settingsTarget = item;
    $('settings-name').textContent = item.bookSourceName || '';
    $('settings-json').value = JSON.stringify(sourceToJsonObj(item), null, 2);
    show($('settings-error'), false);
    show($('dlg-settings'), true);
  }

  function applySettingsJson() {
    var item = state.settingsTarget;
    if (!item) return;
    show($('settings-error'), false);
    var obj;
    try {
      obj = JSON.parse($('settings-json').value);
    } catch (e) {
      $('settings-error').textContent = t('bookSource.jsonFormatError') + ': ' + e.message;
      show($('settings-error'), true);
      return;
    }
    if (!obj.bookSourceName) {
      $('settings-error').textContent = t('bookSource.jsonFormatError') + ': ' + t('bookSource.fieldName');
      show($('settings-error'), true);
      return;
    }
    Object.assign(item, obj);
    notify(t('bookSource.jsonApplied'), 'success');
    show($('dlg-settings'), false);
    saveSettingsSource(item);
  }

  function saveSettingsSource(item) {
    var form = Object.assign({}, item);
    if (form.header && typeof form.header === 'object') form.header = JSON.stringify(form.header);
    if (form.ruleContent && form.ruleContent.replaceRegex) {
      form.replaceRegexStr = form.ruleContent.replaceRegex
        .map(function (r) { return r.pattern + '##' + r.replacement; }).join('\n');
    }
    postJson('save', { raw: form }).then(function (rsp) {
      if (rsp.err !== 'ok') notify(rsp.msg || t('bookSource.saveFailed'), 'error');
      loadSources();
    });
  }

  function confirmDelete(item) {
    state.deleteTarget = item;
    $('delete-name').textContent = (item && item.bookSourceName) || '';
    show($('dlg-delete'), true);
  }

  function doDelete() {
    if (!state.deleteTarget) return;
    var name = state.deleteTarget.bookSourceName;
    postJson('delete', { name: name }).then(function (rsp) {
      if (rsp.err === 'ok') {
        show($('dlg-delete'), false);
        show($('dlg-settings'), false);
        notify(t('bookSource.deleteSuccess'), 'success');
        loadSources();
      } else {
        notify(rsp.msg || t('bookSource.deleteFailed'), 'error');
      }
    });
  }

  function testSource(item) {
    state.testing = item.bookSourceName;
    renderSources();
    api('test?source=' + encodeURIComponent(item.bookSourceName)).then(function (rsp) {
      state.testing = '';
      renderSources();
      if (rsp.err !== 'ok') {
        notify(rsp.msg || t('bookSource.testFailed'), 'error');
        return;
      }
      var result = rsp.data || {};
      $('test-name').textContent = result.source || '';
      var reachable = !!result.reachable;
      var alert = $('test-alert');
      alert.className = 'mb-alert ' + (reachable ? 'mb-alert--success' : 'mb-alert--error');
      alert.textContent = reachable
        ? t('bookSource.testReachable', { n: result.sample_count || 0 })
        : t('bookSource.testUnreachable');
      var samples = result.samples || [];
      $('test-body').innerHTML = samples.map(function (s) {
        return '<tr><td>' + esc(s.name) + '</td><td>' + esc(s.author) + '</td></tr>';
      }).join('');
      show($('dlg-test'), true);
    });
  }

  // ── 搜索 ────────────────────────────────────────────────────────────────
  function renderSearchState() {
    var btn = $('search-btn');
    btn.disabled = state.searching || !val('keyword');
    var lead = state.searching
      ? '<span class="mb-spinner" style="width:14px;height:14px;border-width:2px"></span>'
      : iconSvg('search');
    btn.innerHTML = lead + '<span>' + esc(t('bookSource.searchBtn')) + '</span>';
  }

  function doSearch() {
    var keyword = val('keyword');
    if (!keyword) return;
    state.keyword = keyword;
    if (enabledCount() === 0) {
      notify(t('bookSource.noEnabledSources'), 'error');
      return;
    }
    state.searching = true;
    state.searchResults = [];
    state.pageNum = 1;
    renderSearchState();
    renderResults();

    postJson('search_async', { keyword: keyword }).then(function (rsp) {
      if (rsp.err === 'ok' && rsp.data && rsp.data.task_id) {
        pollSearch(rsp.data.task_id);
      } else {
        state.searching = false;
        renderSearchState();
        notify(rsp.msg || t('bookSource.searchFailed'), 'error');
      }
    });
  }

  function pollSearch(taskId) {
    stopSearchPoll(true);
    state.searchTimer = setInterval(function () {
      api('search_status?task_id=' + encodeURIComponent(taskId)).then(function (rsp) {
        if (rsp.err !== 'ok' || !rsp.data) {
          state.searching = false;
          stopSearchPoll();
          return;
        }
        var data = rsp.data;
        var seen = {};
        state.searchResults.forEach(function (b) { seen[bookKey(b)] = true; });
        (data.results || []).forEach(function (r) {
          (r.books || []).forEach(function (b) {
            var item = Object.assign({}, b, { sourceName: r.source_name });
            var key = bookKey(item);
            if (!seen[key]) {
              seen[key] = true;
              state.searchResults.push(item);
            }
          });
        });
        renderResults();
        if (data.finished) {
          state.searching = false;
          stopSearchPoll();
        }
      });
    }, SEARCH_POLL_MS);
  }

  function stopSearchPoll(keepFlag) {
    if (state.searchTimer) {
      clearInterval(state.searchTimer);
      state.searchTimer = null;
    }
    if (!keepFlag) state.searching = false;
    renderSearchState();
  }

  // ── 下载 / 生成 EPUB ─────────────────────────────────────────────────────
  function findBook(key) {
    for (var i = 0; i < state.searchResults.length; i++) {
      if (bookKey(state.searchResults[i]) === key) return state.searchResults[i];
    }
    return null;
  }

  function downloadBook(book) {
    if (state.activeTask) {
      notify(t('bookSource.downloadBusy'), 'error');
      return Promise.resolve(false);
    }
    var key = bookKey(book);
    if (state.downloadingMap[key]) return Promise.resolve(false);

    var task = {
      name: book.name, source: book.sourceName, progress: 0,
      status: 'started', msg: t('bookSource.taskStarting'), kind: 'download',
    };
    state.tasks.push(task);
    state.downloadingMap[key] = true;
    renderResults();
    renderTasks();

    return postJson('download', {
      source: book.sourceName,
      bookUrl: book.bookUrl,
      bookTitle: book.name,
      maxChapters: 9999,
    }).then(function (rsp) {
      delete state.downloadingMap[key];
      renderResults();
      if (rsp.err === 'ok') {
        state.activeTask = task;
        startPoll(task);
        return true;
      }
      task.status = 'error';
      task.msg = rsp.msg || t('bookSource.downloadFailed');
      renderTasks();
      return false;
    });
  }

  function generateEpub(book) {
    if (state.activeTask) {
      notify(t('bookSource.downloadBusy'), 'error');
      return Promise.resolve(false);
    }
    var key = bookKey(book);
    if (state.generatingMap[key]) return Promise.resolve(false);

    var task = {
      name: book.name, source: book.sourceName, progress: 0,
      status: 'started', msg: t('bookSource.taskStarting'), kind: 'epub', taskKey: key,
    };
    state.tasks.push(task);
    state.generatingMap[key] = true;
    renderResults();
    renderTasks();

    return postJson('generate_epub', {
      source: book.sourceName,
      bookUrl: book.bookUrl,
      bookTitle: book.name,
      maxChapters: 9999,
    }).then(function (rsp) {
      if (rsp.err === 'ok') {
        state.activeTask = task;
        startPoll(task);
        return true;
      }
      delete state.generatingMap[key];
      task.status = 'error';
      task.msg = rsp.msg || t('bookSource.downloadFailed');
      renderResults();
      renderTasks();
      return false;
    });
  }

  function waitActiveTask() {
    return new Promise(function (resolve) {
      (function check() {
        if (!state.activeTask) resolve();
        else setTimeout(check, TASK_POLL_MS);
      })();
    });
  }

  function downloadAll() {
    show($('dlg-confirm-all'), false);
    state.downloadingAll = true;
    var queue = state.searchResults.slice();
    (function next(i) {
      if (i >= queue.length) {
        state.downloadingAll = false;
        return;
      }
      downloadBook(queue[i]).then(function (ok) {
        if (!ok) return next(i + 1);
        waitActiveTask().then(function () { next(i + 1); });
      });
    })(0);
  }

  function cancelTask(task) {
    if (!task.taskId) return;
    postJson('cancel', { task_id: task.taskId }).then(function (rsp) {
      if (rsp.err !== 'ok') notify(rsp.msg || t('bookSource.taskFailed'), 'error');
    });
  }

  function startPoll(task) {
    stopPoll(true);
    state.pollTimer = setInterval(function () {
      api('progress').then(function (rsp) {
        if (rsp.err === 'task.not_found') {
          if (task.status === 'started') {
            task.status = 'done';
            task.progress = 100;
            task.msg = task.kind === 'epub' ? t('bookSource.generateDone') : t('bookSource.taskDone');
          }
          if (task.kind === 'epub' && task.taskKey) delete state.generatingMap[task.taskKey];
          stopPoll();
          renderResults();
          renderTasks();
          return;
        }
        if (rsp.err === 'ok' && rsp.data) {
          task.progress = rsp.data.progress || 0;
          task.taskId = rsp.data.task_id || task.taskId;
          var data = rsp.data.progress_data || {};
          if (data.status) task.msg = data.status;

          if (rsp.data.status === 'completed') {
            task.status = 'done';
            task.progress = 100;
            task.msg = task.kind === 'epub' ? t('bookSource.generateDone') : t('bookSource.taskDone');
            if (task.kind === 'epub') {
              task.epubUrl = apiUrl('download_epub');
              if (task.taskKey) delete state.generatingMap[task.taskKey];
            }
            stopPoll();
            renderResults();
          } else if (rsp.data.status === 'failed') {
            task.status = 'error';
            task.msg = t('bookSource.taskFailed');
            if (task.kind === 'epub' && task.taskKey) delete state.generatingMap[task.taskKey];
            stopPoll();
            renderResults();
          }
          renderTasks();
        }
      });
    }, TASK_POLL_MS);
  }

  function stopPoll(keepActive) {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    if (!keepActive) state.activeTask = null;
  }

  function apiUrl(path) {
    var toolId = (bridge && bridge.toolId) || 'book_source';
    return '/api/toolbox/tool/' + toolId + '/' + path;
  }

  // 封面加载失败（第三方站点的防盗链/404）时换成占位图标。error 事件不冒泡，所以用捕获阶段。
  function onImageError(e) {
    var img = e.target;
    if (!img || img.tagName !== 'IMG' || !img.classList.contains('bs-cover')) return;
    var placeholder = document.createElement('span');
    placeholder.className = 'bs-cover bs-cover--empty';
    placeholder.innerHTML = iconSvg('book');
    if (img.parentNode) img.parentNode.replaceChild(placeholder, img);
  }

  // ── 导入 ────────────────────────────────────────────────────────────────
  function importZip(file) {
    if (!file) return;
    var form = new FormData();
    form.append('file', file);
    api('import_zip', { method: 'POST', body: form }).then(function (rsp) {
      if (rsp.err === 'ok') {
        notify(rsp.msg || t('bookSource.importSuccess'), 'success');
        loadSources();
      } else {
        notify(rsp.msg || t('bookSource.importFailed'), 'error');
      }
    });
  }

  function doImportUrl() {
    var url = val('import-url');
    if (!url) {
      notify(t('bookSource.importUrlEmpty'), 'error');
      return;
    }
    state.importingUrl = true;
    $('import-url-btn').disabled = true;
    postJson('import_url', { url: url }).then(function (rsp) {
      state.importingUrl = false;
      $('import-url-btn').disabled = false;
      if (rsp.err === 'ok') {
        notify(rsp.msg || t('bookSource.importSuccess'), 'success');
        show($('import-url-row'), false);
        $('import-url').value = '';
        loadSources();
      } else {
        notify(rsp.msg || t('bookSource.importFailed'), 'error');
      }
    });
  }

  // ── 标签页 / 依赖提示 ────────────────────────────────────────────────────
  function switchTab(tab) {
    state.tab = tab;
    $('tab-search').setAttribute('aria-selected', String(tab === 'search'));
    $('tab-sources').setAttribute('aria-selected', String(tab === 'sources'));
    show($('pane-search'), tab === 'search');
    show($('pane-sources'), tab === 'sources');
    if (tab === 'sources' && state.sources.length === 0) loadSources();
  }

  function checkDeps() {
    api('deps').then(function (rsp) {
      if (rsp.err !== 'ok' || !rsp.data) return;
      var data = rsp.data;
      var messages = [];
      if (!data.engine) messages.push(t('bookSource.depsEngineMissing', { error: data.engine_error || '' }));
      if (!data.js_rules) {
        messages.push(t('bookSource.depsNoJs'));
        // 宿主没装 dukpy 时，包里内置了一份按平台裁过的副本；解释器不匹配就把
        // "包里带了什么 / 现在需要什么" 一起说出来，不然用户只能干瞪眼
        var vendor = data.dukpy_vendor || {};
        if (vendor.requested_tag) {
          messages.push(t('bookSource.depsNoJsDetail', {
            wanted: vendor.requested_tag,
            bundled: (vendor.bundled_tags || []).join(', ') || '-',
          }));
        }
      }
      if (!data.epub) messages.push(t('bookSource.depsNoEpub'));
      (data.missing_required || []).forEach(function (name) {
        messages.push(t('bookSource.depsMissingRequired', { name: name }));
      });
      var box = $('deps-warning');
      if (messages.length === 0) { show(box, false); return; }
      box.textContent = messages.join(' ');
      show(box, true);
    });
  }

  // ── 事件绑定 ────────────────────────────────────────────────────────────
  function bind() {
    $('tab-search').addEventListener('click', function () { switchTab('search'); });
    $('tab-sources').addEventListener('click', function () { switchTab('sources'); });

    // 搜索
    $('keyword').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') doSearch();
    });
    $('keyword').addEventListener('input', function () {
      renderSearchState();
    });
    $('search-btn').addEventListener('click', doSearch);
    $('download-all-btn').addEventListener('click', function () {
      if (state.downloadingAll) return;
      $('confirm-all-msg').textContent = t('bookSource.confirmDownloadAllMsg', { n: state.searchResults.length });
      show($('dlg-confirm-all'), true);
    });
    $('confirm-all-cancel').addEventListener('click', function () { show($('dlg-confirm-all'), false); });
    $('confirm-all-ok').addEventListener('click', downloadAll);

    // 结果表：事件委托
    $('results-body').addEventListener('click', function (e) {
      var btn = e.target.closest('[data-act]');
      if (!btn) return;
      var book = findBook(btn.getAttribute('data-key'));
      if (!book) return;
      if (btn.getAttribute('data-act') === 'download') downloadBook(book);
      else generateEpub(book);
    });

    // 分页：事件委托
    $('pager').addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-page]');
      if (!btn || btn.disabled) return;
      state.pageNum = Number(btn.getAttribute('data-page'));
      renderResults();
    });

    // 任务：事件委托
    $('tasks-body').addEventListener('click', function (e) {
      var btn = e.target.closest('[data-act="cancel"]');
      if (!btn) return;
      var task = state.tasks[Number(btn.getAttribute('data-idx'))];
      if (task) cancelTask(task);
    });

    // 书源列表：事件委托
    $('source-filter').addEventListener('input', function () {
      state.searchQuery = $('source-filter').value || '';
      renderSources();
    });
    $('refresh-btn').addEventListener('click', loadSources);
    $('add-btn').addEventListener('click', function () { openEdit(null); });
    $('zip-btn').addEventListener('click', function () { $('zip-input').click(); });
    $('zip-input').addEventListener('change', function (e) {
      importZip(e.target.files && e.target.files[0]);
      e.target.value = '';
    });
    $('url-toggle-btn').addEventListener('click', function () {
      state.importUrlOpen = !state.importUrlOpen;
      show($('import-url-row'), state.importUrlOpen);
      if (state.importUrlOpen) $('import-url').focus();
    });
    $('import-url').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') doImportUrl();
    });
    $('import-url-btn').addEventListener('click', doImportUrl);

    $('source-list').addEventListener('click', function (e) {
      var row = e.target.closest('.bs-source-item');
      if (!row) return;
      var item = filteredSources()[Number(row.getAttribute('data-idx'))];
      if (!item) return;
      var btn = e.target.closest('[data-act]');
      if (!btn) { openEdit(item); return; }
      var act = btn.getAttribute('data-act');
      if (act === 'toggle') toggleSource(item);
      else if (act === 'test') testSource(item);
      else if (act === 'settings') openSettings(item);
    });

    // 对话框
    $('edit-cancel').addEventListener('click', function () { show($('dlg-edit'), false); });
    $('edit-save').addEventListener('click', saveSource);
    $('settings-close').addEventListener('click', function () { show($('dlg-settings'), false); });
    $('settings-apply').addEventListener('click', applySettingsJson);
    $('settings-delete').addEventListener('click', function () { confirmDelete(state.settingsTarget); });
    $('delete-cancel').addEventListener('click', function () { show($('dlg-delete'), false); });
    $('delete-confirm').addEventListener('click', doDelete);
    $('test-close').addEventListener('click', function () { show($('dlg-test'), false); });

    // 点遮罩关闭 + Esc 关闭（原生 <dialog> 不可用，自己来）
    ['dlg-edit', 'dlg-settings', 'dlg-delete', 'dlg-confirm-all', 'dlg-test'].forEach(function (id) {
      $(id).addEventListener('click', function (e) {
        if (e.target === $(id)) show($(id), false);
      });
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      ['dlg-edit', 'dlg-settings', 'dlg-delete', 'dlg-confirm-all', 'dlg-test'].forEach(function (id) {
        if (!$(id).hidden) show($(id), false);
      });
    });

    window.addEventListener('beforeunload', function () {
      stopPoll(true);
      stopSearchPoll(true);
    });
    // 封面加载失败（error 不冒泡）用捕获阶段兜住
    document.addEventListener('error', onImageError, true);
  }

  // ── 启动 ────────────────────────────────────────────────────────────────
  function boot() {
    // 主题
    function applyTheme(theme) {
      document.body.setAttribute('data-theme', theme === 'dark' ? 'dark' : 'light');
    }
    applyTheme((bridge && bridge.theme) || 'light');
    if (bridge && bridge.onThemeChange) bridge.onThemeChange(applyTheme);

    // 图标（静态部分）
    Array.prototype.forEach.call(document.querySelectorAll('[data-icon]'), function (node) {
      node.innerHTML = iconSvg(node.getAttribute('data-icon'));
    });

    bind();

    // 文案加载完再首屏渲染：动态部分（表格/列表/带占位符的提示）用的是 i18n.t()，
    // data-i18n 静态元素由 i18n 胶水自己的 applyDom() 负责。
    i18n.ready.then(function () {
      renderSearchState();
      renderResults();
      renderTasks();
      renderSources();
      $('search-hint').textContent = t('bookSource.searchHint', { n: 0 });
      return loadSources();
    }).then(checkDeps);

    // 宿主切换语言：i18n 会重刷 data-i18n 元素，动态部分要自己重渲染
    i18n.onChange(function () {
      renderResults();
      renderTasks();
      renderSources();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
