/**
 * 本地预览 + UI 冒烟用的假宿主：静态文件服务 + `book_source` 那 15 条接口的内存实现。
 *
 * 脚手架没有 `mytool dev`（真机验证只能靠开发者模式装进 MyBooks），但前端页面本身完全
 * 可以脱离 MyBooks 跑：`toolbox-bridge.js` 的 mock + 这一份假接口就够了。用途有两个：
 *
 *   1. 人在浏览器里点一遍（`node dev/preview/server.mjs` 然后开
 *      http://127.0.0.1:8770/get/tool/book_source/index.html ）；
 *   2. `dev/preview/ui_smoke.mjs` 用 jsdom 加载同一个页面跑自动化断言（真 fetch、真 DOM）。
 *
 * 服务端刻意复刻宿主的三个行为细节，否则本地跑得通、装进 MyBooks 反而坏：
 *   - 页面从 `/get/tool/<tool_id>/index.html` 提供（bridge 靠这个路径反解 tool_id）；
 *   - 静态资源同前缀（`lib/theme.css` 相对页面解析）；
 *   - 接口挂在 `/api/toolbox/tool/<tool_id>/<action>`，回 `{"err": ...}` 信封。
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PKG_ROOT = path.resolve(HERE, '..', '..');
const FRONTEND = path.join(PKG_ROOT, 'frontend');
const TOOL_ID = 'book_source';
const PAGE_PATH = `/get/tool/${TOOL_ID}/index.html`;

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
};

function makeSource(i) {
  return {
    bookSourceName: `示例书源 ${i}`,
    bookSourceUrl: `https://source${i}.example.com`,
    bookSourceGroup: i % 2 ? '小说' : '未分组',
    bookSourceType: 0,
    enabled: i !== 3,
    searchUrl: '/search?q={{key}}',
    header: {},
    jsLib: '',
    ruleSearch: { bookList: 'class.list@li', name: 'tag.a@text', author: 'class.au@text', bookUrl: 'tag.a@href' },
    ruleBookInfo: { name: 'id.info@text' },
    ruleToc: { chapterList: 'id.toc@li', chapterName: 'tag.a@text', chapterUrl: 'tag.a@href' },
    ruleContent: { content: 'id.content@html', replaceRegex: [] },
  };
}

const state = {
  sources: [makeSource(1), makeSource(2), makeSource(3)],
  searchCalls: 0,
  searchFinished: false,
  task: null,
  epub: Buffer.from('PK\u0003\u0004mock-epub-bytes'),
};

function resetSearch() {
  state.searchCalls = 0;
  state.searchFinished = false;
}

function makeBooks(keyword, count, label = '') {
  const books = [];
  for (let i = 1; i <= count; i += 1) {
    books.push({
      name: `${keyword}${label} 第 ${i} 卷`,
      author: `作者 ${i}`,
      kind: '玄幻',
      wordCount: `${i}0000`,
      lastChapter: `第 ${i} 章 落幕`,
      intro: '简介',
      coverUrl: i === 1 && !label ? 'https://invalid.example.com/broken-cover.jpg' : '',
      bookUrl: `https://source.example.com/book/${i}`,
      sourceName: '示例书源 1',
    });
  }
  return books;
}

function json(res, payload, status = 200) {
  const body = Buffer.from(JSON.stringify(payload), 'utf-8');
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': body.length });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks)));
  });
}

async function handleApi(req, res, action, url) {
  if (req.method === 'GET' && action === 'list') {
    return json(res, { err: 'ok', data: state.sources });
  }
  if (req.method === 'GET' && action === 'deps') {
    return json(res, {
      err: 'ok',
      data: {
        engine: true, engine_error: '', js_rules: true, epub: true,
        deps: { requests: { ok: true, required: true, purpose: '' }, dukpy: { ok: true, required: false, purpose: '' } },
        missing_required: [],
      },
    });
  }
  if (req.method === 'GET' && action === 'test') {
    const name = url.searchParams.get('source');
    return json(res, {
      err: 'ok',
      data: { source: name, reachable: true, sample_count: 3, samples: makeBooks('测试', 3) },
    });
  }
  if (req.method === 'GET' && action === 'search_status') {
    state.searchCalls += 1;
    // 分两批返回，模拟多源逐步出结果
    const kw = state.keyword || '书';
    const first = state.searchCalls === 1;
    state.searchFinished = !first;
    const results = first
      ? [{ source_name: '示例书源 1', books: makeBooks(kw, 30) }]
      : [{ source_name: '示例书源 1', books: makeBooks(kw, 30) },
         { source_name: '示例书源 2', books: makeBooks(kw, 30, '（副）') }];
    return json(res, { err: 'ok', data: { finished: state.searchFinished, results } });
  }
  if (req.method === 'GET' && action === 'progress') {
    if (!state.task) return json(res, { err: 'ok', data: null });
    const task = state.task;
    task.tick += 1;
    if (task.tick === 1) {
      return json(res, { err: 'ok', data: { task_id: 7, progress: 40, status: 'running',
                                            progress_data: { status: '下载章节 [12/40]' } } });
    }
    task.done = true;
    return json(res, { err: 'ok', data: { task_id: 7, progress: 100, status: 'completed',
                                          progress_data: { status: '完成' } } });
  }
  if (req.method === 'GET' && action === 'download_epub') {
    res.writeHead(200, {
      'Content-Type': 'application/epub+zip',
      'Content-Disposition': "attachment; filename=\"mock.epub\"",
    });
    return res.end(state.epub);
  }

  const raw = await readBody(req);
  let body = {};
  if (raw.length && (req.headers['content-type'] || '').includes('json')) {
    try { body = JSON.parse(raw.toString('utf-8')); } catch { body = {}; }
  }

  switch (action) {
    case 'save': {
      const raw_ = body.raw;
      if (!raw_ || !raw_.bookSourceName) return json(res, { err: 'params.missing', msg: '请提供书源 JSON（含 bookSourceName）' });
      const idx = state.sources.findIndex((s) => s.bookSourceName === raw_.bookSourceName);
      const merged = Object.assign({}, raw_);
      if (idx === -1) state.sources.push(merged);
      else state.sources[idx] = Object.assign(state.sources[idx], merged);
      return json(res, { err: 'ok', data: { status: idx === -1 ? 'added' : 'updated', name: merged.bookSourceName } });
    }
    case 'toggle': {
      const target = state.sources.find((s) => s.bookSourceName === body.name);
      if (!target) return json(res, { err: 'book_source.not_found', msg: `书源不存在: ${body.name}` });
      target.enabled = !target.enabled;
      return json(res, { err: 'ok', data: { status: 'toggled', enabled: target.enabled } });
    }
    case 'delete': {
      const before = state.sources.length;
      state.sources = state.sources.filter((s) => s.bookSourceName !== body.name);
      if (state.sources.length === before) return json(res, { err: 'book_source.not_found', msg: '书源不存在' });
      return json(res, { err: 'ok', data: { status: 'deleted' } });
    }
    case 'search_async':
      if (!body.keyword) return json(res, { err: 'params.missing', msg: '请提供搜索关键词' });
      state.keyword = body.keyword;
      resetSearch();
      return json(res, { err: 'ok', data: { task_id: 'mock-task', total: 2 } });
    case 'download':
    case 'generate_epub':
      if (!body.source || !body.bookUrl) return json(res, { err: 'params.missing', msg: '请提供书源与书籍地址' });
      state.task = { tick: 0, done: false, kind: action };
      return json(res, { err: 'ok', msg: '任务已启动，右上角可以查看进度' });
    case 'cancel':
      state.task = null;
      return json(res, { err: 'ok', msg: '任务已取消' });
    case 'import_zip':
    case 'import_url':
      return json(res, { err: 'ok', msg: '导入成功，新增 1 个书源', data: { added: 1, updated: 0, skipped: 0 } });
    default:
      return json(res, { err: 'not_found', msg: `未知接口 ${action}` }, 404);
  }
}

function serveFile(res, filePath) {
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    return res.end('not found');
  }
  const body = fs.readFileSync(filePath);
  res.writeHead(200, {
    'Content-Type': MIME[path.extname(filePath)] || 'application/octet-stream',
    'Content-Length': body.length,
  });
  return res.end(body);
}

export function createServer() {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const pathname = decodeURIComponent(url.pathname);

    if (pathname === '/static/toolbox-bridge.js') {
      return serveFile(res, path.join(HERE, 'toolbox-bridge.js'));
    }
    if (pathname.startsWith(`/api/toolbox/tool/${TOOL_ID}/`)) {
      const action = pathname.slice(`/api/toolbox/tool/${TOOL_ID}/`.length);
      return handleApi(req, res, action, url);
    }
    if (pathname === PAGE_PATH || pathname === `/get/tool/${TOOL_ID}/`) {
      return serveFile(res, path.join(FRONTEND, 'index.html'));
    }
    if (pathname.startsWith(`/get/tool/${TOOL_ID}/`)) {
      const rel = pathname.slice(`/get/tool/${TOOL_ID}/`.length);
      const target = path.resolve(FRONTEND, rel);
      if (!target.startsWith(FRONTEND)) {
        res.writeHead(403, { 'Content-Type': 'text/plain' });
        return res.end('forbidden');
      }
      return serveFile(res, target);
    }
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    return res.end('not found');
  });
}

export function startServer(port = 8770) {
  const server = createServer();
  return new Promise((resolve) => {
    server.listen(port, '127.0.0.1', () => {
      const actual = server.address().port;   // 传 0 时由系统分配，要回真实端口
      resolve({ server, port: actual, pageUrl: `http://127.0.0.1:${actual}${PAGE_PATH}` });
    });
  });
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const port = Number(process.env.PORT || 8770);
  startServer(port).then(({ pageUrl }) => {
    console.log('本地预览已启动（假数据，不需要 MyBooks）：');
    console.log('  ' + pageUrl);
    console.log('  ' + pageUrl + '?theme=dark&locale=en    # 深色 + 英文');
    console.log('Ctrl+C 退出。');
  });
}
