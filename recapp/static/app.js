// 推荐反馈样本与评估应用 —— 前端逻辑（原生 JS，无构建步骤）
const $ = (s) => document.querySelector(s);
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtTs = (ts) => ts ? new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "-";
const badge = (t) => `<span class="badge ${esc(t)}">${esc(t)}</span>`;
const kv = (obj) => "<dl class='kv'>" + Object.entries(obj)
  .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</dd>`)
  .join("") + "</dl>";

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast-item" + (isErr ? " err" : "");
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}
const run = async (fn) => {
  try { await fn(); } catch (e) { toast(e.message, true); }
};

// ---------- 页签 ----------
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    refreshAll();
  });
});

// ---------- 总览 ----------
async function loadOverview() {
  const o = await api("/api/overview");
  const stats = [
    ["授权版本", o.consent_version], ["撤回用户", o.withdrawn_users],
    ["数据集", o.dataset_count], ["评估", o.evaluation_count],
  ];
  Object.entries(o.event_counts).forEach(([k, v]) => stats.push([`事件:${k}`, v]));
  Object.entries(o.sample_counts).forEach(([k, v]) => stats.push([`样本:${k}`, v]));
  $("#overview").innerHTML = stats.map(([k, v]) =>
    `<span class="stat">${esc(k)}<b>${esc(v)}</b></span>`).join("");
}

// ---------- 样本来源 ----------
async function loadSamples() {
  const rows = await api("/api/samples?limit=60");
  $("#sample-list").innerHTML = rows.map((r) => `
    <div class="card" data-sid="${esc(r.sample_id)}" style="cursor:pointer">
      <h4>${esc(r.sample_id)} <span style="color:var(--muted)">v${r.version}</span>
          ${badge(r.label_status)} ${r.tombstoned ? badge("tombstoned") : ""}</h4>
      ${kv({
        user: r.user_id, item: r.item_id,
        anchor: fmtTs(r.anchor_ts),
        label: `${r.label}（${r.label_reason ?? "-"}）`,
        feature_cutoff: fmtTs(r.feature_cutoff_ts),
        防泄漏校验: r.feature_cutoff_ts === r.anchor_ts ? "通过 ✓" : "失败 ✗",
      })}
    </div>`).join("");
  document.querySelectorAll("#sample-list .card").forEach((c) =>
    c.addEventListener("click", () => {
      $("#sample-id-input").value = c.dataset.sid;
      loadLineage(c.dataset.sid);
    }));
}

async function loadLineage(sid) {
  const d = await api(`/api/sample?sample_id=${encodeURIComponent(sid)}`);
  $("#sample-detail").innerHTML = `
    <h3>样本 ${esc(d.sample_id)} 的来源与版本谱系</h3>
    <h4 style="color:var(--muted)">① 原始事件链（来源）</h4>
    <table><tr><th>event_id</th><th>类型</th><th>时间</th><th>位置</th></tr>
      ${d.source_events.map((e) => `<tr><td class="mono">${esc(e.event_id)}</td>
        <td>${badge(e.event_type)}</td><td>${esc(e.event_time)}</td>
        <td>${esc(e.position ?? "-")}</td></tr>`).join("")}</table>
    <h4 style="color:var(--muted)">② 样本版本（不可变，变化即新版本）</h4>
    <table><tr><th>版本</th><th>标签</th><th>状态</th><th>原因</th>
      <th>特征截止=锚点</th><th>墓碑</th></tr>
      ${d.versions.map((v) => `<tr><td>v${v.version}</td><td>${v.label}</td>
        <td>${badge(v.label_status)}</td><td>${esc(v.label_reason ?? "-")}</td>
        <td>${v.feature_cutoff_ts === v.anchor_ts ? "✓" : "✗"}</td>
        <td>${v.tombstoned ? "是" : "否"}</td></tr>`).join("")}</table>
    <h4 style="color:var(--muted)">③ 标签翻转历史（含触发事件）</h4>
    <table><tr><th>版本</th><th>旧→新</th><th>原因</th><th>触发事件</th></tr>
      ${d.label_history.map((h) => `<tr><td>v${h.version}</td>
        <td>${esc(h.old_label)} → ${h.new_label} (${esc(h.old_status ?? "-")} → ${esc(h.new_status)})</td>
        <td>${esc(h.reason)}</td>
        <td class="mono">${esc(h.trigger_event_ids_json)}</td></tr>`).join("")}</table>
    <h4 style="color:var(--muted)">④ 最新特征快照（锚点时刻冻结）</h4>
    <pre>${esc(d.versions[d.versions.length - 1].features_json)}</pre>`;
}

// ---------- 窗口 ----------
function toLocalInput(ts) {
  return new Date(ts * 1000).toISOString().slice(0, 16);
}
async function loadDatasets() {
  const ds = await api("/api/datasets");
  $("#dataset-list").innerHTML = ds.length ? ds.map((d) => `
    <div class="card">
      <h4>${esc(d.name)} ${badge(d.strategy)}
        <span style="color:var(--muted)">${esc(d.dataset_id)}</span></h4>
      ${kv({
        窗口: `${fmtTs(d.start_ts)} ~ ${fmtTs(d.end_ts)}`,
        成熟窗: `${d.horizon_days} 天`, 当前版本: d.current_version,
      })}
      <table><tr><th>版本</th><th>样本数</th><th>授权版本</th><th>input_hash</th><th>操作</th></tr>
        ${d.versions.map((v) => `<tr><td>v${v.version}</td><td>${v.n_items}</td>
          <td>v${v.built_consent_version}</td>
          <td class="mono">${esc(v.input_hash)}</td>
          <td><button class="ghost" data-eval='${esc(JSON.stringify({ id: d.dataset_id, v: v.version }))}'>
            评估</button></td></tr>`).join("")}</table>
    </div>`).join("") : "<p class='hint'>还没有窗口</p>";
  document.querySelectorAll("[data-eval]").forEach((b) =>
    b.addEventListener("click", () => run(async () => {
      const { id, v } = JSON.parse(b.dataset.eval);
      const job = await api("/api/jobs/evaluate", {
        method: "POST", body: { dataset_id: id, version: v } });
      toast(`评估任务已入队: ${job.job_id}（固定 ${job.input_hash}）`);
      setTimeout(() => refreshAll(), 1200);
    })));
}

$("#btn-create-pair").addEventListener("click", () => run(async () => {
  const start = Math.floor(new Date($("#win-start").value).getTime() / 1000);
  const end = Math.floor(new Date($("#win-end").value).getTime() / 1000);
  const horizon = Number($("#win-horizon").value);
  const name = $("#win-name").value || `窗口-${Date.now()}`;
  for (const strategy of ["frozen", "rolling"]) {
    await api("/api/windows", {
      method: "POST",
      body: { name: `${name}-${strategy}`, strategy, start_ts: start,
              end_ts: end, horizon_days: horizon },
    });
  }
  toast("已创建同输入的冻结/滚动窗口，可在下方构建版本并比较样本数差异");
  loadDatasets();
}));

$("#btn-refresh-datasets").addEventListener("click", () => run(loadDatasets));

// ---------- 许可 ----------
async function loadConsent() {
  const d = await api("/api/consent");
  $("#consent-list").innerHTML = `
    <div class="card"><h4>用户状态</h4>
      ${d.users.map((u) => `<div style="margin:4px 0">${esc(u.user_id)}
        ${badge(u.status)}</div>`).join("")}</div>
    <div class="card"><h4>授权版本流水（只追加）</h4>
      <table>${d.recent_versions.map((v) => `<tr>
        <td>v${v.version}</td><td>${esc(v.user_id)}</td>
        <td>${v.granted ? badge("granted") : badge("withdrawn")}</td>
        <td>${esc(v.reason)}</td><td>${esc(v.created_at)}</td></tr>`).join("")}
      </table></div>`;
  const caches = await api("/api/caches");
  $("#cache-list").innerHTML = caches.length ? caches.map((c) => `
    <div class="card"><h4 class="mono">${esc(c.cache_key)}
      ${c.invalidated ? badge("invalidated") : badge("active")}</h4>
      ${kv({ 涉及用户: c.user_ids.join(", "), 创建: fmtTs(c.created_ts) })}</div>`).join("")
    : "<p class='hint'>暂无缓存（构建窗口版本后会登记特征缓存）</p>";
}
$("#btn-withdraw").addEventListener("click", () => run(async () => {
  const r = await api("/api/withdraw", { method: "POST", body: {
    user_id: $("#consent-user").value, reason: $("#consent-reason").value } });
  toast(`撤回完成：硬删事件 ${r.events_archived_and_deleted}，墓碑样本版本 ${r.sample_versions_tombstoned}，失效缓存 ${r.cache_entries_invalidated}，中止任务 ${r.jobs_aborted.length}`);
  refreshAll();
}));
$("#btn-restore").addEventListener("click", () => run(async () => {
  const r = await api("/api/restore", { method: "POST", body: {
    user_id: $("#consent-user").value, reason: $("#consent-reason").value } });
  toast(`恢复完成：事件 ${r.restored_events} 条，样本版本 ${r.restored_sample_versions} 个（全程留痕）`);
  refreshAll();
}));
$("#btn-refresh-consent").addEventListener("click", () => run(loadConsent));

// ---------- 任务 ----------
async function loadJobs() {
  const jobs = await api("/api/jobs");
  $("#job-list").innerHTML = jobs.length ? jobs.map((j) => `
    <div class="card"><h4>${esc(j.job_type)} ${badge(j.status)}
      <span style="color:var(--muted)">${esc(j.job_id)}</span></h4>
      ${kv({
        固定授权版本: `v${j.pinned_consent_version}`,
        固定输入哈希: j.input_hash ?? "-",
        参数: j.params_json, 错误: j.error ?? "-",
        结果: j.result_json ? esc(j.result_json).slice(0, 220) : "-",
      })}</div>`).join("") : "<p class='hint'>暂无任务</p>";
}
$("#btn-build-all").addEventListener("click", () => run(async () => {
  const j = await api("/api/jobs/build-all", { method: "POST", body: { horizon_days: 14 } });
  toast(`全量构建任务入队 ${j.job_id}（pinned consent v${j.pinned_consent_version}）`);
  setTimeout(() => refreshAll(), 1500);
}));
$("#btn-backfill").addEventListener("click", () => run(async () => {
  const j = await api("/api/jobs/backfill", { method: "POST", body: { horizon_days: 14 } });
  toast(`回补任务入队 ${j.job_id}（pinned consent v${j.pinned_consent_version}）`);
  setTimeout(() => refreshAll(), 1000);
}));
$("#btn-rebuild").addEventListener("click", () => run(async () => {
  const j = await api("/api/jobs/rebuild", { method: "POST", body: { horizon_days: 14 } });
  toast(`重建任务入队 ${j.job_id}`);
  setTimeout(() => refreshAll(), 1000);
}));
$("#btn-refresh-jobs").addEventListener("click", () => run(loadJobs));

// ---------- 评估 ----------
async function loadEvals() {
  const rows = await api("/api/evaluations");
  $("#eval-list").innerHTML = rows.length ? rows.map((r) => {
    const m = r.metrics;
    const row = (title, x) => `<tr><td>${title}</td>
      <td>${x.precision ?? "-"}</td><td>${x.recall ?? "-"}</td>
      <td>${x.f1 ?? "-"}</td><td>${x.accuracy}</td>
      <td>${JSON.stringify(x.confusion)}</td></tr>`;
    return `
    <div class="card"><h4>${esc(r.eval_id)}</h4>
      ${kv({
        数据集: `${r.dataset_id} v${r.dataset_version}`,
        训练_测试: `${r.n_train} / ${r.n_test}`,
        跳过墓碑: r.skipped_tombstones,
        input_hash: r.input_hash, 授权: r.consent_version,
        代码版本: r.code_version, 特征版本: r.feature_version,
        训练正例率: m.train_positive_rate,
        自适应阈值: m.adaptive_threshold, AUC: m.auc,
      })}
      <table><tr><th>阈值</th><th>precision</th><th>recall</th>
        <th>f1</th><th>accuracy</th><th>混淆(tp/fp/fn/tn)</th></tr>
        ${row("固定 0.5", m["at_0.5"])}
        ${row("自适应", m.at_adaptive)}</table>
      <pre>${esc(JSON.stringify(r.metrics, null, 2))}</pre></div>`;
  }).join("") : "<p class='hint'>暂无评估。先在“切窗口”页构建版本，再点评估。</p>";
}

// ---------- 审计 ----------
async function loadAudit() {
  const d = await api("/api/disposal");
  $("#disposal-log").innerHTML = d.logs.length ? d.logs.map((l) => `
    <div class="card"><h4>${esc(l.action)} ${badge(l.entity_type)}</h4>
      ${kv({ 用户: l.user_id ?? "-", 实体: l.entity_id ?? "-",
        详情: l.detail_json, 授权版本: `v${l.consent_version}`, 时间: fmtTs(l.created_ts) })}</div>`).join("")
    : "<p class='hint'>暂无处置记录</p>";
  $("#archive-list").innerHTML = d.archives.length ? d.archives.map((a) => `
    <div class="card"><h4>${esc(a.entity_type)} ${a.restored ? badge("granted") : badge("withdrawn")}
      <span class="mono">${esc(a.entity_id)}</span></h4>
      ${kv({ 用户: a.user_id, 归档时间: fmtTs(a.archived_ts),
        恢复时间: a.restored_ts ? fmtTs(a.restored_ts) : "未恢复" })}</div>`).join("")
    : "<p class='hint'>暂无归档</p>";
}
$("#btn-refresh-audit").addEventListener("click", () => run(loadAudit));
$("#btn-load-sample").addEventListener("click", () =>
  run(() => loadLineage($("#sample-id-input").value.trim())));
$("#btn-refresh-samples").addEventListener("click", () => run(loadSamples));

// ---------- 全局刷新 ----------
async function refreshAll() {
  await run(loadOverview);
  await Promise.allSettled([
    loadSamples(), loadDatasets(), loadConsent(), loadJobs(),
    loadEvals(), loadAudit(),
  ]);
}
// 初始化默认窗口输入：最近 40 天
(async function init() {
  const now = Math.floor(Date.now() / 1000);
  $("#win-start").value = toLocalInput(now - 40 * 86400);
  $("#win-end").value = toLocalInput(now);
  refreshAll();
  setInterval(() => run(loadOverview), 5000);
})();
