document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('page-' + t.dataset.tab).classList.add('active');
  });
});

async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function loadTools() {
  const d = await api('/api/tools');
  const checks = document.getElementById('tool-checks');
  const page = document.getElementById('tools-page');
  checks.innerHTML = '';
  page.innerHTML = '';
  (d.tools || []).forEach(t => {
    const lbl = document.createElement('label');
    lbl.innerHTML = `<input type="checkbox" value="${t.name}" checked> ${t.name}`;
    checks.appendChild(lbl);
    const div = document.createElement('div');
    div.className = 'tool-detail';
    let html = `<h3>${t.name}</h3><p class="desc">${t.description}</p>`;
    html += `<div class="section-title">输入字段</div>`;
    (t.input_fields || []).forEach(f => {
      const req = f.required ? '<span class="field-required">必填</span>' : '<span class="field-optional">可选</span>';
      const opt = f.options && f.options.length ? ` [${f.options.join('/')}]` : '';
      html += `<div class="field-row"><span class="field-name">${f.name}</span><span class="field-type">${f.type}</span>${req}<span class="field-desc">${f.description}${opt}</span></div>`;
    });
    html += `<div class="section-title">输出字段</div>`;
    (t.output_fields || []).forEach(f => {
      html += `<div class="field-row"><span class="field-name">${f.name}</span><span class="field-type">${f.type}</span><span class="field-desc">${f.description}</span></div>`;
    });
    if (t.examples && t.examples.length) {
      html += `<div class="section-title">引导示例</div>`;
      t.examples.forEach(e => {
        html += `<div class="example"><b>${e.title}</b>: ${JSON.stringify(e.input)}<br><i>${e.explanation}</i></div>`;
      });
    }
    html += `<div class="section-title">能力</div><div class="cap-list">`;
    (t.capabilities || []).forEach(c => html += `<span class="cap-tag">${c}</span>`);
    html += `</div>`;
    if (t.limitations && t.limitations.length) {
      html += `<div class="section-title">局限</div><div class="cap-list">`;
      t.limitations.forEach(l => html += `<span class="lim-tag">${l}</span>`);
      html += `</div>`;
    }
    html += `<div class="section-title">内部前置规范</div><p style="font-size:12px;color:#888">${(t.internal_prespec||[]).join(' → ')}</p>`;
    div.innerHTML = html;
    page.appendChild(div);
  });
}

