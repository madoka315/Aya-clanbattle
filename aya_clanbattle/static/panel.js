/* Aya-clanbattle 面板（原生 JS）·「会战战报」双主题渲染层 */
"use strict";

const pathParts = location.pathname.split("/").filter(Boolean); // ["clan", "10086", ...]
const GID = pathParts[0] === "clan" && pathParts[1] ? Number(pathParts[1]) : null;
let CSRF = null, SELF = null, GROUP = null, BOSSES = null, MEMBERS = [];
let GUEST = false;   // 游客只读模式（privacy&0x1 且未登录）
let es = null;
let _lastBossSnap = null;   // SSE 闪烁用：上一次 boss 快照
let _prevPcts = {};         // 血条动画：上次渲染时各 boss 的血量百分比
let _prevHp = {};           // 数字滚动：上次渲染时各 boss 的血量原始值

function tweenNum(el, from, to, dur, fmt) {
  // 数字滚动（easeOutCubic，与血条 .6s 过渡同节奏）
  const t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - k, 3);
    el.textContent = fmt(from + (to - from) * e);
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
let curTab = "panel";
const NUM_FONT = "Bahnschrift, 'DIN Alternate', 'Segoe UI', sans-serif";
const EDIT_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';

/* ---------------- 主题 ---------------- */
function chartTheme() {
  // echarts 颜色不读 CSS 变量，按当前主题取色（与 panel.css token 对应）
  const dark = document.documentElement.dataset.theme === "dark";
  return dark ? {
    accent: "#f0a3ba", gold: "#d4ad55",
    ink: "#f2e9ec", ink2: "#b8a0a5",
    axisLine: "#574a52", axisLabel: "#b8a0a5",
    splitLine: "rgba(255,255,255,.06)",
    tipBg: "#332c35", tipBorder: "#574a52", tipText: "#f2e9ec",
  } : {
    accent: "#e2849f", gold: "#a8802a",
    ink: "#463a3c", ink2: "#8a7377",
    axisLine: "#d8bcb9", axisLabel: "#8a7377",
    splitLine: "#f2e5e2",
    tipBg: "#ffffff", tipBorder: "#d8bcb9", tipText: "#463a3c",
  };
}

function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("aya-theme", t); } catch { /* 隐私模式忽略 */ }
  const btn = $("#theme-toggle");
  if (btn) btn.textContent = t === "dark" ? "☀️" : "🌙";
  // 统计页图表颜色不随 CSS 切换，已渲染就重画
  if (curTab === "stats") loadStats().catch((e) => toast(e.message));
}

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = (n) => Number(n).toLocaleString("en-US");
const fmtHp = (n) => n >= 1e8 ? (n / 1e8).toFixed(n % 1e8 === 0 ? 0 : 2) + "亿" : fmt(n);
const fmtYi = (n) => {
  // 统计大数：亿保留一位小数（≥100 亿取整），万取整
  n = Number(n) || 0;
  if (n >= 1e8) {
    const y = n / 1e8;
    return (y >= 100 ? Math.round(y).toLocaleString("en-US") : y.toFixed(1)) + "亿";
  }
  if (n >= 1e4) return fmt(Math.round(n / 1e4)) + "万";
  return fmt(n);
};
const avatarUrl = (qq) => `/avatar/${qq}`;
const grp = (s) => s.replace(/\B(?=(\d{3})+(?!\d))/g, "\u202F");  // 千位窄空格分组（逗号在小字号数字字体里看着像点）

function toastHost() {
  // 有弹框打开时，toast 挂进弹框（top layer 内 fixed 定位），避免被弹框景深盖住
  const dlg = document.querySelector("dialog[open]");
  if (!dlg) return $("#toast-box");
  let box = dlg.querySelector(".toast-box");
  if (!box) { box = document.createElement("div"); box.className = "toast-box"; dlg.appendChild(box); }
  return box;
}
function toast(msg, ms = 6000) {
  // 多条并存；同文案 3 秒内重复触发（API+SSE 双通道/连点）时移除旧的重新弹出，
  // 保证每次点击都有可见反馈。同屏最多 6 条，超出移除最早的。
  const box = toastHost();
  for (const el of [...box.children]) {
    if (el.textContent === msg && Date.now() - el._ts < 3000) el.remove();
  }
  while (box.children.length >= 6) box.firstElementChild.remove();
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  el._ts = Date.now();
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 250);
  }, ms);
}

