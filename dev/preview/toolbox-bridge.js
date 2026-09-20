/**
 * toolbox-bridge.js 的**本地开发替身**，只在 `dev/preview/` 这个本地预览环境里使用。
 *
 * 真身在宿主仓库：`mybooks/app/public/static/toolbox-bridge.js`（工具页面通过
 * `<script src="/static/toolbox-bridge.js">` 拿到）。脚手架目前没有 `mytool dev`
 * （README「明确不做的事」），所以这里按真身的对外形状做一份最小 mock，让工具前端能脱离
 * MyBooks 单独跑起来点一遍。**不要把这个文件打进工具包**（包形状只允许
 * manifest.json / icon.png / LICENSE / NOTICE.md / backend / frontend）。
 *
 * 对外形状（与真身一致）：toolId / theme / locale / fetch / notify /
 * onThemeChange / onLocaleChange。
 */
(function (window) {
  'use strict';

  function query(name) {
    return new URLSearchParams(window.location.search).get(name);
  }

  function getToolId() {
    var match = window.location.pathname.match(/^\/get\/tool\/([^/]+)\/index\.html$/);
    return match ? match[1] : null;
  }

  var toolId = getToolId();
  var state = {
    theme: query('theme') || 'light',
    locale: query('locale') || 'zh',
  };
  var localeListeners = [];
  var themeListeners = [];

  // 本地预览直接给一个能切主题/语言的浮动开关，方便同时验两套配色
  function mountDevToolbar() {
    window.addEventListener('load', function () {
      var bar = document.createElement('div');
      bar.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:999;display:flex;gap:6px;' +
        'font:12px/1.4 system-ui;opacity:.85';
      var themeBtn = document.createElement('button');
      themeBtn.textContent = 'theme: ' + state.theme;
      themeBtn.onclick = function () {
        var next = state.theme === 'dark' ? 'light' : 'dark';
        var old = state.theme;
        state.theme = next;
        themeBtn.textContent = 'theme: ' + next;
        themeListeners.forEach(function (fn) { fn(next, old); });
      };
      var localeBtn = document.createElement('button');
      localeBtn.textContent = 'locale: ' + state.locale;
      localeBtn.onclick = function () {
        var next = state.locale === 'zh' ? 'en' : (state.locale === 'en' ? 'zh-TW' : 'zh');
        var old = state.locale;
        state.locale = next;
        localeBtn.textContent = 'locale: ' + next;
        localeListeners.forEach(function (fn) { fn(next, old); });
      };
      bar.appendChild(themeBtn);
      bar.appendChild(localeBtn);
      document.body.appendChild(bar);
    });
  }

  var bridge = {
    toolId: toolId,
    fetch: function (path, options) {
      if (!toolId) {
        return Promise.reject(new Error('mock bridge: 无法从 URL 解析 tool_id'));
      }
      var clean = String(path || '').replace(/^\//, '');
      return fetch('/api/toolbox/tool/' + toolId + '/' + clean,
                   Object.assign({ credentials: 'include' }, options || {}))
        .then(function (resp) {
          var type = resp.headers.get('content-type') || '';
          return type.indexOf('application/json') !== -1 ? resp.json() : resp.text();
        });
    },
    notify: function (message, level) {
      // 真身是 postMessage 给宿主弹 snackbar；本地预览就在页面上留个可见记录
      var box = document.getElementById('mock-notify');
      if (!box) {
        box = document.createElement('div');
        box.id = 'mock-notify';
        document.body.appendChild(box);
      }
      var line = document.createElement('div');
      line.textContent = '[' + (level || 'info') + '] ' + message;
      line.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:999;background:#222;color:#fff;' +
        'padding:4px 8px;border-radius:4px;font:12px/1.4 system-ui';
      box.appendChild(line);
      setTimeout(function () { line.remove(); }, 2500);
    },
    onLocaleChange: function (fn) { localeListeners.push(fn); },
    onThemeChange: function (fn) { themeListeners.push(fn); },
  };

  Object.defineProperty(bridge, 'theme', { get: function () { return state.theme; }, enumerable: true });
  Object.defineProperty(bridge, 'locale', { get: function () { return state.locale; }, enumerable: true });

  window.MyBooksToolBridge = bridge;
  mountDevToolbar();
})(window);
