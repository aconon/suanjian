/* 算件 · 单页逻辑
 * 结构：工具 → 口令门 → 全局错误标记（自查用）→ /api/* 正式路由（失败回落演示数据）
 *      → 视图状态机（加载/空/错误/就绪）→ CSV 解析校验 → 各视图渲染 → 路由
 * 后端接入点（与 server.py 一致，见 docs/任务单/03 与 05-持久化与OCR接线）：
 *      POST /api/ingest/csv（确认导入）、POST /api/settle（核算，个人/班组两模式）、
 *      GET /api/payslip?gross=（逐工人工工资条）、GET /api/health（可用性探测）、
 *      POST /api/ocr（拍照识别→预填表）、GET /api/tickets（工票列表）、
 *      POST /api/tickets/save（拍照确认入账）、GET /api/history（核算历史）、
 *      GET /api/export/payslips（工资条 Excel 导出，fetch+blob 下载带 X-Code）、
 *      GET /api/trend（近 6 期趋势：总产值/报废率/人均产出）、
 *      GET /api/compare[?p1=&p2=]（期间对比：两期逐人合格数量/计件/实发+变化，
 *      新出现/消失工人分组；缺省=最近两期）
 * 口令门（任务单 07，塔罗小屋同款）：服务端 config.json 设了 code 才有门——
 *      /api 401 → 清口令重弹浮层；输对存 sessionStorage 本会话不再问
 * 约定：API 返回的金额/数量是精确字符串（防 float 污染），展示时才转 Number，
 *      不做金额 == 比较；发回服务端的数值一律走 String() 保字面值。
 * 期间口径（v1.4）：前端镜像引擎月切日（CUTOFF_DAY=26，上月 26 → 本月 25 一期），
 *      核算页「核算期间」显示期边界而非工票数据首尾日期；规则改动必须两边同步。
 * 自查钩子：?state=loading|empty|error 可强制任意视图的态；JS 出错会写到 <body data-js-error>
 */
