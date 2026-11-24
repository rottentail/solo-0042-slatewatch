"""生成自包含、可双击打开的盲标练习 HTML（只含参训者可见信息）。"""
from __future__ import annotations

import json

from . import model

_PRACTICE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Slatewatch 声画盲标练习 · 盲包</title>
<style>
  :root{--bg:#0b1220;--card:#121c2e;--ink:#e7eefc;--mut:#9db0d0;--line:#26344f;
        --acc:#5b9dff;--acc2:#22c997;--warn:#f0b429;}
  *{box-sizing:border-box}
  body{margin:0;background:linear-gradient(180deg,#0a1120,#0b1220 240px);color:var(--ink);
       font:15px/1.7 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",Roboto,sans-serif;}
  header.top{max-width:980px;margin:0 auto;padding:28px 20px 8px}
  header.top h1{font-size:24px;margin:0 0 6px}
  header.top p{color:var(--mut);margin:6px 0}
  .bar{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 0}
  .bar a,.bar button{background:#1b2942;color:var(--ink);border:1px solid var(--line);
       padding:8px 14px;border-radius:9px;text-decoration:none;cursor:pointer;font-size:14px}
  .bar a:hover,.bar button:hover{border-color:var(--acc)}
  main{max-width:980px;margin:0 auto;padding:12px 20px 80px}
  .notice{background:#10233b;border:1px solid #1d416b;border-radius:12px;padding:14px 16px;margin:16px 0;color:#cfe2ff}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
        padding:18px 18px 20px;margin:22px 0;box-shadow:0 10px 30px rgba(0,0,0,.25)}
  .card h2{margin:0;font-size:19px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .pill{font-size:12px;color:#0b1220;background:var(--acc2);border-radius:999px;padding:2px 10px;font-weight:700}
  .pill.neutral{background:#cdd9ef}
  .scene{color:var(--mut);margin:10px 0 6px}
  table.record{width:100%;border-collapse:collapse;margin:10px 0 6px;font-size:14px}
  table.record th,table.record td{border:1px solid var(--line);padding:7px 9px;vertical-align:top;text-align:left}
  table.record th{background:#0f1a2c;color:#cfe0ff}
  table.record td.t{white-space:nowrap;font-variant-numeric:tabular-nums;color:#bcd2ff;width:86px}
  .quiet{color:var(--warn);font-size:13.5px;margin:6px 0 0}
  .player{display:grid;grid-template-columns:minmax(0,1fr);gap:12px;margin-top:12px}
  .stage{position:relative;background:#000;border:1px solid var(--line);border-radius:12px;overflow:hidden}
  canvas{display:block;width:100%;height:auto;image-rendering:pixelated;background:#000}
  .controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}
  .seg button{background:#0f1a2c;color:var(--ink);border:0;padding:9px 18px;cursor:pointer;font-size:15px}
  .seg button.active{background:var(--acc);color:#061226;font-weight:700}
  .controls button.act{background:#1b2942;border:1px solid var(--line);color:var(--ink);
        padding:9px 16px;border-radius:10px;cursor:pointer;font-size:14px}
  .controls button.act:hover{border-color:var(--acc)}
  input.seek{flex:1 1 200px;accent-color:var(--acc)}
  .tc{color:var(--mut);font-variant-numeric:tabular-nums;font-size:13px;min-width:96px;text-align:right}
  .waves{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .waves figure{margin:0;border:1px solid var(--line);border-radius:12px;padding:8px;background:#0d1728}
  .waves figcaption{font-size:13px;color:var(--mut);margin-bottom:4px;display:flex;justify-content:space-between}
  .waves figure.sel{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc) inset}
  .wave{width:100%;height:auto;display:block}
  .hint{font-size:13px;color:var(--mut)}
  footer{max-width:980px;margin:0 auto;padding:0 20px 60px;color:var(--mut);font-size:13px}
  @media (max-width:720px){.waves{grid-template-columns:1fr}}
</style>
</head>
<body>
<header class="top">
  <h1>Slatewatch 声画关系盲标练习</h1>
  <p>你拿到的只有：短段画面、两条等长试听（甲 / 乙）和一份制作记录。流号、流名细节与覆盖情况在复盘前不予提供。</p>
  <div class="bar">
    <a href="参训者说明.md" target="_blank" rel="noopener">参训说明</a>
    <a href="制作记录.md" target="_blank" rel="noopener">完整制作记录（文本）</a>
    <a href="答题卡.html" target="_blank" rel="noopener">打开 / 打印答题卡</a>
    <button type="button" onclick="window.print()">打印本页</button>
  </div>
  <div class="notice">
    做法：先读制作记录里“什么时间的画面动作应当听到什么现场声”，再分别播放 <b>甲</b>、<b>乙</b>，
    对照白闪 / 接触瞬间与打点声是否同步。每条都可以判 <b>甲</b>、<b>乙</b> 或 <b>无法确认</b>。
    不要根据谁排在前、开头是否对齐、有没有全零波形或文件大小下结论——这些都已被刻意抹平。
  </div>
</header>
<main>
__CARDS_HTML__
</main>
<footer>
  本页为离线自包含盲包，媒体已内嵌，可直接双击打开，也可断网使用。请独立作答后再参加复盘；
  复盘材料由培训主持单独保管。
</footer>
<script type="application/json" id="lab-data">
__DATA_JSON__
</script>
<script>
(function(){
  "use strict";
  var DATA = JSON.parse(document.getElementById('lab-data').textContent);
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var currentPrimary = null;

  function fmt(t){ return (Math.max(0,t)).toFixed(2); }

  cards.forEach(function(card){
    var bid = card.getAttribute('data-bid');
    var cfg = DATA[bid];
    var canvas = card.querySelector('canvas');
    var ctx = canvas.getContext('2d');
    var img = new Image();
    img.src = cfg.film;
    var aud = { jia: new Audio(), yi: new Audio() };
    aud.jia.preload = aud.yi.preload = 'auto';
    aud.jia.src = cfg.jia; aud.yi.src = cfg.yi;

    var label = 'jia';
    var seek = card.querySelector('input.seek');
    var tc = card.querySelector('.tc');
    var playBtn = card.querySelector('button.play');
    var segBtns = Array.prototype.slice.call(card.querySelectorAll('.seg button'));
    var figs = { jia: card.querySelector('figure[data-wav="jia"]'),
                 yi: card.querySelector('figure[data-wav="yi"]') };

    function active(){ return aud[label]; }
    function duration(){ return cfg.duration; }

    function draw(){
      var a = active();
      var t = a && !isNaN(a.currentTime) ? a.currentTime : 0;
      var f = Math.min(cfg.frames - 1, Math.max(0, Math.floor(t * cfg.fps)));
      var sx = (f % cfg.cols) * cfg.w;
      var sy = Math.floor(f / cfg.cols) * cfg.h;
      ctx.fillStyle = '#000'; ctx.fillRect(0,0,cfg.w,cfg.h);
      if (img.complete && img.naturalWidth > 0){
        ctx.drawImage(img, sx, sy, cfg.w, cfg.h, 0, 0, cfg.w, cfg.h);
      }
      seek.value = Math.min(duration(), t);
      tc.textContent = fmt(t) + ' / ' + duration().toFixed(2);
    }

    function setLabel(next){
      var old = active();
      var t = old.currentTime || 0;
      var wasPlaying = !old.paused;
      old.pause();
      label = next;
      var a = active();
      try { a.currentTime = t; } catch(e){ a.addEventListener('loadedmetadata', function(){ a.currentTime = t; }); }
      segBtns.forEach(function(b){ b.classList.toggle('active', b.getAttribute('data-lab') === label); });
      Object.keys(figs).forEach(function(k){ figs[k].classList.toggle('sel', k === label); });
      if (wasPlaying){ a.play().catch(function(){}); playBtn.textContent = '暂停'; }
      draw();
    }

    segBtns.forEach(function(b){
      b.addEventListener('click', function(){ setLabel(b.getAttribute('data-lab')); });
    });

    playBtn.addEventListener('click', function(){
      var a = active();
      if (a.paused){
        if (currentPrimary && currentPrimary !== card){ currentPrimary.__pause && currentPrimary.__pause(); }
        currentPrimary = card;
        a.play().catch(function(){});
        playBtn.textContent = '暂停';
      } else {
        a.pause(); playBtn.textContent = '播放';
      }
    });

    card.querySelector('button.restart').addEventListener('click', function(){
      Object.keys(aud).forEach(function(k){ try{ aud[k].currentTime = 0; }catch(e){} });
      draw();
    });

    seek.addEventListener('input', function(){
      var v = parseFloat(seek.value);
      Object.keys(aud).forEach(function(k){ try{ aud[k].currentTime = v; }catch(e){} });
      draw();
    });

    Object.keys(aud).forEach(function(k){
      aud[k].addEventListener('timeupdate', draw);
      aud[k].addEventListener('ended', function(){ playBtn.textContent = '播放'; });
      aud[k].addEventListener('loadedmetadata', draw);
    });

    card.__pause = function(){ Object.keys(aud).forEach(function(k){ aud[k].pause(); });
                              playBtn.textContent = '播放'; };
    img.addEventListener('load', draw);
    draw();
    (function loop(){ draw(); requestAnimationFrame(loop); })();
  });
})();
</script>
</body>
</html>
"""


def _record_rows(item: model.Item) -> str:
    rows = []
    rows.append(
        "<tr><th>时间</th><th>画面动作（应当看到）</th><th>应当同步听到的现场声</th></tr>"
    )
    for hit in item.correct:
        st = model.STATIONS[hit.station]
        visual = f"滑锤 / 击槌接触{st['shape']}，接触瞬间白闪"
        rows.append(
            f"<tr><td class='t'>{hit.t:0.2f} s</td><td>{visual}</td>"
            f"<td>{st['sound']}</td></tr>"
        )
    for extra in item.extra_record:
        rows.append(f"<tr><td class='t'>—</td><td colspan='2' class='hint'>{extra}</td></tr>")
    return "<table class='record'>" + "".join(rows) + "</table>"


def card_html(item: model.Item, svg_jia: str, svg_yi: str) -> str:
    return f"""
<section class="card" data-bid="{item.bid}">
  <h2>{item.bid} <span class="pill neutral">短段画面 · 两条等长试听</span></h2>
  <p class="scene"><b>画面：</b>{item.scene}</p>
  {_record_rows(item)}
  <p class="quiet">制作记录另注：{model.PLATEAUS['L1'][0]:.2f}–{model.PLATEAUS['L1'][1]:.2f}s 与
     {model.PLATEAUS['L2'][0]:.2f}–{model.PLATEAUS['L2'][1]:.2f}s 是两段等长的静默 / 留白，
     记录没有说明它们是“录到的安静”还是“没有覆盖”，试听波形上二者都是全零平台。</p>
  <div class="player">
    <div class="stage"><canvas width="{model.WIDTH}" height="{model.HEIGHT}"></canvas></div>
    <div class="controls">
      <span class="seg">
        <button type="button" class="active" data-lab="jia">试听 甲</button>
        <button type="button" data-lab="yi">试听 乙</button>
      </span>
      <button type="button" class="act play">播放</button>
      <button type="button" class="act restart">回到开头</button>
      <input class="seek" type="range" min="0" max="{model.DURATION_S}" step="0.01" value="0" aria-label="播放进度">
      <span class="tc">0.00 / {model.DURATION_S:.2f}</span>
    </div>
    <div class="waves">
      <figure data-wav="jia" class="sel"><figcaption><span>甲 · 等长试听</span><span>全零平台 ×2</span></figcaption>{svg_jia}</figure>
      <figure data-wav="yi"><figcaption><span>乙 · 等长试听</span><span>全零平台 ×2</span></figcaption>{svg_yi}</figure>
    </div>
    <p class="hint">提示：可在“甲 / 乙”之间切换，进度会保持；请重点核对白闪瞬间是否正好出现对应音色的打点。</p>
  </div>
</section>
"""


def practice_html(view: dict) -> str:
    cards = "\n".join(
        card_html(item, view[item.bid]["svg_jia"], view[item.bid]["svg_yi"])
        for item in model.ITEMS
    )
    data = {bid: {k: v for k, v in entry.items() if k in
                  ("fps", "frames", "cols", "rows", "w", "h", "duration",
                   "film", "jia", "yi")}
            for bid, entry in view.items()}
    data_json = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return (_PRACTICE_TEMPLATE
            .replace("__CARDS_HTML__", cards)
            .replace("__DATA_JSON__", data_json))


_ANSWER_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>盲标签题卡 · Slatewatch</title>
<style>
 body{font:14px/1.7 "PingFang SC","Microsoft YaHei",system-ui,sans-serif;color:#111;max-width:900px;margin:0 auto;padding:28px 22px 80px}
 h1{font-size:22px} h2{font-size:17px;margin:26px 0 6px;border-top:2px solid #222;padding-top:16px}
 table{border-collapse:collapse;width:100%;margin:8px 0}
 th,td{border:1px solid #888;padding:6px 8px;text-align:left;vertical-align:top}
 th{background:#eef2f8} .id{white-space:nowrap;font-weight:700}
 .opts label{display:inline-block;margin-right:16px;white-space:nowrap}
 .lines{min-height:54px;border:1px dashed #aaa;border-radius:6px;padding:6px;margin-top:6px}
 .meta{display:flex;gap:24px;flex-wrap:wrap;margin:10px 0}
 .meta span{border-bottom:1px solid #333;min-width:160px;display:inline-block;padding:0 8px}
 .key{background:#fff7e0;border:1px solid #e0b84a;border-radius:10px;padding:12px 16px;margin:16px 0}
 @media print{.noprint{display:none} body{padding:0}}
</style></head><body>
<p class="noprint"><button onclick="window.print()" style="padding:8px 16px">打印 / 另存为 PDF</button></p>
<h1>Slatewatch 声画关系盲标 · 答题卡</h1>
<div class="meta">
 <div>参训者：<span>&nbsp;</span></div>
 <div>日期：<span>&nbsp;</span></div>
 <div>组别 / 工位：<span>&nbsp;</span></div>
</div>
<div class="key">
判分只依据发给你的盲材：短段画面、甲等长试听、乙等长试听与制作记录。
每一小题都<b>必须允许自己写“无法确认”</b>——如果盲材内没有可区分两条候选的声画关系，
“无法确认”就是正确、诚实的结论，不要猜流号或制作文件。
</div>
__ROWS__
<h2>总体判断（可选）</h2>
<p>你是否在某一题怀疑“两条试听其实是同一条 / 覆盖情况不同”？请写下你能从盲材中 <b>观察到</b>
的依据，以及哪些信息你认为必须等复盘才能确认：</p>
<div class="lines" style="min-height:110px"></div>
<p class="noprint" style="color:#666;margin-top:24px">答完后请保持独立判断；真实流号、流名与覆盖由主持在复盘时统一公布。</p>
</body></html>
"""


def answer_sheet_html() -> str:
    rows = []
    for item in model.ITEMS:
        hit_times = "、".join(f"{h.t:0.2f}s" for h in item.correct)
        rows.append(f"""
<h2 class="id">{item.bid}</h2>
<table>
 <tr><th style="width:120px">核对动作时点</th><td>{hit_times}（详见制作记录 / 练习页）</td></tr>
 <tr><th>逐条声画核对</th><td>
   甲：<label><input type="checkbox"> 同步</label><label><input type="checkbox"> 不同步</label><label><input type="checkbox"> 不确定</label><br>
   乙：<label><input type="checkbox"> 同步</label><label><input type="checkbox"> 不同步</label><label><input type="checkbox"> 不确定</label>
 </td></tr>
 <tr><th>本小题判定</th><td class="opts">
   <label><input type="radio" name="{item.bid}-v"> 甲是应对齐的主混</label>
   <label><input type="radio" name="{item.bid}-v"> 乙是应对齐的主混</label>
   <label><input type="radio" name="{item.bid}-v"> 存在换错轨</label>
   <label><input type="radio" name="{item.bid}-v"> 无法确认</label>
 </td></tr>
 <tr><th>依据 / 听到的时点</th><td><div class="lines"></div></td></tr>
</table>""")
    return _ANSWER_TEMPLATE.replace("__ROWS__", "\n".join(rows))
