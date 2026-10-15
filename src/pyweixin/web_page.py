"""开发调试用的 Web 页面（单文件，字符串常量）。

## 为什么是字符串而不是静态资源

要打包成桌面程序时，静态资源要多走一步（收集进包、写路径解析）。
开发阶段越少活动部件越好 —— 一页字符串，零依赖，不会因为忘了打包
某个文件而在别人机器上白屏。

## 它显示什么

**商户看得懂的措辞，和 Tk 界面保持一致。** 商户不该因为换了个界面
就要重新学一遍术语。这一版刻意不显示技术字段：模式名、步骤名、
adapter 之类的东西只进日志面板。
"""

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微信AI智能助理 · 开发调试</title>
<style>
  :root {
    --ink: #1b2430; --muted: #667486; --line: #dfe4ea; --bg: #f5f7f9;
    --card: #fff; --accent: #2f6fd0; --bad: #b4232c; --good: #1c7a4a;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.6 "Microsoft YaHei", "PingFang SC", sans-serif;
         color: var(--ink); background: var(--bg); }
  header { background: var(--card); border-bottom: 1px solid var(--line);
           padding: 12px 20px; display: flex; align-items: center; gap: 14px; }
  header h1 { font-size: 16px; margin: 0; }
  .badge { font-size: 12px; color: var(--muted); border: 1px solid var(--line);
           border-radius: 999px; padding: 2px 10px; }
  main { max-width: 1080px; margin: 0 auto; padding: 20px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
          padding: 16px; margin-bottom: 16px; }
  .card h2 { font-size: 14px; margin: 0 0 4px; }
  .hint { color: var(--muted); font-size: 12px; margin: 0 0 12px; }
  button { font: inherit; padding: 7px 14px; border-radius: 6px; border: 1px solid var(--line);
           background: var(--card); color: var(--ink); cursor: pointer; }
  button:hover { border-color: #b9c3cf; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 600; }
  .msg { padding: 9px 12px; border-radius: 6px; margin-bottom: 10px; font-size: 13px; }
  .msg.bad { background: #fdeceb; color: var(--bad); }
  .msg.good { background: #e8f6ee; color: var(--good); }
  .out { white-space: pre-wrap; word-break: break-word; background: #fbfcfd;
         border: 1px solid var(--line); border-radius: 6px; padding: 12px; }
  #logs { max-height: 260px; overflow: auto; font: 12px/1.5 Consolas, monospace;
          background: #1e2530; color: #d6e0ee; border-radius: 6px; padding: 10px;
          white-space: pre-wrap; }
  textarea { width: 100%; min-height: 90px; font: 12px/1.5 Consolas, monospace;
             border: 1px solid var(--line); border-radius: 6px; padding: 8px; }
  .muted { color: var(--muted); }
  .empty { color: var(--muted); padding: 10px 0; }
</style>
</head>
<body>
<header>
  <h1>微信AI智能助理 · 开发调试</h1>
  <span class="badge" id="step">—</span>
  <span class="badge" id="busy" hidden></span>
  <span style="margin-left:auto"></span>
  <span class="badge" id="conn">连接中…</span>
</header>

<main>
  <div id="banner"></div>

  <section class="card">
    <h2>0 · 产品账号</h2>
    <p class="hint">
      智能整理要提交任务给云端，**依赖产品账号**。没登录必然失败，
      所以放在最前面 —— 免得读到一堆消息才发现整理不了。
    </p>
    <div class="row">
      <button id="loginBtn" class="primary" onclick="doLogin()">登录产品账号</button>
      <button id="logoutBtn" onclick="doLogout()" hidden>退出登录</button>
      <span class="muted" id="acct">正在确认登录状态…</span>
    </div>
    <p class="hint" style="margin-top:10px">
      数据目录：<code id="datadir"></code><br>
      目录不对时这里会显示成「没有数据 / 未登录」——先看这里。
    </p>
  </section>

  <section class="card">
    <h2>1 · 微信</h2>
    <p class="hint">读取历史需要微信已登录并打开。</p>
    <div class="row">
      <button onclick="post('/api/wechat/check')">检查微信状态</button>
      <button onclick="post('/api/wechat/start')">打开微信</button>
      <button onclick="load('/api/conversations')">刷新会话列表</button>
    </div>
  </section>

  <section class="card">
    <h2>2 · 选会话</h2>
    <p class="hint">勾选要读取历史的群。不选就是全部。</p>
    <div id="conversations" class="empty">还没有会话，点上面「刷新会话列表」。</div>
    <div class="row" style="margin-top:10px">
      <button onclick="chooseAll(true)">全选</button>
      <button onclick="chooseAll(false)">全不选</button>
      <button onclick="sendChoose()">按勾选读取</button>
    </div>
  </section>

  <section class="card">
    <h2>3 · 读取历史</h2>
    <p class="hint">这一步驱动微信窗口，可能要一两分钟。可以关掉页面，它在后台跑。</p>
    <div class="row">
      <button class="primary" onclick="post('/api/history/read')">开始读取</button>
      <button onclick="post('/api/history/cancel')">停止读取</button>
    </div>
    <div id="progress" style="margin-top:12px"></div>
    <div id="history" style="margin-top:10px"></div>
  </section>

  <section class="card">
    <h2>4 · 智能整理</h2>
    <p class="hint">已整理过的消息不会重复提交 —— 重复提交会真的花钱，而结果只会是「已处理过」。</p>
    <div class="row">
      <button id="analyzeBtn" class="primary" onclick="post('/api/analyze')">开始智能整理</button>
      <span class="muted" id="analyzeHint"></span>
    </div>
    <div id="analysis" style="margin-top:10px"></div>
  </section>

  <section class="card">
    <h2>整理成一段话</h2>
    <p class="hint">把读到的消息交给模型汇总成可读的说明，不生成订单。</p>
    <div class="row">
      <button id="sumBtn" class="primary" onclick="post('/api/summary')">整理成一段话</button>
      <span class="muted" id="sumHint"></span>
    </div>
    <div id="summary" style="margin-top:12px"></div>
  </section>

  <section class="card">
    <h2>商品与客户名单</h2>
    <p class="hint">
      表头认中文。商品：编码、名称、单位、价格、别名。客户：名字、群、地址、偏好、别名。<br>
      别名可以填多个，用逗号隔开 —— 填了以后客户怎么叫都能认出来。重复导入不会产生重复别名。
    </p>
    <div class="row" style="margin-bottom:8px">
      <button onclick="document.getElementById('csv').placeholder='编码,名称,单位,价格,别名'">商品表格式</button>
      <button onclick="document.getElementById('csv').placeholder='名字,群,地址,偏好,别名'">客户表格式</button>
    </div>
    <textarea id="csv" placeholder="把 CSV 内容粘在这里，或用上面的按钮看格式"></textarea>
    <div class="row" style="margin-top:8px">
      <button onclick="reference('products')">导入商品</button>
      <button onclick="reference('customers')">导入客户</button>
    </div>
    <div id="reference" style="margin-top:10px"></div>
  </section>

  <section class="card">
    <h2>订单</h2>
    <div class="row" style="margin-bottom:10px">
      <button onclick="load('/api/orders')">刷新订单</button>
    </div>
    <div id="orders"><p class="empty">还没看。</p></div>
  </section>

  <section class="card">
    <h2>运行日志</h2>
    <p class="hint">出问题时这里是最先要看的地方。</p>
    <div id="logs">（还没有）</div>
  </section>
</main>

<script>const DATADIR = "__DATADIR__";
let polling = null;
const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

async function post(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body || {}),
    });
    const data = await r.json();
    if (!r.ok) { banner(data.error || ("请求失败：" + r.status), true); return data; }
    return data;
  } catch (e) {
    banner("连不上服务：" + e.message, true);
    return null;
  }
}

