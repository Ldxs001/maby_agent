/* 验证 applyEngineScope 的归属判定：跑的是 web_ui.py 里的真实函数体。
   从源码里抠出函数，喂假的 REG / cfgVal，检查它把哪一组收起了。
   不碰服务端，也就不会改到用户正在用的配置。*/
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');
const py = fs.readFileSync(path.join(root, 'podcast_maker', 'web_ui.py'), 'utf8');

// 主脚本段（第二段 <script>）
const a = py.indexOf('<script>');
const b = py.indexOf('<script>', a + 1);
const js = py.slice(b + '<script>'.length, py.indexOf('</script>', b));

const start = js.indexOf('function applyEngineScope()');
if (start < 0) { console.error('FAIL 源码里找不到 applyEngineScope'); process.exit(1); }
// 取到函数结束：从起点往后数花括号配平
let depth = 0, end = -1;
for (let i = js.indexOf('{', start); i < js.length; i++) {
  if (js[i] === '{') depth++;
  else if (js[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
}
const fnSrc = js.slice(start, end);

const makeRow = () => ({ style: { display: '' } });
const makeNode = (row) => ({ closest: (sel) => (sel === '.f' ? row : null) });

function scenario(engine, keys) {
  const REG = {}, rows = {};
  for (const key of keys) {
    const scope = key.indexOf('qwen3tts') >= 0 ? 'qwen3tts' : 'edge';
    const row = makeRow();
    rows[key] = row;
    REG[key] = [{ node: makeNode(row), spec: { engine_scope: scope } }];
  }
  const cfgVal = () => engine;
  const run = new Function('REG', 'cfgVal', fnSrc + '; applyEngineScope();');
  run(REG, cfgVal);
  return rows;
}

const KEYS = ['tts.voice_a', 'tts.voice_b', 'tts.qwen3tts_voice_a', 'tts.qwen3tts_voice_b'];
let bad = 0;
for (const [engine, wantLocal] of [['qwen3tts', true], ['edge', false]]) {
  const rows = scenario(engine, KEYS);
  for (const key of KEYS) {
    const isLocalKey = key.indexOf('qwen3tts') >= 0;
    const shouldShow = wantLocal ? isLocalKey : !isLocalKey;
    const shown = rows[key].style.display !== 'none';
    const ok = shown === shouldShow;
    if (!ok) bad++;
    console.log('  %s  引擎=%s  %s  期望%s  实得%s',
      ok ? 'PASS' : 'FAIL', engine, key.padEnd(22), shouldShow ? '露出' : '收起',
      shown ? '露出' : '收起');
  }
}
console.log(bad === 0 ? '\n两条引擎下归属判定全部正确（8/8）'
                      : '\n有 ' + bad + ' 项判定错误');
process.exit(bad === 0 ? 0 : 1);