async function loadCombos() {
  const d = await api('/api/combos');
  const tbody = document.getElementById('combos-body');
  tbody.innerHTML = '';
  (d.combos || []).forEach(c => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${c.id}</td><td>${c.name}</td><td>${c.desc}</td><td>${c.way_id}</td><td>${(c.scene_tags||[]).join(', ')}</td><td>${JSON.stringify(c.output_limit)}</td>`;
    tr.addEventListener('click', () => showComboDetail(c));
    tbody.appendChild(tr);
  });
}

function showComboDetail(c) {
  const el = document.getElementById('combo-detail');
  el.innerHTML = `<h3>[${c.id}] ${c.name}</h3><p>${c.desc}</p>
<p><b>方式:</b> ${c.way_id}</p>
<p><b>PY 范式:</b> ${c.py_pattern}</p>
<p><b>场景:</b> ${(c.scene_tags||[]).join(', ')}</p>
<p><b>输出限制:</b> ${JSON.stringify(c.output_limit)}</p>
<p><b>Recipe:</b></p><pre>${JSON.stringify(c.recipe, null, 2)}</pre>`;
}

async function loadConfig() {
  const d = await api('/api/config');
  const llm = d.llm || {};
  if (llm.backend) document.getElementById('cfg-backend').value = llm.backend;
  if (llm.base_url) document.getElementById('cfg-base-url').value = llm.base_url;
  if (llm.api_key) document.getElementById('cfg-api-key').value = llm.api_key;
  if (llm.timeout) document.getElementById('cfg-timeout').value = llm.timeout;
  if (llm.max_tokens) document.getElementById('cfg-maxtokens').value = llm.max_tokens;
  const orch = d.orchestrator || {};
  if (orch.max_steps) document.getElementById('cfg-max-steps').value = orch.max_steps;
  if (orch.max_retry) document.getElementById('cfg-max-retry').value = orch.max_retry;
  const ol = orch.output_limit || {};
  if (ol.soft_guide_max_length) document.getElementById('cfg-ol-guide').value = ol.soft_guide_max_length;
  if (ol.diverge_correct_max_length) document.getElementById('cfg-ol-diverge').value = ol.diverge_correct_max_length;
  if (ol.slot_extract_max_fields) document.getElementById('cfg-ol-slot').value = ol.slot_extract_max_fields;
  if (llm.model) loadModels(llm.model);
}

function onBackendChange() {
  const backend = document.getElementById('cfg-backend').value;
  const urlEl = document.getElementById('cfg-base-url');
  if (backend === 'lm-studio' && !urlEl.value) urlEl.value = 'http://localhost:1234';
  if (backend === 'ollama') urlEl.value = 'http://localhost:11434';
}

async function loadModels(presetModel) {
  const backend = document.getElementById('cfg-backend').value;
  const base_url = document.getElementById('cfg-base-url').value;
  const sel = document.getElementById('cfg-model');
  sel.innerHTML = '<option value="">-- 加载中 --</option>';
  try {
    const d = await api(`/api/llm/models?backend=${encodeURIComponent(backend)}&base_url=${encodeURIComponent(base_url)}`);
    sel.innerHTML = '';
    (d.models || []).forEach(m => {
      const opt = document.createElement('option');
      opt.value = m; opt.textContent = m;
      if (m === presetModel) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!sel.options.length) sel.innerHTML = '<option value="">-- 无模型 --</option>';
  } catch (e) {
    sel.innerHTML = '<option value="">-- 加载失败 --</option>';
  }
}

async function testLLM() {
  const st = document.getElementById('cfg-status');
  st.textContent = '测试中...';
  try {
    const d = await api('/api/llm/test?' + new URLSearchParams({
      backend: document.getElementById('cfg-backend').value,
      base_url: document.getElementById('cfg-base-url').value,
      api_key: document.getElementById('cfg-api-key').value,
    }));
    st.textContent = d.success ? '✅ ' + d.msg : '❌ ' + d.msg;
  } catch (e) {
    st.textContent = '❌ ' + e;
  }
}

async function saveConfig() {
  const cfg = {
    llm: {
      backend: document.getElementById('cfg-backend').value,
      base_url: document.getElementById('cfg-base-url').value,
      api_key: document.getElementById('cfg-api-key').value,
      model: document.getElementById('cfg-model').value,
      timeout: parseInt(document.getElementById('cfg-timeout').value),
      max_tokens: parseInt(document.getElementById('cfg-maxtokens').value),
    },
    orchestrator: {
      max_steps: parseInt(document.getElementById('cfg-max-steps').value),
      max_retry: parseInt(document.getElementById('cfg-max-retry').value),
      output_limit: {
        soft_guide_max_length: parseInt(document.getElementById('cfg-ol-guide').value),
        diverge_correct_max_length: parseInt(document.getElementById('cfg-ol-diverge').value),
        slot_extract_max_fields: parseInt(document.getElementById('cfg-ol-slot').value),
      },
    },
  };
  const st = document.getElementById('cfg-status');
  try {
    const d = await api('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });
    st.textContent = d.success ? '✅ 已保存' : '❌ ' + d.error;
  } catch (e) {
    st.textContent = '❌ ' + e;
  }
}

document.getElementById('run-btn').addEventListener('click', async () => {
  const msg = document.getElementById('task-input').value.trim();
  if (!msg) return;
  const tools = [...document.querySelectorAll('#tool-checks input:checked')].map(x => x.value);
  const verbose = document.getElementById('verbose').checked;
  const btn = document.getElementById('run-btn');
  const res = document.getElementById('result');
  btn.disabled = true; btn.textContent = '执行中...';
  res.classList.remove('result-placeholder');
  res.textContent = '执行中，请稍候...';
  try {
    const d = await api('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg, tools, verbose })
    });
    res.textContent = d.success ? d.result : '错误: ' + d.error;
  } catch (e) {
    res.textContent = '请求失败: ' + e;
  } finally {
    btn.disabled = false; btn.textContent = '执行编排';
  }
});

loadTools();
loadCombos();
loadConfig();