async function api(action, payload = {}) {
  const res = await fetch(`/clan/${GID}/api/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, csrf: CSRF, ...payload }),
  });
  const data = await res.json();
  if (data.code !== 0) throw new Error(data.message || `code=${data.code}`);
  return data;
}

function confirmDo(text, fn) {
  if (!confirm(text)) return;
  fn().then((r) => toast(r.message)).catch((e) => toast(e.message));
}

function ask(title, fieldsHtml, onOk) {
  const dlg = $("#dlg");
  dlg.innerHTML = `<h3>${esc(title)}</h3>${fieldsHtml}
    <div class="bar"><button class="primary" id="dlg-ok">确定</button>
    <button id="dlg-cancel">取消</button></div>`;
  dlg.showModal();
  dlg.querySelector("#dlg-cancel").onclick = () => dlg.close();
  dlg.querySelector("#dlg-ok").onclick = async () => {
    try { await onOk(dlg); dlg.close(); }
    catch (e) { toast(e.message); }
  };
  bindPickerPickers(dlg);
  dlg.addEventListener("close", () => {
    const tbox = dlg.querySelector(".toast-box");
    if (tbox) [...tbox.children].forEach((el) => $("#toast-box").appendChild(el));
  });
  return dlg;
}

function memberName(qqid) {
  const m = MEMBERS.find((m) => m.qqid == qqid);
  return m ? (m.nickname || qqid) : qqid;
}

function challengerOf(qqid) {
  for (const n of [1, 2, 3, 4, 5]) {
    const c = ((BOSSES[n] || {}).challengers || []).find((x) => x.qqid === qqid);
    if (c) return c;
  }
  return null;
}

function statusDialog(qqid) {
  // 更新出刀/挂树状态：剩余秒数 + 伤害（万）+ 留言（等同群聊"更新出刀/报伤害"）
  const c = challengerOf(qqid);
  if (!c) { toast("该成员当前没有申请出刀或挂树"); return; }
  ask(`更新出刀 · ${esc(c.name)}`, `
    <label>剩余秒数<input type="number" id="f-seconds" min="0" step="1"
      value="${c.seconds || 1}"></label>
    <label>伤害（万）<input type="number" id="f-damage" min="0" step="any"
      value="${c.damage || 0}"></label>
    <label>留言<input type="text" id="f-comment" maxlength="60"
      value="${esc(c.comment || "")}" placeholder="例如：不想出 / 刀被抢了"></label>`,
    async (dlg) => {
      const r = await api("update_status", {
        qqid,
        seconds: Math.max(0, Math.round(Number(dlg.querySelector("#f-seconds").value) || 0)),
        damage: Math.max(0, Math.round(Number(dlg.querySelector("#f-damage").value) || 0)),
        message: dlg.querySelector("#f-comment").value.trim(),
      });
      toast(r.message);
    });
}

/* ---------------- 面板渲染 ---------------- */
function hpState(pct) {
  if (pct < 25) return "low";
  if (pct < 62) return "mid";
  return "safe";
}

function chipOf(c) {
  let t = esc(c.name);
  if (c.is_continue) t += `<i style="font-style:normal;color:var(--teal)">补</i>`;
  if (c.behalf) t += `<i style="font-style:normal;color:var(--ink-2)">${esc(c.behalf)}代</i>`;
  if (c.damage > 0) t += `@${c.seconds}s,${c.damage > 11000 ? fmt(c.damage) : c.damage + "w"}`;
  if (c.comment) t += ` (${esc(c.comment)})`;
  const cls = c.tree ? "chip tree" : "chip chal";
  const canCancel = !GUEST && (SELF.is_admin || SELF.is_super ||
    c.qqid === SELF.user_id || (c.behalf_qqid || 0) === SELF.user_id);
  const pen = canCancel ? `<span class="pen" data-comment="${c.qqid}" title="更新出刀状态">${EDIT_SVG}</span>` : "";
  const x = canCancel ? `<span class="x" data-cancelapply="${c.qqid}" title="取消申请">✕</span>` : "";
  return `<span class="${cls}" data-qq="${c.qqid}" title="点击操作"><img class="cava" loading="lazy" src="${avatarUrl(c.qqid)}">${t}${pen}${x}${c.tree ? `<i style="font-style:normal">(挂树)</i>` : ""}</span>`;
}

function subChip(num, s) {
  const x = GUEST ? "" : `<span class="x" data-boss="${num}" data-qq="${s.qqid}">✕</span>`;
  return `<span class="chip sub"><b style="font-weight:700">预约</b>${esc(s.name)}${s.note ? ":" + esc(s.note) : ""}${x}</span>`;
}

function renderPanel(flashNums) {
  $("#guild-name").textContent = GROUP.group_name || GROUP.group_id;
  renderGlobalActions();
  const grid = $("#boss-grid");
  grid.innerHTML = "";
  for (const num of [1, 2, 3, 4, 5]) {
    const b = BOSSES[num];
    const pct = b.full_health ? Math.max(0, Math.round(b.health / b.full_health * 100)) : 0;
    const cards = [
      ...b.subscribers.map((s) => subChip(num, s)),
      ...b.challengers.map(chipOf),
    ].join("");
    const killed = b.health === 0 && !b.is_next;
    const st = killed ? "down" : hpState(pct);
    const dis = b.blocked ? ' disabled title="该王本阶段已结束，无法继续挑战"' : "";
    const ticks = [25, 50, 75].map((p) => `<i class="hp-tick" style="left:${p}%"></i>`).join("");
    grid.insertAdjacentHTML("beforeend", `
      <div class="boss ${!killed && st === "low" ? "low" : ""} ${flashNums && flashNums.has(num) ? "flash" : ""}" data-num="${num}">
        <div class="boss-head">
          <div class="boss-ava"><img src="/static/boss_icon/${b.icon_id}.webp" alt="" onerror="this.style.visibility='hidden'"></div>
          <div class="boss-id">
            <div class="boss-name">${b.num}-${esc(b.name)}</div>
            <span class="cycle num">${b.cycle} 周目</span>${b.is_next ? `<span class="tag">下周目</span>` : ""}
          </div>
          ${SELF.is_admin ? `<button class="icon-btn edit" data-op="modify" title="修改 boss 状态" aria-label="修改 boss 状态">${EDIT_SVG}</button>` : ""}
        </div>
        <div class="hp-row">
          <span class="hp-now" data-hp="${b.health}" data-prev="${_prevHp[num] ?? 0}">${fmtHp(b.health)}</span>
          <span class="hp-full">/ ${fmtHp(b.full_health)}</span>
          ${killed ? '<span class="hp-state s-down">击破</span>' : ""}
        </div>
        <div class="hpbar ${killed ? "" : st}"><div class="fill" style="width:${_prevPcts[num] ?? 0}%" data-pct="${pct}"></div>${ticks}</div>
        <div class="chips">${cards || `<span class="muted" style="padding:4px">${GUEST ? "无人挑战" : "无人挑战 · 点击下方按钮申请"}</span>`}</div>
        ${GUEST ? "" : `<div class="ops">
          <button data-op="apply"${dis}>申请出刀</button>
          <button data-op="addrecord"${dis}>报刀</button>
          <button data-op="subscribe">预约</button>
          <button data-op="tree"${dis}>挂树</button>
        </div>`}
      </div>`);
  }
  grid.querySelectorAll(".boss").forEach((card) => {
    const num = Number(card.dataset.num);
    card.querySelectorAll("button[data-op]").forEach((btn) => {
      btn.onclick = () => handleBossOp(btn.dataset.op, num);
    });
    if (GUEST) return;  // 游客只读：chip 快捷操作/取消入口不绑定
    card.querySelectorAll(".chip[data-qq]").forEach((chip) => {
      chip.onclick = () => memberQuickOp(Number(chip.dataset.qq));
    });
    // 留言铅笔：更新出刀/挂树状态（秒数/伤害/留言）
    card.querySelectorAll(".pen[data-comment]").forEach((pen) => {
      pen.onclick = (e) => {
        e.stopPropagation();
        statusDialog(Number(pen.dataset.comment));
      };
    });
    // 预约 ✕：取消预约（自己或管理员）
    card.querySelectorAll(".chip.sub .x").forEach((x) => {
      x.onclick = async (e) => {
        e.stopPropagation();
        try { toast((await api("cancel_subscribe", { boss_num: num, qq: Number(x.dataset.qq) })).message); }
        catch (err) { toast(err.message); }
      };
    });
    // 挑战 ✕：取消申请（自己/被代刀者/管理员）
    card.querySelectorAll(".x[data-cancelapply]").forEach((x) => {
      x.onclick = async (e) => {
        e.stopPropagation();
        const qq = Number(x.dataset.cancelapply);
        confirmDo(`取消 ${memberName(qq)} 的出刀申请？`, async () => {
          try {
            const r = await api("cancelapply", { behalf: qq });
            return r;
          } catch (err) { toast(err.message); }
        });
      };
    });
  });
  // 血条动画：以上次渲染的百分比为起点，下一帧过渡到新值（首次=从 0 生长）
  requestAnimationFrame(() => requestAnimationFrame(() => {
    grid.querySelectorAll(".fill[data-pct]").forEach((el) => { el.style.width = el.dataset.pct + "%"; });
    grid.querySelectorAll(".hp-now[data-hp]").forEach((el) => {
      const from = Number(el.dataset.prev), to = Number(el.dataset.hp);
      if (from !== to) tweenNum(el, from, to, 600, (v) => fmtHp(Math.round(v)));
    });
  }));
  for (const num of [1, 2, 3, 4, 5]) {
    _prevPcts[num] = (BOSSES[num] && BOSSES[num].full_health)
      ? Math.max(0, Math.round(BOSSES[num].health / BOSSES[num].full_health * 100)) : 0;
    _prevHp[num] = BOSSES[num] ? BOSSES[num].health : 0;
  }
  moveTabInk();  // 群名刚从占位符换成真实名，行宽变了，墨线须重测
}

function renderGlobalActions() {
  // 配额徽章 + 全局操作条：轻刷新入口（不重建 boss 卡，不碰血条动画）
  const pc = $("#progress-chip");
  if (GROUP.today_blades !== undefined) {
    pc.hidden = false;
    pc.textContent = `今日 ${GROUP.today_blades} / ${GROUP.quota} 刀`;
    pc.classList.toggle("done", GROUP.today_blades >= GROUP.quota);
    moveTabInk();  // 徽章从 hidden 变可见会改变行宽
  }
  const ga = $("#global-actions");
  if (GUEST) { ga.innerHTML = ""; return; }  // 游客只读：无撤销/SL 等写操作
  ga.innerHTML = `
    <button class="primary" id="ga-undo" ${SELF.can_undo ? "" : "disabled"
      } title="${SELF.can_undo ? "撤销最后一刀" : "最后一刀不是你的记录"}">撤销最后一刀</button>
    <button id="ga-sl">SL 记录${SELF.sl_today ? ' <span class="sl-badge">今日已SL</span>' : ""}</button>
    ${SELF.is_admin ? `<button id="ga-remind">催刀</button><button id="ga-dropmember">删成员</button><button id="ga-addmember">加成员</button>` : ""}
`;
  $("#ga-undo").onclick = () => confirmDo("撤销最后一刀？", async () => {
    const r = await api("undo");
    return r;
  });
  $("#ga-sl").onclick = () => slDialog();
  if (SELF.is_admin) {
    $("#ga-remind").onclick = () => remindDialog();
    $("#ga-dropmember").onclick = () => dropMemberDialog();
    $("#ga-addmember").onclick = () => addMemberDialog();
  }
}

async function refreshSelfState() {
  // 轻刷新：报刀/SL/撤销/取消申请后只更新身份状态与配额，boss 卡视觉交给 SSE 渲染
  try {
    const data = await api("get_data");
    SELF = data.selfData;
    GROUP = data.groupData;
    GROUP.today_counts = data.today_counts || {};
    MEMBERS = data.members;  // 成员态（last_save_slot 等）一并刷新：SL 徽章此前只在 loadData 更新，不刷新页面看不到
    renderGlobalActions();
    if (curTab === "records") loadRecords().catch(() => {});  // 记录页可见时同步重拉（SL/新刀实时上表）
  } catch { /* 静默：下一条 SSE 事件会再触发 */ }
}

function memberQuickOp(qqid) {
  ask(`操作 ${memberName(qqid)}`, `
    <label><input type="radio" name="qo" value="untree" checked> 帮 TA 下树</label>
    <label><input type="radio" name="qo" value="cancelapply"> 帮 TA 取消申请</label>
    <label><input type="radio" name="qo" value="sl"> 帮 TA 记 SL</label>`,
    async (dlg) => {
      const v = dlg.querySelector("input[name=qo]:checked").value;
      if (v === "untree") toast((await api("take_it_of_the_tree", { behalf: qqid })).message);
      if (v === "cancelapply") toast((await api("cancelapply", { behalf: qqid })).message);
      if (v === "sl") toast((await api("save_slot", { member: qqid, status: true })).message);
    });
}

async function handleBossOp(op, num) {
  try {
    if (op === "apply") {
      ask(`申请出刀 ${num}王`, `
        ${SELF.comp_available ? '<label><input type="checkbox" id="f-comp"> 补偿刀</label>' : ""}
        <label>代刀</label>${pickerHTML("p-behalf")}
        <div class="muted" style="margin:4px 0 0">默认为自己</div>`,
        async (dlg) => {
          bindPickerPickers(dlg);
          const behalf = pickerQQ("p-behalf");
          const comp = dlg.querySelector("#f-comp");
          const r = await api("apply", { boss_num: num, is_continue: comp ? comp.checked : false,
                                         behalf: behalf !== SELF.user_id ? behalf : undefined });
          toast(r.message);
        });
    } else if (op === "addrecord") {
      const dlg = ask(`报刀 ${num}王`, `
        <label><input type="checkbox" id="f-defeat"> 尾刀（击败）</label>
        <label>伤害（万）<input type="number" id="f-damage" min="0" step="any" placeholder="非尾刀必填"></label>
        <label>代报</label>${pickerHTML("p-behalf")}
        <div class="muted" style="margin:4px 0 0">默认为自己</div>`,
        async (dlg) => {
          bindPickerPickers(dlg);
          const defeat = dlg.querySelector("#f-defeat").checked;
          const damage = defeat ? 0 : Math.round(Number(dlg.querySelector("#f-damage").value) * 10000);
          if (!defeat && (!Number.isFinite(damage) || damage <= 0)) throw new Error("非尾刀需要填写伤害（万）");
          const behalf = pickerQQ("p-behalf");
          const r = await api("addrecord", { defeat, damage, boss_num: num,
                                             behalf: behalf !== SELF.user_id ? behalf : undefined });
          toast(r.message);
        });
      const dmgLabel = dlg.querySelector("#f-damage").closest("label");
      const syncDmg = () => { dmgLabel.style.display = dlg.querySelector("#f-defeat").checked ? "none" : ""; };
      dlg.querySelector("#f-defeat").onchange = syncDmg; syncDmg();
    } else if (op === "tree") {
      ask(`挂树 ${num}王`, `<label>代挂</label>${pickerHTML("p-behalf")}
        <div class="muted" style="margin:4px 0 0">默认为自己</div>`,
        async (dlg) => {
          bindPickerPickers(dlg);
          const behalf = pickerQQ("p-behalf");
          const r = await api("put_on_the_tree", { boss_num: num,
            behalf: behalf !== SELF.user_id ? behalf : undefined });
          toast(r.message);
        });
    } else if (op === "subscribe") {
      const dlg = ask(`预约 ${num}王`, `
        <label>留言<input type="text" id="f-sub-msg" placeholder="选填"></label>
        <label><input type="checkbox" id="f-hejin"> 希望合刀（勾选后自动填入留言）</label>`,
        async (dlg) => {
          const message = dlg.querySelector("#f-sub-msg").value.trim();
          const r = await api("add_subscribe", { boss_num: num, message });
          toast(r.message);
        });
      dlg.querySelector("#f-hejin").onchange = (e) => {
        if (e.target.checked) dlg.querySelector("#f-sub-msg").value = "希望合刀";
      };
    } else if (op === "modify") {
      modifyDialog(num);
    }
  } catch (e) { toast(e.message); }
}

function modifyDialog(num) {
  const b = BOSSES[num];
  ask(`修改 ${num}王 状态`, `
    <label>周目<input type="number" id="f-cycle" value="${GROUP.cycle}"></label>
    <label><input type="checkbox" id="f-next" ${b.is_next ? "checked" : ""}> 处于下周目</label>
    <label>剩余血量（万）<input type="number" id="f-hp" step="1" min="0"
      value="${+(b.health / 10000).toFixed(2)}"></label>
    <div class="muted" style="margin:-6px 0 8px">满血 ${fmtHp(b.full_health)} · 输入 0 即视为击破</div>`,
    async (dlg) => {
      const hpWan = Number(dlg.querySelector("#f-hp").value);
      if (!Number.isFinite(hpWan) || hpWan < 0) throw new Error("血量格式不正确");
      const bossData = {};
      for (const n of [1, 2, 3, 4, 5]) bossData[n] = {
        is_next: n === num ? dlg.querySelector("#f-next").checked : BOSSES[n].is_next,
        health: n === num ? Math.round(hpWan * 10000) : BOSSES[n].health,
      };
      const r = await api("modify", { cycle: Number(dlg.querySelector("#f-cycle").value), bossData });
      toast(r.message);
    });
}

function slDialog() {
  ask("SL 记录", `
    <label>成员</label>${pickerHTML("p-sl", { mark: (m) => m.last_save_slot === GROUP.pcrdate ? "今日已SL" : "" })}
    <label><input type="checkbox" id="f-cancel"> 取消 SL 记录</label>`,
    async (dlg) => {
      bindPickerPickers(dlg);
      const member = pickerQQ("p-sl");
      const status = !dlg.querySelector("#f-cancel").checked;
      const r = await api("save_slot", { member, status });
      toast(r.message);
      refreshSelfState();
    });
}

function remindDialog(mode) {
  // mode: "zero"=一键催0刀 / "mid"=一键催没出完 / 空=手动勾选
  const cnt = (m) => (GROUP.today_counts || {})[m.qqid] ?? 0;
  const dlg = ask("催刀", `
    <div class="bar" style="margin:0 0 8px;gap:6px">
      <button type="button" id="rk-zero">一键催0刀</button>
      <button type="button" id="rk-mid">一键催没出完</button>
    </div>
    <div style="max-height:260px;overflow:auto">
      ${MEMBERS.map((m) => `
      <label style="display:flex;align-items:center;gap:8px;margin:3px 0">
        <input type="checkbox" class="f-m" value="${m.qqid}">
        <img class="cava" loading="lazy" src="${avatarUrl(m.qqid)}" style="width:22px;height:22px;border-radius:50%;object-fit:cover" alt="">
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(m.nickname || m.qqid)}</span>
        <span class="fin-badge ${cnt(m) >= 3 ? "ok" : "no"}">${cnt(m)}</span>
      </label>`).join("")}
    </div>
    <div style="border-top:1px dashed var(--rule);margin-top:10px;padding-top:10px">
      <label style="margin:0"><input type="checkbox" id="f-private"> 私聊发送</label>
    </div>`,
    async (d) => {
      const memberlist = [...d.querySelectorAll(".f-m:checked")].map((x) => Number(x.value));
      if (!memberlist.length) throw new Error("请先勾选成员");
      if (!confirm(`确认催刀 ${memberlist.length} 人？`)) return;
      const r = await api("send_remind", { memberlist, send_private_msg: d.querySelector("#f-private").checked });
      toast(r.message);
    });
  const setChecks = (fn) => dlg.querySelectorAll(".f-m").forEach((c) => {
    c.checked = fn((GROUP.today_counts || {})[Number(c.value)] ?? 0);
  });
  dlg.querySelector("#rk-zero").onclick = () => setChecks((n) => n === 0);
  dlg.querySelector("#rk-mid").onclick = () => setChecks((n) => n < 3);
  if (mode === "zero") setChecks((n) => n === 0);
  if (mode === "mid") setChecks((n) => n < 3);
}

function dropMemberDialog() {
  ask("删除成员", `
    <div style="max-height:260px;overflow:auto">${MEMBERS.map((m) =>
      `<label style="display:flex;align-items:center;gap:8px;margin:3px 0">
        <input type="checkbox" class="f-m" value="${m.qqid}">
        <img class="cava" loading="lazy" src="${avatarUrl(m.qqid)}" style="width:22px;height:22px;border-radius:50%;object-fit:cover" alt="">
        <span>${esc(m.nickname || m.qqid)}</span></label>`).join("")}</div>`,
    async (dlg) => {
      const memberlist = [...dlg.querySelectorAll(".f-m:checked")].map((x) => Number(x.value));
      const r = await api("drop_member", { memberlist });
      toast(r.message);
      await loadData();
    });
}

function addMemberDialog() {
  ask("添加成员", `
    <label>QQ号<input type="text" id="f-qq"></label>
    <label>角色<select id="f-role"><option value="100">成员</option><option value="10">公会战管理员</option></select></label>`,
    async (dlg) => {
      const r = await api("add_member", { qqid: Number(dlg.querySelector("#f-qq").value),
                                          role: Number(dlg.querySelector("#f-role").value) });
      toast(r.message);
      await loadData();
    });
}

/* ---------------- 记录 ---------------- */
// PCR 换日偏移（cn/tw=3、jp=4）：时间换算统一走这张表，勿再写死 3/4 或 +08:00
function pcrTzHours() {
  return GROUP && GROUP.game_server === "jp" ? 4 : 3;
}
function pcrDayToDateStr(d) {
  // PCR 日标签 = 纪元 + d 天（后端 pcr_date_str 同口径），与服务器时区无关
  return new Date(86400 * d * 1000).toISOString().slice(0, 10);
}
function dateStrToPcrTs(s) {
  // 该日当地 05:00 的 UTC 时间戳：当地 05:00 = UTC (5 - 偏移) 点
  const [y, m, day] = s.split("-").map(Number);
  return Date.UTC(y, m - 1, day, 5 - pcrTzHours(), 0, 0) / 1000;
}

function bladeSeg(c) {
  // 刀轨格：完整刀占 2 格，尾刀/补偿占 1 格；三层结构=周目-王(弱化) + 伤害(主视觉) + 尾/补/代(彩色角标)
  const half = c.is_continue || c.boss_health_remain == 0;
  const cls = c.is_continue ? "comp" : (c.boss_health_remain == 0 ? "tail" : "full");
  const kind = c.is_continue ? "补" : (c.boss_health_remain == 0 ? "尾" : "");
  const tz = pcrTzHours();
  const when = new Date((86400 * c.challenge_pcrdate + c.challenge_pcrtime - tz * 3600) * 1000);
  const dmg = c.challenge_damage >= 10000
    ? `${grp(String(Math.round(c.challenge_damage / 10000)))}<i class="seg-unit">w</i>`
    : esc(fmt(c.challenge_damage));
  return `<span class="blade-seg${half ? "" : " w2"} ${cls}" title="${c.boss_cycle}-${c.boss_num} · ${esc(fmt(c.challenge_damage))}${esc(kind)} · ${esc(when.toLocaleString("zh-CN"))}"><span class="seg-id">${c.boss_cycle}-${c.boss_num}</span><span class="seg-dmg">${dmg}</span>${kind ? `<i class="seg-kind">${kind}</i>` : ""}${c.behalf ? `<i class="seg-kind beh">代</i>` : ""}</span>`;
}
function bladeTrack(cs) {
  const segs = cs.map(bladeSeg);
  let rem = 6 - cs.reduce((a, c) => a + (c.is_continue || c.boss_health_remain == 0 ? 1 : 2), 0);
  while (rem >= 2) { segs.push('<span class="blade-seg empty w2" title="未出刀"></span>'); rem -= 2; }
  if (rem === 1) segs.push('<span class="blade-seg empty" title="未出刀"></span>');
  return `<div class="blade-track">${segs.join("")}</div>`;
}
function finOf(cs) {
  let fin = 0;
  for (const c of cs) {
    if (c.boss_health_remain == 0 && !c.is_continue) fin += 0.5;
    else if (c.is_continue) fin += 0.5;
    else fin += 1;
  }
  return fin;
}

async function loadLatestDay() {
  // 默认落点=当前档案里最新有记录的 PCR 日：今天没数据就逐日回溯（最多 14 天，覆盖整期）
  let day = GROUP.pcrdate;
  for (let i = 0; i < 14; i++) {
    $("#rec-date").value = pcrDayToDateStr(day);
    const r = await api("get_challenge", { ts: dateStrToPcrTs($("#rec-date").value) });
    if (r.challenges.length) { renderDayRecords(r.challenges); return; }
    day -= 1;
  }
  $("#rec-date").value = pcrDayToDateStr(GROUP.pcrdate);  // 新期零记录：回到今天展示空表
  renderDayRecords([]);
}

async function loadRecords() {
  if (recMode === "day") {
    const s = $("#rec-date").value;
    const r = await api("get_challenge", s ? { ts: dateStrToPcrTs(s) } : {});
    renderDayRecords(r.challenges);
  } else {
    const r = await api("get_user_challenge", { qqid: recMemberQQ });
    renderMemberRecords(r);
  }
}

function renderMemberRecords(r) {
  $("#rec-table").classList.remove("timeline");
  const byDay = {};
  for (const c of r.challenges) (byDay[c.challenge_pcrdate] ??= []).push(c);
  let html = `<tr class="head"><th>日期</th><th>刀数</th><th>出刀详情</th></tr>`;
  for (const d of Object.keys(byDay).sort().reverse()) {
    const cs = byDay[d];
    const fin = finOf(cs);
    html += `<tr><td>${pcrDayToDateStr(Number(d))}</td>
      <td><span class="fin-badge ${fin >= 3 ? "ok" : "no"}">${fin}</span></td>
      <td>${bladeTrack(cs)}</td></tr>`;
  }
  $("#rec-table").innerHTML = html || '<tr><td class="muted">无记录</td></tr>';
  $("#rec-summary").textContent = `${r.user_info.nickname} 的全部出刀`;
}

async function openMemberRecords(qqid) {
  recMode = "member";
  recMemberQQ = qqid;
  $("#rec-back").hidden = false;
  $("#rec-view").hidden = true;
  $("#rec-date").hidden = true;   // 单成员视图跨全部日期，日期筛选无意义
  $("#rec-hint").hidden = true;
  try { await loadRecords(); } catch (e) { toast(e.message); }
}

function backToDay() {
  recMode = "day";
  $("#rec-back").hidden = true;
  $("#rec-view").hidden = false;
  $("#rec-date").hidden = false;
  $("#rec-hint").hidden = GUEST || recDayView !== "roster";
  loadRecords().catch((e) => toast(e.message));
}

let recFinDir = "desc";        // 记录页成员列表：刀数排序方向（默认降序）
let lastDayChallenges = null;  // 供排序切换时免请求重渲染
let recMode = "day";           // 记录页模式：day=按日全员 / member=单成员全部记录（点成员行进入）
let recDayView = "roster";     // 日视图子模式：roster=按成员 / timeline=按出刀时间倒序
let recMemberQQ = 0;

function renderDayRecords(challenges) {
  lastDayChallenges = challenges;
  if (recDayView === "timeline") { renderTimelineRecords(challenges); return; }
  $("#rec-table").classList.remove("timeline");
  const byMember = {};
  for (const c of challenges) (byMember[c.qqid] ??= []).push(c);
  let total = 0;
  const rows = MEMBERS.map((m) => {
    const cs = byMember[m.qqid] || [];
    const fin = finOf(cs);
    total += fin;
    return { m, cs, fin };
  }).sort((a, b) => recFinDir === "desc" ? b.fin - a.fin : a.fin - b.fin);
  const arrow = recFinDir === "desc" ? "▼" : "▲";
  const trs = rows.map(({ m, cs, fin }) => `
    <tr data-qq="${m.qqid}"><td><span class="rec-id"><img class="cava" loading="lazy" src="${avatarUrl(m.qqid)}" onerror="this.style.visibility='hidden'" alt=""><span class="rec-name">${esc(m.nickname || m.qqid)}</span>${m.last_save_slot === GROUP.pcrdate ? '<span class="sl-badge">今日已SL</span>' : ""}</span></td>
    <td><span class="fin-badge ${fin >= 3 ? "ok" : "no"}">${fin}</span></td>
    <td>${cs.length ? bladeTrack(cs) : '<div class="muted">未出刀</div>'}</td></tr>`);
  $("#rec-table").innerHTML = `<tr class="head"><th>成员</th><th class="sortable" id="rec-sort-fin" title="点击切换升/降序">刀数<i class="sort-arrow">${arrow}</i></th><th>出刀详情</th></tr>${trs.join("")}`;
  $("#rec-summary").textContent = `合计 ${total} / ${MEMBERS.length * 3}`;
  $("#rec-sort-fin").onclick = toggleRecSort;
  // 整行可点：进入该成员的全部出刀记录（游客无此接口权限，不绑定）
  if (!GUEST) $("#rec-table").querySelectorAll("tr[data-qq]").forEach((tr) => {
    tr.onclick = () => openMemberRecords(Number(tr.dataset.qq));
  });
}

function toggleRecSort() {
  recFinDir = recFinDir === "desc" ? "asc" : "desc";
  if (lastDayChallenges) renderDayRecords(lastDayChallenges);  // 缓存数据免请求重渲染
}

function renderTimelineRecords(challenges) {
  // 按出刀时间从近到远（同一 PCR 日内的墙钟时间；+86400 的补报按取模还原）
  const rows = [...challenges].sort((a, b) =>
    ((b.challenge_pcrtime % 86400) - (a.challenge_pcrtime % 86400))
    || (b.cid - a.cid));
  const fmtTime = (t) => {
    const local = (5 * 3600 + (t % 86400)) % 86400;
    return `${String(Math.floor(local / 3600)).padStart(2, "0")}:${String(Math.floor(local % 3600 / 60)).padStart(2, "0")}`;
  };
  const kindOf = (c) => c.is_continue
    ? '<span class="tl-tag comp">补</span>'
    : (c.boss_health_remain === 0 ? '<span class="tl-tag tail">尾</span>'
                                  : '<span class="tl-tag">完整</span>');
  const totalDamage = challenges.reduce((a, c) => a + c.challenge_damage, 0);
  $("#rec-table").classList.add("timeline");
  $("#rec-table").innerHTML = `<tr class="head"><th>时间</th><th>成员</th><th>王</th><th>伤害</th><th>类型</th></tr>`
    + (rows.map((c) => `
      <tr><td class="tl-time">${fmtTime(c.challenge_pcrtime)}</td>
      <td><span class="rec-name">${esc(memberName(c.qqid))}</span>${c.behalf ? '<span class="tl-tag beh">代</span>' : ""}</td>
      <td>${c.boss_cycle}-${c.boss_num}</td>
      <td>${esc(fmt(c.challenge_damage))}</td>
      <td class="tl-type">${kindOf(c)}</td></tr>`).join("")
      || '<tr><td class="muted">无记录</td></tr>');
  $("#rec-summary").textContent =
    `${rows.length} 条 · 合计 ${finOf(challenges)} 刀 · ${fmtYi(totalDamage)}`;
}

function toggleRecView() {
  recDayView = recDayView === "roster" ? "timeline" : "roster";
  $("#rec-view").textContent = recDayView === "roster" ? "按时间" : "按成员";
  $("#rec-hint").hidden = GUEST || recDayView !== "roster";
  if (lastDayChallenges) renderDayRecords(lastDayChallenges);
}

/* ---------------- 统计：战报版式 ---------------- */
let _charts = [];
let statsSortKey = "damage", statsSortDir = "desc";  // 成员贡献榜排序（默认总伤害降序）
let lastStatsResult = null;

function renderRanking() {
  const r = lastStatsResult;
  if (!r) return;
  const dir = statsSortDir === "desc" ? -1 : 1;
  const ranked = [...r.members].sort((a, b) => ((a[statsSortKey] || 0) - (b[statsSortKey] || 0)) * dir);
  // 伤害条基准=全员最大伤害（与当前排序键无关），切排序时条形比例保持正确
  const maxDmg = Math.max(1, ...r.members.map((m) => m.damage || 0));
  const arrow = (k) => statsSortKey === k ? `<i class="sort-arrow">${statsSortDir === "desc" ? "▼" : "▲"}</i>` : "";
  $("#stats-ranking").innerHTML = `<table class="rank-table">
    <tr><th></th><th>成员</th><th class="sortable" data-key="blades">刀数${arrow("blades")}</th><th>尾刀</th><th class="sortable" data-key="damage" style="min-width:230px">总伤害${arrow("damage")}</th></tr>
    ${ranked.map((m, i) => `
      <tr class="${i < 3 ? "r" + (i + 1) : ""}">
        <td class="rk">${i + 1}</td>
        <td class="rk-name">${esc(m.name)}</td>
        <td class="rk-num">${m.blades}</td>
        <td class="rk-tail">${m.tails || "—"}</td>
        <td><div class="rank-dmg">
          <span class="dmg-bar"><i style="width:${Math.max(1, Math.round(m.damage / maxDmg * 100))}%"></i></span>
          <span class="dmg-val">${fmtYi(m.damage)}</span></div></td>
      </tr>`).join("")}</table>`;
  $("#stats-ranking").querySelectorAll("th.sortable").forEach((th) => {
    th.title = "点击排序（再点翻转升降）";
    th.onclick = () => {
      if (statsSortKey === th.dataset.key) statsSortDir = statsSortDir === "desc" ? "asc" : "desc";
      else { statsSortKey = th.dataset.key; statsSortDir = "desc"; }
      renderRanking();  // 缓存数据免请求重渲染
    };
  });
}

async function loadStats() {
  const bid = $("#stats-bid").value;
  const r = await api("get_statistics", { battle_id: bid });
  const T = chartTheme();

  // —— 报头：期数 / 范围 / 大数字
  const totalBlades = r.days.reduce((a, d) => a + d.blades, 0);
  const totalDamage = r.days.reduce((a, d) => a + d.damage, 0);
  const totalTails = r.members.reduce((a, m) => a + m.tails, 0);
  // 标题=「xx月会战战报」：月份取最新有数据的日子；整个档案零记录则按今天
  const latestDay = r.days.length ? r.days[r.days.length - 1].date : pcrDayToDateStr(GROUP.pcrdate);
  $("#stats-title").textContent =
    bid === "all" ? "全部档案战报" : `${Number(latestDay.slice(5, 7))}月会战战报`;
  $("#stats-range").textContent = r.days.length
    ? `${r.days[0].date} ~ ${r.days[r.days.length - 1].date} · ${r.members.length} 名成员出刀`
    : "暂无出刀记录";
  const dmgYi = fmtYi(totalDamage).match(/^([\d,.]+)(.*)$/);
  $("#stats-totals").innerHTML = [
    [fmt(totalBlades), "", "总刀数"],
    [dmgYi ? dmgYi[1] : fmt(totalDamage), dmgYi ? dmgYi[2] : "", "总伤害"],
    [fmt(totalTails), "", "尾刀"],
    [String(r.members.length), "", "出刀成员"],
  ].map(([num, unit, label]) =>
    `<div class="total-cell"><div class="total-num">${num}${unit ? `<span class="unit">${unit}</span>` : ""}</div><div class="total-label">${label}</div></div>`
  ).join("");

  const hasData = r.days.length > 0;
  document.querySelectorAll("#tab-stats .report-block").forEach((el) => { el.hidden = !hasData; });
  if (!hasData) return;

  // 每次进入统计页都 dispose 重建，让柱条/刀痕重放生长动画
  for (const c of _charts) c.dispose();
  _charts = [];
  const mk = (id) => {
    const c = echarts.init(document.getElementById(id));
    _charts.push(c);
    return c;
  };
  const anim = {
    animationDuration: 800,
    animationEasing: "cubicOut",
    animationDelay: (i) => i * 15,
  };
  const axisStyle = { axisLine: { lineStyle: { color: T.axisLine } },
                      axisTick: { show: false },
                      axisLabel: { color: T.axisLabel } };
  const tipStyle = { backgroundColor: T.tipBg, borderColor: T.tipBorder, textStyle: { color: T.tipText } };

  // —— 每日刀数（刀痕刻记：每刻=一刀）+ 每日伤害（金色折线），拆为两张图。
  // 数据点少时直接在点上标数值（免悬浮）；「全部」档案可达数千点，标值必挤作一团，按点数阈值关闭
  const dayNames = r.days.map((d) => d.date.slice(5));
  const labelBlades = r.days.length <= 45;   // 「12」两位数约 20px，45 点以内放得下
  const labelDmg = r.days.length <= 25;      // 「4.8亿」约 34px，25 点以内放得下
  mk("chart-days").setOption({
    ...anim,
    tooltip: { trigger: "axis", ...tipStyle,
      formatter: (ps) => { const d = r.days[ps[0].dataIndex]; return `${d.date}<br/>刀数 ${d.blades}`; } },
    grid: { top: labelBlades ? 30 : 24, left: 42, right: 24, bottom: 24 },
    xAxis: { ...axisStyle, type: "category", data: dayNames,
             axisLabel: { color: T.axisLabel, fontFamily: NUM_FONT,
                          interval: Math.max(0, Math.ceil(dayNames.length / 16) - 1) } },
    yAxis: { type: "value", ...axisStyle },
    series: [
      { name: "刀数", type: "pictorialBar", symbol: "rect", symbolSize: ["58%", 3],
        symbolRepeat: true, symbolMargin: 2, symbolClip: false,
        data: r.days.map((d) => d.blades), itemStyle: { color: T.accent },
        label: labelBlades ? { show: true, position: "top", color: T.ink2,
                               fontSize: 10, fontFamily: NUM_FONT } : { show: false } },
    ],
  });
  mk("chart-dmg").setOption({
    ...anim,
    tooltip: { trigger: "axis", ...tipStyle,
      formatter: (ps) => { const d = r.days[ps[0].dataIndex]; return `${d.date}<br/>伤害 ${fmtYi(d.damage)}`; } },
    grid: { top: labelDmg ? 30 : 24, left: 52, right: 24, bottom: 24 },
    xAxis: { ...axisStyle, type: "category", data: dayNames,
             axisLabel: { color: T.axisLabel, fontFamily: NUM_FONT,
                          interval: Math.max(0, Math.ceil(dayNames.length / 16) - 1) } },
    yAxis: { type: "value", scale: true, ...axisStyle,
             axisLabel: { color: T.axisLabel, fontFamily: NUM_FONT,
                          formatter: (v) => v >= 1e8 ? (v / 1e8) + "亿" : v >= 1e4 ? (v / 1e4) + "w" : v } },
    series: [
      { name: "伤害", type: "line", symbol: "circle", symbolSize: 4,
        lineStyle: { color: T.gold, width: 1.5 }, itemStyle: { color: T.gold },
        data: r.days.map((d) => d.damage),
        label: labelDmg ? { show: true, position: "top", color: T.ink2, fontSize: 10,
                            fontFamily: NUM_FONT, formatter: (p) => fmtYi(p.value) } : { show: false } },
    ],
  });

  // —— 各王主力：每王按出刀次数 top3（贴纸卡）
  $("#stats-boss-tops").innerHTML = r.bosses.map((b) => `
    <div class="boss-top-card">
      <img src="/static/boss_icon/${b.icon_id}.webp" onerror="this.style.visibility='hidden'" alt="">
      <div class="bt-name">${esc(b.name || b.boss + "王")}</div>
      <div class="bt-sub">刀数</div>
      ${(b.top || []).map((t, i) => `
        <div class="bt-row${i === 0 ? " first" : ""}">
          <span class="bt-medal">${["👑", "🥈", "🥉"][i] || ""}</span>
          <span class="bt-member" title="${esc(t.name)}">${esc(t.name)}</span>
          <span class="bt-count">${t.count}刀</span></div>`).join("")
        || '<div class="bt-empty">无人出刀</div>'}
      <div class="bt-sub">刀均</div>
      ${(b.top_avg || []).map((t, i) => `
        <div class="bt-row avg${i === 0 ? " first" : ""}">
          <span class="bt-medal">${["👑", "🥈", "🥉"][i] || ""}</span>
          <span class="bt-member" title="${esc(t.name)}">${esc(t.name)}</span>
          <span class="bt-count">${fmtYi(t.avg)}<small>·${t.count}刀</small></span></div>`).join("")
        || '<div class="bt-empty">暂无完整刀</div>'}
    </div>`).join("");

  // —— 出刀时刻分布（后端 hours 下标=本地时钟小时，横轴与之 1:1 对应，勿再做 5 点偏移）
  const hourLabels = Array.from({ length: 24 }, (_, i) => `${i}时`);
  mk("chart-hours").setOption({
    ...anim,
    tooltip: { trigger: "axis", ...tipStyle,
               formatter: (ps) => `${hourLabels[ps[0].dataIndex]}：${ps[0].value} 刀` },
    grid: { top: 24, left: 42, right: 24, bottom: 24 },
    xAxis: { ...axisStyle, type: "category", data: hourLabels,
             axisLabel: { color: T.axisLabel, fontFamily: NUM_FONT, interval: 0, fontSize: 10 } },
    yAxis: { type: "value", ...axisStyle },
    series: [{ name: "刀数", type: "bar", barWidth: "55%",
               data: r.hours || [], itemStyle: { color: T.accent, borderRadius: [3, 3, 0, 0] } }],
  });

  // —— 下班榜：每天出满 3 刀的完成时刻（最速按平均最早，最慢按平均最晚）
  const fmtClock = (sec) => {
    const local = (5 * 3600 + Math.round(sec)) % 86400;
    return `${String(Math.floor(local / 3600)).padStart(2, "0")}:${String(Math.floor(local % 3600 / 60)).padStart(2, "0")}`;
  };
  const renderEndRank = (sel, rows, key, label) => {
    $(sel).innerHTML = (rows || []).length ? `<table class="rank-table">
      <tr><th></th><th>成员</th><th>平均完成时刻</th><th>${label}</th></tr>
      ${rows.slice(0, 10).map((m, i) => `
        <tr class="${i < 3 ? "r" + (i + 1) : ""}">
          <td class="rk">${i + 1}</td>
          <td class="rk-name">${esc(m.name)}</td>
          <td class="rk-num">${fmtClock(m.avg)}</td>
          <td class="rk-num">${fmtClock(m[key])}</td>
        </tr>`).join("")}</table>`
      : '<div class="muted" style="padding:6px">暂无数据（还没人出满 3 刀）</div>';
  };
  renderEndRank("#stats-fastest", r.fastest, "best", "最快一次");
  renderEndRank("#stats-slowest", r.slowest, "worst", "最晚一次");

  // —— 成员贡献榜（可按刀数/总伤害排序，renderRanking 用缓存重渲染）
  lastStatsResult = r;
  renderRanking();
}

/* ---------------- 设置 ---------------- */
function updateFetchHint(bf) {
  const el = $("#st-fetch-hint");
  if (el && bf && bf.time) el.textContent = `上次拉取：${bf.time}（${String(bf.msg).split("\n").join("；")}）`;
}

function bossPresetDialog(data) {
  // 站点管理·boss 预置编辑：按服编辑 5 个槽位（系统预置 unitId+头像）与 4 阶段血量（万）
  const names = data.names || {};
  const servers = ["cn", "jp", "tw"].filter((s) => data.boss_id[s]);
  let server = servers.includes(GROUP.game_server) ? GROUP.game_server : (servers[0] || "cn");
  let slots, hp, lbc, picking = -1;  // picking=正在更换预置的槽位下标

  const load = () => {
    slots = (data.boss_id[server] || []).slice(0, 5);
    while (slots.length < 5) slots.push("0");
    hp = (data.boss[server] || []).slice(0, 4)
      .map((row) => row.slice(0, 5).map((h) => +(h / 10000).toFixed(2)));
    while (hp.length < 4) hp.push([0, 0, 0, 0, 0]);
    lbc = data.level_by_cycle[server] || [[-2, 0], [1, 6], [7, 22], [23, 999]];
    picking = -1;
  };

  const render = () => {
    let h = `<label style="display:inline-block">服务器 <select id="bp-server">${servers.map((s) =>
      `<option value="${s}" ${s === server ? "selected" : ""}>${s.toUpperCase()}</option>`).join("")}</select></label>`;
    h += `<div class="bp-slots">${slots.map((uid, i) => {
      const nm = (names[String(i + 1)] || {})[uid];
      if (picking === i) {
        // 同名合并：不同期拉取会积累同 boss 的多个 unitId（同名字同头像），按名字去重展示；
        // 组内代表=当前档 unitId（在组内则选中不换头像文件），否则最小 unitId。
        // boss_id_name 本身不删（历史档案按旧 unitId 回溯名字靠它）。
        const groups = new Map();
        for (const [u, n] of Object.entries(names[String(i + 1)] || {})) {
          (groups.get(n) || groups.set(n, []).get(n)).push(u);
        }
        const dedup = [...groups.entries()].map(([nm, uids]) => ({
          nm, uids: uids.sort((a, b) => Number(a) - Number(b)),
        })).sort((a, b) => Number(a.uids[0]) - Number(b.uids[0]));
        const list = dedup.map(({ nm, uids }) => {
          const isCur = uids.includes(slots[i]);
          const uid = isCur ? slots[i] : uids[0];
          return `<button type="button" class="bp-opt${isCur ? " cur" : ""}" data-i="${i}" data-uid="${uid}">
            <img src="/static/boss_icon/${uid}.webp" onerror="this.style.visibility='hidden'" alt="">${esc(nm)}</button>`;
        }).join("");
        return `<div class="bp-slot"><div class="bp-name">${i + 1}王
          <button type="button" class="bp-change" data-cancel="1">收起</button></div>
          <div class="bp-opts">${list || '<span class="muted">无预置</span>'}</div></div>`;
      }
      return `<div class="bp-slot">
        <img class="bp-ava" src="/static/boss_icon/${uid}.webp" onerror="this.style.visibility='hidden'" alt="">
        <div class="bp-name">${nm ? esc(nm) : "未设置"}</div>
        <button type="button" class="bp-change" data-i="${i}">更换</button></div>`;
    }).join("")}</div>`;
    h += `<table class="bp-grid"><tr><th>阶段</th>${slots.map((_, i) => `<th>${i + 1}王</th>`).join("")}</tr>${
      hp.map((row, p) => `<tr><td>${lbc[p] ? `${lbc[p][0]} 周目` : `阶段${p}`}</td>${
        row.map((v, b) => `<td><input type="number" class="bp-hp" data-p="${p}" data-b="${b}" value="${v}" min="0" step="any"></td>`).join("")}</tr>`).join("")}</table>`;
    const body = $("#bp-body");
    body.innerHTML = h;
    body.querySelector("#bp-server").onchange = (e) => { server = e.target.value; load(); render(); };
    body.querySelectorAll(".bp-change").forEach((b) => {
      b.onclick = () => {
        if (b.dataset.cancel) { picking = -1; render(); }
        else { picking = Number(b.dataset.i); render(); }
      };
    });
    body.querySelectorAll(".bp-opt").forEach((b) => {
      b.onclick = () => { slots[Number(b.dataset.i)] = b.dataset.uid; picking = -1; render(); };
    });
    body.querySelectorAll(".bp-hp").forEach((inp) => {
      inp.oninput = () => { hp[+inp.dataset.p][+inp.dataset.b] = Number(inp.value) || 0; };  // 实时写回：重绘（换预置/切服）不丢未保存输入
    });
  };

  ask("编辑 boss 预置（血量单位：万）", `<div id="bp-body"></div>`, async (d) => {
    const grid = [[0, 0, 0, 0, 0], [0, 0, 0, 0, 0], [0, 0, 0, 0, 0], [0, 0, 0, 0, 0]];
    d.querySelectorAll(".bp-hp").forEach((inp) => {
      grid[Number(inp.dataset.p)][Number(inp.dataset.b)] = Math.round((Number(inp.value) || 0) * 10000);
    });
    const res = await fetch("/admin/api/", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "set_boss_preset", csrf: CSRF, server,
                             boss_id: slots, boss: grid, level_by_cycle: lbc }) });
    const r = await res.json();
    if (r.code !== 0) throw new Error(r.message);
    toast(r.message);
    if (server === GROUP.game_server) {
      const d = await api("get_data");       // 重拉 bossData：旧快照里的 full_health 是按旧预置算的
      BOSSES = d.bossData;
      renderPanel();                         // 预置即时生效：当前面板满血重算
    }
  });
  load();
  render();
}

let stArchives = null;
function renderArchives(ar) {
  // 档案格子墙（GitHub 动态式）：每格=一个档案，颜色按记录数分位淡粉→深粉；点格子二次确认切换
  stArchives = ar;
  // get_archives 只列有记录的档案（GROUP BY bid）：当前档案若被清空则补一个空格，保证"当前"始终可见
  const counts = [...ar.counts];
  if (!counts.some((c) => c.battle_id === ar.battle_id)) {
    counts.push({ battle_id: ar.battle_id, record_count: 0 });
    counts.sort((a, b) => a.battle_id - b.battle_id);
  }
  const nz = counts.filter((c) => c.record_count > 0).map((c) => c.record_count).sort((a, b) => a - b);
  const lvlOf = (n) => {
    if (n === 0) return 0;
    if (nz.length < 4) return Math.max(1, Math.ceil(n / Math.max(...nz, 1) * 4));
    const q = (p) => nz[Math.min(nz.length - 1, Math.floor(nz.length * p))];
    return n <= q(0.25) ? 1 : n <= q(0.5) ? 2 : n <= q(0.75) ? 3 : 4;
  };
  $("#st-archives").innerHTML = counts.map((c) => {
    const cur = c.battle_id === ar.battle_id;
    return `<button class="ar-cell" data-l="${lvlOf(c.record_count)}"${cur ? " data-cur" : ""} data-bid="${c.battle_id}" data-n="${c.record_count}"
      title="#${c.battle_id} · ${c.record_count} 条记录${cur ? " · 当前档案" : ""}">${c.battle_id}</button>`;
  }).join("");
  $("#st-ar-title").textContent = `档案管理（当前 #${ar.battle_id}）`;
  $("#st-archives").querySelectorAll(".ar-cell").forEach((cell) => {
    cell.onclick = async () => {
      const bid = Number(cell.dataset.bid);
      if (bid === stArchives.battle_id) { toast("已是当前档案"); return; }
      if (!confirm(`切换到 #${bid} 档案？该档案现有 ${cell.dataset.n} 条记录。`)) return;
      try {
        toast((await api("switch_data_slot", { battle_id: bid })).message);
        await loadData();
        renderArchives(await api("get_archives"));  // 高亮与标题跟随新当前档案
      } catch (e) { toast(e.message); }
    };
  });
}

async function loadSetting() {
  const el = $("#tab-setting");
  if (!GROUP || GROUP.group_id === undefined) { el.innerHTML = ""; return; }
  let html = "";
  if (SELF.is_admin) {
    const s = await api("get_setting");
    // 同步开关合并为一个（2026-11-02 用户定）：全位=报刀/撤销/申请/取消挂树/预约/取消预约/改状态/SL
    const SYNC_MASK = 0x3cf;
    const syncChecked = (s.notification & SYNC_MASK) ? "checked" : "";
    html += `<div class="section-title">公会设置</div><div class="card">
      <div class="bar"><label>服务器<select id="st-server">
        ${["cn","jp","tw"].map((x) => `<option value="${x}" ${s.groupData.game_server === x ? "selected" : ""}>${x.toUpperCase()}</option>`).join("")}
      </select></label>
      <label><input type="checkbox" id="st-privacy" ${s.privacy & 0x1 ? "checked" : ""}> 允许游客查看出刀表</label>
      <label title="网页端报刀/撤销/申请/取消/挂树/预约/改状态/SL 操作同步到群聊通知"><input type="checkbox" id="st-sync" ${syncChecked}> 网页操作与群聊通知同步</label></div>
      <button class="primary" id="st-save">保存公会设置</button></div>`;
    html += `<div class="section-title" id="st-ar-title">档案管理（当前 #${s.groupData.battle_id}）</div>
      <div class="card"><div id="st-archives" class="ar-grid">加载中…</div>
      <div class="bar"><button class="danger" id="st-clear-cur">清空当前档案</button>
      <button id="st-add-arc">+ 新建档案</button>
      <span class="muted" style="align-self:center">颜色越深=记录越多 · 点击格子切换档案</span></div></div>`;
    if (SELF.is_super) {
      html += `<div class="section-title">权限（主人）</div><div class="card">
        <div class="bar" style="flex-wrap:nowrap">
        <div style="flex:1;min-width:170px">${pickerHTML("p-role-m")}</div>
        <select id="st-role-v"><option value="10">公会战管理员</option><option value="100">成员</option></select>
        <button id="st-role-set">设置</button></div></div>`;
    }
  } else {
    html += `<div class="card muted">公会设置仅公会战管理员可见。</div>`;
  }
  html += `<div class="section-title">账号</div><div class="card">
    <div class="bar"><button id="st-passwd">修改密码</button>
    <span class="muted" style="align-self:center">当前身份：${esc(SELF.nickname)}（${SELF.user_id}）</span></div></div>`;
  if (SELF.is_super) {
    html += `<div class="section-title">站点管理（主人）</div><div class="card">
      <div class="bar"><button id="st-fetchboss">拉取当期 boss 数据</button>
      <span class="muted" id="st-fetch-hint" style="align-self:center"></span></div>
      <div class="bar"><button id="st-bossedit">编辑 boss 预置</button>
      <button id="st-global">全局设置</button></div></div>`;
  }
  el.innerHTML = html;
  bindPickerPickers(el);  // 权限区的页面内嵌 picker：弹框路径外唯一需手动绑定的位置
  if (SELF.is_admin) {
    $("#st-save").onclick = async () => {
      const changed = $("#st-server").value !== s.groupData.game_server;
      const r = await api("put_setting", {
        game_server: $("#st-server").value,
        notification: $("#st-sync").checked ? 0x3cf : 0,
        privacy: (s.privacy & 0x2) | ($("#st-privacy").checked ? 0x1 : 0),
      });
      toast(r.message);
      if (changed) {  // 切服会新建档案：整页数据与设置区都要刷新
        await loadData();
        await loadSetting();
      }
    };
    renderArchives(await api("get_archives"));
    $("#st-clear-cur").onclick = async () => {
      if (!stArchives || !confirm(`确认清空 #${stArchives.battle_id} 号档案的全部出刀记录？`)) return;
      try {
        toast((await api("clear_data_slot", { battle_id: stArchives.battle_id })).message);
        renderArchives(await api("get_archives"));
      } catch (e) { toast(e.message); }
    };
    $("#st-add-arc").onclick = async () => {
      // 新档案号=现有最大+1；switch_archive 对不存在的 bid 即新建空档（yobot 语义）
      const next = Math.max(stArchives.battle_id, ...stArchives.counts.map((c) => c.battle_id)) + 1;
      if (!confirm(`新建并切换到 #${next} 档案？\n新档案为空（周目 1、满血），当前档案数据会保留，可随时切回。`)) return;
      try {
        toast((await api("switch_data_slot", { battle_id: next })).message);
        await loadData();
        renderArchives(await api("get_archives"));
      } catch (e) { toast(e.message); }
    };
  }
  if (SELF.is_super) {
    $("#st-role-set").onclick = async () => {
      try { toast((await api("grant_role", { qqid: pickerQQ("p-role-m"), role: Number($("#st-role-v").value) })).message); }
      catch (e) { toast(e.message); }
    };
  }
  $("#st-passwd").onclick = () => {
    ask("修改密码", `<label>新密码（≥6位）<input type="password" id="f-p1"></label>
      <label>再输一次<input type="password" id="f-p2"></label>`, async (dlg) => {
      const p1 = dlg.querySelector("#f-p1").value, p2 = dlg.querySelector("#f-p2").value;
      if (p1 !== p2) throw new Error("两次输入不一致");
      toast((await api("change_password", { pwd: p1 })).message);
    });
  };
  if (SELF.is_super) {
    // 拉取时间小字：每日定时任务/手动按钮都会把最近拉取写进 config boss_fetch
    fetch("/admin/api/", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "get_global_setting", csrf: CSRF }) })
      .then((r) => r.json()).then((r) => { if (r.code === 0) updateFetchHint(r.setting && r.setting.boss_fetch); })
      .catch(() => {});
    $("#st-fetchboss").onclick = async () => {
      const res = await fetch("/admin/api/", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "fetch_boss_data", csrf: CSRF }) });
      const r = await res.json();
      toast(r.message || (r.code === 0 ? "完成" : r.message));
      updateFetchHint(r.boss_fetch);
    };
    $("#st-bossedit").onclick = async () => {
      try {
        const res = await fetch("/admin/api/", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "get_boss_preset", csrf: CSRF }) });
        const r = await res.json();
        if (r.code !== 0) { toast(r.message); return; }
        bossPresetDialog(r);
      } catch (e) { toast(e.message); }
    };
    $("#st-global").onclick = async () => {
      const res = await fetch("/admin/api/", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "get_global_setting", csrf: CSRF }) });
      const r = await res.json();
      if (r.code !== 0) { toast(r.message); return; }
      const keys = Object.keys(r.setting);
      ask("全局设置（config.json）", keys.map((k) =>
        `<label>${esc(k)}${typeof r.setting[k] === "object" ? `<input type="text" data-k="${k}" value='${esc(JSON.stringify(r.setting[k]))}'>` : `<input type="text" data-k="${k}" value="${esc(r.setting[k])}">`}</label>`).join(""),
        async (dlg) => {
          const setting = {};
          dlg.querySelectorAll("input[data-k]").forEach((x) => {
            try { setting[x.dataset.k] = JSON.parse(x.value); } catch { setting[x.dataset.k] = x.value; }
          });
          const res2 = await fetch("/admin/api/", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "put_global_setting", csrf: CSRF, setting }) });
          const r2 = await res2.json();
          if (r2.code !== 0) throw new Error(r2.message);
          toast("已保存");
        });
    };
  }
}

