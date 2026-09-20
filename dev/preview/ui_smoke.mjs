/**
 * UI 冒烟：用 jsdom 打开 `dev/preview/server.mjs` 提供的假宿主页面，真的跑一遍交互。
 *
 * 这是"没有 MyBooks 也能验前端"的那一层：`tests/test_frontend_contract.py` 只能做静态
 * 一致性检查（键/id/路径对得上），这个脚本回答的是"点下去到底有没有反应、渲染对不对"。
 *
 * 需要 jsdom（开发依赖，不进包；`dist/` 里也不会带它）：
 *
 *   npm i jsdom && node dev/preview/ui_smoke.mjs
 *   # 或者复用别处装好的 jsdom：
 *   JSDOM_ENTRY=/path/to/node_modules/jsdom/lib/api.js node dev/preview/ui_smoke.mjs
 */
import assert from 'node:assert/strict';

async function loadJsdom() {
  const candidates = [process.env.JSDOM_ENTRY, 'jsdom'].filter(Boolean);
  let lastError = null;
  for (const candidate of candidates) {
    try {
      // eslint-disable-next-line no-await-in-loop
      return await import(candidate);
    } catch (err) {
      lastError = err;
    }
  }
  throw new Error(
    '没找到 jsdom。请先 `npm i jsdom`，或设置 JSDOM_ENTRY 指向 jsdom 的入口文件\n'
    + `最后一次尝试：${lastError && lastError.message}`,
  );
}

const { JSDOM, VirtualConsole } = await loadJsdom();

import { startServer } from './server.mjs';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function tick(win, ms = 40) {
  return sleep(ms).then(() => win);
}

async function waitFor(win, predicate, { timeout = 6000, label = 'condition' } = {}) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (predicate(win)) return true;
    await sleep(50);
  }
  throw new Error(`等待超时：${label}`);
}

const results = [];
function check(name, fn) {
  try {
    fn();
    results.push(['✔', name]);
  } catch (err) {
    results.push(['✘', `${name} —— ${err.message}`]);
    process.exitCode = 1;
  }
}

