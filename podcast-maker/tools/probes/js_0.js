
let CFG=null, SCRIPT=[], CUR_JOB=null, VOICES=[], PROJECTS=[], EMOTIONS=[];
/* 两页各一份「可选的期」。EPISEL 是勾上的那些——勾几期就做几期。
   顺序即勾选顺序不保证，所以实际按 EPISODES 的顺序取，跟地图一致。 */
let EPISODES={s:[],r:[]}, EPISEL={s:new Set(),r:new Set()}, LOADED_EP='';
function picked(which){return (EPISODES[which]||[]).filter(x=>EPISEL[which].has(x.no)).map(x=>x.no)}
const $=s=>document.querySelector(s), $$=s=>Array.from(document.querySelectorAll(s));
const el=id=>document.getElementById(id);

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function toast(msg,kind){
  const d=document.createElement('div');
  d.className='toast '+(kind||'');
  d.textContent=msg; el('toasts').appendChild(d);
  setTimeout(()=>{d.style.opacity='0';setTimeout(()=>d.remove(),200)},kind==='err'?6000:3200);
}
function modalBox(){return el('mask').querySelector('.modal')}
function modal(title,body,onOk,okText){
  modalBox().classList.remove('wide'); el('m-body').classList.remove('wide');
  el('m-title').textContent=title; el('m-body').textContent=body;
  el('m-ok').textContent=okText||'确定';
  el('mask').classList.add('on');
  el('m-ok').onclick=()=>{closeModal();onOk&&onOk()};
}
/* 需要放 HTML 的模态框：地图表格、素材清单这类。wide 给更大的宽度与更高的内容区。 */
function modalHtml(title,html,onOk,okText,wide){
  modalBox().classList.toggle('wide',!!wide);
  el('m-body').classList.toggle('wide',!!wide);
  el('m-title').textContent=title;
  el('m-body').innerHTML=html;
  el('m-ok').textContent=okText||'确定';
  el('mask').classList.add('on');
  el('m-ok').onclick=()=>{closeModal();onOk&&onOk()};
}
function closeModal(){el('mask').classList.remove('on')}
function modalInput(title,body,def,onOk,okText){
  modalBox().classList.remove('wide'); el('m-body').classList.remove('wide');
  el('m-title').textContent=title;
  el('m-body').innerHTML='<div style="margin-bottom:10px">'+esc(body)+'</div>'+
    '<input type="text" id="m-input" value="'+esc(def==null?'':def)+'">';
  el('m-ok').textContent=okText||'确定';
  el('mask').classList.add('on');
  el('m-ok').onclick=()=>{const v=el('m-input').value;closeModal();onOk&&onOk(v)};
  const inp=el('m-input'); if(inp){inp.focus();inp.select()}
}
async function api(path,body){
  const opt=body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{};
  const r=await fetch(path,opt);
  return await r.json();
}
function toggleSw(node){node.classList.toggle('on')}

/* ---- 控件登记：同一个点位可能在多处出现，值必须同步 ---- */
const REG={};
function cid(stage,key){return 'f_'+stage+'_'+key.replace(/\./g,'__')}
function vid(stage,key){return 'v_'+stage+'_'+key.replace(/\./g,'__')}
function reg(stage,key,item){item.stage=stage;(REG[key]=REG[key]||[]).push(item)}
/* 一次登记只在节点还挂在文档里时有效。清空容器后旧节点就脱离了文档，
   此时把它们从登记表摘掉——既不会重复堆叠，也不会留下读不到值的空壳。
   早先按 stage 名清理，两个容器共用一个 stage 名，后一次清理会把前一次
   刚登记的控件一起摘掉，于是跨页的同名点位不再同步。 */
function pruneReg(){
  for(const k in REG) REG[k]=REG[k].filter(n=>n.node&&n.node.isConnected);
}
function syncKey(key,v){
  (REG[key]||[]).forEach(n=>{
    if(n.kind==='sw') n.node.classList.toggle('on',!!v);
    else if(n.node){
      if(n.kind==='range'&&n.val) n.val.textContent=fmtVal(n.spec,v);
      // 模型下拉不能直接塞 value：名字不在候选里时 select 会被清成空选中。
      // 一律走重填，把当前值本身作为一项补进去。
      if(n.spec&&n.node.dataset&&n.node.dataset.models){
        n.node.dataset.cur=(v==null?'':String(v));
        fillModelSelect(n.node);
      }else if(String(n.node.value)!==String(v==null?'':v)) n.node.value=(v==null?'':v);
    }
  });
}
function syncAll(){if(!CFG)return;for(const k in REG) syncKey(k,CFG.values[k])}