/* ---------------- 选项卡与启动 ---------------- */
function moveTabInk() {
  // 顶栏激活下划线滑块：跟随当前 tab（适配换行后的第二行）
  const ink = $("#tab-ink");
  if (!ink) return;
  const act = document.querySelector("#topbar .tab.active");
  if (!act) { ink.style.width = "0px"; return; }
  ink.style.left = act.offsetLeft + "px";
  ink.style.top = (act.offsetTop + act.offsetHeight - 2) + "px";
  ink.style.width = act.offsetWidth + "px";
}

// 墨线自愈：字体度量/缩放/滚动条等偶发因素可能让某次测量瞬时失准（用户端偶现难复现），
// 双保险=观察激活页签几何变化即刻重测 + 每秒校准一次兜底
let inkTabRO = null;
function watchActiveTab() {
  const act = document.querySelector("#topbar .tab.active");
  if (!act) return;
  if (!inkTabRO) inkTabRO = new ResizeObserver(moveTabInk);
  inkTabRO.observe(act);  // 重复 observe 同元素无害
}
setInterval(() => {
  const ink = $("#tab-ink"), act = document.querySelector("#topbar .tab.active");
  if (!ink || !act || !ink.style.width) return;
  const ir = ink.getBoundingClientRect(), ar = act.getBoundingClientRect();
  if (Math.abs(ir.left - ar.left) > 2 || Math.abs(ir.width - ar.width) > 2
      || Math.abs(ir.top - (ar.bottom - 2)) > 4) {
    moveTabInk();
  }
}, 1000);