async function load(path) {
  try {
    const r = await fetch(path);
    return await r.json();
  } catch (e) {
    banner("连不上服务：" + e.message, true);
    return null;
  }
}

function banner(text, bad) {
  if (!text) { $("banner").innerHTML = ""; return; }
  $("banner").innerHTML = '<div class="msg ' + (bad ? "bad" : "good") + '">' + esc(text) + "</div>";
}

function render(data) {
  $("conn").textContent = "已连接";
  $("step").textContent = data.step || "—";
  const busy = $("busy");
  busy.hidden = !data.busy;
  busy.textContent = data.busy ? ("进行中：" + data.busy) : "";

  // 账号 —— 整理按钮能不能按，取决于它
  const acct = data.account || {};
  $("loginBtn").hidden = !!acct.logged_in;
  $("logoutBtn").hidden = !acct.logged_in;
  $("acct").textContent = acct.logged_in
    ? ("已登录：" + (acct.name || acct.merchant_id || "（未显示名称）"))
    : "未登录";
  const analyzeBtn = document.querySelector("#analyzeBtn");
  if (analyzeBtn) {
    analyzeBtn.disabled = !acct.logged_in;
    $("analyzeHint").textContent = acct.logged_in ? "" : "（要先登录产品账号）";
  }

  banner(data.error, true);
  if (!data.error && data.notice) banner(data.notice, false);

  // 进度
  if (data.progress) {
    const p = data.progress;
    const pct = p.total ? Math.round(p.done * 100 / p.total) : 0;
    $("progress").innerHTML =
      '<div class="out">已读 ' + p.done + " / " + p.total + "（" + pct + "%）" +
      (p.current ? "<br>当前：" + esc(p.current) : "") +
      (p.message ? "<br>" + esc(p.message) : "") + "</div>";
  } else {
    $("progress").innerHTML = "";
  }

  // 读取结果
  const h = data.history;
  if (h && h.coverage && h.coverage.length) {
    let html = '<table><tr><th>会话</th><th>本次条数</th><th>状态</th></tr>';
    for (const row of h.coverage) {
      let status = "正常";
      if (row.skipped) status = "已跳过";
      if (row.error) status = row.error;
      if (row.truncated) status += "（读满了）";
      // 发送人没认出来必须显式说出来，否则页面上的「读到 N 条」
      // 会让商户以为成功了。
      if (row.unresolved) status += "（" + row.unresolved + " 条发送人没认出来）";
      // 一条都没读到时，区分「群里没消息」和「读取失败」
      if (!row.error && !row.skipped && !row.count) {
        status += (row.fetched === 0)
          ? "（读取器拿回 0 条，这不等于群里没消息）"
          : "（读取器拿回 " + row.fetched + " 条，但本次一条都没进库）";
      }
      html += "<tr><td>" + esc(row.name) + "</td><td>" + row.count +
              "</td><td>" + esc(status) + "</td></tr>";
    }
    $("history").innerHTML = html + "</table><p class=\"hint\">本次合计 " + h.total + " 条。</p>";
  }

  // 整理结果
  const a = data.analysis;
  if (a) {
    $("analysis").innerHTML = '<div class="out">' +
      "处理 " + a.processed + " 条消息<br>生成订单 " + a.with_result + " 单<br>" +
      "需要核对 " + a.needs_review + " 单<br>本次提交任务 " + a.submitted_jobs + " 个" +
      (a.already_analyzed ? "<br>已整理过、本次没重复提交 " + a.already_analyzed + " 条" : "") +
      "</div>";
  }

  // 摘要：最终结果优先；还在跑时显示流式增量，不用干等。
  const s = data.summary;
  if (s) {
    $("summary").innerHTML = s.error
      ? '<div class="msg bad">' + esc(s.error) + "</div>"
      : '<div class="out">' + esc(s.summary) + "</div>";
  } else if (data.summary_stream) {
    $("summary").innerHTML = '<div class="out">' + esc(data.summary_stream) +
      '<br><span class="hint">正在生成……</span></div>';
  }

  $("sumBtn").disabled = !data.can_summarize;
  $("sumHint").textContent = data.can_summarize ? "" : "（还没有可用的消息，先读取历史）";

  // 日志
  if (data.logs && data.logs.length) $("logs").textContent = data.logs.join("\n");
}

