#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doubao-web：豆包解析结果本地展示服务

用法:
    python doubao_web.py [端口]        # 默认 8765，浏览器打开 http://127.0.0.1:8765

纯标准库实现。/proxy 反代豆包 CDN（跨域 + Range + 浏览器缓存），/api/convert 无损/快速转 H.264。
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import doubao_engine as E  # noqa: E402

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5, user-scalable=yes, viewport-fit=cover">
<title>豆包解析 · 本地结果</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#1c1e21;--sub:#65676b;--line:#e4e6eb;--acc:#3b82f6;--ok:#16a34a;--warn:#d97706}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:14px}
.wrap{max-width:1200px;margin:0 auto}
.bar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.bar input{flex:1;min-width:0;border:1px solid var(--line);border-radius:8px;padding:9px 12px;font-size:13px;background:#fff}
.bar button{white-space:nowrap}
#btnClear{padding:8px 13px;font-size:15px;font-weight:700;line-height:1}
#pasteBox{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:98}
.pbox{background:var(--card);border-radius:14px;padding:16px;width:min(92vw,420px);box-shadow:0 8px 30px rgba(0,0,0,.18)}
.ptitle{font-size:13px;font-weight:600;margin-bottom:10px;color:var(--ink)}
#ptext{width:100%;height:90px;border:1px solid var(--line);border-radius:8px;padding:9px 12px;font-size:13px;box-sizing:border-box;resize:none;background:#fff}
.pbtns{display:flex;gap:8px;justify-content:flex-end;margin-top:10px}
button{border:0;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer;background:var(--acc);color:#fff}
button.gray{background:#e4e6eb;color:var(--ink)}
button.ok{background:var(--ok)}
button.warn{background:var(--warn)}
button:disabled{opacity:.5;cursor:wait}
.tabs{display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.tab{padding:11px 44px;border-radius:12px;background:#e4e6eb;color:var(--sub);cursor:pointer;font-size:15px;font-weight:600;user-select:none;text-align:center;min-width:150px}
.tab.on{background:var(--acc);color:#fff}
.toolbar{display:flex;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:6px}
.toolbar .hint{flex:1;color:var(--sub);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fbar{display:flex;align-items:center;flex-wrap:wrap;gap:6px 8px;margin:2px 0 12px;font-size:13px}
.fbar .flabel{color:var(--sub);margin-right:2px}
.chip{padding:5px 12px;border-radius:14px;background:#e4e6eb;color:var(--sub);cursor:pointer;font-size:12px;user-select:none}
.chip.on{background:var(--acc);color:#fff}
.fbar select{border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:12.5px;background:#fff;color:var(--ink)}
.fbar select.on{border-color:var(--acc);color:var(--acc)}
.tip{background:#fff8e6;border:1px solid #ffe9b3;color:#8a6d1a;border-radius:8px;padding:8px 12px;font-size:12px;margin-bottom:12px;display:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.card .media{position:relative;background:#000;height:200px;display:flex;align-items:center;justify-content:center;overflow:hidden}
.card img{max-width:100%;max-height:100%;object-fit:contain;cursor:zoom-in}
.card video{max-width:100%;max-height:100%}
.badge{position:absolute;top:6px;left:6px;background:rgba(0,0,0,.6);color:#fff;font-size:11px;border-radius:6px;padding:1px 7px}
.vinfo{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:5px 9px;font-size:11px;color:var(--ink2,#667);background:var(--card);white-space:nowrap}
.vinfo .zoomlink{cursor:pointer;user-select:none;color:var(--acc);flex:none}
body.hidevid #main .card .media{visibility:hidden!important} /* 整个媒体区隐藏：原生视频+海报+播放按钮全遮住 */
/* 视频卡片的角标/放大已移到 .vinfo 行（原生视频图层会盖住浮层元素） */
.card .meta{padding:7px 10px 2px;font-size:11.5px;color:var(--sub)}
.card .btns{padding:6px 10px 10px;display:flex;gap:6px}
.card .btns button{flex:1;padding:6px 0;font-size:12px}
.empty{min-height:200px}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.92);display:none;flex-direction:column;align-items:center;justify-content:center;z-index:99}
#lbImg{max-width:94vw;max-height:78vh;object-fit:contain;background:transparent}
#lbVid{max-width:94vw;max-height:78vh;background:#000;border-radius:6px}
#lbImg.loading{min-width:120px;min-height:120px;background:url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 40 40"><circle cx="20" cy="20" r="16" stroke="white" stroke-width="3" fill="none" stroke-dasharray="70" stroke-linecap="round"><animateTransform attributeName="transform" type="rotate" from="0 20 20" to="360 20 20" dur="1s" repeatCount="indefinite"/></circle></svg>') center no-repeat}
#lb .lbbar{display:flex;gap:10px;margin-top:14px;align-items:center}
#lb .lbbar button{font-size:14px;padding:9px 18px}
#lb .close{position:absolute;top:12px;right:14px;background:rgba(255,255,255,.15);font-size:20px;width:40px;height:40px;padding:0;border-radius:50%}
#lb .nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.15);font-size:22px;width:46px;height:46px;padding:0;border-radius:50%}
#lb .prev{left:10px}#lb .next{right:10px}
#lb .cnt{color:#fff;font-size:12px;margin-top:8px}
/* ===== 主页（home 模式：logo + 居中大搜索框） ===== */
#logo{display:none;text-align:center;margin:9vh 16px 26px}
body.home #logo{display:block}
#logo .appname{font-size:24px;font-weight:700;margin-top:14px;letter-spacing:1px}
#logo .appsub{font-size:13px;color:var(--sub);margin-top:6px}
.ver{font-size:10px;color:var(--sub);opacity:.55;margin-top:8px}
body.home .wrap{padding-top:0}
body.home .bar{max-width:620px;margin:0 auto 14px;flex-wrap:nowrap}
body.home .bar input{border:2px solid var(--acc);font-size:16px;padding:13px 16px;box-shadow:0 4px 18px rgba(47,107,255,.18)}
body.home .bar input:focus{box-shadow:0 4px 22px rgba(47,107,255,.32)}
body.home #btnClear,body.home #btnPaste{padding:12px 16px}
body.home #btnParse{background:linear-gradient(135deg,#2f6bff,#7a3cff);padding:12px 24px;font-size:15px;font-weight:600;box-shadow:0 4px 16px rgba(47,107,255,.35)}
body.home #mTip{max-width:620px;margin:0 auto 12px}
body.home #logo{animation:pop .45s ease}
@keyframes pop{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}

/* ===== 多选下载选择页（参考豆包"保存图片"页） ===== */
#picker{position:fixed;inset:0;background:var(--bg);z-index:97;display:none;flex-direction:column}
.pkhead{display:flex;align-items:center;gap:10px;padding:12px 14px;background:var(--card);border-bottom:1px solid var(--line)}
.pkhead .pkclose{width:34px;height:34px;padding:0;font-size:20px;line-height:1;border-radius:50%}
.pktitle{flex:1;text-align:center;font-size:16px;font-weight:600;margin-right:34px}
.pkall{color:var(--acc);font-size:14px;cursor:pointer;user-select:none;padding:4px 2px}
.pkgrid{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;padding:12px;align-content:start}
.pktile{position:relative;background:#000;border-radius:8px;overflow:hidden;aspect-ratio:1/1;cursor:pointer}
.pktile img{width:100%;height:100%;object-fit:cover;display:block}
.pkvph{width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:30px;background:#26282c}
.pkdim{position:absolute;left:0;right:0;bottom:0;background:linear-gradient(transparent,rgba(0,0,0,.65));color:#fff;font-size:9.5px;text-align:center;padding:12px 2px 3px;pointer-events:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;height:16px;box-sizing:border-box;line-height:16px}
.pkck{position:absolute;top:6px;right:6px;width:24px;height:24px;border-radius:50%;border:2px solid #fff;background:rgba(0,0,0,.35);color:#fff;font-size:14px;line-height:20px;text-align:center;box-sizing:border-box}
.pktile.sel .pkck{background:var(--acc);border-color:var(--acc)}
.pktile.sel{outline:2px solid var(--acc);outline-offset:-2px}
.pkfoot{padding:10px 14px calc(10px + env(safe-area-inset-bottom));background:var(--card);border-top:1px solid var(--line);display:flex}
.pkfoot button{flex:1;padding:13px 0;font-size:15px;font-weight:600}
.pktip{padding:9px 14px;font-size:12px;line-height:1.6;color:#5a4a00;background:#fff8dc;border-top:1px solid #efe3ad}
.pkhint{padding:7px 14px 0;font-size:11px;color:var(--sub);text-align:center;line-height:1.7}
.dlhint{margin-top:10px;padding:8px 12px;font-size:11px;color:var(--sub);text-align:center;line-height:1.7;background:rgba(127,127,127,.06);border-radius:10px}
.disclaimer{margin-top:16px;padding:0 10px;font-size:10px;color:var(--sub);text-align:center;line-height:1.6;opacity:.85}
.pktip .pkrow{display:flex;align-items:center;gap:8px;margin:5px 0;padding:7px 8px;background:rgba(255,255,255,.75);border:1px solid #d8c878;border-radius:9px}
.pktip .pkrow .pkname{flex:1;font-size:11px;color:#5a4a00;word-break:break-all}
.pktip .pkrow a.pkdlbtn{flex:none;padding:7px 14px;border-radius:8px;font-size:12px;font-weight:700;color:#fff;text-decoration:none;background:linear-gradient(135deg,#2f6bff,#22b1ff);box-shadow:0 1px 6px rgba(47,107,255,.3)}
.pktip .pkrow a.pkdlbtn:active{filter:brightness(.85)}
.pktip .pkfb{display:block;width:100%;margin-top:8px;padding:9px 0;border:none;border-radius:9px;font-size:13px;font-weight:600;color:#fff;background:linear-gradient(135deg,#2f6bff,#22b1ff)}
button.blue{background:linear-gradient(135deg,#2f6bff,#22b1ff);color:#fff;box-shadow:0 2px 10px rgba(47,107,255,.28)}
button.blue.busy,button.ok.busy{filter:brightness(1.12);animation:pulse 1s ease infinite}
@keyframes pulse{50%{filter:brightness(.88)}}
.pkempty{grid-column:1/-1;text-align:center;color:var(--sub);font-size:13px;padding:40px 10px}
.pkempty .pklink{color:var(--acc);cursor:pointer;user-select:none;text-decoration:underline}
/* 解析 0/0 警示页 */
.zero{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:26px 18px;margin:14px 0;text-align:center}
.zero .zt{font-size:16px;font-weight:600;margin-bottom:10px}
.zero .zs{color:var(--sub);font-size:12.5px;line-height:1.8;margin-bottom:10px}
.zero .zd{color:#8a6d1a;background:#fff8e6;border:1px solid #ffe9b3;border-radius:8px;font-size:11px;padding:6px 10px;margin-bottom:14px;word-break:break-all}
.zero button{padding:11px 26px;font-size:14px}
/* ===== 移动端适配 ===== */
@media(max-width:600px){
  body{padding:10px 8px calc(10px + env(safe-area-inset-bottom))}
  /* 主页：logo 上移、搜索框独占一行 */
  #logo{margin:6vh 8px 20px}
  #logo .appname{font-size:21px}
  body.home .bar{flex-wrap:wrap}
  body.home .bar input{flex:1 1 100%}
  /* 输入区：链接独占一行，按钮第二行 */
  .bar{gap:6px}
  .bar input{flex:1 1 100%;font-size:16px;padding:10px 12px} /* 16px 防 iOS 聚焦自动放大 */
  #btnClear,#btnPaste,#btnParse{padding:10px 18px;font-size:14px}
  /* 筛选栏：单行流式排列，档位自动缩小 */
  .fbar{gap:6px;font-size:12.5px}
  .fbar .flabel{margin-right:0}
  .chip{padding:6px 11px;font-size:12.5px}
  .fbar select{padding:5px 8px;font-size:16px} /* ≥16px 防 iOS 聚焦自动放大 */
  #ptext{font-size:16px}
  /* 选项卡：均分一行 */
  .tabs{margin-bottom:12px}
  .tab{flex:1;min-width:0;padding:12px 0;font-size:14px}
  /* 一键下载行 */
  .toolbar{margin-bottom:14px}
  .toolbar button{padding:10px 16px;font-size:13px}
  .toolbar .hint{flex-basis:100%;white-space:normal}
  /* 卡片留白 */
  .card .meta{padding:8px 10px 2px;line-height:1.5}
  .card .btns{padding:6px 10px 12px}
  /* 网格：宽屏两列，窄屏一列 */
  .grid{grid-template-columns:repeat(2,1fr);gap:8px}
  .card .media{height:140px}
  .card .meta{font-size:11px}
  .card .btns button{padding:9px 0;font-size:12px}
  /* 遮罩打开时隐藏背景视频：iOS 原生视频图层会穿透任意 z-index 的浮层 */
  body.hidevid #main .card .media{visibility:hidden!important} /* 整个媒体区隐藏：原生视频+海报+播放按钮全遮住 */
  /* 多选下载选择页：手机 3 列网格 */
  .pkgrid{grid-template-columns:repeat(3,1fr);gap:6px;padding:10px}
  .pkfoot{padding:10px 12px calc(10px + env(safe-area-inset-bottom))}
  /* 灯箱：手机上铺满全屏，隐藏箭头（滑动切换） */
  #lbImg{width:100vw;height:calc(100vh - 130px);max-width:none;max-height:none;object-fit:contain}
  #lbVid{width:100vw;height:calc(100vh - 130px);max-width:none;max-height:none;object-fit:contain}
  #lb .nav{display:none}
  #lb .lbbar{margin-top:10px}
  #lb .lbbar button{padding:12px 22px;font-size:15px}
  #lb .close{top:max(10px,env(safe-area-inset-top))}
  #lb .cnt{margin-top:6px}
}
@media(max-width:380px){
  .grid{grid-template-columns:1fr}
  .card .media{height:200px}
}
/* 横屏手机：灯箱留出底部操作行 */
@media(max-height:450px){
  #lbImg,#lbVid{height:calc(100vh - 96px)}
  #lb .lbbar button{padding:8px 18px}
}
html.restoring body{visibility:hidden} /* 刷新恢复期间整页隐藏，防主页一闪而过 */
</style>
<script>
// 防主页闪烁：首屏绘制前同步判断——若本次会话正在浏览结果页且有数据快照（刷新恢复场景），
// 先把整页隐藏，等恢复逻辑用快照重绘完再显示；700ms 兜底强制显示，防恢复异常导致白屏
try{if(sessionStorage.getItem('dw_active')==='1'&&sessionStorage.getItem('dw_data')){
  document.documentElement.classList.add('restoring');
  setTimeout(function(){document.documentElement.classList.remove('restoring');},700);
}}catch(e){}
</script>
</head>
<body class="home">
<div class="wrap">
  <div id="logo">
    <svg width="92" height="92" viewBox="0 0 64 64" aria-label="logo">
      <defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#2f6bff"/><stop offset="1" stop-color="#7a3cff"/>
      </linearGradient></defs>
      <rect x="4" y="4" width="56" height="56" rx="16" fill="url(#lg)"/>
      <path d="M32 15c7.5 8.5 11.5 13.2 11.5 18.8a11.5 11.5 0 1 1-23 0C20.5 28.2 24.5 23.5 32 15z" fill="#fff"/>
      <line x1="15" y1="51" x2="49" y2="13" stroke="#ff5a5a" stroke-width="5" stroke-linecap="round" opacity=".92"/>
    </svg>
    <div class="appname">无水印解析</div>
    <div class="appsub">粘贴DB分享链接 · 一键提取图片和视频 · 原画质无水印</div>
    <div class="ver">v20260918l</div>
  </div>
  <div class="bar">
    <input id="url" placeholder="https://www.doubao.com/thread/..." autocomplete="off" spellcheck="false">
    <button class="gray" id="btnClear" onclick="clearUrl()" title="清空输入框，自己手动粘贴">×</button>
    <button class="gray" id="btnPaste" onclick="pasteUrl()" title="清空旧链接并填入剪贴板内容">粘贴</button>
    <button id="btnParse" onclick="doParse()">解析</button>
  </div>
  <div class="tip" id="mTip"></div>
  <div class="dlhint">📱 苹果手机：点下载后跳转分享，点「保存到视频」→ 相册可找到<br>💻 win/mac/安卓：在浏览器下载或「下载文件」里找到视频</div>
  <div id="pasteBox" onclick="if(event.target===this)hidePaste()">
    <div class="pbox">
      <div class="ptitle">粘贴链接（Ctrl+V / 长按粘贴）</div>
      <textarea id="ptext" placeholder="按 Ctrl+V 粘贴到这里…"></textarea>
      <div class="pbtns"><button class="gray" onclick="hidePaste()">取消</button><button class="ok" onclick="confirmPaste()">确定</button></div>
    </div>
  </div>
  <div id="main"></div>
  <div class="disclaimer">本工具仅供交流学习使用，不得用于商业用途；所解析内容的版权归原作者所有，如有侵权请联系删除。</div>
</div>
<div id="picker">
  <div class="pkhead">
    <button class="gray pkclose" onclick="hidePicker()">×</button>
    <div class="pktitle" id="pkTitle">保存图片</div>
    <span class="pkall" id="pkAll" onclick="toggleAll()">全选</span>
  </div>
  <div class="pkgrid" id="pkGrid"></div>
  <div class="pktip" id="pkTip" style="display:none"></div>
  <div class="pkhint">📱 仅苹果手机用「批量分享」进相册 · 💻 win/mac/安卓用「批量下载」<br>苹果：点下载后跳转分享 → 点「保存到视频」→ 相册可找到<br>win/mac/安卓：在浏览器下载/下载文件里找到视频</div>
  <div class="pkfoot"><button class="ok" id="pkShare" onclick="doPickShare()">📤 批量分享</button><button class="blue" id="pkDl" onclick="doPickDownload()">⬇ 批量下载</button></div>
</div>
<div id="lb" onclick="if(event.target===this)hideLb()">
  <button class="close" onclick="hideLb()">×</button>
  <button class="nav prev" onclick="lbNav(-1)">‹</button>
  <img id="lbImg" alt="">
  <video id="lbVid" controls playsinline style="display:none"></video>
  <button class="nav next" onclick="lbNav(1)">›</button>
  <div class="lbbar"><button class="ok" id="lbDlBtn" onclick="lbDl()">⬇ 下载</button></div>
  <div class="cnt" id="lbCnt"></div>
</div>
<script>
const isIOS=/iPad|iPhone|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
const isMobile=isIOS||/Android|Mobile/i.test(navigator.userAgent);
let DATA=null,tab='image',lbIdx=0,timeFilter=0,forceFresh=false;
const $=s=>document.querySelector(s);
// 当前选项卡按时间筛选后的列表（timeFilter=0 表示全部）
function curList(){
  const l=tab==='image'?(DATA.images||[]):(DATA.videos||[]);
  if(!timeFilter)return l;
  const cut=Date.now()-timeFilter*60000;
  return l.filter(it=>(it.epoch||0)*1000>=cut);
}
function setFilter(m){timeFilter=m;render();try{localStorage.setItem('dw_filter',m);}catch(e){}}
// 平台下载提示只用主页静态 .dlhint 一处，避免黄色 mTip 重复啰嗦
window.addEventListener('DOMContentLoaded',()=>{
  const q=new URLSearchParams(location.search);
  const qu=q.get('url');
  const qt=q.get('tab');
  if(qu&&qu.includes('/thread/')){
    document.documentElement.classList.remove('restoring');
    if(qt==='video')tab='video';
    $('#url').value=qu;doParse();
    return;
  }
  // 仅"本次会话正在浏览结果页"时才恢复；主页绝不缓存链接——
  // 点过 ×、关掉重开、回退到主页，刷新后都是干净的空输入框（autocomplete=off 防浏览器表单缓存回填）
  try{
    const su=localStorage.getItem('dw_lastUrl');
    if(su&&su.includes('/thread/')&&sessionStorage.getItem('dw_active')==='1'){
      const st=localStorage.getItem('dw_tab');if(st==='video')tab='video';
      timeFilter=+localStorage.getItem('dw_filter')||0;
      const raw=sessionStorage.getItem('dw_data');
      if(raw){
        // 秒恢复：直接用会话快照重绘，不重新请求——无主页闪烁、无"自动点解析"过程
        DATA=JSON.parse(raw);
        (DATA.images||[]).forEach((it,i)=>it._fi=i);
        (DATA.videos||[]).forEach((it,i)=>it._fi=i);
        $('#url').value=su;
        render();
        prefetchImgs();
        loadSizes();
        document.body.classList.remove('home');
        if(sessionStorage.getItem('dw_picker')==='1')openPicker(true); // 刷新前选择页开着 → 原样恢复（含勾选）
        document.documentElement.classList.remove('restoring'); // 快照重绘完成，立即显示（不等兜底超时）
      }else{
        $('#url').value=su;
        document.documentElement.classList.remove('restoring');
        doParse(true); // 没有快照（如分享链接直达）才走重新解析
      }
    }
  }catch(e){}
});
// 前进/后退的页面缓存（bfcache）恢复时：若当前不是结果会话，强制回到干净主页
window.addEventListener('pageshow',e=>{
  if(e.persisted){try{if(sessionStorage.getItem('dw_active')!=='1')clearUrl();}catch(err){}}
});

function fmtSize(n){if(!n)return '';const u=['B','KB','MB','GB'];let i=0;while(n>=1024&&i<3){n/=1024;i++;}return n.toFixed(i===0||n>=100?0:1)+u[i];}
function fmtCodec(c){c=(c||'').toLowerCase();return c.includes('265')||c.includes('hevc')||c.includes('bytevc')?'H.265':c.includes('264')||c==='h264'?'H.264':(c?c.toUpperCase():'');}

async function doParse(keepFilter){
  let u=$('#url').value.trim();
  if(!u){clearUrl();return;} // 空链接：静默回到主页，不弹提醒
  // 从分享口令文本中自动提取链接（豆包 App 复制的往往是一整段文字，可能混有其他链接，优先取豆包的）
  const urls=u.match(/https?:..[^ ]+/g)||[];
  const hit=urls.find(x=>x.includes('doubao.com'))||urls.find(x=>x.includes('/thread/'))||'';
  if(hit){u=hit.replace(/[^a-zA-Z0-9%._~=?&/-]+$/,'');}
  $('#url').value=u; // 回填规范化后的链接
  if(!u.includes('/thread/')){showErr('链接格式不对','链接需包含 /thread/（请从豆包分享面板复制链接）');return;}
  try{localStorage.setItem('dw_lastUrl',u);}catch(e){}
  const btn=$('#btnParse');
  btn.disabled=true;const oldTxt=btn.textContent;btn.textContent='解析中…';
  $('#main').innerHTML='';
  try{
    const r=await fetch('/api/scan?url='+encodeURIComponent(u)+(forceFresh?'&fresh=1':''));
    forceFresh=false;
    DATA=await r.json();
    if(DATA.error){showErr('解析失败',DATA.error,DATA.retry_hint);return;}
    // 记录全量列表下标：多选下载打 zip 时按此下标传给服务端 sel 参数
    (DATA.images||[]).forEach((it,i)=>it._fi=i);
    (DATA.videos||[]).forEach((it,i)=>it._fi=i);
    // 时间筛选规则：刷新恢复解析时保留所选档位；用户主动点解析/粘贴新链接时重置为"全部"
    // （筛选档存在 localStorage 全局生效，残留档位会把新解析内容全部筛成 0）
    if(!keepFilter&&timeFilter){timeFilter=0;try{localStorage.setItem('dw_filter','0');}catch(e){}}
    if(!DATA.image_count&&!DATA.video_count){ // 0/0：豆包间歇性空壳页很常见，先自动跳过缓存重试一次
      if(!forceFresh){
        btn.textContent='内容没取全，自动重试中…';
        await new Promise(r=>setTimeout(r,1500));
        forceFresh=true;
        return doParse(keepFilter);
      }
      document.body.classList.remove('home');
      try{sessionStorage.removeItem('dw_active');}catch(e){}
      $('#main').innerHTML=`<div class="zero">
        <div class="zt">⚠️ 没有解析到图片或视频</div>
        <div class="zs">豆包现在会间歇性返回"空壳页"（反爬策略：页面在、内容没给），这不是链接坏了——点下方「重试解析」一两次通常就能解析出来。<br>如果重试 3 次以上仍是 0，才可能是内容已过期/被删除，可把链接发到浏览器里直接打开确认。</div>
        ${DATA.debug?`<div class="zd">诊断：${JSON.stringify(DATA.debug)}</div>`:''}
        <button class="ok" onclick="forceFresh=true;doParse()">↻ 重试解析（跳过缓存）</button>
      </div>`;
      return;
    }
    render();
    prefetchImgs();
    loadSizes();
    document.body.classList.remove('home'); // 进入结果页
    try{sessionStorage.setItem('dw_active','1');}catch(e){}
  }catch(e){showErr('网络异常',String(e),true);}
  finally{btn.disabled=false;btn.textContent=oldTxt;}
}

// 页内错误页：显示报错原文（可截图反馈）+ 重试按钮，不再用 alert
function showErr(title,msg,retry){
  document.body.classList.remove('home');
  try{sessionStorage.removeItem('dw_active');}catch(e){}
  $('#main').innerHTML=`<div class="zero">
    <div class="zt">⚠️ ${title}</div>
    <div class="zs">${String(msg).replace(/</g,'&lt;')}<br>豆包偶尔拦截抓取（反爬），重试一两次通常就能成功。</div>
    ${retry?`<button class="ok" onclick="forceFresh=true;doParse()">↻ 重试解析</button>`:''}
  </div>`;
}

// 后台补图片文件大小（不阻塞出图）
async function loadSizes(){
  try{
    const r=await fetch('/api/sizes?url='+encodeURIComponent(DATA.share_url));
    const j=await r.json();
    const m=j.sizes||{};
    (DATA.images||[]).forEach((it,i)=>{
      const s=m[String(it.id)];
      if(s){it.size=s;
        const el=$('#meta_i'+i);
        if(el)el.textContent=[it.time||'',(it.format||'jpg').toUpperCase(),fmtSize(s)].filter(Boolean).join(' · ');}
    });
  }catch(e){}
}

// 后台预取原图（配 Cache-Control，点开大图即秒开）
function prefetchImgs(){
  (DATA.images||[]).slice(0,40).forEach(it=>{const im=new Image();im.src='/proxy?u='+encodeURIComponent(it.url);});
}

function render(){
  const list=curList();
  const cards=list.map((it,i)=>{
    let media;
    if(it.kind==='image'){
      media=`<img loading="lazy" src="/proxy?u=${encodeURIComponent(it.thumb||it.url)}" onclick="showLb(${i})">`;
      var badge=`${it.width||'?'}×${it.height||'?'}`;
      var meta=[it.time||'',(it.format||'jpg').toUpperCase(),fmtSize(it.size)].filter(Boolean).join(' · ');
    }else{
      // 角标和放大按钮不放视频图层上：iOS 原生视频会盖住 HTML 元素（显示不全/被遮）
      media=`<video controls preload="none" playsinline poster="/proxy?u=${encodeURIComponent(it.poster||'')}" data-vi="${i}" src=""></video>`;
      const dur=it.duration?(it.duration<60?it.duration.toFixed(0)+'s':(it.duration/60).toFixed(1)+'min'):'';
      var badge=`▶${dur?' '+dur:''} ${it.width||'?'}×${it.height||'?'}`;
      var meta=[it.time||'',fmtCodec(it.codec)||'',fmtSize(it.size)||''].filter(Boolean).join(' · ')||'解析中…';
    }
    const convBtn=(!isIOS&&it.kind==='video'&&it.codec&&!/264/.test(it.codec))?`<button class="warn" onclick="convItem(${i},this)">转码后下载</button>`:'';
    const vinfo=it.kind==='video'?`<div class="vinfo"><span>${badge}</span><span class="zoomlink" onclick="showLb(${i})" title="放大浏览（可切换上一个/下一个）">⤢ 放大</span></div>`:'';
    const badgeSpan=it.kind==='image'?`<span class="badge" id="badge_${tab[0]}${i}">${badge}</span>`:'';
    return `<div class="card"><div class="media">${media}${badgeSpan}</div>${vinfo}
      <div class="meta" id="meta_${tab[0]}${i}">${meta}</div>
      <div class="btns"><button onclick="dlItem('${tab[0]}${i}')">⬇ 下载</button>${convBtn}</div></div>`;
  }).join('');
  const full=tab==='image'?(DATA.images||[]).length:(DATA.videos||[]).length;
  const emptyHint=list.length?'':(full?'<div class="pkempty">时间筛选把 '+full+' 个内容全部筛掉了，<span class="pklink" onclick="setFilter(0)">点此查看全部</span></div>':'解析结果为空');
  $('#main').innerHTML=`
    <div class="fbar">
      <span class="flabel">时间筛选</span>
      <span class="chip ${timeFilter===0?'on':''}" onclick="setFilter(0)">全部</span>
      <span class="chip ${timeFilter===30?'on':''}" onclick="setFilter(30)">0.5h</span>
      <span class="chip ${timeFilter===60?'on':''}" onclick="setFilter(60)">1h</span>
      <span class="chip ${timeFilter===360?'on':''}" onclick="setFilter(360)">6h</span>
      <span class="chip ${timeFilter===720?'on':''}" onclick="setFilter(720)">12h</span>
      <select id="fsel" class="${[1440,2880,4320].includes(timeFilter)?'on':''}" onchange="setFilter(+this.value||0)">
        <option value="0" ${![1440,2880,4320].includes(timeFilter)?'selected':''}>更多</option>
        <option value="1440" ${timeFilter===1440?'selected':''}>24h</option>
        <option value="2880" ${timeFilter===2880?'selected':''}>48h</option>
        <option value="4320" ${timeFilter===4320?'selected':''}>72h</option>
      </select>
    </div>
    <div class="tabs">
      <div class="tab ${tab==='image'?'on':''}" onclick="setTab('image')">图片 ${tab==='image'?list.length:(DATA.images||[]).length}</div>
      <div class="tab ${tab==='video'?'on':''}" onclick="setTab('video')">视频 ${tab==='video'?list.length:(DATA.videos||[]).length}</div>
    </div>
    <div class="toolbar">
      <div class="hint">${DATA.share_name||''}</div>
      <button class="ok" id="btnAll" onclick="openPicker()">批量下载${tab==='image'?'图片':'视频'}（${list.length}）</button>
    </div>
    <div class="${list.length?'grid':'empty'}">${list.length?cards:emptyHint}</div>`;
  // 结果快照入会话缓存：刷新时直接重绘（无网络请求、无主页闪烁），选择页状态也能还原
  try{sessionStorage.setItem('dw_data',JSON.stringify(DATA));sessionStorage.setItem('dw_tab',tab);}catch(e){}
  if(tab==='video')resolveVideos();
}
function setTab(t){tab=t;render();try{localStorage.setItem('dw_tab',t);}catch(e){}}
// ×清空输入框（手动粘贴用） / 粘贴：优先直接读剪贴板；被环境拦截时弹粘贴框兜底
function clearUrl(){$('#url').value='';document.body.classList.add('home');$('#main').innerHTML='';DATA=null;timeFilter=0;try{sessionStorage.removeItem('dw_active');sessionStorage.removeItem('dw_data');sessionStorage.removeItem('dw_picker');sessionStorage.removeItem('dw_pickSel');localStorage.removeItem('dw_lastUrl');localStorage.setItem('dw_filter','0');}catch(e){}$('#url').focus();}
async function pasteUrl(){
  try{
    if(!navigator.clipboard||!navigator.clipboard.readText)throw new Error('unsupported');
    const t=await navigator.clipboard.readText();
    if(t&&t.trim()){$('#url').value=t.trim();flashPaste('已粘贴✓');doParse();return;}
    throw new Error('empty');
  }catch(e){openPasteBox();}
}
function openPasteBox(){
  $('#ptext').value='';
  $('#pasteBox').style.display='flex';
  setTimeout(()=>$('#ptext').focus(),60);
}
function hidePaste(){$('#pasteBox').style.display='none';}
function confirmPaste(){
  const t=$('#ptext').value.trim();
  if(t){$('#url').value=t;flashPaste('已粘贴✓');doParse();}
  hidePaste();
}
function flashPaste(txt){
  const b=$('#btnPaste'),o='粘贴';
  b.textContent=txt;setTimeout(()=>b.textContent=o,1200);
}

// 文件名时间戳：2026-09-17 10:25:25 -> 20260917_102525（保证按名称排序不乱序）
function fstamp(it){const t=(it.time||'').replace(/[-: ]/g,'');return t.length===14?t.slice(0,8)+'_'+t.slice(8):'unknown';}
function fnum(n){return String(n).padStart(3,'0');}

// idx: 'i'+图片序号 / 'v'+视频序号
async function dlItem(idx){
  const isImg=idx[0]==='i';
  const k=+idx.slice(1);
  const it=curList()[k];
  const num=k+1;
  try{
    let u,name;
    if(isImg){
      u='/proxy?u='+encodeURIComponent(it.url);
      name='img_'+fnum(num)+'_'+fstamp(it)+'.'+(it.format==='png'?'png':'jpg');
    }else{
      const s=await pickVideoSrc(it); // H.264 直下 / 服务端转标准 mp4
      u=s.u;name=s.name;
    }
    if(isIOS){shareSave(u,name).catch(()=>window.open(u,'_blank'));return;}
    dlBlob(await getBlobCached(u),name); // blob 下载（预取缓存秒触发；直链在其内核会被当导航打开）
  }catch(e){alert('下载失败：'+e);}
}
// 真·浏览器下载通道：隐藏 iframe 加载附件直链（服务端 Content-Disposition: attachment）。
// 不用 a.click()：夸克/QQ 等内核会把程序化直链当页面导航"打开链接"而不是下载；
// iframe 方式页面永不导航，附件头触发下载管理器接管（显示进度/可保存）
function dl(u,name){
  const f=document.createElement('iframe');
  f.style.display='none';
  f.src=u+(u.includes('?')?'&':'?')+'dl='+encodeURIComponent(name);
  document.body.appendChild(f);
  setTimeout(()=>{try{f.remove();}catch(e){}},120000); // 下载触发后延迟清理
}
// 用已就绪的 blob 触发浏览器下载（不二次拉流，秒触发）；objectURL 延迟释放防大文件中断
function dlBlob(b,name){
  const a=document.createElement('a');
  const url=URL.createObjectURL(b);
  a.href=url;a.download=name;
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>{try{URL.revokeObjectURL(url);}catch(e){}},60000);
}
// iOS：拉取文件后调起系统分享面板，用户点「存储图像/存储视频」直接进相册（免长按）
async function shareSave(u,name){
  const r=await fetch(u);const b=await r.blob();
  const f=new File([b],name,{type:b.type||'application/octet-stream'});
  if(navigator.canShare&&navigator.canShare({files:[f]})){
    try{await navigator.share({files:[f]});return;}catch(e){if(e&&e.name==='AbortError')return;}
  }
  window.open(u,'_blank'); // 兜底：新标签长按保存
}

// 视频变体：[{url,codec,w,h,size}]，分辨率从高到低；缓存到 DATA.videos[i]._vs
async function videoVariants(i){
  const v=curList()[i];
  if(v._vs)return v._vs;
  const r=await fetch('/api/video?u='+encodeURIComponent(v.fallback_api));
  const j=await r.json();
  if(j.error)throw j.error;
  v._vs=j.variants;
  // 预览播放优先 H.264（浏览器兼容性最好），没有就最高清
  const play=j.variants.find(x=>/264/.test(x.codec))||j.variants[0];
  v.codec=play.codec;v.size=play.size;
  const el=document.querySelector(`video[data-vi="${i}"]`);if(el)el.src='/proxy?u='+encodeURIComponent(play.url);
  updateVideoMeta(i);
  return j.variants;
}
function updateVideoMeta(i){
  const v=curList()[i],vs=v._vs;if(!vs||!vs.length)return;
  const best=vs[0];
  const m=$('#meta_v'+i),b=$('#badge_v'+i);
  if(m)m.textContent=[v.time||'',fmtCodec(best.codec),fmtSize(best.size)].filter(Boolean).join(' · ');
  if(b&&!best.h)b.textContent='▶ '+(v.width||'?')+'×'+(v.height||'?');
  // 若当前没有转码按钮且编码非H.264，补一个
  const btns=m&&m.parentElement.querySelector('.btns');
  if(btns&&!btns.querySelector('.warn')&&!isIOS&&!/264/.test(best.codec)){
    const w=document.createElement('button');w.className='warn';w.textContent='转码后下载';w.onclick=()=>convItem(i,w);btns.appendChild(w);
  }
}
async function resolveVideos(){const l=curList();for(let i=0;i<l.length;i++){try{await videoVariants(i);}catch(e){const m=$('#meta_v'+i);if(m)m.textContent='解析失败';}}}

// 视频下载源：优先 H.264 变体（正常 mp4 封装）；没有 H.264 时走服务端 ffmpeg 规整/转码，
// 避免 bytevc1/H.265 裸流（vdat）直接落盘导致手机无法播放
async function pickVideoSrc(it){
  const vs=await videoVariants(curList().indexOf(it));
  const h264=vs.find(x=>/264/.test(x.codec||''));
  const best=h264||vs[0];
  const num=it._fi+1;
  if(h264){
    return {u:'/proxy?u='+encodeURIComponent(best.url),name:'video_'+fnum(num)+'_'+fstamp(it)+'_'+best.h+'p.mp4'};
  }
  return {u:'/api/convert?u='+encodeURIComponent(best.url)+'&codec='+encodeURIComponent(best.codec||''),name:'video_'+fnum(num)+'_'+fstamp(it)+'_h264.mp4'};
}
// blob 缓存：打开选择页即后台预取，点「批量分享」时文件秒备好——
// iOS 的 navigator.share 必须在用户手势有效期内调用，取文件太久手势过期会被拒（NotAllowedError）
const blobCache=new Map();
function getBlobCached(u){
  if(!blobCache.has(u))blobCache.set(u,fetchBlob(u).catch(e=>{blobCache.delete(u);throw e;}));
  return blobCache.get(u);
}
function prefetchPicker(){
  // 并行预取（浏览器自身限并发，无需手动限流），打开选择页即开始
  for(const it of curList()){
    (async()=>{
      try{
        const s=it.kind==='image'?{u:'/proxy?u='+encodeURIComponent(it.url)}:await pickVideoSrc(it);
        getBlobCached(s.u);
      }catch(e){}
    })();
  }
}

// 无损/快速转 H.264：容器不标准优先 -c copy 重封装，编码不兼容才快速转码
async function convItem(i,btn){
  const it=DATA.videos[i];
  const vs=it._vs||await videoVariants(i);
  const best=vs[0];
  btn.disabled=true;const old=btn.textContent;btn.textContent='转换中…';
  try{
    const r=await fetch('/api/convert?u='+encodeURIComponent(best.url)+'&codec='+encodeURIComponent(best.codec||''));
    if(!r.ok){const j=await r.json().catch(()=>({}));throw j.error||('HTTP '+r.status);}
    const b=await r.blob();
    const num=i+1;
    const a=document.createElement('a');a.href=URL.createObjectURL(b);
    a.download='video_'+fnum(num)+'_'+fstamp(it)+'_h264.mp4';
    document.body.appendChild(a);a.click();a.remove();
  }catch(e){alert('转换失败：'+e);}
  btn.disabled=false;btn.textContent=old;
}

async function fetchBlob(u){const r=await fetch(u);if(!r.ok)throw new Error('HTTP '+r.status);return await r.blob();}

/* ===== 多选下载选择页：缩略图网格 + 勾选 + 全选 + 底部"保存到相册" ===== */
let pickSel=new Set();
function openPicker(restore){
  const list=curList();
  if(!list.length)return;
  pickSel=restore?new Set(JSON.parse(sessionStorage.getItem('dw_pickSel')||'[]').filter(i=>i<list.length)):new Set(list.map((_,i)=>i)); // 刷新恢复勾选/默认全选
  document.body.classList.add('hidevid'); // 遮住背景视频（原生图层穿透遮罩）
  document.querySelectorAll('.media video').forEach(v=>{try{v.pause();}catch(e){}});
  $('#pkTip').style.display='none'; // 重置上次的下载提示
  $('#picker').style.display='flex';
  try{sessionStorage.setItem('dw_picker','1');}catch(e){}
  pkRender();
  prefetchPicker(); // 后台预取 blob：iOS 分享必须在手势窗口内弹出面板
}
function hidePicker(){$('#picker').style.display='none';document.body.classList.remove('hidevid');try{sessionStorage.setItem('dw_picker','0');}catch(e){}}
function togglePick(i){pickSel.has(i)?pickSel.delete(i):pickSel.add(i);pkRender();}
function toggleAll(){
  const list=curList();
  if(list.length&&pickSel.size===list.length)pickSel.clear();
  else pickSel=new Set(list.map((_,i)=>i));
  pkRender();
}
function pkRender(){
  const list=curList();
  $('#pkTitle').textContent=tab==='image'?'保存图片':'保存视频';
  const all=pickSel.size===list.length&&list.length>0;
  $('#pkAll').textContent=all?'取消全选':'全选';
  $('#pkGrid').innerHTML=list.map((it,i)=>{
    let media,dim=`${it.width||'?'}×${it.height||'?'}`;
    if(it.kind==='image'){
      media=`<img loading="lazy" src="/proxy?u=${encodeURIComponent(it.thumb||it.url)}">`;
    }else{
      media=it.poster?`<img loading="lazy" src="/proxy?u=${encodeURIComponent(it.poster)}">`:'<div class="pkvph">▶</div>';
      if(it.duration)dim+='·'+(it.duration<60?it.duration.toFixed(0)+'s':(it.duration/60).toFixed(1)+'min');
    }
    return `<div class="pktile ${pickSel.has(i)?'sel':''}" onclick="togglePick(${i})">${media}<span class="pkdim">${dim}</span><span class="pkck">${pickSel.has(i)?'✓':''}</span></div>`;
  }).join('');
  const shareBtn=$('#pkShare'),dlBtn=$('#pkDl');
  const n=pickSel.size,has=n>0;
  shareBtn.disabled=!has;dlBtn.disabled=!has;
  shareBtn.textContent=has?`📤 批量分享（${n}）`:'📤 批量分享';
  dlBtn.textContent=has?`⬇ 批量下载（${n}）`:'⬇ 批量下载';
  try{sessionStorage.setItem('dw_pickSel',JSON.stringify([...pickSel]));}catch(e){} // 勾选状态入快照
}
// 取勾选项的文件 blob 列表（分享用）：并行下载，进度实时更新；图片用原图，视频统一 H.264/标准 mp4
async function buildFiles(list,btn){
  let done=0;const total=list.length;
  const tasks=list.map(async it=>{
    let b,nm,ty;
    if(it.kind==='image'){
      b=await getBlobCached('/proxy?u='+encodeURIComponent(it.url));
      nm='img_'+fnum(it._fi+1)+'_'+fstamp(it)+'.'+(it.format==='png'?'png':'jpg');
      ty=b.type||'image/jpeg';
    }else{
      const s=await pickVideoSrc(it);
      b=await getBlobCached(s.u);
      nm=s.name;ty='video/mp4';
    }
    done++;btn.textContent=`取文件中 ${done}/${total}…`;
    return new File([b],nm,{type:ty});
  });
  return await Promise.all(tasks); // 顺序与勾选顺序一致
}
async function doPickShare(){
  const btn=$('#pkShare');
  const list=curList().filter((_,i)=>pickSel.has(i));
  if(!list.length)return;
  btn.disabled=true;btn.classList.add('busy');const old=btn.textContent;
  try{
    // 合并成一次 share 调用：iOS 分享面板点「存储图像 / 存储视频」勾选的全部直接进相册
    const files=await buildFiles(list,btn);
    if(navigator.canShare&&navigator.canShare({files})){
      await navigator.share({files});
      hidePicker();
    }else{
      alert('当前浏览器不支持多文件分享，请改用「批量下载」或减少勾选数量');
    }
  }catch(e){
    if(e&&e.name==='NotAllowedError'){
      // 手势过期导致分享面板没弹出；此时文件已全部就绪，重试一次必然秒弹
      alert('分享面板没有弹出（系统响应超时）。文件已准备好，请再点一次「批量分享」，弹出面板后立即点「存储图像 / 存储视频」。');
    }else if(!(e&&e.name==='AbortError')){
      alert('分享失败：'+(e&&e.message||e));
    }
  }
  btn.disabled=false;btn.classList.remove('busy');btn.textContent=old;
}
// iOS 手动「⬇ 保存」：单文件弹系统分享面板（存储图像/存储视频 → 直接进相册）。
// iOS 所有浏览器内核（WKWebView）没有真正的网页下载通道，attachment 直链只会打开预览页；
// 分享面板是最可靠入口，blob 已在打开选择页时预取，点击秒弹
let pkRows=[];
async function shareOne(it){
  try{
    let u,name;
    if(it.kind==='image'){u='/proxy?u='+encodeURIComponent(it.url);name='img_'+fnum(it._fi+1)+'_'+fstamp(it)+'.'+(it.format==='png'?'png':'jpg');}
    else{const s=await pickVideoSrc(it);u=s.u;name=s.name;}
    const b=await getBlobCached(u);
    const f=new File([b],name,{type:it.kind==='image'?(b.type||'image/jpeg'):'video/mp4'});
    if(navigator.canShare&&navigator.canShare({files:[f]})){await navigator.share({files:[f]});return;}
    window.location.href=u+(u.includes('?')?'&':'?')+'dl='+encodeURIComponent(name); // 分享不可用才走直链
  }catch(e){if(!(e&&e.name==='AbortError'))alert('保存失败：'+(e&&e.message||e));}
}
function pkRowTap(i){
  const r=pkRows[i];if(!r)return true;
  if(isIOS&&navigator.canShare){shareOne(r.it);return false;} // iOS：分享面板存相册，不导航
  return true; // 电脑/安卓：直链导航，交给浏览器下载管理器
}
async function doPickDownload(){
  const btn=$('#pkDl');
  const list=curList().filter((_,i)=>pickSel.has(i));
  if(!list.length)return;
  btn.disabled=true;btn.classList.add('busy');const old=btn.textContent;
  const rows=[];let auto=0;
  try{
    if(isIOS){
      // iOS：没有网页下载通道，逐文件「⬇ 保存」弹分享面板进相册（文件已预取秒弹）
      for(let i=0;i<list.length;i++){
        const it=list[i];
        btn.textContent=`探测资源 ${i+1}/${list.length}…`;
        let u,name,bytes=null;
        if(it.kind==='image'){
          u='/proxy?u='+encodeURIComponent(it.url);
          name='img_'+fnum(it._fi+1)+'_'+fstamp(it)+'.'+(it.format==='png'?'png':'jpg');
        }else{
          const s=await pickVideoSrc(it); // 视频统一 H.264/标准 mp4（需转码时等服务端 ffmpeg）
          u=s.u;name=s.name;
          if(it._vs){const h=it._vs.find(x=>/264/.test(x.codec||''))||it._vs[0];bytes=h.size||null;}
        }
        let ok=true;
        try{
          const ctl=new AbortController(); // 探针：读到响应头就断开，不拉完整文件
          const r=await fetch(u,{headers:{Range:'bytes=0-1'},signal:ctl.signal});
          ok=r.ok||r.status===206;
          const cr=r.headers.get('Content-Range');
          if(cr){const m=cr.match(/\\/(\\d+)/);if(m)bytes=+m[1];}
          else{const cl=r.headers.get('Content-Length');if(cl&&!bytes)bytes=+cl;}
          ctl.abort();
        }catch(e){if(e&&e.name!=='AbortError')ok=false;}
        rows.push({ok,name,bytes,u,it});
      }
    }else{
      // 安卓/桌面：一次自动批量下载全部（时间戳命名；视频已统一转 H.264 mp4 再下载）
      for(let i=0;i<list.length;i++){
        const it=list[i];btn.textContent=`下载中 ${i+1}/${list.length}…`;
        let u,name;
        if(it.kind==='image'){u='/proxy?u='+encodeURIComponent(it.url);name='img_'+fnum(it._fi+1)+'_'+fstamp(it)+'.'+(it.format==='png'?'png':'jpg');}
        else{const s=await pickVideoSrc(it);u=s.u;name=s.name;}
        try{dlBlob(await getBlobCached(u),name);auto++;}catch(e){}
        await new Promise(r=>setTimeout(r,400)); // 间隔防浏览器拦多文件
      }
    }
    pkRows=rows;
    const tip=$('#pkTip');
    tip.style.display='block';
    tip.innerHTML=isIOS
      ?('逐个点「⬇ 保存」→ 分享面板 →「存储图像 / 存储视频」：<br>'
        +rows.map((r,i)=>'<div class="pkrow"><span class="pkname">'+(r.ok?'✅':'⚠️')+' 📎 '+r.name+(r.bytes?' ('+fmtSize(r.bytes)+')':'')+'</span><a class="pkdlbtn" href="'+r.u+(r.u.includes('?')?'&':'?')+'dl='+encodeURIComponent(r.name)+'" onclick="return pkRowTap('+i+')">⬇ 保存</a></div>').join('')
        +'<button class="pkfb" onclick="doPickShare()">📥 批量分享，一次存全部到相册</button>')
      :('✅ 已批量提交 '+auto+' 个下载任务 → 浏览器下载列表/下载文件夹查看'
        +(auto<list.length?'<br>⚠️ 有 '+(list.length-auto)+' 个文件没取到，请重试或用「批量分享」':'')
        +'<button class="pkfb" onclick="doPickShare()">📥 想直接进相册？用「批量分享」</button>');
  }catch(e){
    if(!(e&&e.name==='AbortError'))alert('下载失败：'+(e&&e.message||e));
  }
  finally{btn.disabled=false;btn.classList.remove('busy');btn.textContent=old;}
}

/* 大图灯箱：图片/视频统一浏览（预取邻图 + 加载动画） */
let lbToken=0; // 防视频异步解析竞态
function showLb(i){lbIdx=i;updateLb();$('#lb').style.display='flex';}
function hideLb(){$('#lbVid')&&$('#lbVid').pause();$('#lb').style.display='none';}
function lbPlayUrl(it,vs){const play=vs.find(x=>/264/.test(x.codec))||vs[0];return '/proxy?u='+encodeURIComponent(play.url);}
async function updateLb(){
  const items=curList();
  const it=items[lbIdx];
  const img=$('#lbImg'),vid=$('#lbVid');
  $('#lbCnt').textContent=(lbIdx+1)+' / '+items.length;
  $('#lbDlBtn').textContent=it.kind==='video'?'⬇ 下载此视频':'⬇ 下载此图';
  if(it.kind==='video'){
    img.style.display='none';
    vid.style.display='block';
    vid.poster=it.poster?('/proxy?u='+encodeURIComponent(it.poster)):'';
    vid.removeAttribute('src');
    const token=++lbToken;
    try{
      const vs=it._vs||await videoVariants(lbIdx);
      if(token!==lbToken)return; // 用户已切到其他项，放弃本次播放
      vid.src=lbPlayUrl(it,vs);
      vid.play().catch(()=>{});
    }catch(e){}
  }else{
    lbToken++;
    vid.style.display='none';vid.pause&&vid.pause();vid.removeAttribute('src');
    img.style.display='block';
    img.classList.add('loading');
    img.onload=()=>img.classList.remove('loading');
    img.src='/proxy?u='+encodeURIComponent(it.url);
    // 预取左右邻图
    const n=items.length;
    [lbIdx+1,lbIdx-1].forEach(j=>{const p=items[(j+n)%n];if(p.kind==='image'){const im=new Image();im.src='/proxy?u='+encodeURIComponent(p.url);}});
  }
}
function lbNav(d){const n=curList().length;lbIdx=(lbIdx+d+n)%n;updateLb();}
/* 手机滑动切换（灯箱打开时生效） */
let lbTX=0,lbTY=0;
document.addEventListener('touchstart',e=>{
  if($('#lb').style.display!=='flex')return;
  lbTX=e.touches[0].clientX;lbTY=e.touches[0].clientY;
},{passive:true});
document.addEventListener('touchend',e=>{
  if($('#lb').style.display!=='flex')return;
  const dx=e.changedTouches[0].clientX-lbTX,dy=e.changedTouches[0].clientY-lbTY;
  if(Math.abs(dx)>60&&Math.abs(dx)>Math.abs(dy)*1.5)lbNav(dx<0?1:-1);
},{passive:true});
async function lbDl(){
  const it=curList()[lbIdx];
  const num=lbIdx+1;
  if(it.kind==='video'){
    const vs=it._vs||await videoVariants(lbIdx);
    const best=vs[0];
    const nm='video_'+fnum(num)+'_'+fstamp(it)+'_'+best.h+'p.mp4';
    const u='/proxy?u='+encodeURIComponent(best.url);
    if(isIOS){shareSave(u,nm).catch(()=>{});return;}
    dl(u,nm);
  }else{
    const nm='img_'+fnum(num)+'_'+fstamp(it)+'.'+(it.format==='png'?'png':'jpg');
    const u='/proxy?u='+encodeURIComponent(it.url);
    if(isIOS){shareSave(u,nm).catch(()=>{});return;}
    dl(u,nm);
  }
}
document.addEventListener('keydown',e=>{if($('#lb').style.display==='flex'){if(e.key==='Escape')hideLb();if(e.key==='ArrowLeft')lbNav(-1);if(e.key==='ArrowRight')lbNav(1);}});
$('#url').addEventListener('keydown',e=>{if(e.key==='Enter')doParse();});
</script>
</body>
</html>"""


def _guess_type(u):
    low = u.lower().split("?")[0]
    for ext, ct in ((".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".png", "image/png"),
                    (".webp", "image/webp"), (".mp4", "video/mp4")):
        if ext in low:
            return ct
    return "application/octet-stream"


def _safe_name(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(s or "doubao"))[:60]


def _remote_size(u):
    """远端文件大小：先 HEAD，失败退 Range 探测。"""
    try:
        req = urllib.request.Request(u, headers=dict(E.HEADERS), method="HEAD")
        with urllib.request.urlopen(req, timeout=8) as r:
            n = r.headers.get("Content-Length")
            if n:
                return int(n)
    except Exception:
        pass
    try:
        req = urllib.request.Request(u, headers={**E.HEADERS, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            cr = r.headers.get("Content-Range") or ""
            if "/" in cr:
                return int(cr.rsplit("/", 1)[1])
    except Exception:
        pass
    return 0


_VV_CACHE = {}   # fallback_api -> variants（进程内缓存，避免重复解密请求）
_SCAN_CACHE = {} # 分享链接 -> 解析结果（scan 秒回；sizes 异步补大小）
_SCAN_DISK = os.path.join(tempfile.gettempdir(), "dw_scan_cache.json")  # 落盘：进程重启/豆包风控期间仍能给出上次结果
_URL_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _url_lock(u):
    """同一链接的并发解析互斥：只放一个请求去抓豆包，避免重复抓取触发风控。"""
    with _LOCKS_GUARD:
        return _URL_LOCKS.setdefault(u, threading.Lock())


def _disk_load():
    try:
        with open(_SCAN_DISK, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _disk_save(url, r):
    try:
        with _LOCKS_GUARD:
            d = _disk_load()
            d[url] = r
            for k in list(d)[:-50]:  # 只留最近 50 条，防无限膨胀
                d.pop(k, None)
            with open(_SCAN_DISK, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _parse_cached(u, fresh=False):
    """带互斥/重试/落盘兜底的解析。
    关键规则：空结果（豆包间歇性反爬返回的空壳页，有框架无内容）绝不缓存——
    缓存了空结果会导致之后每次解析都秒回 0/0（此前「多点几次才正常」的根因之一）。
    - 已缓存且非空：直接秒回
    - 未缓存：同一链接并发只放一个去抓；单次请求内最多重试 4 次（异常和空结果都算失败）
    - 重试全空/失败：回退最近一次落盘的好结果；连历史都没有时，返回空结果附诊断（不写缓存）
    """
    lk = _url_lock(u)
    with lk:
        if not fresh and u in _SCAN_CACHE:
            r = _SCAN_CACHE[u]
            if r.get("image_count") or r.get("video_count"):  # 空结果不算有效缓存
                return r, False
        last_err, last_empty = None, None
        for attempt in range(4):
            try:
                r = E.parse_share(u)
                if r["image_count"] or r["video_count"]:
                    _SCAN_CACHE[u] = r
                    _disk_save(u, r)
                    return r, False
                last_empty = r  # 空壳页：有框架无内容，重试
            except Exception as e:  # noqa: BLE001
                last_err = e
            time.sleep(1.2 * (attempt + 1))
        stale = _disk_load().get(u)
        if stale and (stale.get("image_count") or stale.get("video_count")):
            _SCAN_CACHE[u] = stale
            return stale, True  # 豆包侧暂时取不到，先用历史成功结果
        if last_empty is not None:
            return last_empty, False  # 真空分享或持续空壳：给前端诊断页，但不缓存
        raise last_err


def _scan_debug(u):
    """空结果时抓一次页面做诊断：区分"内容过期/为空"与"风控半页/结构变化"。"""
    try:
        html_text = E.http_get(u, timeout=40)
    except Exception as e:  # noqa: BLE001
        return {"page": f"fetch-failed: {e}"}
    m = re.search(r"<title>([^<]{0,80})", html_text)
    try:
        blocks = E._extract_blocks(html_text)
    except Exception:  # noqa: BLE001
        blocks = []
    return {"page_len": len(html_text), "title": (m.group(1).strip() if m else ""),
            "has_snapshot": "message_snapshot" in html_text, "blocks": len(blocks)}


def _fetch_all(src, tries=3):
    """带重试的整读：CDN/代理偶发 IncompleteRead / 连接重置时重试。"""
    import http.client
    last = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(src, headers=dict(E.HEADERS)), timeout=180) as resp:
                return resp.read()
        except (http.client.IncompleteRead, ConnectionError, urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(1 + k)
    raise last


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # no-store：防手机浏览器把 API 响应（尤其失败响应）缓存住，导致同一链接重复解析"一直失败"
        hdrs = {"Cache-Control": "no-store"}
        hdrs.update(extra or {})
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _q(self, name):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get(name, [""])[0]

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/":
                self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/scan":
                self._api_scan()
            elif path == "/api/video":
                self._api_video()
            elif path == "/api/sizes":
                self._api_sizes()
            elif path == "/api/convert":
                self._api_convert()
            elif path == "/api/zip":
                self._api_zip()
            elif path == "/proxy":
                self._proxy()
            elif path == "/favicon.ico":
                self._send(204, b"")
            else:
                self._send(404, b'{"error":"not found"}')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))
            except Exception:
                pass

    def _api_scan(self):
        u = self._q("url").strip()
        fresh = self._q("fresh") == "1"
        if "/thread/" not in u:
            self._send(400, json.dumps({"error": "链接需包含 /thread/"}).encode("utf-8"))
            return
        try:
            # 走 _parse_cached：重试 4 次 + 空结果不缓存 + 落盘兜底（此前空结果直接写缓存，
            # 导致首次撞上豆包空壳页后每次都秒回 0/0——「新链接也解析失败」的根因）
            r, stale = _parse_cached(u, fresh)
        except Exception as e:  # noqa: BLE001 —— 重试全败：返回明确错误而不是 500
            self._send(200, json.dumps({"error": f"解析失败：{e}", "retry_hint": True},
                                       ensure_ascii=False).encode("utf-8"))
            return
        images = [{k: it.get(k) for k in ("id", "time", "epoch", "width", "height", "format", "url", "thumb")} | {"kind": "image"} for it in r["images"]]
        videos = [{k: it.get(k) for k in ("id", "time", "epoch", "width", "height", "duration", "poster", "fallback_api")} | {"kind": "video"} for it in r["videos"]]
        slim = {"share_name": r["share_name"], "share_url": r["share_url"],
                "image_count": r["image_count"], "video_count": r["video_count"],
                "images": images, "videos": videos}
        if stale:
            slim["stale"] = True  # 豆包侧暂时取不到，返回的是历史成功结果
        # 空结果附带页面诊断，便于区分"内容过期"与"页面结构变化/风控半页"
        if not images and not videos:
            slim["debug"] = _scan_debug(u)
        self._send(200, json.dumps(slim, ensure_ascii=False).encode("utf-8"))

    def _api_sizes(self):
        """异步补图片文件大小：复用 scan 缓存，HEAD 并发探测。"""
        u = self._q("url").strip()
        r = _SCAN_CACHE.get(u)
        if not r:
            self._send(404, b'{"error":"scan first"}')
            return
        items = r["images"]

        def _sz(it):
            s = it.get("size")
            if not s:
                s = _remote_size(it["url"])
                it["size"] = s
            return str(it.get("id")), s

        with ThreadPoolExecutor(max_workers=8) as ex:
            pairs = list(ex.map(_sz, items))
        self._send(200, json.dumps({"sizes": dict(pairs)}, ensure_ascii=False).encode("utf-8"))

    def _api_video(self):
        fb = self._q("u").strip()
        if not fb:
            self._send(400, b'{"error":"missing u"}')
            return
        if fb not in _VV_CACHE:
            _VV_CACHE[fb] = E.resolve_video_variants(fb)
        variants = _VV_CACHE[fb]
        # size 字段缺失时补 HEAD
        for v in variants:
            if not v["size"]:
                v["size"] = _remote_size(v["url"])
        self._send(200, json.dumps({"variants": variants}, ensure_ascii=False).encode("utf-8"))

    def _api_convert(self):
        u = self._q("u").strip()
        if not u.startswith(("http://", "https://")):
            self._send(400, b'{"error":"bad url"}')
            return
        ff = shutil.which("ffmpeg")
        if not ff:
            self._send(500, json.dumps({"error": "未找到 ffmpeg，无法转换"}, ensure_ascii=False).encode("utf-8"))
            return
        tmpd = tempfile.mkdtemp(prefix="douconv_")

        def _run(args):
            return subprocess.run(args, capture_output=True, timeout=900)

        def _reencode():
            return _run([ff, "-y", "-i", src, "-c:v", "libx264", "-preset", "veryfast",
                         "-crf", "20", "-c:a", "aac", "-b:a", "128k",
                         "-movflags", "+faststart", dst])

        def _remux():
            return _run([ff, "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst])

        def _ok():
            return os.path.exists(dst) and os.path.getsize(dst) > 0

        try:
            src = os.path.join(tmpd, "in.bin")
            dst = os.path.join(tmpd, "out.mp4")
            with urllib.request.urlopen(urllib.request.Request(u, headers=dict(E.HEADERS)), timeout=300) as resp, open(src, "wb") as f:
                shutil.copyfileobj(resp, f, 1 << 20)
            codec = self._q("codec").lower()
            hevc = any(t in codec for t in ("265", "hevc", "bytevc"))
            # H.265/bytevc1：编码本身不兼容，直接快速转 H.264；其余先无损重封装，失败再转码
            r = _reencode() if hevc else _remux()
            if r.returncode != 0 or not _ok():
                if os.path.exists(dst):
                    os.remove(dst)
                r = _reencode() if hevc else _remux()
                if r.returncode != 0 or not _ok():
                    msg = (r.stderr or b"").decode("utf-8", "ignore")[-300:]
                    self._send(500, json.dumps({"error": "ffmpeg 转换失败: " + msg}, ensure_ascii=False).encode("utf-8"))
                    return
            with open(dst, "rb") as f:
                data = f.read()
            fname = urllib.parse.quote(self._q("dl").strip() or "convert_h264.mp4")
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{fname}")
            self.end_headers()
            self.wfile.write(data)
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)

    def _proxy(self):
        u = self._q("u")
        if not u.startswith(("http://", "https://")):
            self._send(400, b'{"error":"bad url"}')
            return
        # dl 参数：以附件方式下发（真正走浏览器下载通道），浏览器下载管理器可见、可选保存位置
        dl_name = self._q("dl").strip()
        req = urllib.request.Request(u, headers=dict(E.HEADERS))
        rng = self.headers.get("Range")
        if rng:
            req.add_header("Range", rng)
        with urllib.request.urlopen(req, timeout=120) as resp:
            code = resp.status
            headers = {"Content-Type": resp.headers.get("Content-Type") or _guess_type(u),
                       "Accept-Ranges": "bytes",
                       "Cache-Control": "public, max-age=86400"}
            if dl_name:
                qn = urllib.parse.quote(dl_name)
                headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{qn}"
            if resp.headers.get("Content-Length"):
                headers["Content-Length"] = resp.headers["Content-Length"]
            if resp.headers.get("Content-Range"):
                headers["Content-Range"] = resp.headers["Content-Range"]
            self.send_response(code)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _api_zip(self):
        share_url = self._q("url").strip()
        typ = self._q("type") or "image"
        if "/thread/" not in share_url:
            self._send(400, b'{"error":"bad url"}')
            return
        r = E.parse_share(share_url)
        items = r["images"] if typ == "image" else r["videos"]
        # sel=0,2,5：只打包勾选的序号（0-based，对应解析结果全量列表下标）
        sel = self._q("sel").strip()
        if sel:
            idxs = {int(x) for x in sel.split(",") if x.strip().isdigit()}
            items = [it for i, it in enumerate(items) if i in idxs]
        if typ == "video":
            items = [dict(it, _u=(E.resolve_video_variants(it["fallback_api"]) or [{}])[0].get("url")) for it in items]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for i, it in enumerate(items, 1):
                src = it["url"] if typ == "image" else it["_u"]
                if not src:
                    continue
                ext = (it.get("format") or "jpg") if typ == "image" else "mp4"
                name = f"{i:03d}_{(it.get('time') or '').replace('-','').replace(':','').replace(' ','_')}_{typ}_{it.get('id')}.{ext}"
                z.writestr(name, _fetch_all(src))
        data = buf.getvalue()
        fname = urllib.parse.quote(f"{_safe_name(r['share_name'])}_{typ}.zip")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{fname}")
        self.end_headers()
        self.wfile.write(data)


def main():
    # 云端部署时平台注入 PORT 并要求监听 0.0.0.0；本地无 PORT 环境变量时保持 127.0.0.1:8765
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"豆包解析: http://{host}:{port}  (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