function switchTab(name) {
  if (GUEST && (name === "stats" || name === "setting")) {
    toast("游客模式仅可查看面板与记录");
    return;
  }
  if (!GID && (name === "records" || name === "setting" || name === "stats")) {
    toast("请先从面板页选择/创建公会");
    return;
  }
  for (const t of document.querySelectorAll("#topbar .tab")) {
    t.classList.toggle("active", t.dataset.tab === name);
  }
  moveTabInk();
  watchActiveTab();  // 激活页签换元素了，几何观察跟上
  for (const sec of ["panel", "records", "stats", "setting", "help"]) {
    $(`#tab-${sec}`).hidden = sec !== name;
  }
  const sec = $(`#tab-${name}`);
  sec.classList.remove("enter"); void sec.offsetWidth; sec.classList.add("enter");
  curTab = name;
  if (name === "records") {
    const p = (recMode === "day" && !$("#rec-date").value)
      ? loadLatestDay()   // 首次进入：默认落到最新有数据的一天
      : loadRecords();
    p.catch((e) => toast(e.message));
  }
  if (name === "stats") loadStats().catch((e) => toast(e.message));
  if (name === "setting") loadSetting().catch((e) => toast(e.message));
  if (name === "help") {
    // 首次打开才加载外部文档，避免每次进面板都请求
    const f = $("#help-frame");
    if (f && !f.src) f.src = f.dataset.src;
  }
}