function renderConversations(rows) {
  if (!rows || !rows.length) {
    $("conversations").innerHTML = '<p class="empty">没有会话。</p>';
    return;
  }
  let html = "<table><tr><th></th><th>会话</th><th>类型</th><th>成员</th></tr>";
  for (const row of rows) {
    html += '<tr><td><input type="checkbox" class="pick" value="' + esc(row.name) + '"></td>' +
            "<td>" + esc(row.name) + "</td><td>" + esc(row.kind) +
            "</td><td>" + row.members + "</td></tr>";
  }
  $("conversations").innerHTML = html + "</table>";
}

function chooseAll(flag) {
  for (const box of document.querySelectorAll(".pick")) box.checked = flag;
}

async function sendChoose() {
  const names = [];
  for (const box of document.querySelectorAll(".pick")) if (box.checked) names.push(box.value);
  await post("/api/conversations/choose", {names: names});
  banner(names.length ? ("已选择 " + names.length + " 个会话") : "已清空选择（将读取全部）", false);
}

function renderOrders(rows) {
  if (!rows || !rows.length) {
    $("orders").innerHTML = '<p class="empty">还没有订单。</p>';
    return;
  }
  let html = "<table><tr><th>订单</th><th>客户</th><th>状态</th><th>内容</th></tr>";
  for (const row of rows) {
    html += "<tr><td>" + esc(row.id) + "</td><td>" + esc(row.customer) +
            "</td><td>" + esc(row.status) + "</td><td>" + esc(row.summary) + "</td></tr>";
  }
  $("orders").innerHTML = html + "</table>";
}

