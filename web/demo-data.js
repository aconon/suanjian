/* 算件 · 演示数据（全部合成，零真实数据；离线回落用）
 * 数据自洽：工票合计 / 核算汇总 / 工资条全部由同一份工票行实时计算，不会对不上。
 * 引擎接入后这些函数只作为 /api/* 失败时的演示回落（见 app.js）。
 */
'use strict';
(function () {
  function r2(n) { return Math.round(n * 100) / 100; }

  /* ---------- 工票（票号 / 车间 / 日期 / 行明细） ---------- */
  var sheets = [
    { no: 'SJ-2026748', workshop: '冲压车间', date: '2026-09-12', rows: [
      { process: '冲压', name: '冯玉珍', qty: 72,  price: 1.57, defect: 0 },
      { process: '装配', name: '赵国庆', qty: 341, price: 3.07, defect: 1 },
      { process: '冲压', name: '孙丽华', qty: 58,  price: 2.27, defect: 0 },
      { process: '冲压', name: '吴春梅', qty: 176, price: 2.70, defect: 0 },
      { process: '焊接', name: '陈小红', qty: 206, price: 1.69, defect: 0 }
    ] },
    { no: 'SJ-2026226', workshop: '装配车间', date: '2026-09-13', rows: [
      { process: '装配', name: '周建军', qty: 165, price: 1.01, defect: 0 },
      { process: '装配', name: '王建国', qty: 380, price: 3.17, defect: 5 },
      { process: '裁剪', name: '李秀兰', qty: 199, price: 2.56, defect: 0 },
      { process: '攻丝', name: '赵国庆', qty: 91,  price: 1.89, defect: 0 },
      { process: '打磨', name: '刘志强', qty: 49,  price: 0.92, defect: 6 }
    ] },
    { no: 'SJ-2026272', workshop: '冲压车间', date: '2026-09-02', rows: [
      { process: '攻丝', name: '周建军', qty: 177, price: 1.78, defect: 0 },
      { process: '焊接', name: '蒋大明', qty: 41,  price: 1.06, defect: 0 },
      { process: '冲压', name: '郑海涛', qty: 367, price: 2.56, defect: 0 },
      { process: '冲压', name: '赵国庆', qty: 476, price: 1.02, defect: 0 },
      { process: '攻丝', name: '陈小红', qty: 292, price: 1.01, defect: 2 },
      { process: '焊接', name: '李秀兰', qty: 53,  price: 1.46, defect: 0 },
      { process: '折弯', name: '王建国', qty: 178, price: 0.57, defect: 0 }
    ] },
    { no: 'SJ-2026193', workshop: '装配车间', date: '2026-09-15', rows: [
      { process: '装配', name: '孙丽华', qty: 188, price: 1.12, defect: 0 },
      { process: '装配', name: '吴春梅', qty: 210, price: 1.35, defect: 2 },
      { process: '装配', name: '周建军', qty: 156, price: 2.44, defect: 0 },
      { process: '冲压', name: '郑海涛', qty: 143, price: 2.02, defect: 0 },
      { process: '裁剪', name: '李秀兰', qty: 167, price: 1.58, defect: 1 },
      { process: '焊接', name: '蒋大明', qty: 98,  price: 0.88, defect: 0 }
    ] },
    { no: 'SJ-2026337', workshop: '冲压车间', date: '2026-09-16', rows: [
      { process: '冲压', name: '冯玉珍', qty: 280, price: 1.89, defect: 0 },
      { process: '冲压', name: '吴春梅', qty: 164, price: 2.13, defect: 0 },
      { process: '焊接', name: '陈小红', qty: 191, price: 1.24, defect: 0 },
      { process: '打磨', name: '刘志强', qty: 77,  price: 1.45, defect: 3 },
      { process: '冲压', name: '孙丽华', qty: 203, price: 1.07, defect: 0 }
    ] },
    { no: 'SJ-2026451', workshop: '裁剪车间', date: '2026-09-18', rows: [
      { process: '裁剪', name: '李秀兰', qty: 221, price: 1.63, defect: 0 },
      { process: '折弯', name: '王建国', qty: 178, price: 2.36, defect: 0 },
      { process: '焊接', name: '蒋大明', qty: 132, price: 1.31, defect: 0 },
      { process: '冲压', name: '冯玉珍', qty: 96,  price: 1.74, defect: 0 },
      { process: '装配', name: '周建军', qty: 143, price: 1.96, defect: 0 }
    ] }
  ];

  /* 每张工票：行金额=(数量-废品)×单价，先按行留两位再求和（与引擎口径一致） */
  sheets.forEach(function (s) {
    s.total = r2(s.rows.reduce(function (acc, r) {
      return acc + r2((r.qty - r.defect) * r.price);
    }, 0));
    s.rowCount = s.rows.length;
  });

  var PERIOD = { label: '2026-09 期', start: '2026-08-26', end: '2026-09-25', rule: '江苏版 · 个人计件' };

  /* ---------- 核算：按工人汇总（数量 / 废品 / 计件工资） ---------- */
  function calc(sheetsIn) {
    var list = sheetsIn && sheetsIn.length ? sheetsIn : sheets;
    var map = Object.create(null);  // name -> agg（不继承原型，防 "constructor" 等名撞键）
    var order = [];
    list.forEach(function (s) {
      s.rows.forEach(function (r) {
        if (!map[r.name]) { map[r.name] = { name: r.name, qty: 0, defect: 0, amount: 0, details: [] }; order.push(r.name); }
        var a = map[r.name];
        var amt = r2((r.qty - r.defect) * r.price);
        a.qty += r.qty; a.defect += r.defect; a.amount = r2(a.amount + amt);
        a.details.push({ sheet: s.no, process: r.process, qty: r.qty, defect: r.defect, price: r.price, amount: amt });
      });
    });
    var workers = order.map(function (n) { return map[n]; })
      .sort(function (x, y) { return y.amount - x.amount; });
    return { period: PERIOD, workers: workers, total: r2(workers.reduce(function (a, w) { return a + w.amount; }, 0)) };
  }

  /* ---------- 班组计件（离线演示回落）：按数量占比分摊，尾差挂数量最大者 ---------- */
  /* 对齐引擎 team_piecepay 的演示口径（分取整用 Math.round 近似银行家舍入，仅离线演示；
     在线时一律走 /api/settle 的服务端精确计算） */
  function teamCalc(totalIn, members) {
    var t = r2(Number(totalIn));
    var list = (members || []).map(function (m) { return { name: m.name, qty: Number(m.qty) || 0 }; });
    var qsum = list.reduce(function (a, m) { return a + m.qty; }, 0);
    var bases = list.map(function () { return 0; });
    if (qsum > 0) bases = list.map(function (m) { return r2(t * m.qty / qsum); });
    var residual = r2(t - bases.reduce(function (a, b) { return a + b; }, 0));
    /* 尾差承接链：数量降序（并列取名单先）；承接后为负则该行清零、负余额下传 */
    var order = list.map(function (m, i) { return i; })
      .sort(function (x, y) { return (list[y].qty - list[x].qty) || (x - y); });
    var adjusts = list.map(function () { return 0; });
    var remaining = residual;
    for (var k = 0; k < order.length; k++) {
      var i = order[k];
      if (remaining === 0) break;
      if (bases[i] + remaining >= 0) { adjusts[i] = remaining; remaining = 0; }
      else { adjusts[i] = -bases[i]; remaining = r2(bases[i] + remaining); }
    }
    var lines = list.map(function (m, i) {
      return { name: m.name, qty: m.qty, base_amount: bases[i], adjust_amount: adjusts[i],
        amount: r2(bases[i] + adjusts[i]), remark: adjusts[i] !== 0 ? '尾差调整' : null };
    });
    return {
      team_total: t, lines: lines, residual: residual,
      allocated_total: r2(lines.reduce(function (a, l) { return a + l.amount; }, 0))
    };
  }

  /* ---------- 工资条：计件 + 岗位补贴 - 个税（演示口径） ---------- */
  var SUBSIDY = { '赵国庆': 3600, '王建国': 3400 };  // 演示：班组长岗位补贴（其中两人应发过 5000，能看到个税行生效）
  /* 个税演示口径：月应发≤5000 免；5000~8000 部分按 3%；仅作展示，非真实申报 */
  function taxDemo(gross) {
    if (gross <= 5000) return 0;
    return r2((Math.min(gross, 8000) - 5000) * 0.03);
  }
  function payslips(calcResult) {
    var cr = calcResult || calc(sheets);
    return cr.workers.map(function (w) {
      var subsidy = SUBSIDY[w.name] || 0;
      var gross = r2(w.amount + subsidy);
      var tax = taxDemo(gross);
      return { name: w.name, piece: w.amount, subsidy: subsidy, gross: gross, tax: tax,
        net: r2(gross - tax), details: w.details, qty: w.qty, defect: w.defect };
    });
  }

  /* ---------- 历史（此前期间，静态演示数字） ---------- */
  var history = [
    { period: '08-25 期', time: '2026-08-26 09:15', tickets: 9, workers: 8, total: 15372.60, status: '已核算' },
    { period: '07-25 期', time: '2026-07-25 17:40', tickets: 8, workers: 7, total: 13018.75, status: '已核算' },
    { period: '06-25 期', time: '2026-06-26 10:02', tickets: 7, workers: 7, total: 12446.30, status: '已核算' }
  ];

  /* ---------- 示例 CSV（与工票数据一致） ---------- */
  var csvLines = ['票号,车间,日期,工序,姓名,数量,单价,废品'];
  sheets.forEach(function (s) {
    s.rows.forEach(function (r) {
      csvLines.push([s.no, s.workshop, s.date, r.process, r.name, r.qty, r.price, r.defect].join(','));
    });
  });
  var SAMPLE_CSV = csvLines.join('\n');

  window.DEMO = {
    PERIOD: PERIOD, sheets: sheets, calc: calc, teamCalc: teamCalc, payslips: payslips,
    history: history, SAMPLE_CSV: SAMPLE_CSV, taxDemo: taxDemo, r2: r2
  };
})();