function startSSE() {
  if (es) es.close();
  es = new EventSource(`/clan/${GID}/events`);
  es.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    if (data.bossData) {
      // 内容与已渲染快照完全一致时跳过重绘（SSE 订阅时会立刻推一份初始快照，
      // 若照常重绘会打断首屏血条 0→实际值 的生长动画）
      const changed = !_lastBossSnap ||
        JSON.stringify(_lastBossSnap) !== JSON.stringify(data.bossData);
      const flash = new Set();
      if (changed && _lastBossSnap) {
        for (const n of [1, 2, 3, 4, 5]) {
          const a = _lastBossSnap[n], b = data.bossData[n];
          if (a && b && (a.health !== b.health ||
              JSON.stringify(a.challengers) !== JSON.stringify(b.challengers))) {
            flash.add(n);
          }
        }
      }
      _lastBossSnap = data.bossData;
      BOSSES = data.bossData;
      if (data.base_cycle) GROUP.cycle = data.base_cycle;
      if (changed) renderPanel(flash);
      refreshSelfState();
    }
    if (data.notice) toast(data.notice);
  };
  es.onerror = () => { es.close(); setTimeout(startSSE, 3000); };
}

// ---------------- 成员选择器（头像+昵称下拉） ----------------
function pickerHTML(id, opts = {}) {
  const me = SELF.user_id;
  const list = MEMBERS.filter(m => !opts.filter || opts.filter(m))
    .sort((a, b) => (a.qqid === me ? -1 : b.qqid === me ? 1 : a.qqid - b.qqid));
  const cur = list.find(m => m.qqid === (opts.value ?? me)) || list[0];
  const care = '<svg class="picker-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
  const optsHtml = list.map((m) => `
    <button type="button" class="picker-opt${m.qqid === (cur && cur.qqid) ? " cur" : ""}" data-qq="${m.qqid}">
      <img class="cava" loading="lazy" src="${avatarUrl(m.qqid)}" onerror="this.style.visibility='hidden'" alt="">
      <span class="picker-name">${esc(m.nickname || m.qqid)}</span>
      ${opts.mark && opts.mark(m) ? `<span class="picker-mark">${opts.mark(m)}</span>` : ""}
    </button>`).join("");
  return `<div class="picker" id="${id}" data-qq="${cur ? cur.qqid : 0}">
    <button type="button" class="picker-field">
      <img class="cava" loading="lazy" src="${avatarUrl(cur ? cur.qqid : 0)}" onerror="this.style.visibility='hidden'" alt="">
      <span class="picker-name">${cur ? esc(cur.nickname || cur.qqid) : "无成员"}</span>${care}
    </button>
    <div class="picker-menu">${optsHtml}</div>
  </div>`;
}

