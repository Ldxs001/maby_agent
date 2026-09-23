
let ACTIVE_PROJ=localStorage.getItem('pm_proj')||'';

/* ---- 页面切换：hash 是唯一路由。可以直接把 #config 发给人，也能让
   自动化冒烟落到指定页；只靠按钮的 onclick 切页，外部就没法定位了。 ---- */
let TAB='';
const TABS=['project','script','render','config'];
function hashTab(){
  const t=(location.hash||'').replace(/^#/,'');
  return TABS.indexOf(t)>=0?t:'project';
}
function showTab(name,boot){
  name=name||hashTab();
  if(name===TAB) return;
  TAB=name;
  $$('nav button').forEach(x=>x.classList.toggle('on',x.dataset.tab===name));
  $$('.tab').forEach(t=>t.classList.toggle('on',t.id==='tab-'+name));
  syncAll();
  // 首屏时项目数据已在启动阶段取过，不重复拉；配置页是纯前端渲染，
  // 无论何时第一次显示都必须渲染一次。
  if(boot&&name!=='config') return;
  if(name==='config') renderConfig();
  if(name==='project') loadProjects();
}
function goTab(name){
  if(hashTab()!==name) location.hash=name;
  TAB='';                 // 已在这一页时再点一次＝手动刷新，清标记让它重跑
  showTab(name);
}
function bindNav(){
  $$('nav button').forEach(b=>b.onclick=()=>goTab(b.dataset.tab));
  $$('[data-mat]').forEach(b=>b.onclick=()=>{
    $$('[data-mat]').forEach(x=>x.classList.remove('on')); b.classList.add('on');
    el('mat-paste').style.display=b.dataset.mat==='paste'?'':'none';
    el('mat-file').style.display=b.dataset.mat==='file'?'':'none';
  });
  window.addEventListener('hashchange',()=>showTab(hashTab()));
}
function cfgVal(key){const v=valOf(key);return v===undefined?CFG.values[key]:v}
function fmt(s){s=Math.round(s);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')}

/* ---- 配置加载 ---- */
async function loadConfig(){
  const c=await api('/api/config');
  if(!c.ok){toast('配置加载失败','err');return}
  CFG=c; el('ver').textContent='v'+c.version; EMOTIONS=c.emotions||[];
  renderQuick(); renderCalib(); fillStyleSelect(); renderGauge(null);
  if(TAB==='config') renderConfig();
  renderAudioWarn();
  recalc();
}
function fillStyleSelect(){
  const f=specOf('script.style_preset');
  const n=el('np-style'); if(!f||!n) return;
  n.innerHTML=(f.options||[]).map(o=>'<option value="'+esc(String(o.value))+'"'+
    (String(o.value)===String(cfgVal('script.style_preset'))?' selected':'')+'>'+esc(o.label)+'</option>').join('');
}
function styleLabel(v){
  if(!v) return '';
  const f=specOf('script.style_preset'); if(!f) return v;
  const o=(f.options||[]).find(x=>String(x.value)===String(v));
  return o?o.label:v;
}
function renderAudioWarn(){
  if(!CFG) return;
  const sr=cfgVal('audio.sample_rate'), br=cfgVal('audio.bitrate_kbps'), codec=cfgVal('audio.codec');
  const mp3={8000:64,11025:64,12000:64,16000:160,22050:160,24000:160,32000:320,44100:320,48000:320};
  const aac={8000:96,11025:96,12000:96,16000:128,22050:192,24000:192,32000:256,44100:320,48000:320};
  const ceil=(codec==='aac'?aac:mp3)[sr]||320;
  const box=el('cfg-warn'); if(!box) return;
  const old=box.querySelector('.rate-note'); if(old) old.remove();
  if(br>ceil){
    box.insertAdjacentHTML('beforeend','<div class="rate-note bad" style="font-size:12px;margin-top:8px">'+
      String(codec).toUpperCase()+' 在 '+sr+' Hz 下的码率上限是 '+ceil+' kbps，当前 '+br+
      ' kbps 不可达，编码器会静默钳制。请调整采样率或码率。</div>');
  }
}

/* ---- 音色 ---- */
async function loadVoices(refresh){
  const r=await api('/api/voices'+(refresh?'?refresh=1':''));
  if(!r.ok){toast(r.error||'音色表不可用','err');return}
  VOICES=r.voices||[]; fillVoiceSelects();
}
function fillVoiceSelects(){
  ['tts.voice_a','tts.voice_b'].forEach(key=>{
    (REG[key]||[]).forEach(n=>{
      if(!n.node||n.node.tagName!=='SELECT') return;
      const cur=cfgVal(key);
      const opts=VOICES.length?VOICES:[{name:cur,label:cur}];
      n.node.innerHTML=opts.map(v=>'<option value="'+esc(v.name)+'"'+
        (v.name===cur?' selected':'')+'>'+esc(v.label||v.name)+'</option>').join('');
    });
  });
}

/* ---- 模型列表：本机装了什么由后端说了算，界面只负责摆出来 ---- */
let MODEL_LIST=[], MODEL_NOTE='';
const MODEL_MANUAL='__manual__';      // 下拉末项：填写列表外的名字
async function loadModels(){
  const backend=cfgVal('llm.backend')||'';
  const base=cfgVal('llm.base_url')||'';
  const r=await api('/api/models?backend='+encodeURIComponent(backend)+
                    '&base_url='+encodeURIComponent(base));
  MODEL_LIST=(r&&r.models)||[];
  MODEL_NOTE=(r&&r.ok)
    ? (MODEL_LIST.length?('本机可用 '+MODEL_LIST.length+' 个模型'):'后端没返回模型列表')
    : ('模型列表取不到：'+((r&&r.error)||'未知原因'));
  // 控件先渲染、列表后到（取列表是异步的），所以列表一到就回填下拉选项。
  $$('select[data-models]').forEach(fillModelSelect);
  patchModelNote();
}

/* 模型下拉的选项 = 当前值 ＋ 全部候选 ＋ 手动填写。
   列表取不到时（后端没开正是这种情况）也要能用：那时只剩「手动填写…」这条活路。 */
function fillModelSelect(sel){
  const cur=sel.dataset.cur||'';
  const list=MODEL_LIST.slice();
  const opts=[];
  if(cur&&list.indexOf(cur)<0) opts.push({v:cur,t:cur+'　（当前值，不在列表里）'});
  if(!cur) opts.push({v:'',t:'（未选模型）'});
  if(list.length) opts.push.apply(opts,list.map(m=>({v:m,t:m})));
  else opts.push({v:'',t:'（取不到列表）',off:1});
  opts.push({v:MODEL_MANUAL,t:'〔手动填写…〕'});
  sel.innerHTML=opts.map(o=>'<option value="'+esc(o.v)+'"'+(o.off?' disabled':'')+
    (o.v===cur&&!o.off?' selected':'')+'>'+esc(o.t)+'</option>').join('');
  sel.value=cur;
}
function wireModelSelect(node,key,v){
  node.dataset.cur=(v==null?'':String(v));
  fillModelSelect(node);
  node.onchange=()=>{
    if(node.value===MODEL_MANUAL){ askModel(node,key); return }
    put(key,node.value);
  };
}
function askModel(sel,key){
  modalInput('填写模型名','列表里没有的名字直接填在这里（远端后端常见）。',
    sel.dataset.cur||'', v=>{
      const name=String(v==null?'':v).trim();
      if(!name){ fillModelSelect(sel); return }    // 填了空：当作放弃，恢复原样
      sel.dataset.cur=name;
      fillModelSelect(sel);
      put(key,name);
    });
}
function patchModelNote(){
  const n=el(cid('cfg','llm.model')); if(!n) return;
  const wrap=n.closest('.f'); if(!wrap) return;
  let d=wrap.querySelector('.desc.model-note');
  if(!d){d=document.createElement('div');d.className='desc model-note';wrap.appendChild(d)}
  d.textContent=(MODEL_NOTE?MODEL_NOTE+'　':'')+
    '点开列全部；列表里没有的名字选末项「手动填写…」。';
}

/* ---- 素材 ---- */
async function loadAnchors(){
  const text=el('material').value;
  if(!text.trim()){toast('先粘贴内容');return}
  const r=await api('/api/ingest',{text:text});
  if(!r.ok){toast(r.error,'err');return}
  el('anchor').innerHTML='<option value="">全文（'+r.meta.chars+' 字）</option>'+
    (r.anchors||[]).map(a=>'<option value="'+esc(a)+'">'+esc(a)+'</option>').join('');
  el('mat-meta').textContent=r.meta.chars+' 字 · 锚点 '+(r.anchors||[]).length+' 个';
}
function bindFile(){
  const fi=el('file-input'); if(!fi) return;
  fi.onchange=async e=>{
    const f=e.target.files[0]; if(!f) return;
    const r=await api('/api/ingest',{filename:f.name,data_base64:await readFileB64(f)});
    if(!r.ok){toast(r.error,'err');return}
    el('material').value=r.text;
    el('mat-meta').textContent=f.name+' · '+r.meta.chars+' 字';
    el('anchor').innerHTML='<option value="">全文</option>'+
      (r.anchors||[]).map(a=>'<option value="'+esc(a)+'">'+esc(a)+'</option>').join('');
    toast('已载入 '+f.name+'（'+r.meta.chars+' 字）','ok');
  };
}
async function prepMaterial(){
  const text=el('material').value||'';
  const anchor=el('anchor').value;
  if(!anchor) return {material:text};
  const r=await api('/api/ingest',{text:text,anchor:anchor});
  if(!r.ok){toast(r.error,'err');return null}
  return {material:r.text};
}
function setBar(id,v){const n=el(id);if(n)n.style.width=Math.round(v*100)+'%'}

/* ---- 脚本 ---- */
function renderGauge(est,hint){
  const g=el('gauge');
  if(est){
    const dev=est.deviation_pct;
    const lim=cfgVal('gate.max_deviation_pct');
    const cls=Math.abs(dev)<=lim?'ok':'warn';
    g.innerHTML=
      '<div class="g"><div class="k">台词字数</div><div class="v">'+est.total_chars+'</div><div class="s">'+est.line_count+' 句</div></div>'+
      '<div class="g"><div class="k">预估时长</div><div class="v">'+fmt(est.total_seconds)+'</div><div class="s">目标 '+fmt(est.target_seconds)+'</div></div>'+
      '<div class="g"><div class="k">偏差</div><div class="v '+cls+'">'+(dev>0?'+':'')+dev.toFixed(1)+'%</div><div class="s">阈值 ±'+lim+'%</div></div>'+
      '<div class="g"><div class="k">预估语速</div><div class="v">'+est.speech_cps.toFixed(2)+'</div><div class="s">有效字 / 秒</div></div>';
  }else{
    g.innerHTML='<div class="empty">生成脚本后显示</div>';
  }
  if(hint&&hint.options){
    el('hint').innerHTML='<div class="desc">目标时长反推：'+
      hint.options.map(o=>'语速 '+o.speed.toFixed(2)+'x 约 '+o.effective_chars+' 字').join(' · ')+
      '</div><div class="desc">口径：k 系数由实测回填，样本越多越准。</div>';
  }else{
    el('hint').innerHTML='';
  }
}
// 门禁结果有两个落点：脚本页看生成时那一次，合成页看落盘后的复核。
// 都往 #gates 写的话，合成页那张「校验报告」卡永远不会被填，一直显示
// 「合成完成后显示」——容器在、却没人往里写，比没有这个容器更误导。
function renderGates(rep,box,sum){
  if(!rep) return;
  box=box||'gates'; sum=sum||'gate-sum';
  const items=rep.items||[], pend=rep.pending||[];
  const mk=i=>i.advisory?(i.ok?['ok','✓']:['warn','?']):(i.ok?['ok','✓']:['bad','✕']);
  const lv=i=>i.advisory?'待复核':(i.level==='fail'?'阻断':'记录');
  const s=el(sum);
  if(s){
    s.textContent=(rep.passed?'通过':'未通过')+'（'+items.filter(i=>i.ok).length+'/'+items.length+
      (pend.length?'，待复核 '+pend.length+' 项':'')+'）';
    s.className='tag '+(rep.passed?(pend.length?'warn':'ok'):'bad');
  }
  const bx=el(box); if(!bx) return;
  bx.innerHTML=items.map(i=>{const c=mk(i)[0], m2=mk(i)[1];
    return '<div class="gate"><span class="mk '+c+'">'+m2+'</span>'+
    '<span class="lb" title="'+esc(i.label)+'">'+esc(i.label)+'</span>'+
    '<span class="dt" title="'+esc(i.detail)+'">'+esc(i.detail)+'</span>'+
    '<span class="lv'+(i.advisory&&!i.ok?' warn':'')+'">'+lv(i)+'</span></div>'}).join('');
}
function renderScript(){
  const tb=el('script-tbl').querySelector('tbody');
  el('script-meta').textContent=SCRIPT.length+' 句';
  if(!SCRIPT.length){tb.innerHTML='<tr><td colspan="7" class="empty">尚无脚本</td></tr>';return}
  tb.innerHTML=SCRIPT.map((s,i)=>
    '<tr><td class="num">'+(i+1)+'</td>'+
    '<td><select onchange="SCRIPT['+i+'].speaker=this.value;recalc()" style="width:100%">'+
      '<option value="A"'+(s.speaker==='A'?' selected':'')+'>A</option>'+
      '<option value="B"'+(s.speaker==='B'?' selected':'')+'>B</option></select></td>'+
    '<td><input type="text" value="'+esc(s.text)+'" onchange="SCRIPT['+i+'].text=this.value;recalc()" style="width:100%"></td>'+
    '<td><select onchange="SCRIPT['+i+'].emotion=this.value;recalc()" style="width:100%">'+
      EMOTIONS.map(e=>'<option'+(e===s.emotion?' selected':'')+'>'+esc(e)+'</option>').join('')+'</select></td>'+
    '<td class="num">'+(s.text||'').length+'</td>'+
    '<td class="num">'+(s.estimated_seconds!=null?s.estimated_seconds:'-')+'</td>'+
    '<td class="num">'+(s.actual_seconds!=null?s.actual_seconds:'-')+'</td></tr>').join('');
}
function recalc(){
  if(!SCRIPT.length) return;
  api('/api/script/gate',{script:SCRIPT}).then(r=>{
    if(!r.ok) return;
    SCRIPT=r.estimate.rows.map((row,i)=>Object.assign({},SCRIPT[i],
      {estimated_seconds:row.estimated_seconds,actual_seconds:row.actual_seconds}));
    renderGauge(r.estimate); renderGates(r.report); renderScript();
  });
}
/* ---- 选期（脚本页与合成页共用一套） ---- */
async function loadEpisodes(which){
  const wrap=el(which+'-eps-wrap');
  const pid=(el(which+'-project')||{}).value||'';
  EPISEL[which].clear();
  /* 切回单集模式时收起选期表，顺手把上一次的期表与计数抹掉：整个收起却留着
     旧行，下次展开前若有一帧没重绘，show 出来的就是上一个项目的期。 */
  if(!pid){
    EPISODES[which]=[];
    if(wrap) wrap.style.display='none';
    const box=el(which+'-eps-box'), cnt=el(which+'-eps-cnt');
    if(box) box.innerHTML='';
    if(cnt) cnt.textContent='';
    return
  }
  if(wrap) wrap.style.display='';
  const r=await api('/api/scripts?project_id='+encodeURIComponent(pid));
  let items=(r&&r.items)||[];
  /* 合成页只列有脚本的期。没脚本的期不出现在可选项里，「选了一期却没脚本」
     这个状态就产生不了——门禁在前，不做事后补救。 */
  if(which==='r') items=items.filter(x=>x.has_script);
  EPISODES[which]=items;
  /* 默认替人勾上「该做的那一期」：脚本页是第一个还没有脚本的，合成页是第一个
     有脚本却还没出片的。一次都不点也能直接开工。 */
  const first = which==='s' ? items.find(x=>!x.has_script)
                            : items.find(x=>x.has_script&&!x.done);
  if(first) EPISEL[which].add(first.no);
  renderPick(which);
}
function renderPick(which){
  const items=EPISODES[which]||[], sel=EPISEL[which];
  const box=el(which+'-eps-box'), cnt=el(which+'-eps-cnt');
  if(cnt) cnt.textContent=sel.size?(' · 已选 '+sel.size+' 期'):'';
  if(!box) return;
  if(!items.length){
    box.innerHTML='<div class="empty">'+(which==='r'
      ?'这个项目还没有任何一期有脚本——先到「脚本」页生成。'
      :'没有可选的期（成稿规划要先排图）。')+'</div>';
    return;
  }
  box.innerHTML=items.map(x=>{
    const badge=x.has_script?('<span class="badge ok">'+x.lines+' 句</span>')
                            :'<span class="badge">无脚本</span>';
    const done=x.done?'<span class="badge">已出片</span>':'';
    const peek=(which==='s'&&x.has_script)
      ?'<a class="peek" onclick="event.stopPropagation();peekScript(\''+x.no+'\')">调出来改</a>':'';
    return '<label class="pickrow"><input type="checkbox" '+(sel.has(x.no)?'checked':'')+
      ' onchange="toggleEp(\''+which+'\',\''+x.no+'\',this.checked)">'+
      '<span class="no">第 '+x.no+' 期</span><span class="ti">'+esc(x.title||'')+'</span>'+
      badge+done+peek+'</label>';
  }).join('')+
  '<div class="pickfoot"><a onclick="pickAll(\''+which+'\',true)">全选</a>'+
  '<a onclick="pickAll(\''+which+'\',false)">清空</a>'+
  '<a onclick="closePick()">收起</a></div>';
}
function toggleEp(which,no,on){
  if(on) EPISEL[which].add(no); else EPISEL[which].delete(no);
  renderPick(which);
}
function pickAll(which,on){
  const s=EPISEL[which]; s.clear();
  if(on) (EPISODES[which]||[]).forEach(x=>s.add(x.no));
  renderPick(which);
}
function togglePick(which){
  const box=el(which+'-eps-box'); if(!box) return;
  const was=box.classList.contains('on');
  closePick();
  if(!was) box.classList.add('on');
}
function closePick(){ document.querySelectorAll('.pickbox.on').forEach(b=>b.classList.remove('on')) }
document.addEventListener('click',e=>{ if(!e.target.closest('.pick')) closePick() });

/* 把某一期已经落盘的脚本调出来改。改完点「存回本期」，合成时读的就是这一份。 */
async function peekScript(no){
  const pid=(el('s-project')||{}).value||'';
  const r=await api('/api/script?project_id='+encodeURIComponent(pid)+
                    '&episode_no='+encodeURIComponent(no));
  if(!r.ok){toast(r.error,'err');return}
  SCRIPT=r.script||[]; LOADED_EP=no;
  el('gen-title-wrap').style.display='';
  renderScript(); recalc();
  closePick();
  toast('已载入第 '+no+' 期脚本（'+SCRIPT.length+' 句）· 改完点「存回本期」','ok');
}
async function saveScript(){
  const pid=(el('s-project')||{}).value||'';
  if(!pid){toast('单集模式没有「本期」可存——它不归属任何项目','err');return}
  if(!SCRIPT.length){toast('没有脚本可存','err');return}
  const no=LOADED_EP||picked('s')[0]||'';
  if(!no){toast('先选一期','err');return}
  const r=await api('/api/script/save',{project_id:pid,episode_no:no,script:SCRIPT});
  if(!r.ok){toast(r.error,'err');return}
  toast('第 '+no+' 期脚本已存回项目','ok');
  loadEpisodes('s');
}
async function stopJob(which){
  if(!CUR_JOB){toast('没有正在跑的任务');return}
  const r=await api('/api/job/stop',{task_id:CUR_JOB});
  toast(r.ok?'已请求中止：当前这一步做完就停，不再开新的一期':(r.error||'中止失败'),
        r.ok?'ok':'err');
}
/* 盯一个批任务：进度写成「第 N/M 期 · 第 k 步」，跑完报账。
   中止按钮只在任务跑着的时候露出来——平时摆着容易误点。 */
async function watchBatch(tid,which){
  const btn=el('btn-stop-'+which);
  const st=el(which==='s'?'gen-status':'render-stage');
  const lg=el(which==='s'?'gen-log':'render-log');
  CUR_JOB=tid;
  if(btn) btn.style.display='';
  const tick=async()=>{
    const r=await api('/api/task/'+encodeURIComponent(tid));
    if(!r.ok){toast(r.error||'任务查不到','err');return}
    const j=r.job||{}, b=j.batch||{};
    const head=b.total?('第 '+b.index+'/'+b.total+' 期 · '):'';
    setBar(which==='s'?'gen-bar':'render-bar',
           b.total?((b.index-1+(j.progress||0))/b.total):(j.progress||0));
    if(st) st.textContent=head+(j.stage||'');
    if(lg) lg.textContent=(j.log||[]).join('\n')||'等待中…';
    if(j.status==='running'){setTimeout(tick,1200);return}
    if(btn) btn.style.display='none';
    const res=j.result||{};
    if(j.status==='done'){
      const ok=(res.done||[]).length, bad=(res.failed||[]).length;
      if(lg&&bad) lg.textContent+='\n\n失败明细：\n'+
        (res.failed||[]).map(f=>'第 '+f.no+' 期：'+f.error).join('\n');
      toast('批量跑完：成功 '+ok+' 期'+(bad?('，失败 '+bad+' 期（可在选期里重勾再跑）'):''),
            bad?'err':'ok');
    }else{
      toast('批任务失败：'+(j.error||''),'err');
    }
    loadProjects(); loadHistory(); loadEpisodes(which);
  };
  tick();
}

async function genScript(){
  const pid=(el('s-project')||{}).value||'';
  /* 料源按范式分流：成稿规划的本期料源由后端按地图落点取，本页输入口不参与；
     逐期即兴才在这里读人填的内容。下拉里已经不会出现未排图的成稿规划项目，
     所以这里不必再处理"缺地图"这种情形——那一步已经在上游堵掉了。 */
  const p=PROJECTS.find(x=>x.id===pid);
  const fromMap=!!p&&((p.progress||{}).mode==='mapped');
  const pm=fromMap?{material:''}:(await prepMaterial());
  if(!pm) return;
  if(!pm.material.trim()&&!pid){
    toast('素材为空：粘贴内容、上传文件，或选择一个项目走地图落点','err');return
  }
  const eps=pid?picked('s'):[];
  if(pid&&!eps.length){toast('先在「要做哪几期」里勾上期数','err');return}
  /* 多期交给批任务：勾八期就得点八次、每回还得盯着跑完，批量就是替掉这份守候。
     批任务里每一期走的是与单期同一条生成路径，口径不会漂成两样。 */
  if(pid&&eps.length>1){
    const rb=await api('/api/batch',{kind:'script',project_id:pid,episodes:eps});
    if(!rb.ok){toast(rb.error,'err');return}
    el('btn-gen').disabled=true;
    toast('开始生成 '+eps.length+' 期脚本（串行，失败跳过）','ok');
    await watchBatch(rb.task_id,'s');
    el('btn-gen').disabled=false;
    return;
  }
  el('btn-gen').disabled=true; el('gen-status').textContent='调用模型…'; setBar('gen-bar',0.15);
  el('gen-log').textContent=(fromMap||!pm.material.trim())?'按地图落点取素材…':'调用模型…';
  const t=setInterval(()=>setBar('gen-bar',Math.min(0.9,parseFloat(el('gen-bar').style.width||0)/100+0.05)),700);
  try{
    const r=await api('/api/script/generate',{
      material:pm.material, extra:cfgVal('script.extra_requirement')||'',
      preset:cfgVal('script.style_preset'), project_id:pid, episode_no:eps[0]||''});
    clearInterval(t); setBar('gen-bar',r.ok?1:0);
    if(!r.ok){el('gen-log').textContent='失败：'+r.error+((r.logs||[]).length?('\n'+(r.logs||[]).join('\n')):'');
      el('gen-status').textContent='失败'; toast(r.error,'err'); return}
    SCRIPT=r.script; EMOTIONS=CFG.emotions||[];
    LOADED_EP=eps[0]||'';
    if(r.title){el('gen-title').value=r.title; el('r-title').value=r.title; el('gen-title-wrap').style.display=''}
    el('gen-log').textContent=(r.logs||[]).join('\n')+'\n'+
      '标题：'+(r.title||'（无）')+'\n'+
      '门禁 '+(r.report.passed?'通过':'未通过（'+(r.report.fails.length+r.report.warns.length)+' 项）')+
      ((r.report.pending||[]).length?'，待复核 '+r.report.pending.length+' 项（模型未给出结论，不阻断）':'')+
      (r.degraded?'\n注意：后端不支持约束解码，已降级为提示词约束。':'');
    el('gen-status').textContent='第 '+(r.attempt||1)+' 轮';
    // 内容检的结论就在 report 里（同一个对象），不再单独渲染一遍。
    renderScript(); renderGauge(r.estimate); renderGates(r.report);
    toast('脚本已生成'+(r.title?('：'+r.title):'')+(r.planned_episodes?('（建议共 '+r.planned_episodes+' 期）'):''),'ok');
    if(el('s-project').value) loadProjects();
  }catch(e){clearInterval(t); toast('请求失败：'+e,'err')}
  finally{el('btn-gen').disabled=false}
}
async function gateOnly(){
  if(!SCRIPT.length){toast('尚无脚本');return}
  await recalc(); toast('已重新校验','ok');
}

/* ---- 合成 ---- */
function swOn(id){const n=el(id);return n?n.classList.contains('on'):false}
async function render(){
  const pid=el('r-project').value||'';
  const eps=pid?picked('r'):[];
  if(pid&&!eps.length){toast('先在「合成哪几期」里勾上期数','err');return}

  /* 归属项目时一律按（项目, 期号）读盘上那份脚本，不用本页内存里这一份。
     脚本的落点只有一个——就是「存回本期」存下的那一版。用内存里那份的话，
     改了忘了存、合成出来的却是旧稿，这种事一句提示都不会有。 */
  if(pid&&eps.length>1){
    const rb=await api('/api/batch',{kind:'render',project_id:pid,episodes:eps,
      do_video:swOn('sw-do_video'),preset:cfgVal('script.style_preset'),
      extra:cfgVal('script.extra_requirement')||''});
    if(!rb.ok){toast(rb.error,'err');return}
    el('btn-render').disabled=true;
    toast('开始合成 '+eps.length+' 期（串行，失败跳过）','ok');
    await watchBatch(rb.task_id,'r');
    el('btn-render').disabled=false;
    return;
  }
  if(pid){
    const rb=await api('/api/render',{project_id:pid,episode_no:eps[0],
      do_video:swOn('sw-do_video'),preset:cfgVal('script.style_preset'),
      extra:cfgVal('script.extra_requirement')||''});
    if(!rb.ok){toast(rb.error,'err');return}
    CUR_JOB=rb.task_id; el('btn-render').disabled=true;
    el('btn-stop-r').style.display='';
    toast('已开始合成第 '+eps[0]+' 期','ok'); pollJob();
    return;
  }

  /* 单集模式：不归属任何项目，用本页这份脚本，与从前一致。 */
  const title=el('r-title').value.trim();
  if(!title){toast('本期标题为空——先到「脚本」页生成脚本，标题随脚本产出','err');return}
  if(!SCRIPT.length){toast('尚无脚本，先到「脚本」页生成','err');return}
  const r=await api('/api/render',{title:title,script:SCRIPT,
    material:el('material').value,preset:cfgVal('script.style_preset'),
    extra:cfgVal('script.extra_requirement')||'',
    do_video:swOn('sw-do_video')});
  if(!r.ok){toast(r.error,'err');return}
  CUR_JOB=r.task_id; el('btn-render').disabled=true;
  el('btn-stop-r').style.display='';
  toast('已开始合成','ok'); pollJob();
}
async function pollJob(){
  if(!CUR_JOB) return;
  const r=await api('/api/task/'+CUR_JOB);
  if(!r.ok){el('btn-render').disabled=false;return}
  const j=r.job;
  setBar('render-bar',j.progress);
  el('render-stage').textContent=j.stage+(j.error?' · '+j.error:'');
  el('render-log').textContent=(j.log||[]).join('\n')||'等待中…';
  if(j.status==='running'){setTimeout(pollJob,1200);return}
  el('btn-render').disabled=false;
  const sb=el('btn-stop-r'); if(sb) sb.style.display='none';
  if(j.status==='done'){
    toast('合成完成','ok');
    const res=j.result||{};
    if(res.episode_files) showOutputs(res);
    loadProjects(); loadHistory(); loadEpisodes('r');
  }else{
    toast('合成失败：'+(j.error||''),'err');
    modal('合成失败',(j.error||'')+'\n\n'+(j.log||[]).slice(-8).join('\n'));
  }
}
/* 产物路径 → /api/file 认的相对地址（相对输出目录）。 */
function relOf(p){
  const parts=String(p||'').split(/[\\/]/).filter(Boolean);
  const i=parts.lastIndexOf('projects');
  return i<0?'':parts.slice(i+1).join('/');
}
function fileHref(rel){return '/api/file/'+String(rel).split('/').filter(Boolean).map(encodeURIComponent).join('/')}
/* 产物按类分放在项目里，所以逐个取路径表里的确切位置，不再拿目录名去猜文件名。 */
function showOutputs(res){
  const ep=res.episode_files||{};
  let h='';
  const item=(p,f)=>{if(p){const n=String(p).split(/[\\/]/).pop();
    h+='<div class="kv"><b>'+f+'</b><a target="_blank" href="'+fileHref(relOf(p))+'">'+esc(n)+'</a></div>'}};
  item(ep.video,'横屏视频'); item(ep.video_vertical,'竖屏视频'); item(ep.audio,'音频');
  item(ep.subtitle,'字幕'); item(ep.article,'图文');
  item(ep.bg_h,'背景图（横屏）'); item(ep.bg_v,'背景图（竖屏）');
  const cov=ep.cover||{};
  Object.keys(cov).forEach(k=>{const n=String(cov[k]).split(/[\\/]/).pop();
    h+='<div class="kv"><b>封面 '+esc(k)+'</b><a target="_blank" href="'+fileHref(relOf(cov[k]))+'">'+esc(n)+'</a></div>'});
  el('outputs').innerHTML=h||'<div class="empty">尚无产物</div>';
  renderGates(res.report,'report','rep-sum');
}
async function loadHistory(){
  const r=await api('/api/projects');
  if(!r.ok) return;
  const ps=r.projects||[];
  /* 一期由「树根 + 期号」定位：产物按类型分放在项目里，没有「期目录」
     这种东西可以指。 */
  el('history').innerHTML=ps.length?ps.slice(0,20).map(p=>
    '<div class="kv"><b>'+esc((p.created||'').slice(0,16))+'</b><span>'+
    (p.episode_no?('第 '+esc(p.episode_no)+' 期 · '):'')+esc(p.title||p.root)+
    ' <a href="#" onclick="viewReport(\''+esc(p.root)+'\',\''+esc(p.no)+'\');return false">报告</a></span></div>').join('')
    :'<div class="empty">尚无产出</div>';
}
async function viewReport(root,no){
  const r=await api('/api/report/'+encodeURIComponent(root)+'/'+encodeURIComponent(no));
  if(!r.ok){toast(r.error,'err');return}
  renderGates(r.report,'report','rep-sum');
  goTab('render');
}

/* ---- 项目 ---- */
async function loadProjects(){
  const r=await api('/api/project');
  if(!r.ok){toast(r.error||'项目登记表不可用','err');return}
  PROJECTS=r.projects||[];
  if(!PROJECTS.find(p=>p.id===ACTIVE_PROJ)) ACTIVE_PROJ='';
  renderProjects(); renderProjectOptions(); renderOrphans();
  loadHistory();
}
function renderOrphans(){
  api('/api/project?scope=orphans').then(r=>{
    const list=(r&&r.orphans)||[];
    el('orphan-sum').textContent=list.length?list.length+' 个':'无';
    el('orphans').innerHTML=list.length?list.slice(0,20).map(o=>
      '<div class="kv"><b>'+esc(o.dir)+'</b><span>整棵树没有归属项目（单集产出）</span></div>').join('')
      :'<div class="empty">无</div>';
  });
}
/* 成稿规划必须排过图才算「可归属」：本期料源由地图落点决定，没有地图就没有
   料源。判据只有这一条，项目卡片与两个页面的下拉共用，免得各写一份漂开。 */
function planReady(p){
  const pr=p.progress||{};
  return !(pr.mode==='mapped')||!!pr.mapped;
}
function renderProjects(){
  const box=el('projects');
  el('proj-sum').textContent=PROJECTS.length?PROJECTS.length+' 个项目':'';
  if(!PROJECTS.length){box.innerHTML='<div class="empty">尚无项目。左侧立项后即可按期推进。</div>';return}
  box.innerHTML=PROJECTS.map(p=>{
    const pr=p.progress||{};
    const mapped=pr.mode==='mapped';
    const pct=pr.planned?Math.min(100,Math.round(pr.done/pr.planned*100))
                        :(pr.done?Math.min(100,pr.done*10):0);
    let h='<div class="proj'+(p.archived?' arch':'')+(p.id===ACTIVE_PROJ?' on':'')+'">';
    h+='<div class="top"><span class="nm">'+esc(p.name)+'</span>'+
       '<span class="tag" style="font-size:11px">'+esc(pr.mode_label||'')+'</span>'+
       (p.program_name&&p.program_name!==p.name?'<span class="dim" style="font-size:11px">节目名 '+esc(p.program_name)+'</span>':'')+
       '<span class="sp"></span><span class="dim" style="font-size:11px">'+esc(pr.label||'')+'</span></div>';
    h+='<div class="bar" style="margin:8px 0 10px"><i style="width:'+pct+'%"></i></div>';
    h+='<div class="meta"><span>下一期 第 '+esc(pr.next_episode||'1')+' 期</span>'+
       '<span>风格 '+esc(styleLabel(p.style_preset)||'跟随全局')+'</span>'+
       '<span>素材 '+esc(pr.paradigm_label||'')+'</span>'+
       '<span>建立 '+esc((p.created||'').slice(0,16))+'</span></div>';
    if(p.note) h+='<div class="meta"><span>'+esc(p.note)+'</span></div>';
    if((p.episodes||[]).length){
      h+='<div class="eps">'+p.episodes.slice().reverse().map(e=>
        '<div class="ep"><span class="no">第'+esc(e.no)+'期</span><span class="ti">'+esc(e.title)+
        '</span><span class="dt">'+esc((e.created||'').slice(5,16))+'</span>'+
        '<a href="#" onclick="viewReport(\''+esc(p.id)+'\',\''+esc(e.no)+'\');return false">报告</a></div>').join('')+'</div>';
    }
    const takeable=planReady(p);
    h+='<div class="acts">'+
       (takeable?('<button class="mini" onclick="chooseProject(\''+esc(p.id)+'\')">设为当前</button>')
                :'<button class="mini" disabled title="成稿规划需先排出期数地图，才能选为当前">设为当前</button>')+
       (pr.legacy?('<button class="mini" onclick="setPlanMode(\''+esc(p.id)+'\',\'mapped\')">定为成稿规划</button>'+
                   '<button class="mini" onclick="setPlanMode(\''+esc(p.id)+'\',\'episodic\')">定为逐期即兴</button>'):'')+
       (mapped?('<button class="mini" onclick="editParadigm(\''+esc(p.id)+'\')">素材类型</button>'+
                '<button class="mini" onclick="openSources(\''+esc(p.id)+'\')">成稿</button>'+
                '<button class="mini" onclick="openMap(\''+esc(p.id)+'\')">地图'+
                (pr.mapped?(' · '+pr.mapped+' 期'):' · 未排')+'</button>'):'')+
       '<button class="mini" onclick="editNext(\''+esc(p.id)+'\',\''+esc(pr.next_episode||'1')+'\')">改下一期号</button>'+
       '<button class="mini" onclick="setArchived(\''+esc(p.id)+'\','+(p.archived?'false':'true')+')">'+(p.archived?'恢复':'归档')+'</button>'+
       '<button class="mini del'+(ARM_DEL===p.id?' arm':'')+'" onclick="delProject(\''+esc(p.id)+'\')">'+
       (ARM_DEL===p.id?'确认删除':'删除')+'</button>'+
       '</div></div>';
    return h;
  }).join('');
}
function renderProjectOptions(){
  /* 没排图的成稿规划项目不进下拉：让它列在这里，等于把「该不该能选」推到
     生成时才作答。归档的照旧不列。 */
  const opts='<option value="">（不归属项目 · 单集模式）</option>'+
    PROJECTS.filter(p=>!p.archived&&planReady(p)).map(p=>'<option value="'+esc(p.id)+'">'+esc(p.name)+
      ' · 下一期第 '+esc((p.progress||{}).next_episode||'1')+' 期</option>').join('');
  ['s-project','r-project'].forEach(id=>{
    const n=el(id); if(!n) return;
    n.innerHTML=opts; n.value=ACTIVE_PROJ||'';
  });
  renderProjNote('r');
  renderProjNote('s');
}
/* 两个页面的项目下拉共用一个当前项目：在这里选与在项目卡片上「设为当前」
   是同一件事。否则重新拉一次项目表，手选的归属会被打回原值。 */
function pickProject(which){
  const sel=el(which+'-project'); if(!sel) return;
  ACTIVE_PROJ=sel.value||'';
  localStorage.setItem('pm_proj',ACTIVE_PROJ);
  renderProjectOptions();
  renderProjects();
}
function renderProjNote(which){
  const sel=el(which+'-project'); if(!sel) return;
  const id=sel.value;
  const p=PROJECTS.find(x=>x.id===id);
  const note=el(which+'-proj-note');
  if(p){
    const pr=p.progress||{};
    if(note) note.textContent=p.name+' · 已出 '+(pr.done||0)+' 期'+
      (pr.planned?(' / 计划 '+pr.planned+' 期'):'（总期数由模型规划）')+
      ' · 下一期第 '+(pr.next_episode||'1')+' 期';
    if(which==='s') renderMaterialSource(p);
  }else{
    if(note) note.textContent='单集模式：产物独立成集，不计入任何项目进度。';
    if(which==='s') renderMaterialSource(null);
  }
  /* 选期表跟着项目走。没项目就把选期收起来——单集模式没有「第几期」这回事。 */
  loadEpisodes(which);
}
/* 料源随范式切换：成稿规划的料源由地图落点决定，输入口收起来、只读摆出本期
   会取哪几节——自动取料最怕取错了没人看出来；逐期即兴的料源是当场给的，输入
   口就是正解。这里只回答「料从哪来」，"能不能做"的门禁在上游入口，不在这儿。 */
function renderMaterialSource(p){
  const locked=el('mat-locked'), src=el('mat-src'), input=el('mat-input');
  const pr=(p||{}).progress||{};
  const fromMap=!!p&&pr.mode==='mapped';
  if(input) input.style.display=fromMap?'none':'';
  if(locked) locked.style.display=fromMap?'':'none';
  if(!fromMap) return;
  const eps=(p.map&&p.map.episodes)||[];
  const row=eps.find(e=>String(e.no)===String(pr.next_episode||'1'));
  const refs=(row&&row.refs)||[];
  if(src) src.innerHTML='<b>本期料源＝地图落点</b>　第 '+esc(pr.next_episode||'1')+' 期'+
    (row?('「'+esc(row.title||'未命名')+'」'):'')+'<br>'+
    (refs.length?('将取用：'+refs.map(r=>esc(r.source)+' · '+esc(r.anchor||'全文')).join('；'))
               :'（地图里这一期没有落点）')+
    '<br>要改本期讲什么，去项目卡片上的「地图」改。';
  const meta=el('mat-meta'); if(meta) meta.textContent='按地图落点';
}
function chooseProject(pid){
  ACTIVE_PROJ=(ACTIVE_PROJ===pid)?'':pid;
  localStorage.setItem('pm_proj',ACTIVE_PROJ);
  renderProjects(); renderProjectOptions();
  toast(ACTIVE_PROJ?'已切到该项目，脚本与合成都会挂在它下面':'已取消归属','ok');
}
function bindMode(){
  const box=el('np-mode'); if(!box) return;
  box.querySelectorAll('button').forEach(b=>b.onclick=()=>{
    box.querySelectorAll('button').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
  });
}
function pickMode(){
  const on=el('np-mode').querySelector('button.on');
  return on?on.dataset.mode:'mapped';
}
async function createProject(){
  const name=el('np-name').value.trim();
  if(!name){toast('请填项目名称','err');return}
  const mode=pickMode();
  const planned=el('np-planned').value.trim();
  const body={action:'create',name:name,plan_mode:mode,
    program_name:el('np-program').value.trim(),
    planned_episodes:planned?parseInt(planned,10):null,
    first_episode:el('np-first').value.trim()||'1',
    style_preset:el('np-style').value,note:el('np-note').value.trim(),
    paradigm:el('np-paradigm').value};
  const r=await api('/api/project',body);
  if(!r.ok){toast(r.error,'err');return}
  el('np-name').value=''; el('np-program').value=''; el('np-planned').value=''; el('np-note').value='';
  ACTIVE_PROJ=r.project.id; localStorage.setItem('pm_proj',ACTIVE_PROJ);
  await loadProjects();
  if(mode==='mapped'){
    toast('已立项：'+name+'（成稿规划）','ok');
    // 成稿规划的价值全在"先把稿子给进来"。立完项直接把素材面板推出来，
    // 否则用户会以为还得自己去某个角落找入口。
    openSources(r.project.id);
  }else{
    toast('已立项：'+name+'（逐期即兴）','ok');
  }
}

/* ---- 成稿与期数地图 ---- */
function projName(pid){const p=PROJECTS.find(x=>x.id===pid);return p?p.name:pid}
/* 分支期号的判据与后端 branch_no() 同一条规则，这里只用来给行上色；
   真正编期号的地方只有后端一处。 */
function isBranchNo(no){return /[a-z]+$/.test(String(no||''))}
function refsText(refs){
  const list=refs||[];
  if(!list.length) return '<span style="color:var(--red)">无落点</span>';
  return list.map(r=>esc(r.source)+' · '+esc(r.anchor||'全文')).join('<br>');
}
/* 分块转 base64。逐字节拼接在大文件上是 O(n²)，一本几十万字的书会卡住页面。 */
async function readFileB64(f){
  const bytes=new Uint8Array(await f.arrayBuffer());
  let bin=''; const CH=0x8000;
  for(let i=0;i<bytes.length;i+=CH) bin+=String.fromCharCode.apply(null,bytes.subarray(i,i+CH));
  return btoa(bin);
}

async function openSources(pid){
  const r=await api('/api/sources?project_id='+encodeURIComponent(pid));
  if(!r.ok){toast(r.error,'err');return}
  const list=r.sources||[];
  let h='<div class="desc" style="margin-bottom:12px">成稿入库后按章节切分。排地图只喂章节清单，生成某一期时才按落点取回原文。'
       +'入库会自动探查一次结构与类型，结果落盘冻结——排图直接用这一份，不必再等。</div>';
  h+='<div id="src-list">'+(list.length?list.map(s=>{
    const pv=s.probe||{};
    /* 结构摘要取自探查结果，不由前端另算一份。未探查时退回入库时的粗计数，
       并写明「尚未探查」，免得把两种口径混成一句。 */
    const meta=pv.segments
      ? ('结构 '+pv.segments+' 条 / '+pv.levels+' 层 · 切分单位 H'+pv.unit_level+' · 判定 '+esc(pv.kind_label||'未判定'))
      : (s.chars+' 字 · '+s.sections+' 节 · 尚未探查');
    const warn=(pv.warnings&&pv.warnings.length)
      ? '<div class="dim" style="color:var(--amber);padding:0 0 6px 0">'+esc(pv.warnings[0])+(pv.warnings.length>1?('　等 '+pv.warnings.length+' 条'):'')+'</div>'
      : '';
    return '<div><div class="src-row"><span class="nm">'+esc(s.name)+'</span>'+
      '<span class="dim">'+meta+'</span>'+
      '<button class="mini" onclick="delSource(\''+esc(pid)+'\',\''+esc(s.id)+'\')">移除</button></div>'+warn+'</div>';
  }).join(''):'<div class="empty">还没有成稿。上传文件或粘贴正文。</div>')+'</div>';
  h+='<div style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">'+
     '<div class="f"><label>上传文件 <span class="hint">md / txt / docx</span></label>'+
     '<input type="file" id="src-file" accept=".md,.markdown,.txt,.docx"></div>'+
     '<div class="f"><label>或粘贴正文</label><textarea id="src-text" rows="4" placeholder="粘贴要排进播出的正文"></textarea></div>'+
     '<div class="btn-row"><button class="btn primary" id="src-add-btn" onclick="addSource(\''+esc(pid)+'\')">入库</button>'+
     '<button class="btn" id="src-scan-btn" onclick="scanSources(\''+esc(pid)+'\')">重新探查</button></div></div>';
  modalHtml('成稿 · '+esc(projName(pid)),h,()=>{},'关闭',true);
}

async function addSource(pid){
  const f=el('src-file').files[0];
  const txt=(el('src-text').value||'').trim();
  if(!f&&!txt){toast('选文件或粘贴正文','err');return}
  const body={action:'add',project_id:pid};
  if(f){body.filename=f.name;body.data_base64=await readFileB64(f)}
  else{body.text=txt}
  toast('已入库，正在探查结构与类型…');
  /* 入库要等模型读一遍标题：按钮进等待态并改文案，否则 toast 三秒就没了，
     界面看着像卡死。 */
  const btn=el('src-add-btn');
  if(btn){btn.disabled=true;btn.textContent='入库并探查中…'}
  const r=await api('/api/source',body);
  if(!r.ok){toast(r.error,'err');await openSources(pid);return}
  const pv=r.probe||{};
  toast('已入库：'+((r.source||{}).name||'')
    +(pv.segments?(' · '+pv.segments+' 条 / '+pv.levels+' 层 · 判定 '+(pv.kind_label||'未判定')):''),'ok');
  await openSources(pid);
}

/* 补跑探查：这一份素材进库时没探查过（旧版本入库），或人改了判定依据要重判。
   已探查过且有结果的走缓存，不会白烧一遍模型。 */
async function scanSources(pid){
  toast('正在探查结构与类型：模型要读一遍标题，请稍候…');
  const btn=el('src-scan-btn');
  if(btn){btn.disabled=true;btn.textContent='探查中…'}
  const r=await api('/api/source',{action:'scan',project_id:pid});
  if(!r.ok){toast(r.error,'err');await openSources(pid);return}
  const n=(r.sources||[]).length;
  toast('已探查 '+n+' 份素材'+(r.est_episodes?(' · 按体量约 '+r.est_episodes+' 期'):''),'ok');
  await openSources(pid);
}

async function delSource(pid,sid){
  const r=await api('/api/source',{action:'remove',project_id:pid,id:sid});
  if(!r.ok){toast(r.error,'err');return}
  toast('已移除','ok');
  await openSources(pid);
}

async function doPlanMap(pid){
  closeModal();
  // 排图不再是「一次长调用」：结构直接读、逐节凝缩、再分组，交给模型的上下文
  // 一次比一次小。节数多时调用次数也多，所以文案要如实说清在等什么。
  toast('正在排地图：逐节凝缩后分组，节数多时要等一会儿…');
  const r=await api('/api/plan',{action:'map',project_id:pid});
  if(!r.ok){showPlanReport('排图未完成',r.logs,[r.error||'未知原因']);return}
  await loadProjects();
  const n=(((r.project||{}).map||{}).episodes||[]).length;
  toast('地图已排出 '+n+' 期（'+(r.kind_label||'')+'，每期约 '+r.capacity+' 有效字）','ok');
  openMap(pid);
  // 告警不能只写进进度日志：模型排的期数与项目定的不符、单元没被分进任何一期、
  // 同系列被拆，这些都是「结果已落库但需要人核对」，不弹出来就等于没报。
  showPlanReport('排图结果',r.logs,r.warnings);
}
function showPlanReport(title,logs,warns){
  const ws=(warns||[]).filter(Boolean);
  const ls=(logs||[]).slice(-14);
  if(!ws.length&&!ls.length) return;
  let h='';
  if(ws.length){
    h+='<p class="note">以下几条请核对后再出片：</p><ul style="margin:0 0 0 18px">'+
       ws.map(w=>'<li>'+esc(w)+'</li>').join('')+'</ul>';
  }
  if(ls.length){
    h+='<p class="note" style="margin-top:10px">过程日志</p>'+
       '<div class="dim" style="font-size:12px;max-height:180px;overflow:auto;'+
       'white-space:pre-wrap">'+ls.map(l=>esc(l)).join('\n')+'</div>';
  }
  modalHtml(title,h,()=>{},'知道了',true);
}
async function loadParadigmOptions(){
  const sel=el('np-paradigm'); if(!sel) return;
  const r=await api('/api/plan',{action:'paradigms'});
  if(!r.ok) return;
  Object.keys(r.options||{}).forEach(k=>{
    if(k==='auto') return;
    const o=document.createElement('option');
    o.value=k; o.textContent=r.options[k].label; sel.appendChild(o);
  });
}
async function editParadigm(pid){
  const r=await api('/api/plan',{action:'paradigms',project_id:pid});
  if(!r.ok){toast(r.error,'err');return}
  let opt='<option value="">自适应（按结构推断）</option>';
  Object.keys(r.options||{}).forEach(k=>{
    if(k==='auto') return;
    opt+='<option value="'+esc(k)+'"'+(k===r.current?' selected':'')+'>'+
         esc(r.options[k].label)+'</option>';
  });
  let h='<p class="note">素材类型决定排地图时的组织依据：切分单位、整合依据、'+
        '重点判据、推进方式。与规划方式不同，它不产生产物，改完自己决定要不要重排。</p>';
  h+='<div class="f"><label>素材类型</label><select id="para-sel" onchange="paraDesc()">'+
     opt+'</select><div class="desc" id="para-desc" style="margin-top:6px"></div></div>';
  h+='<div class="btn-row" style="margin-top:12px">'+
     '<button class="btn" onclick="saveParadigm(\''+esc(pid)+'\',false)">只保存（保留旧地图）</button>'+
     '<button class="btn primary" onclick="saveParadigm(\''+esc(pid)+'\',true)">保存并重排地图</button>'+
     '</div>';
  modalHtml('素材类型 · 可改',h,()=>{},'关闭',true);
  paraDesc();
}
function paraDesc(){
  const s=el('para-sel'), d=el('para-desc');
  if(!s||!d) return;
  const v=r=>r.options[r.selectedIndex];
  d.textContent = s.value
    ? ('排图时按「'+v(s).text+'」的组织依据，与探查推断无关')
    : '由探查按结构推断。拿不准就留这个';
}
async function saveParadigm(pid, redo){
  const sel=el('para-sel'); if(!sel) return;
  const r=await api('/api/project',{action:'update',id:pid,paradigm:sel.value});
  if(!r.ok){toast(r.error,'err');return}
  await loadProjects();
  closeModal();
  toast('素材类型已更新','ok');
  if(redo) doPlanMap(pid);
}
async function openMap(pid){
  const r=await api('/api/project');
  const p=(r.projects||[]).find(x=>x.id===pid);
  if(!p){toast('项目不存在','err');return}
  const rows=(p.map&&p.map.episodes)||[];
  let h='<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">'+
    '<button class="btn" onclick="doPlanMap(\''+esc(pid)+'\')">'+(rows.length?'重新排图':'排地图')+'</button>'+
    '<button class="btn" onclick="openInsert(\''+esc(pid)+'\')">插入素材</button>'+
    '<span style="flex:1"></span>'+
    '<span class="dim" style="font-size:12px">共 '+rows.length+' 期</span></div>';
  if(!rows.length){
    h+='<div class="empty">还没有地图。点「排地图」让模型按成稿的章节排出期数、每期要点与落点。</div>';
    return modalHtml('期数地图 · '+esc(p.name),h,()=>{},'关闭',true);
  }
  h+='<div class="tbl-scroll"><table class="map-tbl" id="map-tbl"><thead><tr>'+
     '<th style="width:56px">期号</th><th style="width:170px">标题</th>'+
     '<th style="width:150px">主旨</th>'+
     '<th>要点（每行一条）</th><th style="width:160px">素材落点</th>'+
     '<th style="width:36px"></th></tr></thead><tbody>';
  rows.forEach(e=>{
    h+='<tr data-no="'+esc(e.no)+'" data-refs="'+esc(JSON.stringify(e.refs||[]))+'"'+
       (isBranchNo(e.no)?' class="branch"':'')+'>'+
       '<td>'+esc(e.no)+'</td>'+
       '<td><input class="m-title" value="'+esc(e.title)+'"></td>'+
       '<td><input class="m-gist" value="'+esc(e.gist||'')+'"></td>'+
       '<td><textarea class="m-points" rows="2">'+esc((e.points||[]).join('\n'))+'</textarea></td>'+
       '<td class="refs">'+refsText(e.refs)+'</td>'+
       '<td><button class="mini" onclick="this.closest(\'tr\').remove()">✕</button></td></tr>';
  });
  h+='</tbody></table></div>';
  h+='<div style="margin-top:10px"><button class="mini" onclick="addMapRow()">+ 加一期</button></div>';
  modalHtml('期数地图 · '+esc(p.name),h,()=>saveMap(pid),'保存',true);
}

function addMapRow(){
  const tb=el('map-tbl').querySelector('tbody');
  const nums=Array.from(tb.querySelectorAll('tr')).map(tr=>{
    const m=/^(\d+)/.exec(tr.dataset.no||''); return m?parseInt(m[1],10):0;});
  const no=String((nums.length?Math.max.apply(null,nums):0)+1);
  const tr=document.createElement('tr');
  tr.dataset.no=no; tr.dataset.refs='[]';
  tr.innerHTML='<td>'+esc(no)+'</td>'+
    '<td><input class="m-title" value=""></td>'+
    '<td><input class="m-gist" value=""></td>'+
    '<td><textarea class="m-points" rows="2"></textarea></td>'+
    '<td class="refs"><span style="color:var(--red)">无落点</span></td>'+
    '<td><button class="mini" onclick="this.closest(\'tr\').remove()">✕</button></td>';
  tb.appendChild(tr);
}

async function saveMap(pid){
  const tbl=el('map-tbl'); if(!tbl) return;
  const eps=Array.from(tbl.querySelectorAll('tbody tr')).map(tr=>({
    no:tr.dataset.no,
    title:(tr.querySelector('.m-title').value||'').trim(),
    gist:(tr.querySelector('.m-gist').value||'').trim(),
    points:(tr.querySelector('.m-points').value||'').split('\n').map(s=>s.trim()).filter(Boolean),
    refs:JSON.parse(tr.dataset.refs||'[]')
  }));
  if(!eps.length){toast('地图是空的，未保存','err');return}
  const r=await api('/api/plan',{action:'save',project_id:pid,episodes:eps});
  if(!r.ok){toast(r.error,'err');return}
  await loadProjects();
  toast('地图已保存（'+eps.length+' 期）','ok');
}

async function openInsert(pid){
  const r=await api('/api/sources?project_id='+encodeURIComponent(pid));
  if(!r.ok){toast(r.error,'err');return}
  const list=r.sources||[];
  if(!list.length){toast('项目里还没有成稿，先在「成稿」里载入','err');return}
  let h='<div class="desc" style="margin-bottom:12px">勾选要插入的成稿。模型读完现有各期的标题与要点后，给出插入点与拆分。</div>';
  h+='<div id="ins-src">'+list.map(s=>
    '<label class="src-row" style="cursor:pointer"><input type="checkbox" value="'+esc(s.id)+'">'+
    '<span class="nm">'+esc(s.name)+'</span>'+
    '<span class="dim">'+s.chars+' 字 · '+s.sections+' 节</span></label>').join('')+'</div>';
  modalHtml('插入素材 · 定插入点',h,()=>doInsertPlan(pid),'让模型定插入点',true);
}

async function doInsertPlan(pid){
  const ids=Array.from(document.querySelectorAll('#ins-src input:checked')).map(n=>n.value);
  if(!ids.length){toast('先勾选一份素材','err');return}
  closeModal();
  toast('正在判断该插在哪一期之后…');
  const r=await api('/api/plan',{action:'insert_plan',project_id:pid,source_ids:ids});
  if(!r.ok){toast(r.error,'err');return}
  showInsertSuggestion(pid,r.suggestion||{});
}

function showInsertSuggestion(pid,sug){
  const eps=sug.episodes||[];
  if(!eps.length){toast('模型没有给出要插入的期','err');return}
  let h='<div class="desc" style="margin-bottom:10px">模型判断新素材插在第 <b style="color:var(--gold)">'+
    esc(sug.anchor_no)+'</b> 期之后，拆成 '+eps.length+' 期。插入点与内容都可以改。</div>';
  h+='<div class="f"><label>插在哪一期之后 <span class="hint">必须是现有期号；改后由程序按新锚点重编</span></label>'+
     '<input type="text" id="ins-anchor" value="'+esc(sug.anchor_no)+'"></div>';
  h+='<table class="map-tbl"><thead><tr><th style="width:56px">期号</th><th style="width:170px">标题</th>'+
     '<th>要点（每行一条）</th><th style="width:160px">素材落点</th></tr></thead><tbody id="ins-body">';
  eps.forEach(e=>{
    h+='<tr data-refs="'+esc(JSON.stringify(e.refs||[]))+'">'+
       '<td>'+esc(e.no)+'</td>'+
       '<td><input class="i-title" value="'+esc(e.title)+'"></td>'+
       '<td><textarea class="i-points" rows="2">'+esc((e.points||[]).join('\n'))+'</textarea></td>'+
       '<td class="refs">'+refsText(e.refs)+'</td></tr>';
  });
  h+='</tbody></table>';
  modalHtml('插入建议 · 确认后落库',h,()=>applyInsert(pid),'插入',true);
}

async function applyInsert(pid){
  const anchor=(el('ins-anchor').value||'').trim();
  if(!anchor){toast('插入点不能空','err');return}
  // 不在这边编期号：编了就要跟后端各写一套规则，换锚点后两边对不上。
  // 只把内容与锚点递上去，号由后端按锚点编，回什么就显示什么。
  const rows=Array.from(el('ins-body').querySelectorAll('tr')).map(tr=>({
    title:(tr.querySelector('.i-title').value||'').trim(),
    points:(tr.querySelector('.i-points').value||'').split('\n').map(s=>s.trim()).filter(Boolean),
    refs:JSON.parse(tr.dataset.refs||'[]')
  }));
  const r=await api('/api/plan',{action:'insert_apply',project_id:pid,anchor_no:anchor,episodes:rows});
  if(!r.ok){toast(r.error,'err');return}
  await loadProjects();
  toast('已插入 '+rows.length+' 期：'+(r.episodes||[]).join('、'),'ok');
  openMap(pid);
}

/* 旧项目补选规划方式。它们当年没做过这个选择，所以要给一次机会；
   选完与立项时一样定死，因此提示里写明不可改。 */
async function setPlanMode(pid,mode){
  const label=(mode==='mapped')?'成稿规划':'逐期即兴';
  const r=await api('/api/project',{action:'update',id:pid,plan_mode:mode});
  if(!r.ok){toast(r.error,'err');return}
  await loadProjects();
  toast('规划方式定为「'+label+'」，此后不可更改','ok');
  if(mode==='mapped') openSources(pid);
}
function editNext(pid,cur){
  modalInput('改下一期号','下一个出片的期号。取消即不改动。',cur,async v=>{
    if(v==null||String(v).trim()===''){toast('未改动');return}
    const r=await api('/api/project',{action:'update',id:pid,next_episode:String(v).trim()});
    if(!r.ok){toast(r.error,'err');return}
    toast('下一期号已改为 '+String(v).trim(),'ok'); loadProjects();
  },'保存');
}
async function setArchived(pid,on){
  const r=await api('/api/project',{action:'update',id:pid,archived:on});
  if(!r.ok){toast(r.error,'err');return}
  toast(on?'已归档（产物保留）':'已恢复','ok'); loadProjects();
}
/* 删除要点两下。第一下只把按钮点亮，第二下才真删——删掉的是整个项目目录
   （素材、地图、脚本、各期成品），没有回收站。点亮后 6 秒没下文自动熄灭，
   免得「点过一次」的按钮一直等着被误触。 */
let ARM_DEL='', ARM_TIMER=null;
function fmtBytes(n){
  n=Number(n||0);
  if(n<1024) return n+' B';
  if(n<1048576) return (n/1024).toFixed(0)+' KB';
  if(n<1073741824) return (n/1048576).toFixed(1)+' MB';
  return (n/1073741824).toFixed(2)+' GB';
}
function delProject(pid){
  if(ARM_DEL!==pid){
    ARM_DEL=pid; renderProjects();
    clearTimeout(ARM_TIMER);
    ARM_TIMER=setTimeout(()=>{ if(ARM_DEL===pid){ ARM_DEL=''; renderProjects(); } },6000);
    return;
  }
  clearTimeout(ARM_TIMER); ARM_DEL='';
  doDeleteProject(pid);
}
async function doDeleteProject(pid){
  const r=await api('/api/project',{action:'delete',id:pid});
  if(!r.ok){toast(r.error||'删除失败','err'); loadProjects(); return}
  if(ACTIVE_PROJ===pid){ ACTIVE_PROJ=''; localStorage.removeItem('pm_proj'); }
  toast('已删除「'+(r.name||pid)+'」'+
        (r.dir_removed?('，项目目录一并删掉，释放 '+fmtBytes(r.freed_bytes))
                      :'（磁盘上本就没有这个项目目录）'),'ok');
  loadProjects();
}

/* ---- 后端 ---- */
async function testBackend(){
  const r=await api('/api/backend/test',{});
  const dot=el('backend-dot'), txt=el('backend-txt');
  dot.className='dot '+(r.ok?'on':'off');
  txt.textContent=r.ok?'后端可用':'后端不可用';
  toast(r.message,r.ok?'ok':'err');
}
async function testBackendSilent(){
  const r=await api('/api/backend/test',{});
  el('backend-dot').className='dot '+(r.ok?'on':'off');
}

window.onload=async()=>{
  bindNav(); bindFile(); bindMode(); loadParadigmOptions();
  await loadConfig();
  await loadProjects();          // 脚本页与合成页的项目下拉都靠这份数据
  loadVoices();
  loadModels();
  api('/api/backends').then(r=>{if(r.ok&&r.backend)el('backend-txt').textContent=r.backend+' · '+(r.model||'未设模型')});
  testBackendSilent();
  recalc();
  showTab(hashTab(),true);       // 首屏按 hash 落地，没有 hash 就是项目页
};
