# -*- coding: utf-8 -*-
"""生成 AIGC 标识落地报告页（自包含 HTML，音频内联为 base64）。"""
import base64
import os

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
OUT = os.path.join(HERE, "_aigc_probe")
RHYTHM = os.path.join(OUT, "ai_rhythm_short_long_short_short.wav")

with open(RHYTHM, "rb") as f:
    rhythm_b64 = base64.b64encode(f.read()).decode("ascii")

AIGC_JSON = ('{"AIGC":{"Label":"1","ContentProducer":"wUwproject",'
             '"ProduceID":"ep-20260911-113022","ReservedCode1":"",'
             '"ContentPropagator":"wUwproject",'
             '"PropagateID":"ep-20260911-113022","ReservedCode2":""}}')

HTML = u"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>音频 AIGC 标识落地实测报告</title>
<style>
  :root{
    --bar:#171b22; --bar-2:#1f242e; --ink:#1a1d23; --body:#2c3138;
    --dim:#6b7280; --line:#e3e6ec; --bg:#f7f8fa; --card:#ffffff;
    --ok:#0f7b4f; --bad:#b4322c; --warn:#a05a00; --code:#0f1319;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--body);
       font:15px/1.75 "Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif;
       -webkit-font-smoothing:antialiased}
  header{background:var(--bar);color:#eef1f5;padding:26px 34px 22px}
  header h1{margin:0 0 6px;font-size:21px;font-weight:600;letter-spacing:.4px}
  header .sub{color:#9aa4b2;font-size:13px}
  header .sub b{color:#cfd6e0;font-weight:600}
  .wrap{max-width:1040px;margin:0 auto;padding:26px 22px 60px}
  section{background:var(--card);border:1px solid var(--line);border-radius:8px;
          padding:22px 24px;margin:0 0 20px}
  h2{margin:0 0 14px;font-size:17px;color:var(--ink);font-weight:600;
     padding-bottom:10px;border-bottom:2px solid var(--bar)}
  h2 .n{display:inline-block;background:var(--bar);color:#fff;font-size:12px;
        padding:1px 8px;border-radius:4px;margin-right:9px;vertical-align:2px}
  h3{font-size:14.5px;color:var(--ink);margin:20px 0 9px;font-weight:600}
  h3:first-of-type{margin-top:6px}
  p{margin:9px 0}
  table{width:100%;border-collapse:collapse;margin:11px 0;font-size:13.5px}
  th,td{border:1px solid var(--line);padding:8px 11px;text-align:left;
        vertical-align:top}
  th{background:#f0f2f6;color:var(--ink);font-weight:600}
  tbody tr:nth-child(even){background:#fafbfc}
  code{font-family:Consolas,"SF Mono",Menlo,monospace;font-size:12.5px;
       background:#eef0f4;padding:1.5px 5px;border-radius:3px;color:#1f2733}
  pre{background:var(--code);color:#d7dee8;padding:14px 16px;border-radius:6px;
      overflow-x:auto;font-family:Consolas,"SF Mono",Menlo,monospace;
      font-size:12.5px;line-height:1.65;margin:11px 0}
  pre .k{color:#7fd1a8}
  .ok{color:var(--ok);font-weight:600}
  .bad{color:var(--bad);font-weight:600}
  .warn{color:var(--warn);font-weight:600}
  .tag{display:inline-block;font-size:11.5px;padding:1px 7px;border-radius:3px;
       font-weight:600;line-height:1.6}
  .t-ok{background:#e3f4ec;color:var(--ok)}
  .t-bad{background:#fbe9e8;color:var(--bad)}
  .t-warn{background:#fdf1e0;color:var(--warn)}
  .t-info{background:#e8eef8;color:#2a4a86}
  .lead{background:#f4f6fa;border-left:3px solid var(--bar);padding:13px 16px;
        border-radius:0 6px 6px 0;margin:12px 0}
  .lead b{color:var(--ink)}
  audio{width:100%;margin:10px 0 4px}
  ul{margin:9px 0;padding-left:22px}
  li{margin:5px 0}
  .bar-ok{display:inline-block;height:9px;background:var(--ok);border-radius:2px;
          vertical-align:middle}
  .bar-warn{display:inline-block;height:9px;background:var(--warn);border-radius:2px;
            vertical-align:middle}
  footer{color:var(--dim);font-size:12.5px;text-align:center;padding:8px 0 0}
  @media (max-width:640px){
    header{padding:20px 18px}
    .wrap{padding:16px 12px 40px}
    section{padding:16px 14px}
    table{font-size:12.5px}
    th,td{padding:6px 8px}
  }
</style>
</head>
<body>
<header>
  <h1>音频 AIGC 标识落地实测报告</h1>
  <div class="sub">
    依据 <b>《人工智能生成合成内容标识办法》</b>（2025-09-01 施行）与配套强制性国标
    <b>GB 45438-2025</b>（网络安全技术 人工智能生成合成内容标识方法）<br>
    实测对象：podcast-maker &nbsp;|&nbsp; 实测时间：09/16/2026 10:30
  </div>
</header>

<div class="wrap">

<section>
  <h2><span class="n">结论</span>先说结果</h2>
  <div class="lead">
    <b>1. 显式标识。</b>音频只有两条合规路径：<b>语音标识</b>（正常语速念一句含
    “人工智能/AI”+“生成/合成”的话）或<b>音频节奏标识</b>（播“短长短短”，即 AI 的摩斯码）。
    你说的“加一句本播客由 AI 合成”<b>完全合规</b>——两个要素都齐。<br><br>
    <b>2. 隐式标识。</b>在文件元数据里写一段固定格式 JSON，字段名或关键词里必须含
    <code>AIGC</code>。实测 <b>mp3 与 wav 写得进读得出</b>，<b>mp4 会静默丢弃</b>自定义键
    （退出码 0、无报错、读回什么都没有）——这条是最容易踩空的坑。<br><br>
    <b>3. 那三个 BGM。</b>不是白噪音，是<b>正弦叠加的和弦 pad</b>；但也<b>不是 AI 生成的</b>，
    是纯数学公式算出来的（无模型、无随机源）。数量也不是三个，是<b>四个</b>。
  </div>
</section>

<section>
  <h2><span class="n">一</span>显式标识：两条合规路径</h2>

  <h3>强标原文要求（GB 45438-2025 §5.3 音频内容显式标识）</h3>
  <table>
    <thead><tr><th style="width:78px">条款</th><th>要求</th></tr></thead>
    <tbody>
      <tr><td>a)</td><td>应采用<b>语音标识</b>或<b>音频节奏标识</b>（二者之一即可）</td></tr>
      <tr><td>b)</td><td>语音标识须同时含：<b>人工智能要素</b>（“人工智能”或“AI”）
          ＋ <b>生成合成要素</b>（“生成”和/或“合成”）</td></tr>
      <tr><td>c)</td><td>音频节奏标识应为<b>“短长短短”</b>的节奏（即 “AI” 的摩斯码 <code>·-··</code>）</td></tr>
      <tr><td>d)</td><td>位置：音频的<b>起始</b>／<b>末尾</b>／中间适当位置。起始＝内容开始<b>之前</b>，
          末尾＝内容结束<b>之后</b></td></tr>
      <tr><td>e)</td><td>语音标识应使用<b>正常语速</b>（汉语约 120 字/min～160 字/min）</td></tr>
      <tr><td>f)</td><td>节奏标识应<b>清晰可辨</b></td></tr>
    </tbody>
  </table>

  <h3>措辞对照</h3>
  <table>
    <thead><tr><th style="width:300px">口语内容</th><th style="width:120px">是否合规</th><th>说明</th></tr></thead>
    <tbody>
      <tr><td>本节目由 <b>AI 合成</b></td><td><span class="tag t-ok">合规</span></td>
          <td>AI ＋ 合成，两要素齐</td></tr>
      <tr><td>本播客由<b>人工智能生成</b></td><td><span class="tag t-ok">合规</span></td>
          <td>人工智能 ＋ 生成</td></tr>
      <tr><td>以下内容由<b>人工智能（AI）生成合成</b></td><td><span class="tag t-ok">合规</span></td>
          <td>措辞更周正，推荐</td></tr>
      <tr><td>本节目为虚拟主播</td><td><span class="tag t-bad">不合规</span></td>
          <td>既无“人工智能/AI”，也无“生成/合成”</td></tr>
      <tr><td>AI 播客</td><td><span class="tag t-bad">不合规</span></td>
          <td>有“AI”，缺“生成/合成”</td></tr>
    </tbody>
  </table>

  <h3>节奏标识·实物试听（本次实测生成）</h3>
  <p>单位时长 120 ms；短＝1 单位、长＝3 单位、间隔＝1 单位；总长 1.08 s。
     四个发声段实测为 0.12s / 0.36s / 0.12s / 0.12s，与“短长短短”一致。</p>
  <audio controls preload="metadata" src="data:audio/wav;base64,__RHYTHM__"></audio>
  <p style="font-size:12.5px;color:var(--dim)">
     用纯标准库正弦波合成（C6 / 1046.5 Hz），零新增依赖，与现有 BGM 是同一套加法合成写法。</p>
</section>

<section>
  <h2><span class="n">二</span>隐式标识：文件元数据怎么写</h2>

  <h3>规定格式（GB 45438-2025 附录 E，规范性）</h3>
  <pre>{"AIGC":{<span class="k">"Label"</span>:"value1",
         <span class="k">"ContentProducer"</span>:"value2",
         <span class="k">"ProduceID"</span>:"value3",
         <span class="k">"ReservedCode1"</span>:"value4",
         <span class="k">"ContentPropagator"</span>:"value5",
         <span class="k">"PropagateID"</span>:"value6",
         <span class="k">"ReservedCode2"</span>:"value7"}}</pre>

  <table>
    <thead><tr><th style="width:170px">字段</th><th style="width:170px">含义</th><th>取值与要求</th></tr></thead>
    <tbody>
      <tr><td><code>Label</code></td><td>生成合成标签</td>
          <td><b>三态</b>：<code>1</code> 属于、<code>2</code> 可能、<code>3</code> 疑似 AI 生成合成。字符串</td></tr>
      <tr><td><code>ContentProducer</code></td><td>生成合成服务提供者</td>
          <td>提供者名称或编码。字符串</td></tr>
      <tr><td><code>ProduceID</code></td><td>内容制作编号</td>
          <td>提供者对该内容的唯一编号（可用 UUID）。字符串</td></tr>
      <tr><td><code>ReservedCode1</code></td><td>预留 1</td>
          <td>供生成方自做安全防护（如数字签名）。字符串</td></tr>
      <tr><td><code>ContentPropagator</code></td><td>内容传播服务提供者</td>
          <td>提供者名称或编码。<b>首次写入时＝ContentProducer</b></td></tr>
      <tr><td><code>PropagateID</code></td><td>内容传播编号</td>
          <td>传播方对该内容的唯一编号。<b>首次写入时＝ProduceID</b></td></tr>
      <tr><td><code>ReservedCode2</code></td><td>预留 2</td>
          <td>供传播方自做安全防护。字符串</td></tr>
    </tbody>
  </table>
  <p style="font-size:13px">
    <span class="tag t-info">硬性</span>
    字段名称或关键词中<b>应包含 <code>AIGC</code></b>；一份文件<b>只保留一份</b>隐式标识；
    取值字符应主要由 GB 18030-2022 中除 <code>"</code> 与 <code>\\</code> 外的单字节可打印字符构成。
    数字水印属“内容隐式标识”，强标<b>不强制</b>（“准许采用”）。
  </p>

  <h3>本次写入→读回实测</h3>
  <p>写入值（<code>ContentPropagator</code> / <code>PropagateID</code> 按首次写入规则与生成方一致）：</p>
  <pre>__AIGC__</pre>
  <table>
    <thead><tr><th style="width:170px">容器</th><th style="width:150px">用 <code>AIGC</code> 作键名</th>
      <th style="width:150px">用 <code>comment</code> 作键名</th><th>结论</th></tr></thead>
    <tbody>
      <tr><td><b>mp3</b>（ID3v2.3 / 2.4）</td>
          <td><span class="tag t-ok">写入成功</span></td>
          <td><span class="tag t-ok">写入成功</span></td>
          <td>键名直接写 <code>AIGC</code>，ffprobe 读回键名即 <code>AIGC</code></td></tr>
      <tr><td><b>wav</b>（LIST INFO）</td>
          <td><span class="tag t-bad">被丢弃</span></td>
          <td><span class="tag t-ok">写入成功</span></td>
          <td>只能走 <code>comment</code>，靠值里的关键词满足“含 AIGC”</td></tr>
      <tr><td><b>mp4</b>（udta/ilst）</td>
          <td><span class="tag t-bad">静默丢弃</span></td>
          <td><span class="tag t-ok">写入成功</span></td>
          <td><b>最危险</b>：ffmpeg 接受参数、退出码 0、无任何警告，读回为空</td></tr>
    </tbody>
  </table>
  <p style="font-size:13px">
    JSON 在三种容器里都能被完整读回并解析，7 个字段一个不少。
  </p>
</section>

<section>
  <h2><span class="n">三</span>现有产物的元数据现状</h2>
  <table>
    <thead><tr><th style="width:330px">文件</th><th>当前元数据</th><th style="width:110px">AIGC 标识</th></tr></thead>
    <tbody>
      <tr><td><code>projects/20260911-113022/podcast.mp3</code></td>
          <td><code>encoder: Lavf63.1.101</code></td>
          <td><span class="tag t-bad">无</span></td></tr>
      <tr><td><code>projects/20260911-113022/final.mp4</code></td>
          <td><code>major_brand / minor_version / compatible_brands / encoder</code></td>
          <td><span class="tag t-bad">无</span></td></tr>
      <tr><td><code>bgm.wav</code>（合成产物）</td>
          <td>只有 <code>fmt</code> 与 <code>data</code> 两个 chunk，零元数据</td>
          <td><span class="tag t-bad">无</span></td></tr>
    </tbody>
  </table>
  <p>这三行全是 ffmpeg 自动塞的容器信息，与 AI 标识无关。整条流水线目前<b>不写任何标识</b>。</p>
</section>

<section>
  <h2><span class="n">四</span>内置 BGM：不是白噪音，也不是 AI 生成</h2>

  <div class="lead">
    <b>先说结论。</b>它是 <code>assets_factory.py:967</code> 的 <code>make_bgm()</code>，
    纯标准库写的一段<b>正弦叠加和弦 pad</b>——几个频率成整数比的正弦波叠在一起，
    再加一个缓慢的幅度颤音（LFO）和一层低八度铺底。
    <b>跟白噪音毫无关系</b>，但<b>也确实不是 AI 合成</b>：没有模型，没有任何随机数。
  </div>

  <h3>档位不是三个，是四个</h3>
  <table>
    <thead><tr><th style="width:100px">键</th><th style="width:70px">名称</th>
      <th style="width:80px">根音</th><th>和弦（实测换算出的音名）</th></tr></thead>
    <tbody>
      <tr><td><code>still</code></td><td>静水</td><td>130.81 Hz</td>
          <td>C3 · G3 · C4 　<span style="color:var(--dim)">空五度</span></td></tr>
      <tr><td><code>pensive</code></td><td>沉思</td><td>130.81 Hz</td>
          <td>C3 · E3 · G3 · C4 · E4 · G4 　<span style="color:var(--dim)">C 大三和弦</span></td></tr>
      <tr><td><code>bright</code></td><td>轻快</td><td>146.83 Hz</td>
          <td>D3 · F#3 · A3 · C#4 · D4 · F#4 · A4 　<span style="color:var(--dim)">D 大七和弦</span></td></tr>
      <tr><td><code>deep</code></td><td>深沉</td><td>98.00 Hz</td>
          <td>G2 · D3 · G3 · A#3 　<span style="color:var(--dim)">G 小三和弦</span></td></tr>
    </tbody>
  </table>
  <p style="font-size:13px">实测峰值频率与理论值的偏差都在 ±1 音分以内，是精确的整数比谐波关系。</p>

  <h3>物理量对照（本次实测）</h3>
  <table>
    <thead><tr><th style="width:150px">样本</th><th style="width:130px">谱平坦度</th>
      <th style="width:120px">过零率</th><th>能量分布</th></tr></thead>
    <tbody>
      <tr><td><b>真白噪音</b>（对照）</td>
          <td>0.8451 <span class="bar-warn" style="width:60px"></span></td>
          <td>0.5011</td>
          <td>60Hz–2kHz 占 17.0%，6kHz–11kHz 占 45.9%</td></tr>
      <tr><td>静水 still</td><td>0.0041</td><td>0.0119</td><td>60Hz–2kHz 占 100.0%，高频 0.0%</td></tr>
      <tr><td>沉思 pensive</td><td>0.0030</td><td>0.0165</td><td>60Hz–2kHz 占 100.0%，高频 0.0%</td></tr>
      <tr><td>轻快 bright</td><td>0.0035</td><td>0.0189</td><td>60Hz–2kHz 占 100.0%，高频 0.0%</td></tr>
      <tr><td>深沉 deep</td><td>0.0034</td><td>0.0168</td><td>60Hz–2kHz 占 89.7%，高频 0.0%</td></tr>
    </tbody>
  </table>
  <ul>
    <li><b>谱平坦度</b>是判断“噪音还是音调”的硬指标：白噪音全频段等能量，趋近 1；
        纯正弦趋近 0。实测两者差 <b>200～280 倍</b>。</li>
    <li><b>过零率</b>差 26～42 倍——白噪音每秒穿零上万次，这里是几个低频正弦。</li>
    <li><b>能量分布</b>：BGM 的高频段（6kHz 以上）能量<b>精确为 0</b>；白噪音有 45.9% 在那里。</li>
    <li><b>确定性检验</b>：同一档位连生两次，两个文件 SHA-256 完全一致——
        说明代码里没有任何随机源，是纯确定性公式。</li>
    <li><b>容器</b>：WAV 头只有 <code>fmt</code> 和 <code>data</code> 两个 chunk，不带任何元数据。</li>
  </ul>

  <h3>它算不算“AI 生成合成内容”</h3>
  <p>按《标识办法》第三条与强标 3.1 的定义，“人工智能生成合成内容”指<b>利用人工智能技术</b>
     生成、合成的内容。这段 BGM 用的是正弦公式，不涉及任何 AI 技术，<b>单看它不构成 AI 生成内容</b>。
     但成品的播客人声是 Qwen3-TTS 合成的，整条音轨仍属 AI 生成内容——<b>该标的还得标</b>。</p>
</section>

<section>
  <h2><span class="n">五</span>你是“用户”还是“服务提供者”</h2>
  <table>
    <thead><tr><th style="width:280px">场景</th><th style="width:190px">你的身份</th><th>义务</th></tr></thead>
    <tbody>
      <tr><td>自己用 podcast-maker 做节目并发布</td>
          <td><span class="tag t-info">用户</span></td>
          <td>办法第十条：发布时<b>主动声明</b>，并使用平台提供的标识功能进行标识</td></tr>
      <tr><td>把 podcast-maker 交给别人用（含开源分发）</td>
          <td><span class="tag t-warn">生成合成服务提供者</span></td>
          <td>强标 3.7 定义涵盖“组织或个人”通过可编程接口等方式向公众提供生成合成服务；
              第四、五条的<b>显式＋隐式标识</b>义务全在工具侧</td></tr>
    </tbody>
  </table>
  <p>工具一旦对外分发，标识就不该是使用者手动补的，而要写进生成管线。</p>
</section>

<section>
  <h2><span class="n">六</span>落地建议（尚未改动产品代码）</h2>
  <table>
    <thead><tr><th style="width:120px">项</th><th style="width:120px">落点</th><th>做法</th></tr></thead>
    <tbody>
      <tr><td>显式·语音</td><td><code>script_engine.py</code><br><code>pin_intro_outro()</code></td>
          <td>复用现成的“首末句写死”机制，在片头或片尾固定一句标识语。
              注意 <code>gate.max_chars</code> 单句 40 字上限</td></tr>
      <tr><td>显式·节奏</td><td><code>assets_factory.py</code><br>与 <code>make_bgm()</code> 同级</td>
          <td>加一个 <code>make_ai_rhythm()</code>，同一套纯标准库加法合成，零新增依赖；
              拼在最前或最后。本次已跑通（见试听）</td></tr>
      <tr><td>隐式</td><td><code>audio_engine.to_encoded()</code><br>及视频合成收尾</td>
          <td>追加 <code>-metadata</code>：<b>mp3/wav 用 <code>AIGC</code> 键</b>，
              <b>mp4 必须用 <code>comment</code> 键</b>（自定义键会被静默丢弃）。
              建议做成配置开关</td></tr>
    </tbody>
  </table>
  <p style="font-size:13px">
    以上均未动手，等口令。本轮只新增了两个诊断脚本与一个报告页，产品代码零改动。
  </p>
</section>

<footer>
  podcast-maker &nbsp;|&nbsp; 诊断脚本：<code>tools/probes/probe_bgm.py</code>、
  <code>tools/probes/probe_aigc_meta.py</code> &nbsp;|&nbsp; 09/16/2026 10:30
</footer>

</div>
</body>
</html>
"""

HTML = HTML.replace("__RHYTHM__", rhythm_b64).replace("__AIGC__", AIGC_JSON)

dst = os.path.join(OUT, "aigc_labeling_report.html")
with open(dst, "w", encoding="utf-8") as f:
    f.write(HTML)
print("已生成：%s（%.1f KB）" % (dst, os.path.getsize(dst) / 1024.0))
