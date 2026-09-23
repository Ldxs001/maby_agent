/*
 * 卡片按钮静态体检（浏览器外）。
 *
 * 项目卡片的按钮是运行时拼串生成的：onclick="fn('id')"。拼接写错一个引号，
 * JS 语法检查看不出来（拼串本身合法），浏览器要到点下去才在 console 报错，
 * 界面上就是「点了没反应」。这里把渲染函数真跑一遍，把每个 onclick 拿回来
 * 单独校验语法——不用起服务，也不碰任何数据。
 */
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const dir = path.dirname(__filename);
const src = [0, 1].map(i => fs.readFileSync(path.join(dir, `_js_${i}.js`), "utf-8")).join("\n;\n");

function fakeEl(id) {
  const node = {
    id: id,
    textContent: "",
    innerHTML: "",
    value: "",
    style: {},
    options: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild() {}, removeChild() {}, querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener() {}, remove() {}, focus() {}, click() {},
    setAttribute() {}, getAttribute: () => null, scrollIntoView() {},
    dataset: {}, children: [],
  };
  node.parentNode = null;
  return node;
}

const els = {};
const document = {
  getElementById: id => (els[id] = els[id] || fakeEl(id)),
  querySelectorAll: () => [],
  querySelector: () => null,
  createElement: () => fakeEl("new"),
  addEventListener() {},
  body: fakeEl("body"),
  documentElement: fakeEl("html"),
};

const sandbox = {
  console,
  document,
  window: {},
  location: { hash: "", href: "http://127.0.0.1/" },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: { userAgent: "node" },
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: fn => setTimeout(fn, 0),
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  alert() {}, confirm: () => false, prompt: () => null,
  URL: { createObjectURL: () => "", revokeObjectURL() {} },
  Blob: function () {}, FileReader: function () {},
  AudioContext: function () {},
  Event: function () {}, CustomEvent: function () {},
  getComputedStyle: () => ({ display: "block" }),
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

vm.createContext(sandbox);

let loadErr = null;
try {
  vm.runInContext(src, sandbox, { filename: "web_ui_inline.js" });
} catch (e) {
  loadErr = e;
}
if (loadErr) {
  console.log("前端脚本加载期报错：" + loadErr.message);
  console.log("（下面仍继续校验——加载期的报错本身也是病情）");
}

// 两个形态的项目都造上：归档的、成稿规划的、逐期即兴的、还没排图的。
// 卡片是分叉渲染的，分支不铺到就验不全。
const samples = [
  {
    id: "20260912-121051", name: "长篇小说拆解", program_name: "夜读", archived: false,
    created: "2026-09-12 12:10:51", style_preset: "warm", note: "",
    progress: { mode: "mapped", mapped: 12, done: 3, planned: 12, next_episode: "4",
                mode_label: "成稿规划", label: "已出 3 期", legacy: false },
    episodes: [{ no: "1", title: "开篇", created: "2026-09-12 13:00:00" }],
  },
  {
    id: "20260911-113022", name: "旧项目", program_name: "旧项目", archived: true,
    created: "2026-09-11 11:30:22", style_preset: "", note: "停更",
    progress: { mode: "episodic", mapped: 0, done: 2, planned: null, next_episode: "3",
                mode_label: "逐期即兴", label: "已出 2 期", legacy: false },
    episodes: [],
  },
  {
    id: "20260912-999999", name: "没排图的", program_name: "没排图的", archived: false,
    created: "2026-09-12 20:00:00", style_preset: "", note: "",
    progress: { mode: "mapped", mapped: 0, done: 0, planned: null, next_episode: "1",
                mode_label: "成稿规划", label: "未排图", legacy: false },
    episodes: [],
  },
  {
    id: "20260901-000000", name: "上古项目", program_name: "上古项目", archived: false,
    created: "2026-09-01 00:00:00", style_preset: "", note: "",
    progress: { mode: "legacy", mapped: 0, done: 0, planned: null, next_episode: "1",
                mode_label: "未选规划方式", label: "待补选", legacy: true },
    episodes: [],
  },
];

// CFG 得给一份最小真身：卡片里有风格标签，取标签要穿到 CFG.params 里找点位。
// 不给它，带风格的项目整张卡片就渲染不出来——而这正是按钮最多的那种卡片。
vm.runInContext(`CFG = {
  params: { script: { script: [
    { key: "script.style_preset", label: "风格预设",
      options: [{ value: "warm", label: "温暖叙述" }] },
    { key: "script.target_minutes", label: "目标时长", type: "float" }
  ] } },
  values: {}, data: {}, errors: [], warnings: [], calibration: []
};`, sandbox);

let rendered = "";
const run = (label, code) => {
  try {
    vm.runInContext(code, sandbox);
    rendered = els["projects"] ? els["projects"].innerHTML : "";
    return true;
  } catch (e) {
    console.log(`  渲染 ${label} 抛异常：${e.message}`);
    return false;
  }
};

console.log("== 项目卡片渲染 ==");
// 逐个样本单独渲染：一个项目坏了，不该把别的项目的按钮一起藏起来。
samples.forEach((p, i) => {
  vm.runInContext(`PROJECTS = ${JSON.stringify([p])}; ACTIVE_PROJ = "";`, sandbox);
  const ok = run(`样本${i + 1}`, "renderProjects()");
  const n = (rendered.match(/onclick=/g) || []).length;
  console.log(`  样本${i + 1}（${p.name}）: ${ok ? "渲染 OK" : "渲染失败"}，按钮 ${n} 个`);
  if (i === 0) rendered = els["projects"].innerHTML;
});

// 全量一起渲染一遍，取回来做按钮体检
vm.runInContext(`PROJECTS = ${JSON.stringify(samples)}; ACTIVE_PROJ = "20260912-121051";`, sandbox);
run("全量", "renderProjects()");
const allHtml = els["projects"].innerHTML;

const attrs = [...allHtml.matchAll(/on(click|change|input)="([^"]*)"/g)];
console.log(`\n== 按钮体检（共 ${attrs.length} 个事件属性）==`);
const bad = [];
for (const m of attrs) {
  const code = m[2].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
  try {
    new Function(code);
  } catch (e) {
    bad.push([code, e.message]);
  }
}
if (!bad.length) {
  console.log("  全部可解析");
} else {
  for (const [code, msg] of bad) console.log(`  坏：${code}\n        ${msg}`);
}
console.log(`\n坏的共 ${bad.length} 个`);

// 下拉选项也过一遍：选项的 value 是项目 id，同样靠拼串
console.log("\n== 项目下拉 ==");
try {
  vm.runInContext("renderProjectOptions()", sandbox);
  const sel = els["s-project"].innerHTML;
  const opts = [...sel.matchAll(/<option value="([^"]*)"/g)].map(m => m[1]);
  console.log(`  选项 ${opts.length} 个：${opts.join(", ")}`);
  console.log(`  归档项目在下拉里出现：${/20260911-113022/.test(sel) ? "是（不该）" : "否（对）"}`);
  console.log(`  未排图的成稿项目在下拉里出现：${/20260912-999999/.test(sel) ? "是（不该）" : "否（对）"}`);
} catch (e) {
  console.log("  下拉渲染抛异常：" + e.message);
}

process.exit(bad.length ? 1 : 0);