function closePickers() {
  document.querySelectorAll(".picker.open").forEach((p) => p.classList.remove("open"));
}
function pickerQQ(id) {
  const p = document.getElementById(id);
  return p ? Number(p.dataset.qq) : 0;
}
function bindPickerPickers(scope = document) {
  scope.querySelectorAll(".picker:not([data-bound])").forEach((pk) => {
    pk.dataset.bound = "1";
    pk.querySelector(".picker-field").onclick = (e) => {
      e.stopPropagation();
      const was = pk.classList.contains("open");
      closePickers();
      if (!was) pk.classList.add("open");
    };
    pk.querySelectorAll(".picker-opt").forEach((opt) => {
      opt.onclick = (e) => {
        e.stopPropagation();
        pk.dataset.qq = opt.dataset.qq;
        const m = MEMBERS.find(x => x.qqid === Number(opt.dataset.qq));
        pk.querySelector(".picker-field .picker-name").textContent = m ? (m.nickname || m.qqid) : opt.dataset.qq;
        const img = pk.querySelector(".picker-field .cava");
        img.style.visibility = ""; img.src = avatarUrl(Number(opt.dataset.qq));
        pk.querySelectorAll(".picker-opt").forEach(o => o.classList.toggle("cur", o === opt));
        closePickers();
      };
    });
  });
}