'use strict';
(function () {

  /* ---------- 工具 ---------- */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmtMoney(n) {
    var v = Number(n);
    if (!isFinite(v)) return '—';
    return '¥' + v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtInt(n) {
    var v = Number(n);
    return isFinite(v) ? v.toLocaleString('zh-CN') : '—';
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  /* 拍照路径缩略图：整张 dataURL 太大（8MB 照片≈10MB 文本），innerHTML 里拖着走会卡，
     先用 canvas 压到长边 480px 再进视图；解码失败就退回原图 */
  function makeThumb(dataUrl, cb) {
    var img = new Image();
    img.onload = function () {
      try {
        var k = Math.min(1, 480 / Math.max(img.width || 1, img.height || 1));
        var cv = document.createElement('canvas');
        cv.width = Math.max(1, Math.round((img.width || 1) * k));
        cv.height = Math.max(1, Math.round((img.height || 1) * k));
        cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
        cb(cv.toDataURL('image/jpeg', 0.8));
      } catch (e) { cb(dataUrl); }
    };
    img.onerror = function () { cb(dataUrl); };
    img.src = dataUrl;
  }

  /* 线条图标（lucide 风，stroke 1.75） */
  var ICONS = {
    inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11Z"/>',
    receipt: '<path d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1Z"/><path d="M8 7h8M8 11h8M8 15h5"/>',
    calc: '<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M8 6h8"/><path d="M8 10h.01M12 10h.01M16 10h.01M8 14h.01M12 14h.01M16 14h.01M8 18h.01M12 18h.01M16 18h.01"/>',
    banknote: '<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="2"/><path d="M6 12h.01M18 12h.01"/>',
    history: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5M12 7v5l4 2"/>',
    alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 20h16a2 2 0 0 0 1.73-2Z"/><path d="M12 9v4M12 17h.01"/>',
    upload: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M12 12v6M9 15l3 3 3-3"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    chart: '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="m7 14 4-4 4 4 5-5"/>',
    camera: '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3Z"/><circle cx="12" cy="13" r="3"/>',
    printer: '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><path d="M6 9V3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v6"/><rect x="6" y="14" width="12" height="8" rx="1"/>',
    gear: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>'
  };
  function icon(name, size, color) {
    return '<svg width="' + (size || 24) + '" height="' + (size || 24) + '" viewBox="0 0 24 24" fill="none" ' +
      'stroke="' + (color || 'var(--text-3)') + '" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      (ICONS[name] || '') + '</svg>';
  }

  /* 轻提示 */
  var toastTimer = null;
  function toast(msg) {
    var el = $('#toast');
    el.textContent = msg;
    el.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2400);
  }

  /* ---------- 口令门（任务单 07，塔罗小屋同款机制） ---------- */
  /* 口令只进 sessionStorage 与请求头 X-Code，不落 console/页面常驻；
     隐私模式 sessionStorage 会抛异常，一律吞掉（顶多每次重输）。 */
  var CODE_KEY = 'sj_code';
  function getCode() {
    try { return sessionStorage.getItem(CODE_KEY) || ''; } catch (e) { return ''; }
  }
  function setCode(v) {
    try { sessionStorage.setItem(CODE_KEY, v); } catch (e) { /* 存不进就算了 */ }
  }
  function clearCode() {
    try { sessionStorage.removeItem(CODE_KEY); } catch (e) { }
  }
  function showGate(msg) {
    var g = $('#codeGate');
    if (!g) return;
    g.hidden = false;
    var inp = $('#codeInput');
    if (inp && !msg) inp.value = '';   // 有错误提示时保留现场，只清空重来
    var err = $('#codeErr');
    if (err) { err.textContent = msg || ''; err.hidden = !msg; }
    if (inp) inp.focus();
  }
  function hideGate() {
    var g = $('#codeGate');
    if (g) g.hidden = true;
  }
  function submitCode() {
    var inp = $('#codeInput');
    var err = $('#codeErr');
    var btn = $('#codeSubmit');
    var val = ((inp && inp.value) || '').trim();
    if (!val) {
      if (err) { err.textContent = '口令不能是空'; err.hidden = false; }
      return;
    }
    if (btn) btn.disabled = true;
    setCode(val);                        // 先暂存：验对了才真正留下（api() 会带上）
    api('/tickets').then(function () {   // 拿真接口验口令（/api/health 豁免验不了）
      hideGate();
      toast('口令对上了');
      /* 被 401 打断的装载可能停在半路：输对后强制重拉真数据 */
      store.tickets.loaded = false;
      store.history.loaded = false;
      store.compare.loaded = false;
      showView(currentRoute);
    }).catch(function (err2) {
      var m = (err2 && err2.status === 429)
        ? err2.message  /* 试错锁定：服务端人话（锁 X 分钟），照实说 */
        : (err2 && err2.status === 401)
          ? '口令不对，再输一次'
          : '连不上服务（' + ((err2 && err2.message) || '网络问题') + '），等一下再试';
      if (err) { err.textContent = m; err.hidden = false; }
      if (inp) { inp.value = ''; inp.focus(); }
    }).then(function () {
      if (btn) btn.disabled = false;     // 不管成败都放开按钮（输错可重输）
    });
  }

  /* ---------- 全局错误标记（无头自查 grep 用） ---------- */
  function onGlobalError(e) {
    document.body.dataset.jsError = String((e && e.message) || 'error');
  }
  function onUnhandledRejection(e) {
    var r = e && e.reason;
    document.body.dataset.jsError = 'unhandledrejection: ' + String((r && r.message) || r);
  }
  window.addEventListener('error', onGlobalError);
  window.addEventListener('unhandledrejection', onUnhandledRejection);

  /* ---------- /api/* 正式路由：先走真接口，不通则回落演示数据 ---------- */
  var usingDemo = false;
  function markDemo() {
    if (!usingDemo) { usingDemo = true; var b = $('#demoBadge'); if (b) b.hidden = false; }
  }
  function api(path, opts) {
    opts = opts || {};
    var timeoutMs = opts.timeoutMs || 3000;  // 默认 3s；settle/ingest/ocr 等长请求按调用放宽（F8）
    var fetchOpts = Object.assign({}, opts);
    delete fetchOpts.timeoutMs;               // 非标准字段不进 fetch
    var headers = Object.assign({}, opts.headers);
    var code = getCode();
    /* F6：百分号编码传输——HTTP 头按 latin-1 走，中文口令原样放头必成乱码；
       encodeURIComponent 后是纯 ASCII，服务端 unquote 还原比对 */
    if (code) headers['X-Code'] = encodeURIComponent(code);
    fetchOpts.headers = headers;
    var ctrl = ('AbortController' in window) ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, timeoutMs) : null;
    var p = fetch('/api' + path, ctrl ? Object.assign({ signal: ctrl.signal }, fetchOpts) : fetchOpts)
      .then(function (res) {
        if (res.status === 401 || res.status === 429) {
          /* 口令门统一收口：401=口令不对/没带，429=试错太多次被锁。清口令重弹，
             本轮调用照常报错；各视图的 catch 里这两态一律让路（不许掉进演示回落） */
          clearCode();
          showGate();
        }
        if (!res.ok) {
          // 业务校验错（400 等）带服务端的人话 msg；网络/代理错保底 HTTP 状态码
          return res.json().catch(function () { return null; }).then(function (body) {
            var err = new Error((body && body.error) || ('HTTP ' + res.status));
            err.status = res.status;
            throw err;
          });
        }
        return res.json();
      })
      .finally(function () { if (timer) clearTimeout(timer); });
    return p;
  }

  /* ---------- 视图状态 ---------- */
  var forceState = null;   // ?state=loading|empty|error 自查钩子
  try {
    var qs = new URLSearchParams(location.search);
    var s = qs.get('state');
    if (s === 'loading' || s === 'empty' || s === 'error') forceState = s;
  } catch (e) { /* 老浏览器无 URLSearchParams 就跳过钩子 */ }

  var store = {
    tickets: { loaded: false, status: 'loading', data: [] },
    calc:    { loaded: false, status: 'empty',  data: null, mode: 'individual', teamTotal: '' },
    payslip: { loaded: false, status: 'empty',  data: null },
    trend:   { loaded: false, status: 'loading', data: [] },
    history: { loaded: false, status: 'loading', data: [] },
    compare: { loaded: false, status: 'loading', data: null, p1: '', p2: '', onlineEmpty: '', periods: [] },
    import:  { mode: 'drop' }   // drop | parsing | preview | error | ocr-uploading | ocr-review | ocr-error | ocr-saving
  };
  var ui = { expandedTicket: null, expandedWorker: null };

  function effStatus(v) { return forceState || v.status; }

  /* ---------- 数据装载（真接口优先，演示回落） ---------- */
  /* GET /api/tickets（05 契约：列表含行）拉已入库的票；服务不可用回落演示数据。
     老版本服务没有该路由（404）→ 退回 /health 探测，行为与接库前一致（在线=空态待导入） */
  function loadTickets(force) {
    var v = store.tickets;
    if (v.loaded && !force) return Promise.resolve();
    v.status = 'loading'; render(currentRoute);
    var demoFill = function () {
      markDemo();
      return sleep(200).then(function () {
        if (!v.data || !v.data.length) {
          v.data = window.DEMO ? window.DEMO.sheets.slice() : [];
          v.demoData = true;  // 当前工票列表是离线演示用的内置数据，真实导入时整体让位（见 mergeImported）
        }
        v.status = 'ready';
      });
    };
    return api('/tickets')
      .then(function (out) {
        var list = normalizeTicketList(out);
        if (list == null) throw new Error('工票列表形状不认识');
        if (list.length || !v.data || !v.data.length) {
          v.data = normalizeSheets(list); v.demoData = false;
        }
        v.status = 'ready';
      })
      .catch(function (err) {
        if (err && (err.status === 401 || err.status === 429)) return;  // 口令门已重弹/锁定，让路（不进演示回落）
        if (err && err.status === 404) {
          return api('/health').then(function () {
            v.data = v.data || []; v.demoData = false; v.status = 'ready';
          }).catch(demoFill);
        }
        return demoFill();
      })
      .then(function () { v.loaded = true; render(currentRoute); });
  }
  /* GET /api/history（05 契约：按期聚合）。服务不可用回落演示数据+徽章（机制沿用 F20）；
     老版本服务 404 → /health 探测保持在线空态 */
  function loadHistory(force) {
    var v = store.history;
    if (v.loaded && !force && v.data && v.data.length) return Promise.resolve();
    v.status = 'loading'; render(currentRoute);
    var demoFill = function () {
      markDemo();
      return sleep(200).then(function () {
        /* 演示历史只在首次（本地没记录）填充，之后不覆盖会话里的核算记录（F20） */
        if (!v.data || !v.data.length) {
          v.data = window.DEMO ? window.DEMO.history.slice() : [];
        }
        v.status = 'ready';
      });
    };
    return api('/history')
      .then(function (out) {
        var list = normalizeHistory(out);
        /* 服务端是底账权威：有记录就整体采用；服务还没底账时保留会话里的核算记录 */
        if (list.length || !v.data || !v.data.length) v.data = list;
        v.status = 'ready';
      })
      .catch(function (err) {
        if (err && (err.status === 401 || err.status === 429)) return;  // 口令门已重弹/锁定，让路（不进演示回落）
        if (err && err.status === 404) {
          return api('/health').then(function () {
            v.data = v.data || []; v.status = 'ready';
          }).catch(demoFill);
        }
        return demoFill();
      })
      .then(function () { v.loaded = true; render(currentRoute); });
  }

  /* GET /api/trend（近 6 期：总产值/报废率/人均产出）。机制沿用 loadHistory：
     401 让路口令门、404（老服务）→ /health 探测保在线空态、不通 → 演示回落+徽章 */
  function loadTrend(force) {
    var v = store.trend;
    if (v.loaded && !force && v.data && v.data.length) return Promise.resolve();
    v.status = 'loading'; render(currentRoute);
    var demoFill = function () {
      markDemo();
      return sleep(200).then(function () {
        if (!v.data || !v.data.length) v.data = demoTrendItems();
        v.status = 'ready';
      });
    };
    return api('/trend')
      .then(function (out) {
        v.data = normalizeTrend(out);
        v.status = 'ready';
      })
      .catch(function (err) {
        if (err && (err.status === 401 || err.status === 429)) return;  // 口令门已重弹/锁定，让路（不进演示回落）
        if (err && err.status === 404) {
          return api('/health').then(function () {
            v.data = v.data || []; v.status = 'ready';
          }).catch(demoFill);
        }
        return demoFill();
      })
      .then(function () { v.loaded = true; render(currentRoute); });
  }

  /* 离线演示趋势：从 DEMO.history（新期在前）倒成升序；演示历史没有报废口径
     → null（宁缺勿编，图表这一项显示提示文案） */
  function demoTrendItems() {
    if (!window.DEMO || !window.DEMO.history) return [];
    return window.DEMO.history.slice().reverse().map(function (h) {
      var total = Number(h.total) || 0;
      var workers = Number(h.workers) || 0;
      return {
        period: String(h.period || ''), settlements: null,
        totalStr: String(h.total), total: total,
        scrapRate: null, scrapStr: '',
        workers: workers,
        perCapita: workers > 0 ? total / workers : null,
        perCapitaStr: workers > 0 ? String(Math.round(total / workers * 100) / 100) : ''
      };
    });
  }

  /* GET /api/trend 响应 → 趋势视图模型：服务端精确字符串原样保留（totalStr/
     scrapStr/perCapitaStr 供表格展示），Number 只进图表比例计算 */
  function normalizeTrend(out) {
    var list = (out && out.items) || out;
    if (!Array.isArray(list)) return [];
    return list.map(function (i) {
      i = i || {};
      return {
        period: String(i.period || ''),
        settlements: i.settlements != null ? Number(i.settlements) : null,
        totalStr: i.total_amount != null ? String(i.total_amount) : '',
        total: Number(i.total_amount) || 0,
        scrapRate: i.scrap_rate != null ? Number(i.scrap_rate) : null,
        scrapStr: i.scrap_rate != null ? String(i.scrap_rate) : '',
        workers: i.workers != null ? Number(i.workers) : null,
        perCapita: i.per_capita != null ? Number(i.per_capita) : null,
        perCapitaStr: i.per_capita != null ? String(i.per_capita) : ''
      };
    });
  }

  /* GET /api/compare[?p1=&p2=]（任务单 10：两期逐人对比）。机制沿用 loadTrend：
     401 让路口令门、404（老服务）→ /health 探测保在线空态、不通 → 演示回落+徽章；
     400 = 服务在线但凑不出对比（如只有一期）→ 人话上屏，**不进演示回落** */
  function loadCompare(force) {
    var v = store.compare;
    if (v.loaded && !force) return Promise.resolve();
    v.status = 'loading'; render(currentRoute);
    var qs = (v.p1 && v.p2)
      ? ('?p1=' + encodeURIComponent(v.p1) + '&p2=' + encodeURIComponent(v.p2)) : '';
    var demoFill = function () {
      markDemo();
      return sleep(200).then(function () {
        var d = demoCompare();
        /* R8-F3：演示期标签（如「08-25 期」）不进 store——置空串，下次装载走
           缺省（最近两期），服务恢复后不再带演示标签发 400 */
        v.p1 = ''; v.p2 = '';
        if (d) {
          v.data = d;
          v.periods = d.periods || [];   /* 演示期表只喂选择器选项，不当请求参数 */
        } else { v.data = null; v.onlineEmpty = '离线演示数据也不够两期，连上服务再来看。'; }
        v.status = 'ready';
      });
    };
    return api('/compare' + qs)
      .then(function (out) {
        v.data = normalizeCompare(out);
        if (v.data) { v.p1 = v.data.p1; v.p2 = v.data.p2; }  /* 缺省装载回填选中期 */
        v.periods = (v.data && v.data.periods) || [];        /* R8-F1：记住期表（空态选择器用） */
        v.onlineEmpty = '';
        v.status = 'ready';
      })
      .catch(function (err) {
        if (err && (err.status === 401 || err.status === 429)) return;  // 口令门已重弹/锁定，让路（不进演示回落）
        if (err && err.status === 404) {
          return api('/health').then(function () {
            v.data = null;
            v.onlineEmpty = '这个版本的服务还没有「对比」，更新一下服务再用。';
            v.status = 'ready';
          }).catch(demoFill);
        }
        if (err && err.status === 400) {
          /* R8-F1：400=这次请求的参数有毒（倒序/期没记录/同参）→ 清掉 v.p1/v.p2，
             下次进页/重试走缺省（最近两期）自恢复；期表留着喂空态选择器 */
          v.data = null;
          v.p1 = ''; v.p2 = '';
          v.onlineEmpty = (err && err.message) || '';
          v.status = 'ready';
          return;
        }
        return demoFill();
      })
      .then(function () { v.loaded = true; render(currentRoute); });
  }

  /* 离线演示对比：DEMO.history 只有期合计（无逐人明细），只演示摘要层，
     逐人明细宁缺勿编（workers 空 + 演示口径提示）。history 新期在前。 */
  function demoCompare() {
    if (!window.DEMO || !window.DEMO.history || window.DEMO.history.length < 2) return null;
    var h = window.DEMO.history;
    var older = h[1], newer = h[0];
    var t1 = Number(older.total) || 0, t2 = Number(newer.total) || 0;
    var diff = Math.round((t2 - t1) * 100) / 100;
    var rate = t1 > 0 ? String(Math.round(diff / t1 * 10000) / 100) : null;
    var piece = { p1: String(older.total), p2: String(newer.total),
      diff: String(diff), rate: rate };
    return {
      p1: String(older.period || ''), p2: String(newer.period || ''),
      periods: h.map(function (x) { return String(x.period || ''); }),
      workers: [], newWorkers: [], goneWorkers: [],
      totals: { qualified: { p1: null, p2: null, diff: null, rate: null },
                piece: piece, net: { p1: null, p2: null, diff: null, rate: null } },
      demo: true
    };
  }

  /* GET /api/compare 响应 → 对比视图模型（契约见 server.py api_compare）：
     服务端精确字符串原样保留（表格/数字展示），Number 只进条形比例计算；
     rate/缺口径字段可为 null（宁缺勿编，视图侧显示「—」） */
  function normalizeCompare(out) {
    if (!out || !out.p1 || !out.p2) return null;
    var s = function (x) { return x != null ? String(x) : null; };
    var block = function (b) {
      b = b || {};
      return { p1: s(b.p1), p2: s(b.p2), diff: s(b.diff), rate: s(b.rate) };
    };
    var solo = function (w, side) {
      w = w || {};
      var r = w[side] || {};
      return { name: String(w.name || ''),
        qualified: s(r.qualified), piece: s(r.piece), net: s(r.net),
        pieceNum: Number(r.piece) || 0 };
    };
    return {
      p1: String(out.p1), p2: String(out.p2),
      periods: (out.periods || []).map(String),
      workers: (out.workers || []).map(function (w) {
        w = w || {};
        return {
          name: String(w.name || ''),
          qualified: block(w.qualified), piece: block(w.piece), net: block(w.net),
          pieceNum1: Number(w.piece && w.piece.p1) || 0,
          pieceNum2: Number(w.piece && w.piece.p2) || 0
        };
      }),
      newWorkers: (out.new_workers || []).map(function (w) { return solo(w, 'p2'); }),
      goneWorkers: (out.gone_workers || []).map(function (w) { return solo(w, 'p1'); }),
      totals: {
        qualified: block(out.totals && out.totals.qualified),
        piece: block(out.totals && out.totals.piece),
        net: block(out.totals && out.totals.net)
      },
      demo: false
    };
  }

  /* GET /api/tickets 响应 → 前端工票视图模型。
     字段名跟 core/db.py list_tickets 对齐（sheet_no / rows[PieceRecord.to_dict]），
     读侧做别名容错（no / price）；金额=精确字符串，进视图前才转 Number */
  function normalizeTicketList(out) {
    var list = (out && (out.tickets || out.items)) || out;
    if (!Array.isArray(list)) return null;
    return list.map(function (t) {
      t = t || {};
      var no = t.sheet_no != null ? t.sheet_no : t.no;
      return {
        no: String(no == null ? '' : no), workshop: String(t.workshop || ''), date: String(t.date || ''),
        rows: (t.rows || []).map(function (r) {
          r = r || {};
          return {
            process: String(r.process || ''), name: String(r.name || ''),
            qty: Number(r.qty), price: Number(r.unit_price != null ? r.unit_price : r.price),
            defect: Number(r.defect || 0),
            amountStr: r.amount != null ? String(r.amount) : null  // 服务端权威金额（有才用）
          };
        })
      };
    });
  }

  /* GET /api/history 响应 → 历史视图模型。
     两类形状都认：服务端按期聚合条目（{period,count,modes,total_amount,first_at,last_at}，
     见 server.py api_history）与结算底账明细（{period,mode,result,created_at}，对齐
     core/db.py list_settlements）；tickets/workers 缺信息时置 null（视图侧不显示该段，
     不显示「0 张工票」骗人） */
  function normalizeHistory(out) {
    var list = (out && (out.items || out.settlements || out.history)) || out;
    if (!Array.isArray(list)) return [];
    var modeCn = function (m) {
      return m === 'team' ? '班组计件' : (m === 'individual' ? '个人计件' : '');
    };
    return list.map(function (h) {
      h = h || {};
      var res = h.result || {};
      var workers = h.workers != null ? h.workers
        : ((res.by_person || res.lines || []).length || null);
      var total = h.total != null ? h.total
        : (h.total_amount != null ? h.total_amount
          : (res.total_amount != null ? res.total_amount : res.team_total));
      var period = h.period || h.label || '';
      var modeRaw = h.mode || (h.modes && h.modes.length ? h.modes[0] : '');
      return {
        period: period ? (/期$/.test(period) ? period : period + ' 期') : '未归期',
        time: h.time || h.created_at || h.last_at || h.first_at || '',
        mode: modeCn(modeRaw),
        count: h.count != null ? Number(h.count) : null,
        tickets: h.tickets != null ? Number(h.tickets) : null,
        workers: workers == null ? null : Number(workers),
        total: Number(total) || 0,  // 精确字符串 → 展示 Number
        status: h.status || '已核算'
      };
    });
  }

  function normalizeSheets(list) {
    return (list || []).map(function (s) {
      var rows = (s && s.rows) || [];
      var cents = 0;
      rows.forEach(function (r) { cents += rowAmountCents(r); });
      return { no: s.no, workshop: s.workshop, date: s.date, rows: rows, rowCount: rows.length, total: Math.round(cents) / 100 };
    });
  }

  /* 行金额（分）：服务端返回值优先（amountStr=引擎按分取整的精确字符串）；
     没有服务端值（导入后未核算）才本地估算，且走分整数避免浮点尾差 */
  function rowAmountCents(r) {
    if (r.amountStr != null) return Math.round(Number(r.amountStr) * 100);
    return Math.round((Number(r.qty) - Number(r.defect || 0)) * Number(r.price) * 100);
  }

  /* ---------- 核算 ---------- */
  /* 展示用核算期间：月切日口径（与 core/engine.py CUTOFF_DAY=26 同一条规则的
     前端镜像，契约测试逐例与引擎对数）。期 = 上月 26 日（含）~ 本月 25 日（含），
     期标签取期内的"本月"：2026-08-26 ~ 2026-09-25 = 2026-09 期。
     单期 → 显示期边界（08-26→09-25），不再冒充工票数据的首尾日期；
     跨期/认不出的日期 → mixed，退回票面日期并如实标注（宁缺勿编）。 */
  var CUTOFF_DAY = 26;
  function periodOfDate(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s == null ? '' : s).trim());
    if (!m) return null;
    var y = Number(m[1]), mo = Number(m[2]), d = Number(m[3]);
    if (d >= CUTOFF_DAY) { mo += 1; if (mo > 12) { mo = 1; y += 1; } }  // 26 日起归下一期（跨年自动进位）
    return y + '-' + (mo < 10 ? '0' + mo : '' + mo);
  }
  function periodBounds(p) {
    var m = /^(\d{4})-(\d{2})$/.exec(String(p == null ? '' : p));
    if (!m) return null;
    var y = Number(m[1]), mo = Number(m[2]);
    var pm = mo - 1, py = y;
    if (pm < 1) { pm = 12; py -= 1; }
    var two = function (n) { return (n < 10 ? '0' : '') + n; };
    return { start: py + '-' + two(pm) + '-' + CUTOFF_DAY, end: y + '-' + two(mo) + '-25' };
  }
  function periodFromTickets() {
    var rule = '江苏版 · 个人计件';
    var dates = [];
    (store.tickets.data || []).forEach(function (s) { if (s.date) dates.push(s.date); });
    if (!dates.length) return { rule: rule };
    var seen = Object.create(null), n = 0;
    dates.forEach(function (d) {
      var p = periodOfDate(d);
      if (p && !seen[p]) { seen[p] = 1; n++; }
    });
    if (n === 1) {
      var label = Object.keys(seen)[0];
      var b = periodBounds(label);
      return { label: label + ' 期', start: b.start, end: b.end, rule: rule };
    }
    /* 跨期/有认不出的日期：不能冒充单期月切日口径，退回数据首尾并如实标注 */
    dates.sort();
    var end = dates[dates.length - 1];
    return { label: end.slice(5) + ' 期', start: dates[0], end: end, rule: rule, mixed: true };
  }
  /* 本批工票的唯一核算期（settle 归期用，R9-F3）：全部工票日期都归到同一期
     才返回 'YYYY-MM'；跨期/没有日期返回 ''（宁缺勿编，与 periodFromTickets
     的 mixed/空口径一致）。班组计件服务端不会自动归期（成员行没有日期），
     前端把显示的这个期随 body 传过去，班组数据才能进趋势/对比/导出。 */
  function periodKeyFromTickets() {
    var seen = Object.create(null), n = 0;
    (store.tickets.data || []).forEach(function (s) {
      if (!s.date) return;
      var p = periodOfDate(s.date);
      if (p && !seen[p]) { seen[p] = 1; n++; }
    });
    return n === 1 ? Object.keys(seen)[0] : '';
  }
  /* /api/settle 结果（金额/数量=精确字符串）→ 核算视图数据（展示时才转 Number） */
  function settleToCalcView(out) {
    var detailsBy = Object.create(null);  // 工人名做键：不继承原型（"constructor" 等名不得撞）
    (out.lines || []).forEach(function (l) {
      (detailsBy[l.name] = detailsBy[l.name] || []).push({
        process: l.process, qty: Number(l.qty), defect: Number(l.defect),
        price: Number(l.unit_price), amount: Number(l.amount)
      });
    });
    var workers = (out.by_person || []).map(function (p) {
      return {
        name: p.name, qty: Number(p.qualified_qty), defect: Number(p.defect_qty),
        amount: Number(p.amount),
        amountStr: p.amount,  // 服务端原字符串直存：发 /api/payslip 用，不经 Number 往返
        details: detailsBy[p.name] || []
      };
    }).sort(function (a, b) { return b.amount - a.amount; });
    return {
      real: true, workers: workers,
      total: Number(out.total_amount), totalDefect: Number(out.total_defect),
      period: periodFromTickets()
    };
  }
  /* 逐工人 GET /api/payslip?gross=（全员成功才算真接口通过，任一失败走演示回落） */
  function fetchRealPayslips(calcView) {
    return Promise.all(calcView.workers.map(function (w) {
      return api('/payslip?gross=' + encodeURIComponent(w.amountStr)).then(function (ps) {
        return {
          name: w.name, piece: w.amount, subsidy: 0,
          gross: Number(ps.gross), tax: Number(ps.tax), net: Number(ps.net),
          details: w.details, qty: w.qty, defect: w.defect,
          taxNote: ps.tax_note || ''
        };
      });
    })).then(function (ps) {
      calcView.taxNote = ps.length ? ps[0].taxNote : '';
      return ps;
    });
  }

  /* ---------- 工资条导出（GET /api/export/payslips → .xls 下载） ---------- */
  /* 下载走 fetch+blob 而不是 <a href> 直链：口令门的 X-Code 头只有 fetch 能带；
     文件名优先取服务端 Content-Disposition 里的中文真名（filename* UTF-8）。
     R5-F3：抄 api() 的 AbortController 模式，导出宽限 30s，到点掐断说人话。 */
  function downloadExport(path, fallbackName) {
    var headers = {};
    var code = getCode();
    if (code) headers['X-Code'] = encodeURIComponent(code);  // 口令门：导出也进门（F6：中文口令编码传输）
    var ctrl = ('AbortController' in window) ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 30000) : null;  // 宽限 30s
    var fetchOpts = ctrl ? Object.assign({ signal: ctrl.signal }, { headers: headers })
                         : { headers: headers };
    var p = fetch('/api' + path, fetchOpts).then(function (res) {
      if (res.status === 401 || res.status === 429) {
        clearCode(); showGate();                    // 与 api() 同款收口：清口令重弹（429=试错锁定）
        var e1 = new Error(res.status === 429 ? '试错太多次被锁，稍等再试' : '需要口令');
        e1.status = res.status; throw e1;
      }
      if (!res.ok) {
        return res.json().catch(function () { return null; }).then(function (body) {
          var e2 = new Error((body && body.error) || ('HTTP ' + res.status));
          e2.status = res.status; throw e2;
        });
      }
      var cd = res.headers.get('Content-Disposition') || '';
      var m = /filename\*=UTF-8''([^;\s]+)/i.exec(cd);
      var name = m ? decodeURIComponent(m[1]) : fallbackName;
      return res.blob().then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url; a.download = name;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
      });
    }).finally(function () { if (timer) clearTimeout(timer); });
    if (!ctrl) return p;                            // 老浏览器无 AbortController：照旧无超时
    return p.catch(function (err) {                 // 宽限到点被掐 → 标记成人话错误
      if (err && err.name === 'AbortError') {
        var e3 = new Error('导出超时，再试一次'); e3.timeout = true; throw e3;
      }
      throw err;
    });
  }

  function exportPayslips() {
    var btn = $('#exportPayslipsBtn');
    if (btn) btn.disabled = true;                   // 在途防抖（对齐 F5 做法）
    toast('正在生成工资条文件…');
    downloadExport('/export/payslips', '算件-工资条.xls').then(function () {
      toast('工资条已导出，Excel 可直接打开');
    }).catch(function (err) {
      if (err && (err.status === 401 || err.status === 429)) return;  // 门已弹/试错锁定
      if (err && err.timeout) { toast('导出超时，再试一次'); return; }  // R5-F3：超时单说，不甩 HTTP 术语
      toast('导出没成功：' + ((err && err.message) || '服务不通，稍后再试'));
    }).then(function () { if (btn) btn.disabled = false; });  // 成败/超时一律恢复按钮
  }

  /* ---------- 数据备份（任务 09：设置页 → GET /api/backup 下载） ---------- */
  /* 复用 downloadExport 的下载机制（fetch+blob、X-Code 头、401 清口令重弹、
     文件名取 Content-Disposition、30s 宽限）——导出与备份走同一条经过验证的路 */
  function downloadBackup() {
    var btn = $('#backupBtn');
    if (btn) btn.disabled = true;                   // 在途防抖
    toast('正在打包数据备份…');
    downloadExport('/backup', '算件-备份.json').then(function () {
      toast('备份已下载，把这个文件收好');
    }).catch(function (err) {
      if (err && (err.status === 401 || err.status === 429)) return;  // 门已弹/试错锁定
      if (err && err.timeout) { toast('备份超时，再试一次'); return; }
      toast('备份没成功：' + ((err && err.message) || '服务不通，稍后再试'));
    }).then(function () { if (btn) btn.disabled = false; });
  }

  /* ---------- 班组计件（模式切换） ---------- */
  /* 班组成员行（05 契约：team 模式的 rows=成员行 {name, qty}）。
     分摊权重取合格数量（数量−废品），按首次出现顺序 */
  function teamMembersFromTickets() {
    var map = Object.create(null);  // name -> 合格数量合计（不继承原型，F4 同款防线）
    var order = [];
    (store.tickets.data || []).forEach(function (s) {
      (s.rows || []).forEach(function (r) {
        if (!map[r.name]) { map[r.name] = 0; order.push(r.name); }
        map[r.name] += (Number(r.qty) || 0) - (Number(r.defect || 0) || 0);
      });
    });
    return order.map(function (n) { return { name: n, qty: map[n] }; });
  }
  /* /api/settle team 结果（金额/数量=精确字符串）→ 核算视图数据（展示时才转 Number）。
     lines 原样保留（含 remark=尾差调整）供分摊明细表渲染 */
  function teamToCalcView(out) {
    var p = periodFromTickets();
    p.rule = '江苏版 · 班组计件';
    var lines = (out && out.lines) || [];
    var workers = lines.map(function (l) {
      return {
        name: l.name, qty: Number(l.qty), defect: 0,
        amount: Number(l.amount),
        amountStr: l.amount,  // 服务端原字符串直存：发 /api/payslip 用，不经 Number 往返
        details: []           // 班组口径没有行明细，工资条侧换提示文案
      };
    }).sort(function (a, b) { return b.amount - a.amount; });
    return {
      real: true, kind: 'team', workers: workers, lines: lines,
      teamTotal: Number(out.team_total), residual: Number(out.residual),
      total: Number(out.allocated_total), totalDefect: 0, period: p
    };
  }

  function runCalc() {
    var v = store.calc;
    if (!store.tickets.data || !store.tickets.data.length) {
      toast('先在「导入」上传工票（CSV 或拍照），才能核算');
      location.hash = '#/import';
      return;
    }
    var mode = v.mode === 'team' ? 'team' : 'individual';
    var teamTotal = String(v.teamTotal == null ? '' : v.teamTotal).trim();  // 输入框原字符串：回传保字面值
    if (mode === 'team' && !(Number(teamTotal) > 0)) {
      toast('班组计件要先填班组总额（大于 0 的数字）');
      var totalBox = $('#teamTotalInput');
      if (totalBox) totalBox.focus();
      return;
    }
    v.status = 'loading'; render(currentRoute);
    var body;
    var srcRows = [];  // 个人模式：与提交 rows 同序的源行引用，settle 成功后回填服务端金额（F13）
    if (mode === 'team') {
      body = { rows: teamMembersFromTickets(), mode: 'team', team_total: teamTotal };
      /* F3：班组没有行日期可自动归期，前端把本期归期随 body 传（跨期/无日期不传，
      服务端记未归期，与核算页显示口径一致） */
      var tp = periodKeyFromTickets();
      if (tp) body.period = tp;
    } else {
      // 行数据映射成服务端严格字段（PieceRecord.from_dict：多余字段会 400）
      var rows = [];
      store.tickets.data.forEach(function (s) {
        (s.rows || []).forEach(function (r) {
          rows.push({
            process: String(r.process), name: String(r.name),
            qty: String(r.qty), unit_price: String(r.price),
            defect: String(r.defect || 0), date: s.date || undefined
          });
          srcRows.push(r);
        });
      });
      body = { rows: rows, mode: 'individual' };
    }
    api('/settle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      timeoutMs: 30000  // F8：大单/弱网下 3s 会把真核算掐进演示回落（换尺子），放宽到 30s
    }).then(function (out) {
      if (mode === 'team') {
        var teamView = teamToCalcView(out);
        return fetchRealPayslips(teamView).then(function (ps) {
          return { calc: teamView, payslips: ps };
        });
      }
      /* 服务端按行返回权威金额：回填工票行（行序与提交序一致），工票视图
         的行金额/合计从此用服务端值，不再前端自算（F13） */
      var lines = (out && out.lines) || [];
      if (lines.length === srcRows.length) {
        lines.forEach(function (l, i) { srcRows[i].amountStr = String(l.amount); });
        store.tickets.data = normalizeSheets(store.tickets.data);
      }
      var calcView = settleToCalcView(out);
      return fetchRealPayslips(calcView).then(function (ps) {
        return { calc: calcView, payslips: ps };
      });
    }).catch(function (err) {
      if (err && (err.status === 401 || err.status === 429)) {
        return { aborted: true };  // 口令门已重弹/锁定，输对后重跑（不进演示回落）
      }
      if (err && err.status === 400) {
        /* 服务在线且明确拒收：错误态上屏服务端人话报错，不静默回落演示数据（F21） */
        v.status = 'error'; v.errorMsg = err.message; v.loaded = true;
        render(currentRoute);
        return { aborted: true };
      }
      markDemo();
      // 演示引擎：离线演示回落（服务不通/旧版本时的兜底链路），口径见 README「口径声明」
      return sleep(500).then(function () {
        if (mode === 'team') {
          /* 班组演示：按数量占比分摊、尾差挂数量最大者（对齐引擎演示口径，仅离线演示） */
          return demoPayslips(teamToCalcView(window.DEMO.teamCalc(teamTotal, teamMembersFromTickets())));
        }
        // 个人演示：按「(数量-废品)×单价，行内留两位再合计」口径即时计算
        var result = window.DEMO.calc(store.tickets.data);
        return demoPayslips(result);
      });
    }).then(function (pack) {
      if (!pack || pack.aborted) return;  // 400 已上屏错误态（F21）
      var result = pack.calc;
      v.errorMsg = null;
      v.data = result; v.status = 'ready'; v.loaded = true;
      store.payslip.data = pack.payslips; store.payslip.status = 'ready'; store.payslip.loaded = true;
      store.payslip.taxNote = result.real ? (result.taxNote || '') : '';
      var periodLabel = (result.period && result.period.label) || '本期';
      store.history.data.unshift({
        period: periodLabel, time: '刚刚', mode: result.kind === 'team' ? '班组计件' : '个人计件',
        tickets: store.tickets.data.length, workers: result.workers.length,
        total: result.total, status: '已核算'
      });
      store.history.status = 'ready'; store.history.loaded = true;
      render(currentRoute);
    });
  }

  /* ---------- CSV 解析与校验（说人话的报错） ---------- */
  var CSV_COLS = ['票号', '车间', '日期', '工序', '姓名', '数量', '单价', '废品'];

  function parseCSV(text) {
    var rows = [], row = [], cell = '', inQ = false, i, ch, next;
    text = text.replace(/^\uFEFF/, '');
    for (i = 0; i < text.length; i++) {
      ch = text[i];
      if (inQ) {
        if (ch === '"') {
          next = text[i + 1];
          if (next === '"') { cell += '"'; i++; }
          else inQ = false;
        } else cell += ch;
      } else if (ch === '"') inQ = true;
      else if (ch === ',') { row.push(cell); cell = ''; }
      else if (ch === '\n' || ch === '\r') {
        if (ch === '\r' && text[i + 1] === '\n') i++;
        row.push(cell); cell = '';
        if (row.some(function (c) { return c.trim() !== ''; })) rows.push(row);
        row = [];
      } else cell += ch;
    }
    row.push(cell);
    if (row.some(function (c) { return c.trim() !== ''; })) rows.push(row);
    return rows;
  }

  function validateCSV(rows) {
    if (!rows || !rows.length) {
      return { error: '这个文件是空的，先导出一份有内容的 CSV 再来。' };
    }
    var head = rows[0].map(function (c) { return c.trim(); });
    var missing = CSV_COLS.filter(function (c) { return head.indexOf(c) === -1; });
    if (missing.length) {
      return { error: '第一行要包含这些列名：' + CSV_COLS.join('、') + '（缺 ' + missing.join('、') + '）。' };
    }
    var idx = {}; head.forEach(function (c, i) { idx[c] = i; });
    var ok = [], bad = [];
    rows.slice(1).forEach(function (r, i) {
      var line = i + 2;
      var get = function (c) { return (r[idx[c]] || '').trim(); };
      var no = get('票号'), ws = get('车间'), date = get('日期'), proc = get('工序'),
        name = get('姓名'), qtyS = get('数量'), priceS = get('单价'), defS = get('废品');
      var problems = [];
      if (!no) problems.push('「票号」是空的');
      if (!ws) problems.push('「车间」是空的');
      if (!date) problems.push('「日期」是空的');
      if (!proc) problems.push('「工序」是空的');
      if (!name) problems.push('「姓名」是空的');
      var qty = Number(qtyS), price = Number(priceS), def = Number(defS || 0);
      if (qtyS === '' || !isFinite(qty) || qty <= 0 || qty % 1 !== 0) problems.push('数量要填大于 0 的整数，现在写的是「' + (qtyS || '空') + '」');
      if (priceS === '' || !isFinite(price) || price <= 0) problems.push('单价要填大于 0 的数字，现在写的是「' + (priceS || '空') + '」');
      if (defS !== '' && (!isFinite(def) || def < 0 || def % 1 !== 0)) problems.push('废品要填 0 或正整数，现在写的是「' + defS + '」');
      if (isFinite(qty) && isFinite(def) && qty % 1 === 0 && def % 1 === 0 && def > qty) problems.push('废品（' + def + '）比数量（' + qty + '）还多，检查一下');
      if (problems.length) bad.push({ line: line, reason: problems.join('；') });
      else ok.push({ no: no, workshop: ws, date: date, process: proc, name: name, qty: qty, price: price, defect: def });
    });
    if (!ok.length && bad.length) {
      return { error: '这批 ' + bad.length + ' 行都没法导入（' + bad[0].reason + '），改好再试。' };
    }
    return { ok: ok, bad: bad };
  }

  /* 合并导入行 → 工票列表。R9-F2 口径：同票号 = 整票替换（不再行级追加）——
     同一份 CSV 手滑导两遍，票数/行数都不变（幂等，与 /api/tickets/save 的
     sheet_no 覆盖同语义，拍照入账重复确认也走这里）。返回 {added, replaced, sheets}。 */
  function mergeImported(okRows) {
    if (store.tickets.demoData) {
      /* 内置演示票不与真实导入掺水：首笔真实导入整体替换（在线/回落两路都经这里） */
      store.tickets.data = [];
      store.tickets.demoData = false;
    }
    var byNo = Object.create(null);  // 票号做键：不继承原型（"__proto__"/"constructor" 不得撞，F4）
    store.tickets.data.forEach(function (s) { byNo[s.no] = s; });
    /* 先把本次导入的行按票号聚成整票：一批同票号的行 = 一张完整工票 */
    var groups = Object.create(null), order = [];
    okRows.forEach(function (r) {
      if (!groups[r.no]) { groups[r.no] = { no: r.no, workshop: r.workshop, date: r.date, rows: [] }; order.push(r.no); }
      var g = groups[r.no];
      g.workshop = r.workshop; g.date = r.date;
      g.rows.push({ process: r.process, name: r.name, qty: r.qty, price: r.price, defect: r.defect });
    });
    var added = 0, replaced = 0;
    order.forEach(function (no) {
      if (byNo[no]) replaced++; else added++;
      byNo[no] = groups[no];   // 整票替换：旧行不残留（旧实现无脑 push，重复导入行翻倍）
    });
    var sheets = Object.keys(byNo).map(function (k) { return byNo[k]; });
    sheets = normalizeSheets(sheets);
    store.tickets.data = sheets;
    store.tickets.status = 'ready'; store.tickets.loaded = true;
    return { added: added, replaced: replaced, sheets: sheets.length };
  }

  function startImportText(text, fileName) {
    store.import = { mode: 'parsing', fileName: fileName };
    render(currentRoute);
    return sleep(250).then(function () {
      var res = validateCSV(parseCSV(text));
      if (res.error) {
        store.import = { mode: 'error', fileName: fileName, message: res.error };
      } else {
        store.import = { mode: 'preview', fileName: fileName, ok: res.ok, bad: res.bad };
      }
      render(currentRoute);
    });
  }

  /* CSV 单元格转义（值里含逗号/引号/换行时加引号，与服务端 csv.reader 对齐） */
  function csvCell(v) {
    var s = String(v);
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  function confirmImport() {
    var imp = store.import;
    if (imp.mode !== 'preview' || !imp.ok || !imp.ok.length) return Promise.resolve();
    var okRows = imp.ok;
    /* 在途防抖（F5，对照 runCalc 的做法）：入口同步置 parsing，重复点
       「确认导入」时上面的 mode 守卫直接挡掉；响应/失败都落终态 */
    store.import = { mode: 'parsing', fileName: imp.fileName };
    render(currentRoute);
    /* 第一步（R9-F1）：POST /api/ingest/csv 做权威解析。只发预览校验通过的行
       （保持「坏行跳过」体验），服务端校验并返回精确字符串；行序与预览一致 */
    var csvText = [CSV_COLS.join(',')].concat(okRows.map(function (r) {
      return [r.no, r.workshop, r.date, r.process, r.name, r.qty, r.price, r.defect].map(csvCell).join(',');
    })).join('\n');
    var done = function (r) {
      store.import = { mode: 'drop' };
      location.hash = '#/tickets';
      toast('导入完成：新增 ' + r.added + ' 张工票' +
        (r.replaced ? '，更新 ' + r.replaced + ' 张' : '') + '，现在共 ' + r.sheets + ' 张');
    };
    /* 第二步（R9-F1）：解析成功后逐票 POST /api/tickets/save 落库——
       sheet_no UNIQUE 重存=覆盖（幂等地基），与拍照入账同一条路；
       此前只解析不落库，刷新页面工票全空（会话态与库态不对称） */
    var saveFail = function (err) {
      if (err && (err.status === 401 || err.status === 429)) {
        store.import = { mode: 'drop' }; render(currentRoute); return;  // 口令门已重弹/锁定
      }
      /* 解析成功但没存进库：如实报错不假成功。重导同文件幂等（同票号覆盖），不会翻倍。
         R11-F4：批内多票同败时 sheetNos 带全部失败票号一并报（旧口径只报首票） */
      var nos = (err && err.sheetNos && err.sheetNos.length) ? err.sheetNos : ((err && err.sheetNo) ? [err.sheetNo] : []);
      var why = (err && err.message) || '服务不通';
      var msg = (nos.length ? '工票 ' + nos.join('、') + ' ' : '') + '没存进库：' + why + '。重试一遍即可（同票号会覆盖，不会重复）。';
      store.import = { mode: 'error', fileName: imp.fileName, message: msg };   // R11-F5：与其余 store.import 同款单行书写
      render(currentRoute);
    };
    return api('/ingest/csv', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ csv: csvText }),
      timeoutMs: 30000  // F8：大 CSV（近 1MB）弱网下 3s 会掐，放宽到 30s
    }).then(function (out) {
      var recs = (out && out.records) || [];
      if (recs.length !== okRows.length) throw new Error('服务端返回条数与预览不一致');
      /* 以服务端精确字符串为准：视图行（数值进视图）+ 存库行（字符串保字面值） */
      var viewRows = [], saveRows = [];
      okRows.forEach(function (r, i) {
        var rec = recs[i] || {};
        viewRows.push({ no: r.no, workshop: r.workshop, date: r.date,
          process: rec.process, name: rec.name,
          qty: Number(rec.qty), price: Number(rec.unit_price), defect: Number(rec.defect || 0) });
        saveRows.push({ no: r.no, workshop: r.workshop, date: r.date,
          process: rec.process, name: rec.name,
          qty: rec.qty, unit_price: rec.unit_price,
          defect: rec.defect == null ? '0' : String(rec.defect) });
      });
      /* 按票号聚成整票再存（一批同票号行=一张票），失败带票号报错 */
      var byNo = Object.create(null), order = [];
      saveRows.forEach(function (r) {
        if (!byNo[r.no]) { byNo[r.no] = { sheet_no: r.no, workshop: r.workshop, date: r.date, rows: [] }; order.push(r.no); }
        var g = byNo[r.no];
        g.workshop = r.workshop; g.date = r.date;
        g.rows.push({ process: r.process, name: r.name,
          qty: r.qty, unit_price: r.unit_price, defect: r.defect });
      });
      /* R10-1：旧写法把全部票 Promise.all 一次齐发——队尾请求还在浏览器连接
         队列里排队就把 api() 默认 3s 计时器烧光，大单队尾被假杀成「服务不通」。
         改分批：每批 6 个批间等齐，且逐票 save 与 ingest 同级放宽到 30s——
         排队不再吃掉计时窗口。R11-F3 口径校准：批大小 6 取的是主流浏览器
         对同主机的 HTTP/1.1 并发数（主流浏览器为 6，部分 WebKit 为 4）——
         超出并发上限仅排队（请求在浏览器连接队列里等，并不会失败），所以
         批大小 6 在并发只有 4 的 WebKit 下同样安全，只是队首排队稍长。 */
      var SAVE_BATCH = 6;
      function saveOne(no) {
        return api('/tickets/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(byNo[no]),
          timeoutMs: 30000   // R10-1：与 ingest 同级（默认 3s 会假杀大单队尾）
        }).catch(function (err) {
          err.sheetNo = no;   // 把失败票号带上，报错指到具体那张票
          throw err;
        });
      }
      var savedAll = Promise.resolve();
      for (var bStart = 0; bStart < order.length; bStart += SAVE_BATCH) {
        (function (nos) {
          savedAll = savedAll.then(function () {
            /* R11-F4：批内 allSettled——同批多票失败要一并报齐，不再被
               Promise.all 首个拒绝抢跑只报首票；批间仍串行、任一批有失败
               就停发后续批次；401/429 混在多败里优先透传（门口径让路用）。 */
            return Promise.allSettled(nos.map(saveOne)).then(function (rs) {
              var fails = [];
              rs.forEach(function (r) { if (r.status === 'rejected') fails.push(r.reason); });
              if (!fails.length) return;
              var gate = fails.filter(function (e) {
                return e && (e.status === 401 || e.status === 429);
              })[0];
              var first = gate || fails[0];
              var msgs = [];
              fails.forEach(function (e) {
                var m = (e && e.message) || '服务不通';
                if (msgs.indexOf(m) < 0) msgs.push(m);   // 同文案去重，不重复报两遍
              });
              var err = new Error(msgs.join('；'));
              err.sheetNo = first.sheetNo;   // 首失败票号（单败/旧口径兼容）
              err.sheetNos = fails.map(function (e) { return (e && e.sheetNo) || ''; }).filter(Boolean);
              err.status = first.status;     // 门口径优先：saveFail 据此让路口令门
              throw err;
            });
          });
        })(order.slice(bStart, bStart + SAVE_BATCH));
      }
      return savedAll.then(function () {
        done(mergeImported(viewRows));
      }, saveFail);
    }).catch(function (err) {
      if (err && (err.status === 401 || err.status === 429)) {
        store.import = { mode: 'drop' }; render(currentRoute); return;  // 口令门已重弹/锁定
      }
      if (err && err.status === 400) {
        /* 服务在线且明确拒收（如日期格式不对）：原样展示行号报错，不静默回落 */
        store.import = { mode: 'error', fileName: imp.fileName, message: err.message };
        render(currentRoute);
        return;
      }
      markDemo();
      done(mergeImported(okRows));  // 服务不通/旧版本：按原客户端口径导入（演示回落不变）
    });
  }

  function readFile(file) {
    if (!file) return;
    if (!/\.csv$/i.test(file.name) && file.type && !/csv|text/.test(file.type)) {
      store.import = { mode: 'error', message: '只认 .csv 文件，这个是「' + file.name + '」，换一个试试。' };
      render(currentRoute);
      return;
    }
    var fr = new FileReader();
    fr.onload = function () { startImportText(String(fr.result || ''), file.name); };
    fr.onerror = function () {
      store.import = { mode: 'error', message: '文件读取失败，重新选一次试试。' };
      render(currentRoute);
    };
    fr.readAsText(file, 'UTF-8');
  }

  /* ---------- 拍照/选图 → POST /api/ocr → 预填表人工确认（05 契约） ---------- */
  function readImageFile(file) {
    if (!file) return;
    if (!file.type || file.type.indexOf('image/') !== 0) {
      store.import = { mode: 'error', message: '只认照片文件（JPG/PNG 等），这个不是图片，换一张试试。' };
      render(currentRoute);
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      store.import = { mode: 'error', message: '照片太大（上限 8MB），离近一点拍或者压缩后再传。' };
      render(currentRoute);
      return;
    }
    var fr = new FileReader();
    fr.onload = function () {
      var dataUrl = String(fr.result || '');
      var comma = dataUrl.indexOf(',');
      startOcr(comma === -1 ? dataUrl : dataUrl.slice(comma + 1), file.type, file.name, dataUrl);
    };
    fr.onerror = function () {
      store.import = { mode: 'error', message: '照片读取失败，重新选一次试试。' };
      render(currentRoute);
    };
    fr.readAsDataURL(file);
  }

  function startOcr(b64, mime, fileName, dataUrl) {
    store.import = { mode: 'ocr-uploading', fileName: fileName, image: dataUrl };
    render(currentRoute);
    /* 缩略图异步压缩：回来时只补 src，不整页重渲染（review 态下重渲染会丢输入焦点） */
    makeThumb(dataUrl, function (thumb) {
      store.import.thumb = thumb;
      var el = $('#ocrThumb');
      if (el) el.src = thumb;
    });
    api('/ocr', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_base64: b64, mime: mime || 'image/jpeg' }),
      timeoutMs: 65000  // 契约：识别超时 60s，前端放 65s 余量（api 默认 3s 会掐死）
    }).then(function (out) {
      ocrReceived(out, fileName);
    }).catch(function (err) {
      ocrFailed(err, fileName);
    });
  }

  /* {sheet, needs_review, errors}（05 契约）：errors 有值 → 整单打回；
     否则进预填表（needs_review=true 时可疑字段标黄+行内编辑，人工改完才可入账） */
  function ocrReceived(out, fileName) {
    var prev = store.import;
    var sheet = out && out.sheet;
    var errors = (out && out.errors) || (sheet && sheet.errors) || [];
    if (errors.length) {
      store.import = { mode: 'ocr-error', fileName: fileName, thumb: prev.thumb || prev.image,
        errors: errors.map(String), message: '这张照片没认成整单，被打了回来。重拍一张更清楚的，或改用 CSV 导入。' };
      render(currentRoute);
      return;
    }
    if (!sheet || !Array.isArray(sheet.rows) || !sheet.rows.length) {
      store.import = { mode: 'ocr-error', fileName: fileName, thumb: prev.thumb || prev.image,
        errors: [], message: '照片里没认出工票表格行。重拍一张更正、更亮的试试。' };
      render(currentRoute);
      return;
    }
    var rows = sheet.rows.map(function (r) {
      r = r || {};
      return {
        process: r.process == null ? '' : String(r.process),
        name: r.name == null ? '' : String(r.name),
        qty: r.qty == null ? '' : String(r.qty),            // null/「[?]」原样进表单，标黄等人工
        unit_price: r.unit_price == null ? '' : String(r.unit_price),
        defect: (r.defect == null || r.defect === '') ? '0' : String(r.defect)
      };
    });
    store.import = {
      mode: 'ocr-review', fileName: fileName, thumb: prev.thumb || prev.image,
      needsReview: !!(out && out.needs_review) || !!(sheet.needs_review),
      fields: { no: String(sheet.sheet_no || ''), workshop: String(sheet.workshop || ''), date: String(sheet.date || '') },
      rows: rows
    };
    render(currentRoute);
  }

  function ocrFailed(err, fileName) {
    var prev = store.import;
    if (err && (err.status === 401 || err.status === 429)) {
      /* 口令门已重弹（429=试错锁定，直接用服务端人话）：拍照流程打回重选态 */
      store.import = { mode: 'ocr-error', fileName: fileName, thumb: prev.thumb || prev.image,
        message: (err.status === 429) ? err.message : '要先输对口令。输完后重选照片再试一次。' };
      render(currentRoute);
      return;
    }
    var msg = (err && err.status === 400)
      ? err.message  // 服务在线且明确说明（如「未配置识别服务，请用 CSV 导入」），原样上屏
      : '拍照识别没成功（' + ((err && err.message) || '服务不通') + '）。稍后再试，或先用 CSV 导入。';
    store.import = { mode: 'ocr-error', fileName: fileName, thumb: prev.thumb || prev.image, message: msg };
    render(currentRoute);
  }

  /* 预填字段可疑判定：空/[?] 一律人工确认；数字/日期按引擎口径校验（说人话的口径） */
  function ocrFieldBad(kind, val) {
    var s = String(val == null ? '' : val).trim();
    if (s === '' || s.indexOf('[?]') !== -1) return true;
    if (kind === 'date') return !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(s);
    if (kind === 'qty') { var q = Number(s); return !isFinite(q) || q <= 0 || q % 1 !== 0; }
    if (kind === 'unit_price') { var p = Number(s); return !isFinite(p) || p <= 0; }
    if (kind === 'defect') { var d = Number(s); return !isFinite(d) || d < 0 || d % 1 !== 0; }
    return false;  // 票号/车间/工序/姓名：非空即可
  }
  var OCR_FLD_KIND = { no: 'text', workshop: 'text', date: 'date',
    process: 'text', name: 'text', qty: 'qty', unit_price: 'unit_price', defect: 'defect' };

  /* 还差几处人工确认（黄底）。数字间联检：废品不得比数量多 */
  function ocrPendingCount(imp) {
    var f = imp.fields || {}, n = 0;
    ['no', 'workshop', 'date'].forEach(function (k) { if (ocrFieldBad(OCR_FLD_KIND[k], f[k])) n++; });
    (imp.rows || []).forEach(function (r) {
      Object.keys(OCR_FLD_KIND).forEach(function (k) {
        if (k === 'no' || k === 'workshop' || k === 'date') return;
        var bad = ocrFieldBad(OCR_FLD_KIND[k], r[k]);
        if (!bad && k === 'defect' && isFinite(Number(r.qty)) && Number(r.defect) > Number(r.qty)) bad = true;
        if (bad) n++;
      });
    });
    return n;
  }

  /* 确认入账：人工改完（待确认清零）才走到这里 → POST /api/tickets/save（05 契约）。
     服务不通/旧版本 → 本地入账（演示回落，机制沿用 confirmImport） */
  function confirmOcr() {
    var imp = store.import;
    if (imp.mode !== 'ocr-review' || ocrPendingCount(imp) > 0) return Promise.resolve();
    var f = imp.fields;
    var rows = imp.rows.map(function (r) {
      return { process: String(r.process).trim(), name: String(r.name).trim(),
        qty: String(r.qty).trim(), unit_price: String(r.unit_price).trim(),
        defect: (String(r.defect == null ? '0' : r.defect).trim() || '0') };
    });
    /* 本地入账行（数值字段进视图前转 Number；金额无服务端值，行金额本地估算） */
    var numericRows = rows.map(function (r) {
      return { no: f.no.trim(), workshop: f.workshop.trim(), date: f.date.trim(),
        process: r.process, name: r.name, qty: Number(r.qty), price: Number(r.unit_price), defect: Number(r.defect) };
    });
    var done = function (finalRows) {
      var r = mergeImported(finalRows);
      store.import = { mode: 'drop' };
      location.hash = '#/tickets';
      toast('入账完成：新增 ' + r.added + ' 张工票' +
        (r.replaced ? '，更新 ' + r.replaced + ' 张' : '') + '，现在共 ' + r.sheets + ' 张');
    };
    store.import = { mode: 'ocr-saving', fileName: imp.fileName, thumb: imp.thumb };
    render(currentRoute);
    return api('/tickets/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sheet_no: f.no.trim(), workshop: f.workshop.trim(), date: f.date.trim(), rows: rows })
    }).then(function (out) {
      /* 服务端可能回显权威行（数量/单价=精确字符串）：有条数一致的回显就信回显 */
      var echo = out && (out.rows || (out.ticket && out.ticket.rows));
      if (Array.isArray(echo) && echo.length === rows.length) {
        done(echo.map(function (r) {
          return { no: f.no.trim(), workshop: f.workshop.trim(), date: f.date.trim(),
            process: String(r.process), name: String(r.name),
            qty: Number(r.qty), price: Number(r.unit_price), defect: Number(r.defect || 0) };
        }));
      } else {
        done(numericRows);
      }
    }).catch(function (err) {
      if (err && err.status === 400) {
        /* 服务在线且明确拒收（如日期格式不对）：错误态上屏人话报错，不静默回落 */
        store.import = { mode: 'ocr-error', fileName: imp.fileName, thumb: imp.thumb, message: err.message };
        render(currentRoute);
        return;
      }
      if (err && (err.status === 401 || err.status === 429)) {
        /* 口令门已重弹（429=试错锁定，用服务端人话）：打回重选态，输对口令后重试 */
        store.import = { mode: 'ocr-error', fileName: imp.fileName, thumb: imp.thumb,
          message: (err.status === 429) ? err.message : '要先输对口令。输完后重新走一遍入账。' };
        render(currentRoute);
        return;
      }
      markDemo();
      done(numericRows);  // 服务不通/旧版本：本地入账（演示回落）
    });
  }

  /* ---------- 渲染：三态骨架 ---------- */
  function stateBlock(iconName, title, desc, actionsHtml) {
    return '<div class="state">' + icon(iconName, 24) +
      '<div class="t">' + esc(title) + '</div>' +
      (desc ? '<div class="d">' + esc(desc) + '</div>' : '') +
      (actionsHtml || '') + '</div>';
  }
  function loadingBlock() {
    var rows = '';
    for (var i = 0; i < 4; i++) {
      var w = 90 - i * 12;
      rows += '<div class="sk" style="width:' + w + '%"></div>';
    }
    return '<div class="card"><div class="sk-row" aria-busy="true">' + rows + '</div></div>';
  }
  function errorBlock(title, desc, act) {
    return stateBlock('alert', title, desc,
      '<button class="btn btn-sm" data-act="' + (act || 'retry') + '">重试</button>');
  }

  /* ---------- 视图：导入 ---------- */
  function ocrThumbHtml(imp) {
    if (!imp || (!imp.thumb && !imp.image)) return '';
    return '<div class="ocr-thumb-wrap"><img id="ocrThumb" class="ocr-thumb" src="' +
      esc(imp.thumb || imp.image) + '" alt="工票照片缩略图"></div>';
  }
  function ocrFieldInput(cls, id, kind, val, ph) {
    return '<input class="' + cls + (ocrFieldBad(kind, val) ? ' suspect' : '') +
      '" data-fld="' + id + '" value="' + esc(val) + '" placeholder="' + esc(ph || '') + '" autocomplete="off">';
  }

  function renderImport(v) {
    var imp = store.import;
    if (forceState === 'loading') return loadingBlock();
    if (forceState === 'error') return errorBlock('文件解析失败', '这个 CSV 的列名对不上，第一行需要：票号、车间、日期、工序、姓名、数量、单价、废品。');
    if (forceState === 'empty') return stateBlock('upload', '还没有选择文件', '选一份 CSV 工票文件，或者先看看示例。', '<button class="btn btn-sm" data-act="sample">载入示例 CSV</button>');

    if (imp.mode === 'parsing') {
      return '<div class="sect">正在解析 ' + esc(imp.fileName || '文件') + '…</div>' + loadingBlock();
    }
    if (imp.mode === 'ocr-uploading') {
      return '<div class="sect">正在识别 ' + esc(imp.fileName || '照片') + ' · 最多等一分钟…</div>' +
        ocrThumbHtml(imp) + loadingBlock();
    }
    if (imp.mode === 'ocr-saving') {
      return '<div class="sect">正在入账 ' + esc(imp.fileName || '照片') + '…</div>' +
        ocrThumbHtml(imp) + loadingBlock();
    }
    if (imp.mode === 'ocr-error') {
      var errs = (imp.errors && imp.errors.length)
        ? '<ul class="problems">' + imp.errors.slice(0, 6).map(function (e) {
            return '<li><span class="ln">!</span><span>' + esc(e) + '</span></li>';
          }).join('') + '</ul>'
        : '';
      return ocrThumbHtml(imp) +
        '<div class="sect">拍照识别 · 整单被打回</div>' +
        '<div class="card"><div class="hint hint-danger">' + esc(imp.message || '识别失败') + '</div>' + errs + '</div>' +
        '<div class="gap-row mt-12">' +
        '<button class="btn" data-act="choose-image">重选照片</button>' +
        '<button class="btn" data-act="rechoose">改用 CSV 导入</button></div>';
    }
    if (imp.mode === 'ocr-review') {
      var f = imp.fields || {};
      var pend = ocrPendingCount(imp);
      var html = '<div class="sect">识别结果 · ' + esc(imp.fileName || '照片') + ' · 人工确认</div>';
      html += '<div class="card"><div class="ocr-head">' + ocrThumbHtml(imp) +
        '<span class="badge ' + (imp.needsReview ? 'badge-warn' : 'badge-ok') + '"><span class="dot"></span>' +
        (imp.needsReview ? 'AI 标了要复核的字段（黄底）' : '识别干净，确认后即可入账') + '</span></div>' +
        '<div class="form-grid">' +
        '<label class="field">票号' + ocrFieldInput('input', 'f.no', 'text', f.no, '如 SJ-2026001') + '</label>' +
        '<label class="field">车间' + ocrFieldInput('input', 'f.workshop', 'text', f.workshop, '如 冲压车间') + '</label>' +
        '<label class="field">日期' + ocrFieldInput('input', 'f.date', 'date', f.date, 'YYYY-MM-DD') + '</label>' +
        '</div></div>';
      html += '<div class="sect">工票行（黄底 = 要人工改）</div>' +
        '<div class="card"><div class="table-scroll"><table><thead><tr>' +
        '<th>工序</th><th>姓名</th><th class="num">数量</th><th class="num">单价</th><th class="num">废品</th></tr></thead><tbody>' +
        imp.rows.map(function (r, i) {
          return '<tr>' +
            '<td>' + ocrFieldInput('cell-input', 'r' + i + '.process', 'text', r.process, '工序') + '</td>' +
            '<td>' + ocrFieldInput('cell-input', 'r' + i + '.name', 'text', r.name, '姓名') + '</td>' +
            '<td class="num">' + ocrFieldInput('cell-input num', 'r' + i + '.qty', 'qty', r.qty, '数量') + '</td>' +
            '<td class="num">' + ocrFieldInput('cell-input num', 'r' + i + '.unit_price', 'unit_price', r.unit_price, '单价') + '</td>' +
            '<td class="num">' + ocrFieldInput('cell-input num', 'r' + i + '.defect', 'defect', r.defect, '0') + '</td>' +
            '</tr>';
        }).join('') + '</tbody></table></div></div>';
      html += '<div class="card mt-8"><div class="hint" id="ocrPending">' +
        (pend ? '还有 ' + pend + ' 处要人工确认（黄底字段），改完才能入账' : '都确认过了，可以入账') +
        '</div></div>';
      html += '<div class="gap-row mt-16">' +
        '<button class="btn" data-act="choose-image">重选照片</button>' +
        '<button class="btn btn-primary" id="ocrConfirmBtn" data-act="confirm-ocr"' + (pend ? ' disabled' : '') + '>确认入账 ' + imp.rows.length + ' 行</button>' +
        '</div>';
      return html;
    }
    if (imp.mode === 'error') {
      return stateBlock('alert', '这份 CSV 导不进去', imp.message,
        '<button class="btn btn-sm" data-act="rechoose">重选文件</button>');
    }
    if (imp.mode === 'preview') {
      var okRows = imp.ok, bad = imp.bad;
      var previewRows = okRows.slice(0, 5).map(function (r) {
        return '<tr><td>' + esc(r.process) + '</td><td>' + esc(r.name) + '</td>' +
          '<td class="num">' + fmtInt(r.qty) + '</td><td class="num">' + esc(r.price) + '</td>' +
          '<td class="num">' + fmtInt(r.defect) + '</td></tr>';
      }).join('');
      var headRow = '<tr><th>工序</th><th>姓名</th><th class="num">数量</th><th class="num">单价</th><th class="num">废品</th></tr>';
      var html = '<div class="sect">解析结果 · ' + esc(imp.fileName) + '</div>';
      html += '<div class="card"><div class="row" style="justify-content:space-between">' +
        '<span style="font-weight:500">' + (bad.length ? okRows.length + ' 行能用，' + bad.length + ' 行要改' : okRows.length + ' 行全部认得') + '</span>' +
        (bad.length ? '<span class="badge badge-warn"><span class="dot"></span>' + bad.length + ' 行跳过</span>'
          : '<span class="badge badge-ok"><span class="dot"></span>可导入</span>') +
        '</div></div>';
      if (bad.length) {
        html += '<div class="card mt-8"><ul class="problems">' + bad.slice(0, 6).map(function (b) {
          return '<li><span class="ln">第 ' + b.line + ' 行</span><span>' + esc(b.reason) + '</span></li>';
        }).join('') + (bad.length > 6 ? '<li><span class="ln">…</span><span>还有 ' + (bad.length - 6) + ' 行没列出来</span></li>' : '') + '</ul></div>';
      }
      html += '<div class="sect">预览（前 ' + Math.min(5, okRows.length) + ' 行）</div>' +
        '<div class="card"><div class="table-scroll"><table><thead>' + headRow + '</thead><tbody>' + previewRows + '</tbody></table></div></div>';
      html += '<div class="gap-row mt-16">' +
        '<button class="btn" data-act="rechoose">重选文件</button>' +
        '<button class="btn btn-primary" data-act="confirm"' + (okRows.length ? '' : ' disabled') + '>确认导入 ' + okRows.length + ' 条</button>' +
        '</div>';
      return html;
    }
    /* 默认：拖放/选择（v1.4：拍照前置最顶——手机主路径妈妈最常用；CSV 次之；示例殿后） */
    return '<div class="drop" id="dropzone">' + icon('camera', 24) +
      '<div class="t">拍张工票照片，自动识别</div>' +
      '<div class="d">对准工票拍一张清楚的，识别成预填表，可疑字段标黄等你确认；也可以改用 CSV 文件导入</div>' +
      '<button class="btn btn-primary" data-act="choose-image">' + icon('camera', 14) + '拍照 / 选图</button>' +
      '<button class="btn" data-act="choose">' + icon('upload', 14) + '选择 CSV 文件</button>' +
      '<button class="btn btn-sm" data-act="sample">载入示例 CSV</button>' +
      '<input type="file" id="csvInput" accept=".csv,text/csv" hidden>' +
      '<input type="file" id="imgInput" accept="image/*" capture="environment" hidden></div>' +
      '<div class="sect">说明</div>' +
      '<div class="card"><div class="hint">CSV 一行一条计件记录，第一行要有：票号、车间、日期、工序、姓名、数量、单价、废品；数量、单价、废品要填数字。拍照导入会先识别成预填表，可疑字段标黄，人工改完确认才入账。</div></div>';
  }

  /* ---------- 视图：工票 ---------- */
  function renderTickets(v) {
    var st = effStatus(v);
    if (st === 'loading') return loadingBlock();
    var data = v.data || [];
    if (st === 'empty' || (st === 'ready' && !data.length)) {
      return stateBlock('receipt', '还没有工票', '在「导入」里上传 CSV 工票，这里就会列出来。',
        '<button class="btn btn-sm btn-primary" data-act="goto-import">去导入</button>');
    }
    if (st === 'error') return errorBlock('工票列表没取到', '刚才没拿到工票数据，等一下再试一次。');
    var total = data.reduce(function (a, s) { return a + (Number(s.total) || 0); }, 0);
    var rowsCount = data.reduce(function (a, s) { return a + (s.rowCount || 0); }, 0);
    var html = '<div class="sect">本期工票 · 共 ' + data.length + ' 张 · ' + rowsCount + ' 行 · 合计 ' + fmtMoney(total) + '</div><div class="card">';
    html += data.map(function (s) {
      var open = ui.expandedTicket === s.no;
      var btn = '<button class="ticket-btn" data-act="toggle-ticket" data-no="' + esc(s.no) + '" aria-expanded="' + (open ? 'true' : 'false') + '">' +
        '<span><span class="no">' + esc(s.no) + '</span><span class="meta" style="display:block">' + esc(s.workshop) + ' · ' + esc(s.date) + ' · ' + (s.rowCount || 0) + ' 行</span></span>' +
        '<span class="amt">' + fmtMoney(s.total) + '</span></button>';
      var detail = '<div class="ticket-detail" id="detail-' + esc(s.no) + '"' + (open ? '' : ' hidden') + '>' +
        '<div class="table-scroll"><table><thead><tr><th>工序</th><th>姓名</th><th class="num">数量</th><th class="num">单价</th><th class="num">废品</th><th class="num">金额</th></tr></thead><tbody>' +
        (s.rows || []).map(function (r) {
          var amt = rowAmountCents(r) / 100;  // 服务端 amountStr 优先（F13）
          return '<tr><td>' + esc(r.process) + '</td><td>' + esc(r.name) + '</td>' +
            '<td class="num">' + fmtInt(r.qty) + '</td><td class="num">' + esc(r.price) + '</td>' +
            '<td class="num">' + fmtInt(r.defect || 0) + '</td><td class="num money">' + fmtMoney(amt) + '</td></tr>';
        }).join('') +
        '</tbody></table></div></div>';
      return btn + detail;
    }).join('');
    html += '</div>';
    return html;
  }

  /* ---------- 视图：核算 ---------- */
  /* 模式切换卡（个人/班组）：班组多一个「班组总额」输入；总额按输入原字符串回传 */
  function calcModeCard(v) {
    var isTeam = v.mode === 'team';
    return '<div class="card" style="padding:12px">' +
      '<div class="seg" role="group" aria-label="核算模式">' +
      '<button type="button" class="seg-btn' + (isTeam ? '' : ' on') + '" data-act="calc-mode" data-mode="individual" aria-pressed="' + !isTeam + '">个人计件</button>' +
      '<button type="button" class="seg-btn' + (isTeam ? ' on' : '') + '" data-act="calc-mode" data-mode="team" aria-pressed="' + isTeam + '">班组计件</button>' +
      '</div>' +
      (isTeam
        ? '<div class="field-line"><label for="teamTotalInput">班组总额（元）</label>' +
          '<input class="input" id="teamTotalInput" inputmode="decimal" autocomplete="off" placeholder="如 25000" value="' + esc(v.teamTotal || '') + '"></div>' +
          '<div class="hint">总额按每人工票合格数量（数量−废品）占比分摊，分摊到分；尾差挂在数量最大的成员行。</div>'
        : '<div class="hint">个人计件：金额 =（数量 − 废品）× 单价，按行到分后汇总。</div>') +
      '</div>';
  }

  /* 班组分摊明细（remark=尾差调整 的行带标注徽章；合计行「调整」列=尾差） */
  function teamResultHtml(v) {
    var d = v.data, period = d.period || {};
    return '<div class="card mt-8" style="padding:4px 12px">' +
      '<div class="kv"><span class="k">核算期间' + (period.mixed ? '（跨期，按票面日期）' : '（月切日口径）') + '</span><span class="v">' + esc(period.start || '—') + ' → ' + esc(period.end || '—') + '</span></div>' +
      '<div class="kv"><span class="k">口径</span><span class="v">' + esc(period.rule || '江苏版 · 班组计件') + '</span></div>' +
      '<div class="kv"><span class="k">班组总额</span><span class="v money">' + fmtMoney(d.teamTotal) + '</span></div>' +
      '<div class="kv"><span class="k">尾差</span><span class="v money">' + fmtMoney(d.residual) + '</span></div>' +
      '<div class="kv"><span class="k">分摊合计</span><span class="v money">' + fmtMoney(d.total) + '</span></div>' +
      '</div>' +
      '<button class="btn mt-12" data-act="calc">重新核算</button>' +
      '<div class="sect">分摊明细（尾差挂在数量最大的成员行）</div>' +
      '<div class="card"><div class="table-scroll"><table><thead><tr>' +
      '<th>成员</th><th class="num">数量</th><th class="num">基础分摊</th><th class="num">调整</th><th class="num">分摊额</th></tr></thead><tbody>' +
      (d.lines || []).map(function (l) {
        var adj = Number(l.adjust_amount);
        return '<tr><td>' + esc(l.name) +
          (l.remark ? ' <span class="badge badge-warn" style="margin-left:4px"><span class="dot"></span>' + esc(l.remark) + '</span>' : '') + '</td>' +
          '<td class="num">' + fmtInt(l.qty) + '</td>' +
          '<td class="num money">' + fmtMoney(l.base_amount) + '</td>' +
          '<td class="num money">' + (adj ? fmtMoney(adj) : '—') + '</td>' +
          '<td class="num money">' + fmtMoney(l.amount) + '</td></tr>';
      }).join('') +
      '<tr class="tr-total"><td>合计</td><td class="num">—</td><td class="num">—</td>' +
      '<td class="num money">' + fmtMoney(d.residual) + '</td><td class="num money">' + fmtMoney(d.total) + '</td></tr>' +
      '</tbody></table></div></div>' +
      '<div class="card mt-8"><div class="hint">班组计件：总额按合格数量占比分摊到分，尾差（合计行「调整」列）由数量最大的成员承接，保证分摊合计 = 班组总额。</div></div>';
  }

  function renderCalc(v) {
    var st = effStatus(v);
    if (st === 'loading') {
      return '<div class="sect">正在核算，几秒钟就好…</div>' + loadingBlock();
    }
    if (st === 'error') return errorBlock('这次没算完', v.errorMsg || '核算中途出了问题，重新点一次「开始核算」。');
    var hasTickets = store.tickets.data && store.tickets.data.length;
    var modeCard = calcModeCard(v);
    if (st === 'empty' || !v.data) {
      return modeCard + '<div class="mt-12"></div>' +
        stateBlock('calc', hasTickets ? '本月还没核算' : '还没法核算',
        hasTickets ? '工票已就位（' + store.tickets.data.length + ' 张），选好模式点开始算。' : '先把工票导进来（CSV 或拍照），才能开始核算。',
        '<button class="btn btn-sm btn-primary" data-act="' + (hasTickets ? 'calc' : 'goto-import') + '">' + (hasTickets ? '开始核算' : '去导入') + '</button>');
    }
    if (v.data && v.data.kind === 'team') return modeCard + teamResultHtml(v);
    var d = v.data, period = d.period || (window.DEMO && window.DEMO.PERIOD) || {};
    var html = modeCard +
      '<div class="card mt-8" style="padding:4px 12px">' +
      '<div class="kv"><span class="k">核算期间' + (period.mixed ? '（跨期，按票面日期）' : '（月切日口径）') + '</span><span class="v">' + esc(period.start || '—') + ' → ' + esc(period.end || '—') + '</span></div>' +
      '<div class="kv"><span class="k">口径</span><span class="v">' + esc(period.rule || '江苏版 · 个人计件') + '</span></div>' +
      '<div class="kv"><span class="k">工票</span><span class="v">' + (store.tickets.data.length) + ' 张 · ' + (store.tickets.data.reduce(function (a, s) { return a + (s.rowCount || 0); }, 0)) + ' 行</span></div>' +
      '</div>' +
      '<button class="btn mt-12" data-act="calc">重新核算</button>' +
      '<div class="sect">按工人汇总</div>' +
      '<div class="card"><div class="table-scroll"><table id="calcTable"><thead><tr><th>工人</th><th class="num">' + (d.real ? '合格数量' : '数量') + '</th><th class="num">废品</th><th class="num">计件工资</th></tr></thead><tbody>' +
      (d.workers || []).map(function (w) {
        return '<tr><td>' + esc(w.name) + '</td><td class="num">' + fmtInt(w.qty) + '</td><td class="num">' + fmtInt(w.defect) + '</td><td class="num money">' + fmtMoney(w.amount) + '</td></tr>';
      }).join('') +
      '<tr class="tr-total"><td>合计</td><td class="num">—</td><td class="num">—</td><td class="num money">' + fmtMoney(d.total) + '</td></tr>' +
      '</tbody></table></div></div>' +
      '<div class="card mt-8"><div class="hint">报废对账：应产 / 实报 / 报废 / 下落不明 已对平，无差异。金额 =（数量 − 废品）× 单价，行内留两位再合计。</div></div>';
    return html;
  }

  /* ---------- 视图：工资条 ---------- */
  /* 打印版工资条（任务 09）：每工人一张卡、一卡两联——存根联（老板留底）+
     员工联（发车间），中间虚线裁剪；只含票头+明细+签字线（导航/按钮/徽章
     由 @media print 全部隐掉）。屏幕上整个容器 display:none，只进打印输出。 */
  function payslipPrintCopy(p, periodLabel, copyTag) {
    var rows = (p.details || []).map(function (x) {
      return '<tr><td>' + esc(x.process) + '</td>' +
        '<td class="num">' + fmtInt(x.qty) + '</td>' +
        '<td class="num">' + fmtInt(x.defect || 0) + '</td>' +
        '<td class="num">' + esc(x.price) + '</td>' +
        '<td class="num">' + fmtMoney(x.amount) + '</td></tr>';
    }).join('');
    var detailTable = rows
      ? '<table class="print-table"><thead><tr><th>工序</th><th class="num">数量</th>' +
        '<th class="num">废品</th><th class="num">单价</th><th class="num">金额</th></tr></thead>' +
        '<tbody>' + rows + '</tbody></table>'
      : '<div class="print-note">班组计件 · 按合格数量占比分摊</div>';
    return '<div class="print-copy">' +
      '<div class="print-head"><span class="print-title">算件 · 计件工资条</span>' +
      '<span class="print-copy-tag">' + esc(copyTag) + '</span></div>' +
      '<div class="print-meta">' + esc(periodLabel) + ' · 姓名：' + esc(p.name) + '</div>' +
      detailTable +
      '<div class="print-sum">计件工资 ' + fmtMoney(p.piece) + ' · 应发 ' + fmtMoney(p.gross) +
      ' · 个税（演示口径）' + fmtMoney(p.tax) + ' · 实发 ' + fmtMoney(p.net) + '</div>' +
      '<div class="print-sign">领款人签字：____________　老板签字：____________　日期：____________</div>' +
      '</div>';
  }

  function payslipPrintCard(p, periodLabel) {
    return '<div class="print-card">' +
      payslipPrintCopy(p, periodLabel, '存根联（老板留底）') +
      '<div class="print-cut">沿此虚线剪开</div>' +
      payslipPrintCopy(p, periodLabel, '员工联（发车间）') +
      '</div>';
  }

  function renderPayslip(v) {
    var st = effStatus(v);
    if (st === 'loading') return loadingBlock();
    if (st === 'error') return errorBlock('工资条没生成', '刚才没算完，先去「核算」跑一次，工资条会自动出来。');
    var data = v.data;
    if (st === 'empty' || !data || !data.length) {
      return stateBlock('banknote', '还没有工资条', '先在「核算」里跑一次本月核算，工资条会自动生成。',
        '<button class="btn btn-sm btn-primary" data-act="goto-calc">去核算</button>');
    }
    var totalNet = data.reduce(function (a, p) { return a + Number(p.net || 0); }, 0);
    var html = '<div class="sect sect-row"><span>' + data.length + ' 名工人 · 实发合计 ' + fmtMoney(totalNet) + ' · 点开看明细</span>' +
      '<span class="gap-row">' +
      '<button class="btn btn-sm" id="printPayslipsBtn" data-act="print-payslips">' + icon('printer', 14) + '打印工资条</button>' +
      '<button class="btn btn-sm" id="exportPayslipsBtn" data-act="export-payslips">' + icon('download', 14) + '导出工资条</button>' +
      '</span></div>';
    html += data.map(function (p) {
      var open = ui.expandedWorker === p.name;
      var head = '<button class="ticket-btn" data-act="toggle-worker" data-name="' + esc(p.name) + '" aria-expanded="' + (open ? 'true' : 'false') + '">' +
        '<span style="font-weight:500">' + esc(p.name) + '</span>' +
        '<span class="amt">实发 ' + fmtMoney(p.net) + '</span></button>';
      var detail = '<div class="ticket-detail" id="payslip-' + esc(p.name) + '"' + (open ? '' : ' hidden') + '>' +
        '<div style="padding:4px 12px 8px">' +
        '<div class="kv"><span class="k">计件工资</span><span class="v money">' + fmtMoney(p.piece) + '</span></div>' +
        '<div class="kv"><span class="k">岗位补贴</span><span class="v money">' + fmtMoney(p.subsidy) + '</span></div>' +
        '<div class="kv"><span class="k">应发</span><span class="v money">' + fmtMoney(p.gross) + '</span></div>' +
        '<div class="kv"><span class="k">个税（演示口径）</span><span class="v money">' + fmtMoney(p.tax) + '</span></div>' +
        '<div class="kv" style="font-weight:500"><span>实发</span><span class="v money">' + fmtMoney(p.net) + '</span></div>' +
        '<div style="font-size:10px;color:var(--text-3);margin-top:8px">明细：' +
        ((p.details || []).length
          ? (p.details).map(function (x) { return esc(x.process) + ' ' + fmtInt(x.qty) + ' 件 × ¥' + esc(x.price); }).join('；')
          : '班组计件 · 按合格数量占比分摊') +
        '</div></div></div>';
      return '<div class="card">' + head + detail + '</div>';
    }).join('');
    html += '<div class="card mt-8"><div class="hint">' +
      esc(store.payslip.taxNote || '个税按演示口径粗算（月应发超 5000 的部分按 3%），仅作展示，不是申报数。') +
      '</div></div>';
    /* 打印版（任务 09）：屏幕上 display:none；Ctrl+P 时由 @media print 只留这段 */
    var periodLabel = (store.calc.data && store.calc.data.period && store.calc.data.period.label) ||
      (window.DEMO && window.DEMO.PERIOD && window.DEMO.PERIOD.label) || '本期';
    html += '<div class="print-sheets" aria-hidden="true">' +
      data.map(function (p) { return payslipPrintCard(p, periodLabel); }).join('') +
      '</div>';
    return html;
  }

  /* ---------- 视图：历史 ---------- */
  function renderHistory(v) {
    var st = effStatus(v);
    if (st === 'loading') return loadingBlock();
    if (st === 'error') return errorBlock('历史记录没取到', '刚才没拿到历史记录，等一下再试一次。');
    var data = v.data || [];
    if (st === 'empty' || !data.length) {
      return stateBlock('history', '还没有核算记录', '跑完第一次核算，这里会留下每一期的底账。',
        '<button class="btn btn-sm btn-primary" data-act="goto-calc">去核算</button>');
    }
    return '<div class="sect">每次核算留一条记录，可回看</div><div class="card">' +
      data.map(function (h) {
        /* 底账没有的字段（如工票数）不显示，宁缺勿编 0 */
        var meta = [h.time ? esc(h.time) : '', h.mode ? esc(h.mode) : '',
          h.count != null ? fmtInt(h.count) + ' 次核算' : '',
          h.tickets != null ? fmtInt(h.tickets) + ' 张工票' : '',
          h.workers != null ? fmtInt(h.workers) + ' 人' : '']
          .filter(function (x) { return x; }).join(' · ');
        return '<button class="ticket-btn" data-act="noop">' +
          '<span><span class="no">' + esc(h.period) + ' <span class="badge badge-ok" style="margin-left:4px"><span class="dot"></span>' + esc(h.status) + '</span></span>' +
          '<span class="meta" style="display:block">' + meta + '</span></span>' +
          '<span class="amt">' + fmtMoney(h.total) + '</span></button>';
      }).join('') + '</div>';
  }

  /* ---------- 视图：趋势（近 6 期线条报表） ---------- */
  /* SVG 折线（零依赖）：viewBox 320×96 + preserveAspectRatio="none" 铺满卡片宽，
     polyline 加 vector-effect="non-scaling-stroke" 保住 1.5px 发丝线不被拉伸变粗；
     null 点跳过（该期没口径不编 0）；有效点不足 2 个画不出线 → 显示提示文案 */
  function sparkline(values) {
    var pts = [];
    values.forEach(function (v, i) {
      if (v != null && isFinite(v)) pts.push([i, v]);
    });
    if (pts.length < 2) {
      return '<div class="chart-empty">这一项还凑不出走势（至少要两期数据）</div>';
    }
    var W = 320, H = 96, PAD = 8;
    var lo = Infinity, hi = -Infinity;
    pts.forEach(function (p) {
      if (p[1] < lo) lo = p[1];
      if (p[1] > hi) hi = p[1];
    });
    if (lo === hi) { lo -= 1; hi += 1; }            // 全平：给条中线而不是除零
    var x = function (i) {
      return PAD + (W - 2 * PAD) * (values.length < 2 ? 0.5 : i / (values.length - 1));
    };
    var y = function (v) {
      return PAD + (H - 2 * PAD) * (1 - (v - lo) / (hi - lo));
    };
    var line = pts.map(function (p) {
      return x(p[0]).toFixed(1) + ',' + y(p[1]).toFixed(1);
    }).join(' ');
    var dots = pts.map(function (p) {
      return '<circle cx="' + x(p[0]).toFixed(1) + '" cy="' + y(p[1]).toFixed(1) + '" r="2.5"></circle>';
    }).join('');
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<polyline class="chart-line" points="' + line + '" fill="none" vector-effect="non-scaling-stroke"></polyline>' +
      '<g class="chart-dots">' + dots + '</g></svg>';
  }

  function renderTrend(v) {
    var st = effStatus(v);
    if (st === 'loading') return loadingBlock();
    if (st === 'error') return errorBlock('趋势没取到', '刚才没拿到趋势数据，等一下再试一次。');
    var data = v.data || [];
    if (st === 'empty' || !data.length) {
      return stateBlock('chart', '还没有可看趋势的核算记录',
        '跑完几期核算（工票带日期归期），这里会画出总产值、报废率、人均产出的走势。',
        '<button class="btn btn-sm btn-primary" data-act="goto-calc">去核算</button>');
    }
    var periods = data.map(function (d) { return d.period; });
    var cards = [
      { title: '总产值', unit: '元', values: data.map(function (d) { return d.total; }),
        fmt: function (n) { return fmtMoney(n); } },
      { title: '报废率', unit: '%', values: data.map(function (d) { return d.scrapRate; }),
        fmt: function (n) { return n + '%'; } },
      { title: '人均产出', unit: '元', values: data.map(function (d) { return d.perCapita; }),
        fmt: function (n) { return fmtMoney(n); } }
    ].map(function (c) {
      var last = null;
      for (var i = c.values.length - 1; i >= 0; i--) {
        if (c.values[i] != null && isFinite(c.values[i])) { last = c.values[i]; break; }
      }
      return '<div class="card chart-card">' +
        '<div class="chart-head"><span class="chart-title">' + esc(c.title) +
        '<span class="chart-unit">（' + esc(c.unit) + '）</span></span>' +
        '<span class="chart-now">' + (last == null ? '—' : c.fmt(last)) + '</span></div>' +
        sparkline(c.values) +
        '<div class="chart-axis"><span>' + esc(periods[0] || '') + '</span>' +
        '<span>' + esc(periods[periods.length - 1] || '') + '</span></div>' +
        '</div>';
    }).join('');
    var table = '<div class="sect">各期数据（金额为精确值）</div>' +
      '<div class="card"><div class="table-scroll"><table><thead><tr>' +
      '<th>期</th><th class="num">总产值</th><th class="num">报废率</th>' +
      '<th class="num">人均产出</th><th class="num">人数</th></tr></thead><tbody>' +
      data.slice().reverse().map(function (d) {   // 表格新期在前（与历史页一致）
        return '<tr><td>' + esc(d.period) + '</td>' +
          '<td class="num money">' + (d.totalStr ? fmtMoney(d.totalStr) : '—') + '</td>' +
          '<td class="num">' + (d.scrapStr ? d.scrapStr + '%' : '—') + '</td>' +
          '<td class="num money">' + (d.perCapitaStr ? fmtMoney(d.perCapitaStr) : '—') + '</td>' +
          '<td class="num">' + (d.workers != null ? fmtInt(d.workers) : '—') + '</td></tr>';
      }).join('') + '</tbody></table></div></div>';
    return '<div class="sect">近 ' + data.length + ' 期走势（不足 6 期就先看已有的）</div>' +
      '<div class="chart-stack">' + cards + '</div>' + table;
  }

  /* ---------- 视图：期间对比（任务单 10：两期逐人） ---------- */
  /* 箭头一律小 SVG 三角（几何形状）；涨跌色只落在变化徽章上（涨=--ok 绿 /
     跌=--warn 暖橙），条形图保持中性灰/品牌色——上色克制 */
  function cmpArrow(up) {
    return '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">' +
      '<path d="' + (up ? 'M5 1.5 9 8.5H1Z' : 'M5 8.5 1 1.5H9Z') +
      '" fill="currentColor"/></svg>';
  }
  function cmpDelta(b, fmt) {
    if (!b || b.diff == null) return '<span class="cmp-delta flat">—</span>';
    var d = Number(b.diff);
    if (!isFinite(d) || d === 0) return '<span class="cmp-delta flat">没变</span>';
    var up = d > 0;
    /* rate 是服务端带符号的精确字符串（跌向自带 '-'），原样上屏不再加符号
       ——否则跌向会出现「--71.43%」双负号（无头实测抓到） */
    var pct = (b.rate != null && b.rate !== '') ? '（' + esc(b.rate) + '%）' : '';
    return '<span class="cmp-delta ' + (up ? 'up' : 'down') + '">' + cmpArrow(up) +
      (up ? '+' : '-') + fmt(Math.abs(d)) + pct + '</span>';
  }
  /* 一个工人两根并排条（上=p1 中性灰 / 下=p2 品牌色），宽度按全员两期最大
     计件等比；值为 0 画 0 宽（不冒充有量） */
  function cmpBarPair(n1, n2, s1, s2, maxV) {
    var w = function (n) {
      return (maxV > 0 && n > 0) ? Math.max(2, Math.round(n / maxV * 100)) : 0;
    };
    return '<div class="cmp-bars">' +
      '<div class="cmp-bar cmp-bar-1"><i style="width:' + w(n1) + '%"></i><b>' + cmpVal(s1) + '</b></div>' +
      '<div class="cmp-bar cmp-bar-2"><i style="width:' + w(n2) + '%"></i><b>' + cmpVal(s2) + '</b></div>' +
      '</div>';
  }
  function cmpVal(x) { return (x != null && x !== '') ? esc(x) : '—'; }

  /* 期选择器（R8-F1 收敛为一个构造：就绪态与空态同一份，复制两份迟早走样） */
  function cmpPickerHtml(periods, sel1, sel2) {
    var opts = function (sel) {
      return (periods || []).map(function (p) {
        return '<option value="' + esc(p) + '"' + (p === sel ? ' selected' : '') + '>' + esc(p) + '</option>';
      }).join('');
    };
    return '<div class="card cmp-picker">' +
      '<label class="cmp-pick"><span>前一期间</span>' +
      '<select id="cmpP1" class="input cmp-select" data-act="cmp-p1">' + opts(sel1) + '</select></label>' +
      '<span class="cmp-vs" aria-hidden="true">对比</span>' +
      '<label class="cmp-pick"><span>后一期间</span>' +
      '<select id="cmpP2" class="input cmp-select" data-act="cmp-p2">' + opts(sel2) + '</select></label>' +
      '</div>';
  }

  function renderCompare(v) {
    var st = effStatus(v);
    if (st === 'loading') return loadingBlock();
    if (st === 'error') return errorBlock('对比没取到', '刚才没拿到对比数据，等一下再试一次。');
    var d = v.data;
    if (!d) {
      /* R8-F1 空态保留期选择器：400 清参后用户改完下拉直接重发，不再是
         无选择器的死胡同；期表=最近一次成功/演示装载记住的，从没有过就不摆 */
      var pick = (v.periods && v.periods.length >= 2)
        ? cmpPickerHtml(v.periods, v.p1, v.p2) : '';
      return pick + stateBlock('calc', '还凑不出两期对比',
        v.onlineEmpty || '对比要两个已归期的核算记录，跑完两期核算再来看。',
        '<button class="btn btn-sm btn-primary" data-act="goto-calc">去核算</button>');
    }
    var picker = cmpPickerHtml(d.periods, d.p1, d.p2);
    /* 摘要：两期总产值并排双柱（高度按两期最大值等比）；实发/合格数量走数值行 */
    var tp = d.totals.piece || {}, tn = d.totals.net || {}, tq = d.totals.qualified || {};
    var maxT = Math.max(Number(tp.p1) || 0, Number(tp.p2) || 0);
    var colH = function (sv) {
      var n = Number(sv) || 0;
      return (maxT > 0 && n > 0) ? Math.max(4, Math.round(n / maxT * 100)) : 0;
    };
    var col = function (sv, lab, cls) {
      return '<div class="cmp-col"><div class="cmp-colwrap">' +
        '<div class="cmp-colbar ' + cls + '" style="height:' + colH(sv) + '%"></div></div>' +
        '<span class="cmp-colnum">' + (sv != null && sv !== '' ? fmtMoney(sv) : '—') + '</span>' +
        '<span class="cmp-collab">' + esc(lab) + '</span></div>';
    };
    var kv = function (k, a, b, delta) {
      return '<div class="kv"><span class="k">' + k + '</span>' +
        '<span class="v">' + a + ' → ' + b + (delta ? ' ' + delta : '') + '</span></div>';
    };
    /* 两侧都没口径（离线演示只有总产值）的行整个不渲染——宁缺勿列，
       不摆「— → —」空行凑数 */
    var kvMaybe = function (k, a, b, delta) {
      return (a === '—' && b === '—') ? '' : kv(k, a, b, delta);
    };
    var summary = '<div class="card cmp-sum">' +
      '<div class="chart-head"><span class="chart-title">两期合计（总产值）</span>' +
      cmpDelta(tp, fmtMoney) + '</div>' +
      '<div class="cmp-cols">' + col(tp.p1, d.p1, 'cmp-colbar-1') +
      col(tp.p2, d.p2, 'cmp-colbar-2') + '</div>' +
      '<div class="mt-12">' +
      kvMaybe('实发合计（个税演示口径）', cmpVal(tn.p1) === '—' ? '—' : fmtMoney(tn.p1),
        cmpVal(tn.p2) === '—' ? '—' : fmtMoney(tn.p2), cmpDelta(tn, fmtMoney)) +
      kvMaybe('合格数量合计', cmpVal(tq.p1), cmpVal(tq.p2),
        cmpDelta(tq, function (n) { return String(n); })) +
      '</div>' +
      (d.demo ? '<div class="hint cmp-demo-hint">离线演示：演示数据只有期合计，连上服务跑两期核算才能看逐人明细。</div>' : '') +
      '</div>';
    /* 工人明细：条宽按全员（含新/消失）两期最大计件等比；顺序=服务端按
       |实发变化| 降序排好的（前端按序渲染不重排） */
    var maxP = 0;
    d.workers.concat(d.newWorkers, d.goneWorkers).forEach(function (w) {
      maxP = Math.max(maxP, w.pieceNum1 || 0, w.pieceNum2 || 0, w.pieceNum || 0);
    });
    var workerRows = d.workers.map(function (w) {
      return '<div class="cmp-worker">' +
        '<div class="cmp-whead"><span class="cmp-wname">' + esc(w.name) + '</span>' +
        cmpDelta(w.net, fmtMoney) + '</div>' +
        cmpBarPair(w.pieceNum1, w.pieceNum2,
          w.piece.p1 != null ? w.piece.p1 : '', w.piece.p2 != null ? w.piece.p2 : '', maxP) +
        '<div class="cmp-wmeta">合格 ' + cmpVal(w.qualified.p1) + '→' + cmpVal(w.qualified.p2) +
        ' · 计件 ' + cmpVal(w.piece.p1) + '→' + cmpVal(w.piece.p2) +
        ' · 实发 ' + cmpVal(w.net.p1) + '→' + cmpVal(w.net.p2) + '</div>' +
        '</div>';
    }).join('');
    var soloRow = function (w) {
      return '<div class="cmp-solo"><span class="cmp-wname">' + esc(w.name) + '</span>' +
        '<span class="cmp-solo-meta">合格 ' + cmpVal(w.qualified) +
        ' · 计件 ' + cmpVal(w.piece) + ' · 实发 ' + cmpVal(w.net) + '</span></div>';
    };
    var html = picker + summary;
    if (d.workers.length) {
      html += '<div class="sect">工人明细（按实发变化从大到小）</div>' +
        '<div class="card">' + workerRows + '</div>';
    }
    if (d.newWorkers.length) {
      html += '<div class="sect">新出现的工人（' + d.newWorkers.length + ' 人）</div>' +
        '<div class="card">' + d.newWorkers.map(soloRow).join('') + '</div>';
    }
    if (d.goneWorkers.length) {
      html += '<div class="sect">这期没出现的工人（' + d.goneWorkers.length + ' 人）</div>' +
        '<div class="card">' + d.goneWorkers.map(soloRow).join('') + '</div>';
    }
    if (!d.workers.length && !d.newWorkers.length && !d.goneWorkers.length && !d.demo) {
      html += '<div class="card"><div class="state"><div class="t">两期没有对得上的工人</div>' +
        '<div class="d">这两期里没有任何同名的工人，看看期间是不是选对了。</div></div></div>';
    }
    return html;
  }

  /* ---------- 视图：设置（任务 09：数据备份入口） ---------- */
  /* 静态视图（无 loader）。备份走 GET /api/backup（fetch+blob 下载，X-Code
     照带）；文件名服务端已带时间戳，前端只兜底缺头时的名字 */
  function renderSettings() {
    return '<div class="sect">数据备份</div>' +
      '<div class="card" style="padding:12px">' +
      '<div style="font-weight:500">把这台算件的数据打包带走</div>' +
      '<div class="hint" style="padding:8px 0 0">备份是一个 .json 文件（文件名自带下载时间），里面有：全部工票、核算底账、操作留痕，以及每张工票的原始 CSV。换电脑、重装系统之前，先下来一份收好。</div>' +
      '<button class="btn btn-primary mt-12" id="backupBtn" data-act="download-backup">' + icon('download', 14) + '下载数据备份</button>' +
      '</div>' +
      '<div class="sect">说明</div>' +
      '<div class="card"><div class="hint">备份只装业务数据，不装密钥：口令和识别服务的配置（data/config.json）不进备份文件——界面下载和命令行备份（tools/backup.command）都不带，密钥请单独保管。</div></div>' +
      '<div class="card mt-8"><div class="hint">打印工资条：在「工资条」页点「打印工资条」，一张 A5 纸一个工人、上下两联，沿虚线剪开，签字后一联留底一联发车间。</div></div>';
  }

  /* ---------- 路由 ---------- */
  var ROUTES = ['import', 'tickets', 'calc', 'payslip', 'trend', 'compare', 'history', 'settings'];
  var RENDER = { import: renderImport, tickets: renderTickets, calc: renderCalc,
    payslip: renderPayslip, trend: renderTrend, compare: renderCompare,
    history: renderHistory, settings: renderSettings };
  var LOADERS = { import: loadTickets, tickets: loadTickets, calc: loadTickets,
    payslip: loadTickets, trend: loadTrend, compare: loadCompare,
    history: loadHistory };
  var currentRoute = 'import';

  function routeFromHash() {
    var h = (location.hash || '').replace(/^#\/?/, '');
    return ROUTES.indexOf(h) !== -1 ? h : 'import';
  }

  function render(route) {
    route = route || currentRoute;
    var host = $('#view-' + route);
    if (!host) return;
    var html = (route === 'import') ? renderImport(store.import) : RENDER[route](store[route]);
    host.innerHTML = html;
  }

  function showView(route) {
    currentRoute = route;
    ROUTES.forEach(function (r) {
      var sec = $('#view-' + r);
      if (sec) sec.hidden = (r !== route);
    });
    $all('.tab').forEach(function (t) {
      var on = t.dataset.route === route;
      t.classList.toggle('on', on);
      if (on) t.setAttribute('aria-current', 'page');
      else t.removeAttribute('aria-current');
    });
    render(route);
    var loader = LOADERS[route];
    if (loader) loader(route === 'history' || route === 'trend' || route === 'compare');  // 这三页每次进都重拉
    window.scrollTo(0, 0);
  }

  /* ---------- 事件委托 ---------- */
  function onDocClick(ev) {
    var t = ev.target.closest ? ev.target.closest('[data-act], .tab') : null;
    if (!t) return;
    if (t.classList.contains('tab')) return;  // 锚点原生跳转
    var act = t.dataset.act;
    if (act === 'code-submit') {
      submitCode();
      return;
    }
    if (act === 'choose' || act === 'rechoose') {
      store.import = { mode: 'drop' };
      render(currentRoute);
      var input = $('#csvInput');
      if (input) input.click();
    } else if (act === 'sample') {
      if (window.DEMO) startImportText(window.DEMO.SAMPLE_CSV, '示例工票.csv');
    } else if (act === 'confirm') {
      confirmImport();
    } else if (act === 'choose-image') {
      store.import = { mode: 'drop' };
      render(currentRoute);
      var imgIn = $('#imgInput');
      if (imgIn) imgIn.click();
    } else if (act === 'confirm-ocr') {
      confirmOcr();
    } else if (act === 'calc-mode') {
      store.calc.mode = t.dataset.mode === 'team' ? 'team' : 'individual';
      render(currentRoute);
    } else if (act === 'toggle-ticket') {
      ui.expandedTicket = ui.expandedTicket === t.dataset.no ? null : t.dataset.no;
      render(currentRoute);
    } else if (act === 'toggle-worker') {
      ui.expandedWorker = ui.expandedWorker === t.dataset.name ? null : t.dataset.name;
      render(currentRoute);
    } else if (act === 'calc') {
      runCalc();
    } else if (act === 'export-payslips') {
      exportPayslips();
    } else if (act === 'print-payslips') {
      window.print();   /* 打印对话框是模态的：不用防抖；打印版 CSS 接管输出 */
    } else if (act === 'download-backup') {
      downloadBackup();
    } else if (act === 'goto-import') {
      location.hash = '#/import';
    } else if (act === 'goto-calc') {
      location.hash = '#/calc';
    } else if (act === 'retry') {
      var v = store[currentRoute === 'import' ? 'tickets' : currentRoute];
      v.loaded = false;
      showView(currentRoute);
    }
  }
  document.addEventListener('click', onDocClick);

  function onDocChange(ev) {
    if (ev.target && ev.target.id === 'csvInput') {
      readFile(ev.target.files && ev.target.files[0]);
    } else if (ev.target && ev.target.id === 'imgInput') {
      readImageFile(ev.target.files && ev.target.files[0]);
      ev.target.value = '';  // 同一张照片连选两次也要触发 change
    } else if (ev.target && (ev.target.id === 'cmpP1' || ev.target.id === 'cmpP2')) {
      /* 对比页换期间：同选一期前端先拦——R8-F2 校验前置，非法不写 store
         （回滚该控件的旧选中+人话 toast，不发请求），合法才写入并重发 */
      var c = store.compare;
      var cd = c.data;
      var key = ev.target.id === 'cmpP1' ? 'p1' : 'p2';
      var shown1 = c.p1 || (cd && cd.p1) || '';   /* 显示中的两期（演示态选中在 data 上） */
      var shown2 = c.p2 || (cd && cd.p2) || '';
      var other = key === 'p1' ? shown2 : shown1; /* 另一侧正在显示的期 */
      if (other && ev.target.value === other) {
        ev.target.value = key === 'p1' ? shown1 : shown2;   /* 回滚控件旧值，非法选择不残留 */
        toast('两个期间不能选同一期');
        return;
      }
      c[key] = ev.target.value;
      if (c.p1 && c.p2) loadCompare(true);   /* 两期都选齐才重发（单边空=等另一边） */
    }
  }
  document.addEventListener('change', onDocChange);

  /* 行内编辑（拍照预填表 + 班组总额）：只局部更新，不整页重渲染（防丢输入焦点） */
  function onDocInput(ev) {
    var t = ev.target;
    if (t && t.id === 'codeInput') {
      var gateErr = $('#codeErr');           // 重输时收起「口令不对」提示
      if (gateErr) gateErr.hidden = true;
      return;
    }
    if (t && t.id === 'teamTotalInput') {
      store.calc.teamTotal = t.value;  // 原字符串存住，核算时原样回传
      return;
    }
    var fld = t && t.getAttribute && t.getAttribute('data-fld');
    if (!fld) return;
    var imp = store.import;
    if (imp.mode !== 'ocr-review') return;
    var kind;  // 该字段的校验口径（黄标跟着它切换）
    if (/^f\.(.+)$/.test(fld)) {
      var fk = fld.slice(2);
      imp.fields[fk] = t.value;
      kind = OCR_FLD_KIND[fk];
    } else {
      var rm = /^r(\d+)\.([a-z_]+)$/.exec(fld);  // r1.name → 行号 1 + 字段 name
      if (!rm || !imp.rows[Number(rm[1])]) return;
      imp.rows[Number(rm[1])][rm[2]] = t.value;
      kind = OCR_FLD_KIND[rm[2]];
    }
    if (!kind) return;
    /* 该字段的黄标随当前值切换；底部计数/按钮同步（门禁：清零才可入账） */
    t.classList.toggle('suspect', ocrFieldBad(kind, t.value));
    var pend = ocrPendingCount(imp);
    var btn = $('#ocrConfirmBtn');
    if (btn) btn.disabled = pend > 0;
    var cnt = $('#ocrPending');
    if (cnt) cnt.textContent = pend ? '还有 ' + pend + ' 处要人工确认（黄底字段），改完才能入账' : '都确认过了，可以入账';
  }
  document.addEventListener('input', onDocInput);

  /* 口令框：回车提交（手机键盘右下角直达）；输入时收起错误提示 */
  function onDocKeydown(ev) {
    if (ev.key === 'Enter' && ev.target && ev.target.id === 'codeInput') {
      submitCode();
    }
  }
  document.addEventListener('keydown', onDocKeydown);

  function onHashChange() { showView(routeFromHash()); }
  window.addEventListener('hashchange', onHashChange);

  /* ---------- 启动 ---------- */
  showView(routeFromHash());

  /* 自查钩子：?autocalc=1 工票就位后自动跑一次核算（无头验证核算/工资条的就绪态用） */
  try {
    var hookQs = new URLSearchParams(location.search);
    if (hookQs.get('autocalc') === '1') {
      var hookCalc = function () {
        if (store.tickets.data && store.tickets.data.length) runCalc();
      };
      if (hookQs.get('sample') === 'good' && window.DEMO) {
        /* 组合钩子：解析 → 确认导入（走正式 /api/ingest/csv）→ 核算（走 /api/settle+/api/payslip），
           无头验证服务在线时的完整真链路用。确认导入会跳到 #/tickets，
           这里记住初始视图并还原，保证无头 dump 能验到请求的那个视图 */
        var hookRoute = routeFromHash();
        startImportText(window.DEMO.SAMPLE_CSV, '示例工票.csv').then(function () {
          if (store.import.mode === 'preview') confirmImport().then(function () {
            if (routeFromHash() !== hookRoute) location.hash = '#/' + hookRoute;
            hookCalc();
          });
        });
      } else {
        loadTickets().then(hookCalc);
      }
    }
    /* 自查钩子：?sample=good|bad 自动走一次 CSV 解析（验证导入预览态/错误态用） */
    if (hookQs.get('sample') === 'good' && window.DEMO && hookQs.get('autocalc') !== '1') {
      startImportText(window.DEMO.SAMPLE_CSV, '示例工票.csv');
    } else if (hookQs.get('sample') === 'bad' && window.DEMO) {
      startImportText(
        '票号,车间,日期,工序,姓名,数量,单价,废品\n' +
        'SJ-TEST,冲压车间,2026-09-12,冲压,冯玉珍,72,1.57,0\n' +
        'SJ-TEST,冲压车间,2026-09-12,冲压,孙丽华,七十二,2.27,0\n' +
        'SJ-TEST,冲压车间,2026-09-12,冲压,吴春梅,176,2.70,99\n',
        '坏示例.csv');
    }
  } catch (e) { /* 无 URLSearchParams 时跳过 */ }

  /* ---------- 离线演示回落（/api/* 不通时的兜底链路，runCalc 的 catch 调用） ---------- */
  /* 演示核算结果 + 演示工资条打包：返回 { calc, payslips }。
     全部合成数据、零真实数据，口径见 README「口径声明」与 web/demo-data.js */
  function demoPayslips(result) {
    var pack = { calc: result, payslips: window.DEMO ? window.DEMO.payslips(result) : [] };
    return pack;
  }
})();