function decimalsOf(spec){
  const st=String(spec.step==null?1:spec.step);
  const i=st.indexOf('.');
  return i<0?0:(st.length-i-1);
}
function fmtNum(spec,v){
  if(spec.type==='float') return Number(v).toFixed(decimalsOf(spec));
  return String(v);
}
function fmtVal(spec,v){
  if(v==null||v==='') return '';
  if(spec.type==='int'||spec.type==='float'){
    let s=fmtNum(spec,v);
    if(spec.unit) s+=' '+spec.unit;
    if(spec.unit==='分钟'){
      const tot=Math.round(Number(v)*60);
      s+='　'+Math.floor(tot/60)+' 分 '+String(tot%60).padStart(2,'0')+' 秒';
    }
    return s;
  }
  if(spec.type==='enum'){
    const o=(spec.options||[]).find(x=>String(x.value)===String(v));
    return o?o.label:String(v);
  }
  return '';
}

function control(stage,key,spec){
  const wrap=document.createElement('div');
  const wide=(spec.type==='text'||spec.type==='path');
  wrap.className='f'+(wide?' wide':'');
  const id=cid(stage,key);
  const v=CFG.values[key];

  if(spec.type==='bool'){
    wrap.innerHTML='<label class="sw'+(v?' on':'')+'" id="'+id+'" onclick="toggleSw(this)">'+
      '<i></i><span>'+esc(spec.label)+'</span></label>'+
      (spec.help?'<div class="desc">'+esc(spec.help)+'</div>':'');
    // 在 wrap 内部找，不能用 getElementById：此刻 wrap 还没进文档树，
    // 文档里根本没有这个 id，查回来是 null，于是控件注册成空节点、
    // 事件也没绑上——滑块滑了不生效就是这个形状。
    const sw=wrap.querySelector('label');
    sw.onclick=()=>{put(key,sw.classList.contains('on'))};
    reg(stage,key,{kind:'sw',node:sw,spec:spec});
    return wrap;
  }

  let h='<label><span>'+esc(spec.label)+'</span><span class="val" id="'+vid(stage,key)+'"></span></label>';

  if(spec.type==='enum'||spec.options_source==='voices'){
    let opts=spec.options||[];
    if(spec.options_source==='voices'){
      opts=VOICES.length?VOICES.map(x=>({value:x.name,label:x.label||x.name}))
                        :[{value:v,label:String(v)}];
    }
    // 白名单里没装的项由后端标 off：照列不误，但置 disabled。
    // 暗显表达「不可选」，不再另写「未安装」字样——那是同一件事说两遍。
    h+='<select id="'+id+'">'+opts.map(o=>'<option value="'+esc(String(o.value))+'"'+
      (o.off?' disabled':'')+
      (String(o.value)===String(v)?' selected':'')+'>'+esc(o.label)+
      (o.desc?' · '+esc(o.desc):'')+'</option>').join('')+'</select>';
  }else if(spec.options_source==='models'){
    // 模型名用下拉（select）。从前这里是「输入框 + datalist」——datalist 是浏览器的
    // **补全候选**，它拿框里已有的字去筛：框里填着 qwen/qwen3.5-35b-a3b 时，15 个
    // 候选只剩含这串字的那一个，看着就像"下拉拉不出来"。select 才是点开列全部。
    // 列表里没有的名字从末项「手动填写…」进，这条能力不丢。
    h+='<select id="'+id+'" data-models="1"></select>';
  }else if(spec.type==='int'||spec.type==='float'){
    const step=spec.step||(spec.type==='int'?1:0.1);
    if(spec.min!==undefined&&spec.max!==undefined){
      h+='<input type="range" id="'+id+'" min="'+spec.min+'" max="'+spec.max+
         '" step="'+step+'" value="'+v+'">'+
         '<div class="scale"><span>'+fmtNum(spec,spec.min)+'</span><span>'+fmtNum(spec,spec.max)+'</span></div>';
    }else{
      h+='<input type="number" id="'+id+'" value="'+v+'">';
    }
  }else if(spec.type==='text'){
    // help 已经作为常驻说明渲染在下方，再塞进 placeholder 就是同一句话写两遍：
    // 输入框里一句、框下面又一句。
    h+='<textarea id="'+id+'">'+esc(v||'')+'</textarea>';
    if(spec.help) h+='<div class="desc">'+esc(spec.help)+'</div>';
  }else{
    h+='<input type="text" id="'+id+'" value="'+esc(v==null?'':v)+'"'+
       (spec.type==='path'?' placeholder="留空即不使用"':'')+'>';
  }
  wrap.innerHTML=h;

  const node=wrap.querySelector('[id="'+id+'"]');
  const val=wrap.querySelector('[id="'+vid(stage,key)+'"]');
  if(val) val.textContent=fmtVal(spec,v);
  if(node){
    if(node.type==='range'){
      node.oninput=()=>{if(val)val.textContent=fmtVal(spec,node.value)};
      node.onchange=()=>put(key,spec.type==='int'?parseInt(node.value,10):parseFloat(node.value));
    }else if(spec.type==='int'||spec.type==='float'){
      node.onchange=()=>put(key,spec.type==='int'?parseInt(node.value,10):parseFloat(node.value));
    }else{
      node.onchange=()=>put(key,node.value);
    }
    if(spec.options_source==='models') wireModelSelect(node,key,v);
  }
  reg(stage,key,{kind:node&&node.type==='range'?'range':'ctl',node:node,val:val,spec:spec});
  return wrap;
}