async function initGuildSwitcher() {
  // 多公会用户：群名旁显示下拉切换；单公会不显示
  try {
    const res = await fetch("/api/my_guilds/");
    const r = await res.json();
    if (r.code !== 0 || !Array.isArray(r.guilds) || r.guilds.length < 2) return;
    const btn = $("#guild-switch"), menu = $("#guild-menu");
    menu.innerHTML = r.guilds.map((g) => `
      <button class="gm-item ${g.group_id === GID ? "cur" : ""}" data-gid="${g.group_id}">
        <span>${esc(g.group_name || g.group_id)}</span>
        <span class="gid">#${g.group_id}</span>
      </button>`).join("");
    btn.hidden = false;
    moveTabInk();  // 切换箭头出现会加宽品牌区
    const close = () => { menu.classList.remove("open"); btn.setAttribute("aria-expanded", "false"); };
    btn.onclick = (e) => {
      e.stopPropagation();
      const open = menu.classList.toggle("open");
      btn.setAttribute("aria-expanded", String(open));
    };
    menu.onclick = (e) => {
      const item = e.target.closest(".gm-item");
      if (!item) return;
      const gid = Number(item.dataset.gid);
      close();
      if (gid !== GID) location.href = `/clan/${gid}/`;
    };
    document.addEventListener("click", (e) => {
      if (menu.classList.contains("open") && !menu.contains(e.target) && !btn.contains(e.target)) close();
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  } catch { /* 切换器非关键功能，失败静默 */ }
}

async function loadData() {
  const data = await api("get_data");
  GROUP = data.groupData;
  GROUP.today_counts = data.today_counts || {};
  BOSSES = data.bossData;
  MEMBERS = data.members;
  SELF = data.selfData;
  CSRF = SELF.csrf;
  _lastBossSnap = null;
  document.title = (GROUP.group_name || GROUP.group_id) + " - 公会战";
  renderPanel();
  _lastBossSnap = BOSSES;   // 登记：首条 SSE init 快照与此一致时跳过重绘
  initGuildSwitcher();
  if (SELF.is_admin) {
    $("#rk-zero-page").hidden = false;
    $("#rk-mid-page").hidden = false;
  }
}

async function boot() {
  const tt = $("#theme-toggle");
  if (tt) {
    tt.textContent = document.documentElement.dataset.theme === "dark" ? "☀️" : "🌙";
    tt.onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  }
  document.querySelectorAll("#topbar .tab").forEach((t) => {
    t.onclick = (e) => { e.preventDefault(); switchTab(t.dataset.tab); };
  });
  moveTabInk();
  watchActiveTab();
  window.addEventListener("resize", moveTabInk);
  // 顶栏自身尺寸变化（换行、行高变化）时重测墨线；字体晚到（Bahnschrift 等）换行盒也重测
  if (window.ResizeObserver) new ResizeObserver(moveTabInk).observe($("#topbar"));
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(moveTabInk);
  document.addEventListener("click", closePickers);
  $("#rec-back").onclick = backToDay;
  $("#rec-date").onchange = () => loadRecords().catch((e) => toast(e.message));    // 选完日期即刷新
  $("#rec-refresh").onclick = () => loadRecords().catch((e) => toast(e.message));
  $("#rec-view").onclick = toggleRecView;
  $("#stats-refresh").onclick = () => loadStats().catch((e) => toast(e.message));
  $("#stats-bid").onchange = () => loadStats().catch((e) => toast(e.message));
  $("#rk-zero-page").onclick = () => remindDialog("zero");
  $("#rk-mid-page").onclick = () => remindDialog("mid");
  // 先取身份拿 CSRF（读操作免 CSRF，写操作需要）
  const meRes = await fetch("/api/whoami/" + (GID ? `?gid=${GID}` : ""));
  const me = await meRes.json();
  if (me.code !== 0) { location.href = "/login/?callback=" + location.pathname; return; }
  CSRF = me.csrf; SELF = me;
  GUEST = !!me.guest;
  if (GUEST) {
    // 游客只读：隐藏写操作入口与仅成员可见的页签，顶栏改为登录
    document.body.classList.add("guest");
    document.querySelectorAll("#topbar .tab").forEach((t) => {
      if (t.dataset.tab === "stats" || t.dataset.tab === "setting") t.hidden = true;
    });
    $("#rec-hint").hidden = true;
    const lg = $("#logout");
    if (lg) { lg.textContent = "登录"; lg.href = "/login/?callback=" + location.pathname; }
    moveTabInk();
  }

  if (!GID) {
    // 公会选择页
    const res = await fetch("/api/my_guilds/");
    const r = await res.json();
    if (r.code !== 0) { location.href = "/login/"; return; }
    $("#guild-name").textContent = "选择公会";
    $("#tab-panel").innerHTML = `<div class="section-title">我的公会</div>
      <div class="guild-grid">${r.guilds.map((g) =>
        `<a class="guild-card" href="/clan/${g.group_id}/"><div class="gname">${esc(g.group_name)}</div><div class="gid">#${g.group_id}</div></a>`).join("")
        || '<span class="muted">尚未加入任何公会</span>'}</div>`;
    return;
  }

  try {
    await loadData();
  } catch (e) {
    if (/初始化|not exists/i.test(e.message)) {
      $("#guild-name").textContent = GID;
      $("#tab-panel").innerHTML = `<div class="card"><h3 style="margin-top:0">本群尚未初始化公会</h3>
        <div class="bar"><select id="cg-server"><option value="cn">国服</option><option value="jp">日服</option><option value="tw">台服</option></select>
        <button class="primary" id="cg-go">创建公会</button></div></div>`;
      if (me.is_super || me.is_admin) {
        $("#cg-go").onclick = async () => {
          const r = await api("create_guild", { game_server: $("#cg-server").value });
          toast(r.message);
          location.reload();
        };
      } else {
        $("#cg-go").style.display = "none";
      }
    } else {
      $("#tab-panel").innerHTML = `<div class="card"><h3 style="margin-top:0">加载失败</h3>
        <p class="muted">${esc(e.message)}</p>
        <button class="primary" onclick="location.reload()">重试</button></div>`;
    }
    return;
  }
  startSSE();
  if (SELF.must_change_password) {
    ask("首次登录，请设置新密码", `<label>新密码（≥6位）<input type="password" id="f-p1"></label>`,
      async (dlg) => {
        const p1 = dlg.querySelector("#f-p1").value;
        if (p1.length < 6) throw new Error("至少 6 位");
        await api("change_password", { pwd: p1 });
        SELF.must_change_password = false;
      });
  }
}

boot().catch((e) => toast(e.message));