async function doLogin() {
  await post("/api/login");
  banner("已发起登录。按提示完成授权，登录成功后这里会自动变。", false);
}

async function doLogout() {
  if (!confirm("确定退出登录吗？\n\n已经读到的消息和订单会留在这台电脑上，没有删除。")) return;
  await post("/api/logout");
  banner("已退出登录。消息和订单都还在，重新登录后可以继续整理，不会重复读取。", false);
}

async function reference(kind) {
  const text = $("csv").value.trim();
  if (!text) { banner("先把 CSV 内容粘进来。", true); return; }
  const out = await post("/api/reference", {kind: kind, text: text});
  if (!out) return;
  if (out.error) {
    $("reference").innerHTML = '<div class="msg bad">' + esc(out.error) + "</div>";
  } else {
    let html = '<div class="msg good">新增 ' + (out.created || 0) + " 条，更新 " +
               (out.updated || 0) + " 条";
    if (out.skipped) html += "，跳过 " + out.skipped + " 条";
    html += "</div>";
    if (out.problems && out.problems.length) {
      html += "<pre class=\"out\">" + out.problems.map(esc).join("\n") + "</pre>";
    }
    $("reference").innerHTML = html;
  }
}

// 轮询：读取和整理都是后台跑的，页面靠这个拿到进度。
// 1.2 秒一次 —— 再快只是白耗 CPU，长任务本来就以分钟计。
async function tick() {
  const data = await load("/api/state");
  if (data) render(data);
}

const origLoad = load;
load = async function (path) {
  const data = await origLoad(path);
  if (path === "/api/conversations") renderConversations(data.conversations);
  if (path === "/api/orders") renderOrders(data.orders);
  return data;
};

tick().then(() => $("datadir").textContent = DATADIR);
polling = setInterval(tick, 1200);
</script>
</body>
</html>
"""