function valOf(key){
  const a=REG[key]||[];
  if(!a.length) return undefined;
  const n=a[0];
  if(n.kind==='sw') return n.node.classList.contains('on');
  const spec=n.spec||{};
  if(spec.type==='int') return parseInt(n.node.value,10);
  if(spec.type==='float') return parseFloat(n.node.value);
  if(spec.type==='bool') return n.node.classList.contains('on');
  return n.node.value;
}
async function put(key,value){
  const r=await api('/api/config',{patch:{[key]:value}});
  if(r.rejected&&r.rejected.length) toast('写入被拒：'+r.rejected.join('；'),'err');
  else if(r.errors&&r.errors.length) toast(r.errors[0],'err');
  else if(r.warnings&&r.warnings.length) toast(r.warnings[0],'');
  if(r.ok){
    if(r.values) for(const k in r.values){CFG.values[k]=r.values[k];syncKey(k,r.values[k])}
    else {CFG.values[key]=value;syncKey(key,value)}
  }else{
    await loadConfig(); syncAll();
  }
  if(key.indexOf('audio.')===0) renderAudioWarn();
  // 换了后端或地址，模型列表整个作废，得重新问一遍才准
  if(key==='llm.backend'||key==='llm.base_url') loadModels();
  if(key.indexOf('tts.speed')===0||key==='script.target_minutes') recalcSoon();
}
let recalcTimer=null;
function recalcSoon(){clearTimeout(recalcTimer);recalcTimer=setTimeout(()=>{if(SCRIPT.length)recalc()},700)}

/* ---- 渲染 ---- */
function orderSections(keys){
  const ord=(CFG.ui.section_order||[]);
  return keys.slice().sort((a,b)=>{
    const ia=ord.indexOf(a), ib=ord.indexOf(b);
    if(ia<0&&ib<0) return a<b?-1:1;
    if(ia<0) return 1; if(ib<0) return -1;
    return ia-ib;
  });
}
function secLabel(s){return (CFG.ui.section_labels||{})[s]||s}

function put2(target,keys){
  const box=(typeof target==='string')?el(target):target;
  if(!box) return;
  box.innerHTML=''; pruneReg();
  keys.forEach(k=>{
    const spec=specOf(k); if(!spec) return;
    box.appendChild(control('quick',k,spec));
  });
}
function specOf(key){
  for(const st in CFG.params){
    for(const s in CFG.params[st]){
      const f=CFG.params[st][s].find(x=>x.key===key);
      if(f) return f;
    }
  }
  return null;
}
function renderConfig(){
  const box=el('stage-config'); if(!box) return;
  box.innerHTML=''; pruneReg();
  const groups=CFG.params.config||{};
  const secs=orderSections(Object.keys(groups));
  secs.forEach(sec=>{
    const card=document.createElement('div');
    card.className='card';
    card.innerHTML='<h2>'+esc(secLabel(sec))+'</h2>'+
      (CFG.ui.section_note[sec]?'<p class="note">'+esc(CFG.ui.section_note[sec])+'</p>':'');
    const g=document.createElement('div'); g.className='grid';
    (groups[sec]||[]).forEach(item=>g.appendChild(control('cfg',item.key,item)));
    card.appendChild(g); box.appendChild(card);
  });
  const total=Object.keys(CFG.values).length;
  el('cfg-sum').textContent=total+' 项 · '+secs.length+' 个分区';
  const w=[]; (CFG.warnings||[]).forEach(m=>w.push('<div class="warn" style="font-size:12px">'+esc(m)+'</div>'));
  (CFG.errors||[]).forEach(m=>w.push('<div class="bad" style="font-size:12px">'+esc(m)+'</div>'));
  el('cfg-warn').innerHTML=w.join('');
  patchModelNote();
}
function renderQuick(){
  // 称呼与语速跟脚本同时生效：称呼直接写进提示词，模型据此称呼两位说话人。
  // 放在这里是为了「想改名就在脚本页改得到」，不必翻到配置页去找。
  put2('quick-script',['script.target_minutes','script.style_preset',
                       'tts.name_a','tts.name_b','tts.speed_a','tts.speed_b']);
  put2('quick-render',['video.fps','video.produce_vertical','subtitle.preset','background.preset',
                       'animation.mode','audio.bitrate_kbps']);
}
function renderCalib(){
  const box=el('calib-box'); if(!box) return;
  const rows=CFG.calibration||[];
  if(!rows.length){box.innerHTML='<div class="empty">尚无实测样本。合成一次后自动生成。</div>';return;}
  let h='<div class="tbl-scroll"><table><thead><tr><th>音色</th><th class="num">语速</th><th class="num">k（有效字/秒）</th><th class="num">样本</th><th>模型</th></tr></thead><tbody>';
  rows.forEach(r=>{h+='<tr><td>'+esc(r.voice)+'</td><td class="num">'+r.speed.toFixed(2)+'</td><td class="num">'+r.k+'</td><td class="num">'+r.samples+'</td><td>'+esc(r.mode)+'</td></tr>'});
  box.innerHTML=h+'</tbody></table></div>';
}
