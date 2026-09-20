/**
 * 最小可用的工具内 i18n 胶水代码——纯原生 JS，不依赖任何框架。
 *
 * 边界：字串目录（frontend/locales/*.json）和这份胶水代码都归工具自己所有，
 * `toolbox-bridge.js`（宿主提供）只负责告诉这里"当前该用哪个语言、语言变了"，不托管任何
 * 字串。
 *
 * 这只是脚手架给的默认实现，不是强制约定——想用别的 i18n 方案（vue-i18n、react-intl……）
 * 直接删掉这个文件、按自己的技术栈接 bridge.locale / bridge.onLocaleChange 即可。
 */
(function (window) {
  'use strict';

  /**
   * @param {object} [options]
   * @param {string} [options.localesDir="locales/"] 字串目录相对 index.html 的路径
   * @param {string} [options.defaultLocale] 覆盖 manifest.json 里的 default（一般不需要传）
   */
  function createI18n(options) {
    options = options || {};
    var localesDir = options.localesDir || 'locales/';
    var manifestUrl = localesDir + 'manifest.json';

    var cache = {}; // locale -> 字串表
    var listeners = [];
    var current = null;
    var defaultLocale = options.defaultLocale || null;
    var availableLocales = [];

    function loadJson(url) {
      return fetch(url).then(function (resp) {
        return resp.json();
      });
    }

    function loadCatalog(locale) {
      if (cache[locale]) return Promise.resolve(cache[locale]);
      return loadJson(localesDir + locale + '.json')
        .then(function (dict) {
          cache[locale] = dict;
          return dict;
        })
        .catch(function () {
          cache[locale] = {};
          return cache[locale];
        });
    }

    function resolveLocale(locale) {
      if (availableLocales.indexOf(locale) !== -1) return locale;
      var base = String(locale || '').split('-')[0];
      if (availableLocales.indexOf(base) !== -1) return base;
      return defaultLocale;
    }

    function t(key, params) {
      var dict = cache[current] || {};
      var fallback = cache[defaultLocale] || {};
      var str = dict[key] || fallback[key] || key;
      if (params) {
        Object.keys(params).forEach(function (k) {
          str = str.split('{' + k + '}').join(params[k]);
        });
      }
      return str;
    }

    function applyDom(root) {
      var scope = root || document;
      scope.querySelectorAll('[data-i18n]').forEach(function (el) {
        el.textContent = t(el.getAttribute('data-i18n'));
      });
      // data-i18n-attr="placeholder:app.search.placeholder;title:app.search.title"
      scope.querySelectorAll('[data-i18n-attr]').forEach(function (el) {
        el.getAttribute('data-i18n-attr')
          .split(';')
          .forEach(function (rule) {
            var parts = rule.split(':');
            if (parts.length === 2) {
              el.setAttribute(parts[0].trim(), t(parts[1].trim()));
            }
          });
      });
    }

    function setLocale(locale) {
      var resolved = resolveLocale(locale);
      return loadCatalog(resolved).then(function () {
        var old = current;
        current = resolved;
        document.documentElement.setAttribute('lang', current);
        applyDom();
        if (resolved !== old) {
          listeners.forEach(function (fn) {
            fn(current, old);
          });
        }
      });
    }

    var bridge = window.MyBooksToolBridge;
    var ready = loadJson(manifestUrl)
      .then(function (manifest) {
        availableLocales = manifest.locales || [];
        defaultLocale = defaultLocale || manifest.default || availableLocales[0] || 'en';
        return loadCatalog(defaultLocale);
      })
      .then(function () {
        var initialLocale = (bridge && bridge.locale) || defaultLocale;
        return setLocale(initialLocale);
      });

    if (bridge && bridge.onLocaleChange) {
      bridge.onLocaleChange(function (newLocale) {
        setLocale(newLocale);
      });
    }

    return {
      ready: ready,
      t: t,
      locale: function () {
        return current;
      },
      setLocale: setLocale,
      applyDom: applyDom,
      // 用了前端框架、需要重新渲染而不是直接改 DOM 的工具，可以订阅这个而不是依赖 applyDom
      onChange: function (fn) {
        listeners.push(fn);
        return function unsubscribe() {
          listeners = listeners.filter(function (f) {
            return f !== fn;
          });
        };
      },
    };
  }

  window.MyBooksToolI18n = { create: createI18n };
})(window);