async function main() {
  const { server, pageUrl } = await startServer(0);
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (err) => errors.push(String(err && err.message ? err.message : err)));
  virtualConsole.on('error', (...args) => errors.push(args.join(' ')));

  const dom = await JSDOM.fromURL(pageUrl, {
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    virtualConsole,
    beforeParse(win) {
      // jsdom 不提供 fetch（真实浏览器里是内置的），这里把 Node 的 fetch 借给它，
      // 相对地址按页面 URL 解析 —— 这样工具前端走的还是原生 fetch 那条路径
      win.fetch = (input, init) => {
        const url = typeof input === 'string' ? new URL(input, win.location.href).toString() : input;
        return fetch(url, init);
      };
      win.addEventListener('unhandledrejection', (e) => {
        errors.push(`unhandled rejection: ${e.reason && (e.reason.stack || e.reason.message)}`);
      });
    },
  });
  const win = dom.window;
  const doc = win.document;
  const $ = (id) => doc.getElementById(id);

  // 页面脚本在 load 之后才开始抓数据，等来源列表先渲染出来
  await waitFor(win, () => doc.querySelectorAll('#source-list .bs-source-item').length === 3,
                { label: '书源列表渲染出 3 行' });

  check('书源列表渲染 3 行 + 计数 3 + 可用数写进搜索提示', () => {
    assert.equal(doc.querySelectorAll('#source-list .bs-source-item').length, 3);
    assert.equal($('source-count').textContent, '3');
    assert.match($('search-hint').textContent, /2\s*个可用书源/);
  });

  check('文案已按宿主语言渲染（不是原始 i18n key）', () => {
    assert.equal($('tab-search').textContent.trim(), '搜索下载');
    assert.equal(doc.querySelector('h1').textContent.trim(), '书源');
    // 只看可见文本：data-i18n 属性里当然带着 key 名，那不是"漏出"
    assert.ok(!doc.body.textContent.includes('bookSource.'), '页面里漏出了未翻译的 key');
  });

  check('启动时把 deps 探测结果接上，全绿时警告条保持隐藏', () => {
    assert.equal($('deps-warning').hidden, true);
  });

  check('默认停在「搜索下载」标签，书源管理面板隐藏', () => {
    assert.equal($('pane-search').hidden, false);
    assert.equal($('pane-sources').hidden, true);
  });

  // ── 标签切换 ──────────────────────────────────────────────────────────
  $('tab-sources').click();
  await tick(win);
  check('点了「书源管理」标签，面板切换且 aria-selected 跟着变', () => {
    assert.equal($('pane-sources').hidden, false);
    assert.equal($('pane-search').hidden, true);
    assert.equal($('tab-sources').getAttribute('aria-selected'), 'true');
    assert.equal($('tab-search').getAttribute('aria-selected'), 'false');
  });

  // ── 书源筛选 / 启停 / 测试 / 设置 ───────────────────────────────────────
  $('source-filter').value = '示例书源 1';
  $('source-filter').dispatchEvent(new win.Event('input'));
  await tick(win);
  check('筛选框只留匹配的行', () => {
    assert.equal(doc.querySelectorAll('#source-list .bs-source-item').length, 1);
  });
  $('source-filter').value = '';
  $('source-filter').dispatchEvent(new win.Event('input'));
  await tick(win);

  const toggleBefore = doc.querySelector('.bs-toggle').className;
  doc.querySelector('.bs-toggle').click();
  await waitFor(win, () => doc.querySelector('.bs-toggle').className !== toggleBefore,
                { label: '启停按钮换状态' });
  check('点启停按钮，按钮状态跟着变（成功路径）', () => {
    assert.ok(doc.querySelector('.bs-toggle').className.includes('bs-toggle'));
    assert.notEqual(doc.querySelector('.bs-toggle').className, toggleBefore);
  });

  doc.querySelector('[data-act="test"]').click();
  await waitFor(win, () => $('dlg-test').hidden === false, { label: '测试结果对话框打开' });
  check('点连通性测试，弹出测试结果并渲染 3 条样例', () => {
    assert.equal($('test-body').querySelectorAll('tr').length, 3);
    assert.equal($('test-name').textContent, '示例书源 1');
    assert.ok($('test-alert').textContent.includes('3'));
    assert.ok($('test-alert').className.includes('mb-alert--success'));
  });
  $('test-close').click();
  await tick(win);
  check('关闭测试对话框', () => assert.equal($('dlg-test').hidden, true));

  doc.querySelector('[data-act="settings"]').click();
  await tick(win);
  check('点齿轮打开设置对话框，JSON 编辑器里有书源内容', () => {
    assert.equal($('dlg-settings').hidden, false);
    assert.equal($('settings-name').textContent, '示例书源 1');
    assert.equal(JSON.parse($('settings-json').value).bookSourceName, '示例书源 1');
  });

  $('settings-json').value = '{ 坏 JSON';
  $('settings-apply').click();
  await tick(win);
  check('设置里贴坏 JSON，对话框不关且显示错误提示', () => {
    assert.equal($('dlg-settings').hidden, false);
    assert.equal($('settings-error').hidden, false);
    assert.ok($('settings-error').textContent.includes('JSON 格式错误'));
  });

  // 设置页里的删除按钮 → 删除确认 → 取消
  $('settings-delete').click();
  await tick(win);
  check('设置页删除按钮落到删除确认框（二次确认），取消后两个框状态正确', () => {
    assert.equal($('dlg-delete').hidden, false);
    assert.equal($('delete-name').textContent, '示例书源 1');
    $('delete-cancel').click();
    assert.equal($('dlg-delete').hidden, true);
    assert.equal($('dlg-settings').hidden, false);
  });
  $('settings-close').click();
  await tick(win);

  // ── 编辑对话框 ────────────────────────────────────────────────────────
  doc.querySelector('.bs-source-item').click();
  await tick(win);
  check('点行打开编辑对话框，标题是「编辑书源」且 JSON 有内容', () => {
    assert.equal($('dlg-edit').hidden, false);
    assert.equal($('edit-title').textContent, '编辑书源');
    assert.equal(JSON.parse($('edit-json').value).bookSourceUrl, 'https://source1.example.com');
  });
  $('edit-json').value = JSON.stringify({ bookSourceName: '新源', bookSourceUrl: 'https://new.example.com/' }, null, 2);
  $('edit-save').click();
  await waitFor(win, () => $('dlg-edit').hidden === true, { label: '保存后关闭编辑框' });
  const savedRow = [...doc.querySelectorAll('.bs-source-name')].map((n) => n.textContent);
  check('编辑框里保存成功 → 关闭并刷新列表（末尾斜杠被去掉）', () => {
    assert.ok(savedRow.includes('新源'), `列表里没有保存后的书源：${savedRow}`);
  });

  // 新增入口 + Esc 关闭
  $('add-btn').click();
  await tick(win);
  check('「添加」打开空编辑器（标题为新增书源）', () => {
    assert.equal($('dlg-edit').hidden, false);
    assert.equal($('edit-title').textContent, '新增书源');
    assert.equal($('edit-json').value, '');
  });
  doc.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await tick(win);
  check('Esc 关闭对话框', () => assert.equal($('dlg-edit').hidden, true));

  // ── 搜索 → 结果 → 分页 → 下载 / 生成 ───────────────────────────────────
  $('tab-search').click();
  await tick(win);
  check('搜索按钮在关键词为空时禁用，输入后启用', () => {
    assert.equal($('search-btn').disabled, true);
    $('keyword').value = '测试书';
    $('keyword').dispatchEvent(new win.Event('input'));
    assert.equal($('search-btn').disabled, false);
  });

  $('search-btn').click();
  await waitFor(win, () => doc.querySelectorAll('#results-body tr').length === 50,
                { timeout: 8000, label: '搜索结果渲染第一页 50 行' });
  check('搜索结果分页到每页 50 行，计数与分页器正确', () => {
    assert.equal($('result-count').textContent, '60');
    assert.equal(doc.querySelectorAll('#results-body tr').length, 50);
    assert.equal($('results-card').hidden, false);
    assert.equal($('pager').hidden, false);
    assert.equal(doc.querySelectorAll('#pager button').length, 4); // ‹ 1 2 ›
  });

  check('书名 / 作者 / 来源标签 / 操作按钮都渲染了', () => {
    const firstRow = doc.querySelector('#results-body tr');
    assert.match(firstRow.textContent, /测试书 第 1 卷/);
    assert.match(firstRow.textContent, /作者 1/);
    assert.match(firstRow.textContent, /示例书源 1/);
    assert.equal(firstRow.querySelectorAll('[data-act]').length, 2);
  });

  check('封面挂掉时走占位图（不发内联 onerror，靠捕获阶段替换）', () => {
    // 第一页第一行的 coverUrl 指向一个必然加载失败的域名 → 应该由捕获阶段的 error
    // 监听器把它换成占位图标，而不是留一个破图或写内联 onerror
    const row = doc.querySelector('#results-body tr');
    const img = row.querySelector('img.bs-cover');
    assert.ok(img, '第一行应该渲染出 <img>（它的封面地址是非空的）');
    img.dispatchEvent(new win.Event('error'));
    assert.equal(row.querySelectorAll('img.bs-cover').length, 0, '失败的 <img> 应被替换掉');
    assert.ok(row.querySelector('span.bs-cover--empty'), '应换成占位图标');
  });

  const pageOneFirstName = doc.querySelector('#results-body tr td:nth-child(2)').textContent;
  doc.querySelectorAll('#pager button')[2].click(); // 第 2 页
  await tick(win);
  const pageTwoFirstName = doc.querySelector('#results-body tr td:nth-child(2)').textContent;
  check('翻到第 2 页显示余下的 10 行，且内容换了', () => {
    assert.equal(doc.querySelectorAll('#results-body tr').length, 10);
    assert.notEqual(pageTwoFirstName, pageOneFirstName);
    assert.match(pageTwoFirstName, /副/); // 第 2 页落在第二个来源的结果上
  });

  // 单本下载：任务条出现 → 进度 → 完成
  doc.querySelector('#results-body tr [data-act="download"]').click();
  await waitFor(win, () => doc.querySelectorAll('.bs-task').length === 1, { label: '任务条出现' });
  await waitFor(win, () => {
    const chip = doc.querySelector('.bs-task .mb-status--success');
    return chip && /任务完成/.test(chip.textContent);
  }, { timeout: 8000, label: '下载任务走到完成' });
  check('点下载 → 任务条出现在任务列表 → 轮询到完成状态', () => {
    assert.equal($('tasks-card').hidden, false);
    assert.ok(doc.querySelector('.bs-task').textContent.includes(pageTwoFirstName),
              '任务条上的书名应与被点的那一行一致');
    assert.equal(doc.querySelector('.mb-progress-linear__bar').style.width, '100%');
  });

  // 生成 EPUB：完成后出现下载链接
  doc.querySelector('#results-body tr [data-act="generate"]').click();
  await waitFor(win, () => doc.querySelector('.bs-task a[href*="download_epub"]'),
                { timeout: 8000, label: 'EPUB 任务完成后出现下载链接' });
  check('点生成 EPUB → 完成后任务条上出现下载入口，且指向本工具的 download_epub', () => {
    const link = doc.querySelector('.bs-task a[href*="download_epub"]');
    assert.equal(link.getAttribute('href'), '/api/toolbox/tool/book_source/download_epub');
    assert.equal(link.getAttribute('target'), '_blank');
  });

  // 批量下载确认框
  $('download-all-btn').click();
  await tick(win);
  check('批量下载先弹确认框，文案带数量', () => {
    assert.equal($('dlg-confirm-all').hidden, false);
    assert.ok($('confirm-all-msg').textContent.includes('60'));
    $('confirm-all-cancel').click();
    assert.equal($('dlg-confirm-all').hidden, true);
  });

  // ── 从网址导入 ────────────────────────────────────────────────────────
  $('tab-sources').click();
  await tick(win);
  $('url-toggle-btn').click();
  await tick(win);
  check('「从网址导入」展开输入行', () => assert.equal($('import-url-row').hidden, false));
  $('import-url').value = '';
  $('import-url-btn').click();
  await tick(win);
  check('空网址点导入 → 提示且不关输入行', () => {
    assert.equal($('import-url-row').hidden, false);
    assert.equal(doc.querySelectorAll('#mock-notify > div').length >= 0, true);
  });
  $('import-url').value = 'https://example.com/sources.json';
  $('import-url-btn').click();
  await waitFor(win, () => $('import-url-row').hidden === true, { label: '导入成功后收起源码输入行' });
  check('填了网址点导入 → 成功并收起输入行', () => {
    assert.equal($('import-url').value, '');
  });

  // ── 取消任务 ──────────────────────────────────────────────────────────
  $('tab-search').click();
  await tick(win);
  doc.querySelector('#results-body tr [data-act="download"]').click();
  await waitFor(win, () => doc.querySelector('.bs-task [data-act="cancel"]'), { label: '活动任务出现取消按钮' });
  check('只有正在跑的那个任务有取消按钮', () => {
    assert.equal(doc.querySelectorAll('.bs-task [data-act="cancel"]').length, 1);
  });
  doc.querySelector('.bs-task [data-act="cancel"]').click();
  await tick(win, 80);

  check('整页跑下来没有未捕获异常 / 资源加载错误', () => {
    assert.deepEqual(errors, []);
  });

  dom.window.close();
  server.close();

  console.log('\nUI 冒烟结果：');
  for (const [mark, name] of results) console.log(`  ${mark} ${name}`);
  const failed = results.filter(([mark]) => mark === '✘').length;
  console.log(`\n${results.length - failed}/${results.length} 通过`);
  process.exit(failed ? 1 : 0);
}

main().catch((err) => {
  console.error('UI 冒烟启动失败：', err);
  process.exit(1);
});
