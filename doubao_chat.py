#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doubao-chat：豆包分享对话 1:1 复刻本地网页（单页对话流，无图片/视频选项卡）

用法:
    python doubao_chat.py [端口]   # 默认 8766，浏览器打开 http://127.0.0.1:8766

与 doubao_web.py 的区别：
  - 正文按对话消息流渲染（用户气泡 / AI 回复 / 生成的图片视频 / 用户上传的参考图），
    便于人工审阅“哪些要、哪些作废”。
  - 解析框下方固定为「批量下载图片 / 批量下载视频」，不随滚动悬浮。
  - 正文媒体不叠下载按钮：长按媒体或消息进入多选模式（顶部出现“多选”条，媒体打钩，
    底部出现「分享 / 下载」）；或点开灯箱后右上角下载（iOS 走分享面板进相册）。
  - 下载/分享与 doubao_web 完全同源（同一套预取/分享/下载逻辑，已验证兼容各手机）。
纯标准库；媒体通道（/proxy /api/video /api/convert）与解析缓存全部复用 doubao_web。
"""
import datetime
import json
import os
import re
import sys
import threading
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import doubao_engine as E  # noqa: E402
import doubao_web as W  # noqa: E402

# ------------------------------------------------------- 对话模型


def _sender_of(msg):
    return str(msg.get("sender_id") or msg.get("sec_sender") or "")


def _role_list(ml):
    """判定每条消息角色：优先 user_type（1=用户 2=AI，页面数据自带）；
    缺失时兜底：含创作块(2074)的发送者 = AI，否则首条消息发送者 = 用户。"""
    roles = []
    bot_ids = set()
    for msg in ml:
        for cb in (msg.get("content_block") or []):
            if cb.get("block_type") == 2074:
                bot_ids.add(_sender_of(msg))
                break
    first = _sender_of(ml[0]) if ml else ""
    for msg in ml:
        ut = msg.get("user_type")
        if ut in (1, 2):
            roles.append("user" if ut == 1 else "ai")
        elif bot_ids:
            roles.append("ai" if _sender_of(msg) in bot_ids else "user")
        else:
            roles.append("user" if _sender_of(msg) == first else "ai")
    return roles


def _fmt_time(epoch):
    if not epoch:
        return ""
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def build_conversation(share_url):
    """解析分享页并构建完整对话流。返回 dict(messages, images, videos, uploads, share_name)。"""
    html_text = E.http_get(share_url)
    blocks = E._extract_blocks_multi(html_text)
    if not blocks or "message_snapshot" not in E.htmllib_unescape(html_text):
        raise ValueError("页面未返回可解析数据（可能被风控拦截或网络抖动），请稍后重试")
    mlists = E.find_message_lists(blocks)
    messages, images, videos = [], [], []
    for ml in mlists:
        roles = _role_list(ml)
        imgs, vids = E.collect_items(ml)
        images += imgs
        videos += vids
        # 按消息归组：msg_index -> 本消息的创作（保持块内顺序；图片在前视频在后可接受）
        cre_by_msg = {}
        for it in imgs + vids:
            cre_by_msg.setdefault(it["msg_index"], []).append(it)
        for mi, msg in enumerate(ml):
            epoch = int(msg.get("create_time") or 0)
            role = roles[mi] if mi < len(roles) else "user"
            blks = []
            queue = list(cre_by_msg.get(mi, []))  # 依块顺序消费
            for cb in (msg.get("content_block") or []):
                bt = cb.get("block_type")
                cj = E._parse_block_content(cb)
                if bt == 10000 and isinstance(cj, dict):
                    tb = cj.get("text_block") or {}
                    txt = (tb.get("text") or "").strip()
                    if txt:
                        blks.append({"t": "text", "text": txt})
                elif bt == 10052 and isinstance(cj, dict):
                    ab = cj.get("attachment_block") or {}
                    for att in (ab.get("attachments") or []):
                        im = att.get("image") or {}
                        ori = im.get("image_ori") or {}
                        thu = im.get("image_thumb") or {}
                        u = (ori.get("url") or thu.get("url") or "").replace("&amp;", "&")
                        if u:
                            blks.append({"t": "up", "url": u,
                                         "thumb": ((thu.get("url") or u)).replace("&amp;", "&")})
                elif bt == 2074:
                    while queue:
                        it = queue.pop(0)
                        if it["kind"] == "image":
                            blks.append({"t": "img", "ref": it})
                        else:
                            blks.append({"t": "vid", "ref": it})
            if blks:
                messages.append({"mi": mi, "epoch": epoch, "time": _fmt_time(epoch),
                                 "role": role, "blocks": blks})
    # 去重（同引擎规则），并按生成时间从旧到新
    images = E._dedupe(images)
    videos = E._dedupe(videos)
    images.sort(key=lambda x: x.get("epoch") or 0)
    videos.sort(key=lambda x: x.get("epoch") or 0)
    share_name = E._find_share_name(blocks)
    return {"share_name": share_name, "share_url": share_url,
            "image_count": len(images), "video_count": len(videos),
            "messages": messages, "images": images, "videos": videos}


def _item_failed(it):
    """沿用 doubao_smart 的硬失败规则：视频 status!=3/model_status!=10、tips 非空、时长 0、无地址。"""
    if it["kind"] == "video":
        if (it.get("status") not in (3, None)) or (it.get("model_status") not in (10, None)):
            return True
        if it.get("tips"):
            return True
        if it.get("duration") in (0, None) and it.get("fallback_api"):
            return True
        if not it.get("fallback_api"):
            return True
        return False
    return (it.get("status") not in (2, None)) or bool(it.get("tips"))


def _slim(r):
    """前端精简载荷：blocks 里引用只留下标与必要字段。"""
    img_idx = {id(it): i for i, it in enumerate(r["images"])}
    vid_idx = {id(it): i for i, it in enumerate(r["videos"])}
    msgs = []
    for m in r["messages"]:
        blks = []
        for b in m["blocks"]:
            if b["t"] in ("img", "vid"):
                arr = r["images"] if b["t"] == "img" else r["videos"]
                idxm = img_idx if b["t"] == "img" else vid_idx
                i = idxm.get(id(b["ref"]))
                if i is None:
                    continue
                blks.append({"t": b["t"], "i": i, "fail": _item_failed(b["ref"])})
            else:
                blks.append(b)
        msgs.append({"mi": m["mi"], "epoch": m["epoch"], "time": m["time"],
                     "role": m["role"], "blocks": blks})
    def slim_img(it, i):
        return {"_fi": i, "kind": "image", "id": it.get("id"), "time": it.get("time"),
                "epoch": it.get("epoch"), "width": it.get("width"), "height": it.get("height"),
                "format": it.get("format"), "url": it.get("url"), "thumb": it.get("thumb") or ""}
    def slim_vid(it, i):
        return {"_fi": i, "kind": "video", "id": it.get("id"), "time": it.get("time"),
                "epoch": it.get("epoch"), "width": it.get("width"), "height": it.get("height"),
                "duration": it.get("duration"), "poster": it.get("poster") or "",
                "fallback_api": it.get("fallback_api") or "", "fail": _item_failed(it)}
    return {"share_name": r["share_name"], "share_url": r["share_url"],
            "image_count": len(r["images"]), "video_count": len(r["videos"]),
            "messages": msgs,
            "images": [slim_img(it, i) for i, it in enumerate(r["images"])],
            "videos": [slim_vid(it, i) for i, it in enumerate(r["videos"])]}


# ------------------------------------------------------- 解析缓存（同 doubao_web 规则：空结果绝不缓存）

_CHAT_CACHE = {}
_CHAT_DISK = os.path.join(W.tempfile.gettempdir(), "dwc_chat_cache.json")
_CHAT_LOCKS = {}
_CHAT_LOCKS_GUARD = threading.Lock()


def _chat_lock(u):
    with _CHAT_LOCKS_GUARD:
        return _CHAT_LOCKS.setdefault(u, threading.Lock())


def _disk_load():
    try:
        with open(_CHAT_DISK, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _disk_save(url, r):
    try:
        with _CHAT_LOCKS_GUARD:
            d = _disk_load()
            d[url] = r
            for k in list(d)[:-30]:
                d.pop(k, None)
            with open(_CHAT_DISK, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _chat_cached(u, fresh=False):
    import time
    lk = _chat_lock(u)
    with lk:
        if not fresh and u in _CHAT_CACHE:
            r = _CHAT_CACHE[u]
            if r.get("image_count") or r.get("video_count") or r.get("messages"):
                return r, False
        last_err, last_empty = None, None
        for attempt in range(4):
            try:
                r = build_conversation(u)
                if r["image_count"] or r["video_count"] or r["messages"]:
                    _CHAT_CACHE[u] = r
                    _disk_save(u, r)
                    return r, False
                last_empty = r
            except Exception as e:  # noqa: BLE001
                last_err = e
            time.sleep(1.2 * (attempt + 1))
        stale = _disk_load().get(u)
        if stale and (stale.get("image_count") or stale.get("video_count") or stale.get("messages")):
            _CHAT_CACHE[u] = stale
            return stale, True
        if last_empty is not None:
            return last_empty, False
        raise last_err


# ------------------------------------------------------- 页面

CHAT_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5, user-scalable=yes, viewport-fit=cover">
<title>豆包对话复刻 · 本地审阅</title>
<style>
:root{--bg:#fcfcfc;--card:#fff;--ink:rgba(0,0,0,.85);--sub:#9a9aa0;--line:#ececec;--acc:#0066ff;--ok:#16a34a;--warn:#d97706}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--ink);font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:0 16px calc(16px + env(safe-area-inset-bottom))}
.wrap{max-width:768px;margin:0 auto;padding-top:12px}
.bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.bar input{flex:1 1 100%;min-width:0;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:16px;background:#fff}
button{border:0;border-radius:9px;padding:8px 15px;font-size:13.5px;cursor:pointer;background:var(--acc);color:#fff;font-family:inherit}
button.gray{background:#e6e8ec;color:var(--ink)}
button.ok{background:var(--ok)}
button:disabled{opacity:.5;cursor:wait}
/* 首页 hero：居中 + logo + 提示语（结果出现后自动收紧为顶部输入条） */
.hero{display:flex;flex-direction:column;justify-content:center;min-height:calc(100vh - 90px);padding:24px 0 12px}
body.hasdata .hero{display:block;min-height:0;padding:6px 0 0}
.heroin{max-width:560px;margin:0 auto;width:100%;text-align:center}
.hero .hmk{display:flex;flex-direction:column;align-items:center;gap:10px;margin-bottom:18px}
.hero .htitle{font-size:24px;font-weight:700;color:#111;letter-spacing:.5px}
.hero .hsub{font-size:13px;color:var(--sub)}
body.hasdata .hmk,body.hasdata .hhint{display:none}
.hhint{margin:30px auto 0;max-width:92vw;width:fit-content;display:flex;flex-direction:column;gap:10px}
.hrow{display:flex;align-items:flex-start;gap:9px;font-size:12.5px;color:var(--sub);line-height:1.75;text-align:left}
.hi{flex:none;font-size:13px;line-height:1.7}
/* markdown 渲染（AI 气泡内；用户气泡仍为纯文本 pre-wrap） */
.mrow.ai .bubble{white-space:normal}
.md p{margin:0 0 8px}
.md>:last-child{margin-bottom:0}
.md h3,.md h4,.md h5,.md h6{margin:10px 0 6px;font-weight:600;color:inherit}
.md h3{font-size:17px}.md h4{font-size:16px}.md h5,.md h6{font-size:15px}
.md ul,.md ol{margin:4px 0 8px;padding-left:1.5em}
.md li{margin:2px 0}
.md .mdpre{background:#fff;border:1px solid #e6e6ea;border-radius:10px;padding:10px 12px;margin:8px 0;overflow-x:auto}
.md .mdpre code{font:12.5px/1.6 ui-monospace,SFMono-Regular,Consolas,"Courier New",monospace;color:#1f2328;white-space:pre}
.md code.ic{background:rgba(0,0,0,.06);border-radius:4px;padding:1px 5px;font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;color:#1f2328}
.md .mdq{border-left:3px solid #d4d4d8;padding:2px 12px;margin:8px 0;color:#5f6368}
.md .mdhr{border:0;border-top:1px solid #ddd;margin:12px 0}
.md .mdtw{overflow-x:auto;margin:8px 0}
.md table.mdt{border-collapse:collapse;font-size:14px}
.md .mdt th,.md .mdt td{border:1px solid #e2e2e6;padding:5px 10px;text-align:left}
.md .mdt th{background:#ebebee;font-weight:600}
.md a{color:var(--acc);text-decoration:underline}
/* 解析框下方：左下/右下 批量按钮（小、不突出） */
.batchbar{display:none;justify-content:space-between;gap:8px;margin:4px 0 10px}
.batchbar.show{display:flex}
.bbtn{background:#f0f1f3;color:#3c3f46;font-size:13.5px;padding:7px 14px;border-radius:10px}
.bbtn:active{filter:brightness(.92)}
.bbtn.b{font-weight:400}
/* 多选模式：长按进入，顶部“多选”条 + 媒体打钩 + 底部 分享/下载 */
.selbar{position:fixed;top:0;left:0;right:0;z-index:96;display:none;align-items:center;gap:10px;padding:calc(env(safe-area-inset-top) + 10px) 12px 10px;background:#fff;border-bottom:1px solid var(--line);box-shadow:0 2px 12px rgba(0,0,0,.06)}
.selbar .st{flex:1;text-align:center;font-size:15px;font-weight:600}
.selbar button{background:var(--acc);font-size:13.5px}
.selbar .gray2{background:#e6e8ec;color:var(--ink)}
body.selmode .selbar{display:flex}
body.selmode .bar,body.selmode .batchbar{visibility:hidden}
.selck{position:absolute;top:6px;left:6px;width:24px;height:24px;border-radius:50%;border:2px solid #fff;background:rgba(0,0,0,.35);color:#fff;font-size:14px;line-height:20px;text-align:center;box-sizing:border-box;display:none;z-index:6;pointer-events:none}
body.selmode #chat .med .selck{display:block}
#chat .med.sel-on .selck{background:var(--acc);border-color:var(--acc)}
#chat .med.sel-on{outline:3px solid var(--acc);outline-offset:-3px}
body.selmode #chat .med{cursor:pointer}
.selfoot{position:fixed;left:0;right:0;bottom:0;z-index:96;display:none;flex-direction:column;background:#fff;border-top:1px solid var(--line);box-shadow:0 -2px 12px rgba(0,0,0,.06)}
body.selmode .selfoot{display:flex}
.selbtns{display:flex;gap:10px;padding:10px 14px calc(10px + env(safe-area-inset-bottom))}
.selbtns button{flex:1;padding:13px 0;font-size:15px;font-weight:500}
.selTip{display:none;max-height:34vh;overflow-y:auto;padding:9px 14px;font-size:12px;line-height:1.6;color:#5a4a00;background:#fff8dc;border-top:1px solid #efe3ad}
#meta{display:none;color:var(--sub);font-size:12.5px;text-align:center;margin:2px 0 10px}
#meta b{color:var(--ink);font-weight:400}
/* 对话流（1:1 复刻豆包分享页：用户蓝色靠右 / AI 灰色全宽，无头像） */
#chat{display:none}
.shead{padding:14px 0 0}
.shead h1{font-size:21px;font-weight:700;color:#000;line-height:1.45;letter-spacing:.2px}
.shead .sdate{font-size:12px;color:#9a9a9a;margin-top:6px}
.sdiv{border-bottom:1px solid var(--line);margin:16px 0 6px}
.mrow{margin:16px 0}
.mrow.user{display:flex;justify-content:flex-end}
.mrow.user .mbody{display:flex;flex-direction:column;align-items:flex-end;max-width:100%}
.mtime{text-align:center;color:#b0b0b4;font-size:11px;margin:16px 0 8px;user-select:none}
.bubble{max-width:600px;padding:9px 16px;border-radius:12px;font-size:16px;line-height:1.7;word-break:break-word;white-space:pre-wrap;text-align:left}
.mrow.user .bubble{background:#0066ff;color:#fff}
.mrow.ai .bubble{background:#f4f4f4;color:rgba(0,0,0,.85);border-radius:16px;max-width:none;padding:14px 16px}
.bubble a{color:inherit;text-decoration:underline}
/* 媒体：不叠任何下载按钮 */
.meds{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.mrow.ai .meds{margin-left:0}
.mrow.user .meds{justify-content:flex-end}
.med{position:relative;border-radius:12px;overflow:hidden;background:#111;cursor:zoom-in;-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}
.med img{display:block;max-width:min(320px,68vw);max-height:300px;object-fit:cover}
.med.v{width:min(240px,60vw);aspect-ratio:9/16;display:flex;align-items:center;justify-content:center}
.med.v.l{aspect-ratio:16/9;width:min(340px,86vw)}
.med.v img{width:100%;height:100%;object-fit:cover;opacity:.92}
.playbd{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
.playbd i{width:44px;height:44px;border-radius:50%;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center}
.playbd i:after{content:"";border-style:solid;border-width:9px 0 9px 15px;border-color:transparent transparent transparent #fff;margin-left:4px}
.vdur{position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,.62);color:#fff;font-size:11px;border-radius:6px;padding:1px 7px;pointer-events:none}
.failchip{display:inline-flex;align-items:center;gap:6px;border:1px dashed #c9ccd4;color:#8a8f99;background:#f3f4f6;border-radius:10px;padding:7px 12px;font-size:12.5px;margin-top:8px}
.upimg img{max-width:min(150px,32vw);max-height:150px}
.empty{min-height:180px}
/* 时间筛选栏（移植自已验证的网站模式） */
.fbar{display:none;align-items:center;flex-wrap:wrap;gap:6px;margin:2px 0 10px}
.fbar.show{display:flex}
.flabel{font-size:12px;color:var(--sub)}
.chip{padding:5px 12px;border-radius:16px;background:#f0f1f3;color:#3c3f46;font-size:12.5px;cursor:pointer;user-select:none}
.chip.on{background:var(--acc);color:#fff}
.fsel{border:0;border-radius:16px;padding:5px 10px;font-size:12.5px;background:#f0f1f3;color:#3c3f46;cursor:pointer}
.fsel.on{background:var(--acc);color:#fff}
.emptyhint{background:#fff;border:1px dashed #d8dadf;border-radius:12px;padding:16px;text-align:center;color:var(--sub);font-size:13px;margin:12px 0}
.emptyhint span{color:var(--acc);cursor:pointer}
html.restoring body{visibility:hidden} /* 刷新恢复期间整页隐藏，防主页一闪而过 */
/* 单击媒体操作菜单（豆包风底部弹层）：放大 / 下载 / 复制 / 多选 */
#act{position:fixed;inset:0;background:rgba(0,0,0,.4);display:none;align-items:flex-end;justify-content:center;z-index:97}
.actbox{width:100%;max-width:480px;background:#fff;border-radius:18px 18px 0 0;padding:8px 14px calc(12px + env(safe-area-inset-bottom));box-shadow:0 -4px 24px rgba(0,0,0,.18)}
.actbtn{display:block;width:100%;text-align:left;background:transparent;color:var(--ink);border-radius:12px;padding:14px 12px;font-size:15.5px;border-bottom:1px solid #f0f1f3}
.actbtn:active{background:#f4f5f7}
.actbtn.cancel{text-align:center;color:var(--sub);border-bottom:0;margin-top:6px;font-size:14.5px}
.acttip{font-size:11.5px;color:var(--sub);text-align:center;padding:2px 0 8px}
/* 解析中 / 0/0 / 错误页（沿用已验证样式） */
.zero{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:26px 18px;margin:14px 0;text-align:center}
.zero .zt{font-size:16px;font-weight:500;margin-bottom:10px}
.zero .zs{color:var(--sub);font-size:12.5px;line-height:1.8;margin-bottom:10px}
.zero .zd{color:#8a6d1a;background:#fff8e6;border:1px solid #ffe9b3;border-radius:8px;font-size:11px;padding:6px 10px;margin-bottom:14px;word-break:break-all}
.zero button{padding:11px 26px;font-size:14px}
/* 粘贴兜底框 */
#pasteBox{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:98}
.pbox{background:var(--card);border-radius:14px;padding:16px;width:min(92vw,420px)}
.ptitle{font-size:13px;margin-bottom:10px;color:var(--ink)}
#ptext{width:100%;height:90px;border:1px solid var(--line);border-radius:8px;padding:9px 12px;font-size:16px;resize:none;background:#fff}
.pbtns{display:flex;gap:8px;justify-content:flex-end;margin-top:10px}
/* 灯箱：× 左上，下载 右上 */
#lb{position:fixed;inset:0;background:rgba(0,0,0,.93);display:none;flex-direction:column;align-items:center;justify-content:center;z-index:99}
#lbImg{max-width:94vw;max-height:76vh;object-fit:contain}
#lbImg.loading{min-width:120px;min-height:120px;background:url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 40 40"><circle cx="20" cy="20" r="16" stroke="white" stroke-width="3" fill="none" stroke-dasharray="70" stroke-linecap="round"><animateTransform attributeName="transform" type="rotate" from="0 20 20" to="360 20 20" dur="1s" repeatCount="indefinite"/></circle></svg>') center no-repeat}
#lbVid{max-width:94vw;max-height:76vh;background:#000;border-radius:6px}
#lbClose{position:absolute;top:calc(env(safe-area-inset-top) + 10px);left:12px;background:rgba(255,255,255,.15);font-size:20px;width:40px;height:40px;padding:0;border-radius:50%}
#lbDl{position:absolute;top:calc(env(safe-area-inset-top) + 10px);right:12px;background:rgba(255,255,255,.18);font-size:14px;padding:10px 16px;border-radius:20px}
#lb .nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.15);font-size:22px;width:46px;height:46px;padding:0;border-radius:50%}
#lb .prev{left:10px}#lb .next{right:10px}
#lbCnt{color:#fff;font-size:12px;margin-top:10px}
/* 批量选择页（与已验证版本同源） */
#picker{position:fixed;inset:0;background:var(--bg);z-index:97;display:none;flex-direction:column}
.pkhead{display:flex;align-items:center;gap:10px;padding:12px 14px;background:var(--card);border-bottom:1px solid var(--line)}
.pkhead .pkclose{width:34px;height:34px;padding:0;font-size:20px;line-height:1;border-radius:50%}
.pktitle{flex:1;text-align:center;font-size:16px;font-weight:500;margin-right:34px}
.pkall{color:var(--acc);font-size:14px;cursor:pointer;user-select:none;padding:4px 2px}
.pkgrid{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;padding:12px;align-content:start}
.pktile{position:relative;background:#000;border-radius:8px;overflow:hidden;aspect-ratio:1/1;cursor:pointer}
.pktile img{width:100%;height:100%;object-fit:cover;display:block}
.pkdim{position:absolute;left:0;right:0;bottom:0;background:linear-gradient(transparent,rgba(0,0,0,.65));color:#fff;font-size:9.5px;text-align:center;padding:12px 2px 3px;pointer-events:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;height:16px;box-sizing:border-box;line-height:16px}
.pkck{position:absolute;top:6px;right:6px;width:24px;height:24px;border-radius:50%;border:2px solid #fff;background:rgba(0,0,0,.35);color:#fff;font-size:14px;line-height:20px;text-align:center;box-sizing:border-box}
.pktile.sel .pkck{background:var(--acc);border-color:var(--acc)}
.pktile.sel{outline:2px solid var(--acc);outline-offset:-2px}
.pkfoot{padding:10px 14px calc(10px + env(safe-area-inset-bottom));background:var(--card);border-top:1px solid var(--line);display:flex}
.pkfoot button{flex:1;padding:13px 0;font-size:15px;font-weight:500}
.pktip{display:none;padding:9px 14px;font-size:12px;line-height:1.6;color:#5a4a00;background:#fff8dc;border-top:1px solid #efe3ad;max-height:34vh;overflow-y:auto}
.pkhint{padding:7px 14px 0;font-size:11px;color:var(--sub);text-align:center;line-height:1.7}
.pktip .pkrow,.selTip .pkrow{display:flex;align-items:center;gap:8px;margin:5px 0;padding:7px 8px;background:rgba(255,255,255,.75);border:1px solid #d8c878;border-radius:9px}
.pktip .pkrow .pkname,.selTip .pkrow .pkname{flex:1;font-size:11px;color:#5a4a00;word-break:break-all}
.pktip .pkrow a.pkdlbtn,.selTip .pkrow a.pkdlbtn{flex:none;padding:7px 14px;border-radius:8px;font-size:12px;font-weight:700;color:#fff;text-decoration:none;background:linear-gradient(135deg,#2f6bff,#22b1ff)}
.pktip .pkfb{display:block;width:100%;margin-top:8px;padding:9px 0;border:none;border-radius:9px;font-size:13px;font-weight:500;color:#fff;background:linear-gradient(135deg,#2f6bff,#22b1ff)}
button.blue{background:linear-gradient(135deg,#2f6bff,#22b1ff)}
button.blue.busy,button.ok.busy{filter:brightness(1.12);animation:pulse 1s ease infinite}
@keyframes pulse{50%{filter:brightness(.88)}}
.disclaimer{margin-top:18px;padding:0 10px;font-size:10px;color:var(--sub);text-align:center;line-height:1.6;opacity:.85}
.hver{display:inline-block;margin-top:4px;font-size:9.5px;color:#b0b4bc;letter-spacing:.5px}
/* 遮罩打开时隐藏正文视频缩略块之外的原生视频（灯箱视频不在 #chat 内，不受影响） */
body.hidevid #chat .med video{visibility:hidden!important}
@media(max-width:600px){
  body{padding:0 12px calc(14px + env(safe-area-inset-bottom))}
  .bubble{max-width:78vw;font-size:15px}
  .mrow.ai .bubble{max-width:none}
  .med img{max-width:min(300px,78vw)}
  .med.v{width:min(220px,64vw)}
  .med.v.l{width:min(320px,90vw)}
  .pkgrid{grid-template-columns:repeat(3,1fr);gap:6px;padding:10px}
  #lbImg,#lbVid{width:100vw;height:calc(100vh - 120px);max-width:none;max-height:none}
  #lb .nav{display:none}
}
</style>
<script>
// 防主页闪烁：刷新恢复场景先隐藏整页，等快照重绘完再显示（700ms 兜底）
try{if(sessionStorage.getItem('dwc_active')==='1'&&sessionStorage.getItem('dwc_data')){
  document.documentElement.classList.add('restoring');
  setTimeout(function(){document.documentElement.classList.remove('restoring');},700);
}}catch(e){}
</script>
</head>
<body>
<div class="wrap">
  <div class="hero" id="hero">
    <div class="heroin">
      <div class="hmk">
        <svg width="64" height="64" viewBox="0 0 64 64" aria-hidden="true">
          <defs><linearGradient id="hlg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3b82f6"/><stop offset="1" stop-color="#8b5cf6"/></linearGradient></defs>
          <rect x="2" y="2" width="60" height="60" rx="16" fill="url(#hlg)"/>
          <path d="M20 20h24a5 5 0 0 1 5 5v11a5 5 0 0 1-5 5H31l-9 8v-8h-2a5 5 0 0 1-5-5V25a5 5 0 0 1 5-5z" fill="#fff"/>
          <circle cx="26" cy="31" r="2.6" fill="#3b82f6"/><circle cx="33.5" cy="31" r="2.6" fill="#5f7df8"/><circle cx="41" cy="31" r="2.6" fill="#8b5cf6"/>
        </svg>
        <div class="htitle">DB</div>
        <div class="hsub">对话 1:1 还原 · 图片视频无水印保存</div>
      </div>
      <div class="bar">
        <input id="url" placeholder="粘贴 DB app 分享出来的链接…" autocomplete="off" spellcheck="false">
        <button class="gray" id="btnClear" onclick="clearUrl()" title="清空输入框，自己手动粘贴">×</button>
        <button class="gray" id="btnPaste" onclick="pasteUrl()" title="清空旧链接并填入剪贴板内容">粘贴</button>
        <button id="btnParse" onclick="doParse()">解析</button>
      </div>
      <div class="hhint">
        <div class="hrow"><span class="hi">🔗</span><span>把 DB app 分享出来的链接粘贴到上面<br>点「解析」还原完整对话</span></div>
        <div class="hrow"><span class="hi">👆</span><span>点图片 / 视频：下载 · 复制链接 · 多选</span></div>
        <div class="hrow"><span class="hi">☑️</span><span>长按图片直接进多选</span></div>
        <div class="hrow"><span class="hi">⬇️</span><span>解析框下方可批量下载图片 / 视频</span></div>
      </div>
    </div>
  </div>
  <div class="batchbar" id="batchbar">
    <button class="bbtn" id="btnBI" onclick="openPicker('image')">批量下载图片</button>
    <button class="bbtn" id="btnBV" onclick="openPicker('video')">批量下载视频</button>
  </div>
  <div class="fbar" id="fbar"></div>
  <div id="meta"></div>
  <div id="chat"></div>
  <div id="main"></div>
  <div class="disclaimer">本工具仅供交流学习使用，不得用于商业用途。<br>所解析内容的版权归原作者所有，如有侵权请联系删除。<br><span class="hver">V10 · 2026-09-18</span></div>
</div>
<div class="selbar"><button class="gray2" onclick="exitSel()">取消</button><div class="st" id="selTitle">多选</div><button onclick="toggleSelAll()">全选</button></div>
<div class="selfoot"><div class="selTip" id="selTip"></div><div class="selbtns"><button class="ok" id="selShare" onclick="doSelShare()">📤 分享</button><button class="blue" id="selDl" onclick="doSelDownload()">⬇ 下载</button></div></div>
<div id="act" onclick="if(event.target===this)hideAct()">
  <div class="actbox">
    <div class="acttip">点「多选」可批量勾选后分享 / 下载</div>
    <button class="actbtn" onclick="actLb()">🔍 放大查看</button>
    <button class="actbtn" onclick="actDl()">⬇ 下载（苹果弹出分享面板，存储到相册）</button>
    <button class="actbtn" onclick="actCopy()">📋 复制链接（图片为无水印原图直链）</button>
    <button class="actbtn" onclick="actSel()">☑ 多选</button>
    <button class="actbtn cancel" onclick="hideAct()">取消</button>
  </div>
</div>
<div id="pasteBox" onclick="if(event.target===this)hidePaste()">
  <div class="pbox">
    <div class="ptitle">粘贴链接（Ctrl+V / 长按粘贴）</div>
    <textarea id="ptext" placeholder="按 Ctrl+V 粘贴到这里…"></textarea>
    <div class="pbtns"><button class="gray" onclick="hidePaste()">取消</button><button class="ok" onclick="confirmPaste()">确定</button></div>
  </div>
</div>
<div id="picker">
  <div class="pkhead">
    <button class="gray pkclose" onclick="hidePicker()">×</button>
    <div class="pktitle" id="pkTitle">保存图片</div>
    <span class="pkall" id="pkAll" onclick="toggleAll()">全选</span>
  </div>
  <div class="pkgrid" id="pkGrid"></div>
  <div class="pktip" id="pkTip"></div>
  <div class="pkhint">📱 仅苹果手机用「批量分享」进相册 · 💻 win/mac/安卓用「批量下载」<br>苹果：点保存后跳分享 → 「存储图像 / 存储视频」进相册　|　其他：浏览器下载管理里找</div>
  <div class="pkfoot"><button class="ok" id="pkShare" onclick="doPickShare()">📤 批量分享</button><button class="blue" id="pkDl" onclick="doPickDownload()">⬇ 批量下载</button></div>
</div>
<div id="lb" onclick="if(event.target===this)hideLb()">
  <button id="lbClose" onclick="hideLb()">×</button>
  <button id="lbDl" onclick="lbDl()">⬇ 下载</button>
  <button class="nav prev" onclick="lbNav(-1)">‹</button>
  <img id="lbImg" alt="">
  <video id="lbVid" controls playsinline style="display:none"></video>
  <button class="nav next" onclick="lbNav(1)">›</button>
  <div id="lbCnt"></div>
</div>
<script>
const isIOS=/iPad|iPhone|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
const isCoarse=matchMedia('(pointer: coarse)').matches;
let DATA=null,MEDIA=[],lbIdx=0,forceFresh=false,pickKind='image',pickSel=new Set(),pkRows=[],timeFilter=0;
const $=s=>document.querySelector(s);
function fmtSize(n){if(!n)return '';const u=['B','KB','MB','GB'];let i=0;while(n>=1024&&i<3){n/=1024;i++;}return n.toFixed(i===0||n>=100?0:1)+u[i];}
function fmtDur(d){return !d?'':(d<60?d.toFixed(0)+'s':(d/60).toFixed(1)+'min');}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function mdLite(s){let h=esc(s);h=h.replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>');h=h.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');return h;}
/* ===== markdown 渲染（AI 回复：标题/列表/代码块/表格/引用/链接/粗斜体） ===== */
function inlineMd(s){
  const codes=[],links=[];
  s=s.replace(/`([^`\n]+)`/g,(m,c)=>{codes.push('<code class="ic">'+c+'</code>');return '\u0001'+(codes.length-1)+'\u0001';});
  s=s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,(m,t,u)=>{links.push('<a href="'+u+'" target="_blank" rel="noopener">'+t+'</a>');return '\u0002'+(links.length-1)+'\u0002';});
  s=s.replace(/\bhttps?:\/\/[^\s<>"')，。；：、）】]+/g,m=>'<a href="'+m+'" target="_blank" rel="noopener">'+m+'</a>');
  s=s.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g,'$1<em>$2</em>');
  s=s.replace(/~~([^~\n]+)~~/g,'<del>$1</del>');
  s=s.replace(/\u0001(\d+)\u0001/g,(m,i)=>codes[+i]);
  s=s.replace(/\u0002(\d+)\u0002/g,(m,i)=>links[+i]);
  return s;
}
function mdRender(src){
  const lines=String(src||'').replace(/\r\n?/g,'\n').split('\n');
  let html='',i=0;
  while(i<lines.length){
    const ln=lines[i];
    if(!ln.trim()){i++;continue;}
    if(/^\s*```\s*\S*\s*$/.test(ln)){
      i++;const buf=[];
      while(i<lines.length&&!/^\s*```\s*$/.test(lines[i])){buf.push(lines[i]);i++;}
      i++;html+='<pre class="mdpre"><code>'+esc(buf.join('\n'))+'</code></pre>';continue;
    }
    if(/^#{1,6}\s+/.test(ln)){
      const lv=Math.min((ln.match(/^#+/)[0]).length+2,6);
      html+='<h'+lv+'>'+inlineMd(esc(ln.replace(/^#{1,6}\s+/,'').trim()))+'</h'+lv+'>';i++;continue;
    }
    if(/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(ln)){html+='<hr class="mdhr">';i++;continue;}
    if(/^\s*\|.*\|\s*$/.test(ln)&&i+1<lines.length&&/^\s*\|[\s:|-]+\|\s*$/.test(lines[i+1])){
      const pr=r=>r.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
      const head=pr(ln);i+=2;const rows=[];
      while(i<lines.length&&/^\s*\|.*\|\s*$/.test(lines[i])){rows.push(pr(lines[i]));i++;}
      html+='<div class="mdtw"><table class="mdt"><thead><tr>'+head.map(c=>'<th>'+inlineMd(esc(c))+'</th>').join('')+'</tr></thead><tbody>'
        +rows.map(r=>'<tr>'+r.map(c=>'<td>'+inlineMd(esc(c))+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';continue;
    }
    if(/^\s*>\s?/.test(ln)){
      const buf=[];
      while(i<lines.length&&/^\s*>\s?/.test(lines[i])){buf.push(lines[i].replace(/^\s*>\s?/,''));i++;}
      html+='<blockquote class="mdq">'+inlineMd(esc(buf.join('\n'))).replace(/\n/g,'<br>')+'</blockquote>';continue;
    }
    const ulm=ln.match(/^(\s*)[-*\u2022]\s+(.*)$/),olm=ln.match(/^(\s*)\d+[.\u3001)]\s+(.*)$/);
    if(ulm||olm){
      const ord=!!olm&&!ulm;
      const re=ord?/^(\s*)\d+[.\u3001)]\s+(.*)$/:/^(\s*)[-*\u2022]\s+(.*)$/;
      const items=[];
      while(i<lines.length){
        const mm=lines[i].match(re);
        if(mm){items.push(mm[2]);i++;}
        else if(items.length&&lines[i].trim()&&/^\s{2,}\S/.test(lines[i])&&!/^\s*(```|#{1,6}\s|>\s?|\|)/.test(lines[i])){items[items.length-1]+='<br>'+lines[i].trim();i++;}
        else break;
      }
      const tag=ord?'ol':'ul';
      html+='<'+tag+'>'+items.map(t=>'<li>'+inlineMd(esc(t))+'</li>').join('')+'</'+tag+'>';continue;
    }
    const buf=[];
    while(i<lines.length&&lines[i].trim()&&!/^\s*(```|#{1,6}\s|>\s?|[-*\u2022]\s|\d+[.\u3001)]\s|\|)/.test(lines[i])&&!/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(lines[i])){buf.push(lines[i]);i++;}
    if(!buf.length){buf.push(lines[i]);i++;}
    html+='<p>'+inlineMd(esc(buf.join('\n'))).replace(/\n/g,'<br>')+'</p>';
  }
  return '<div class="md">'+html+'</div>';
}

function setFilter(m){timeFilter=m;try{localStorage.setItem('dwc_filter',m);}catch(e){}renderChat();window.scrollTo(0,0);}
function filterBar(){
  $('#fbar').classList.add('show');
  $('#fbar').innerHTML=`<span class="flabel">时间筛选</span>`
    +[[0,'全部'],[30,'0.5h'],[60,'1h'],[360,'6h'],[720,'12h']].map(([v,t])=>`<span class="chip ${timeFilter===v?'on':''}" onclick="setFilter(${v})">${t}</span>`).join('')
    +`<select class="fsel ${[1440,2880,4320].includes(timeFilter)?'on':''}" onchange="setFilter(+this.value||0)"><option value="0" ${![1440,2880,4320].includes(timeFilter)?'selected':''}>更多</option><option value="1440" ${timeFilter===1440?'selected':''}>24h</option><option value="2880" ${timeFilter===2880?'selected':''}>48h</option><option value="4320" ${timeFilter===4320?'selected':''}>72h</option></select>`;
}

window.addEventListener('DOMContentLoaded',()=>{
  const q=new URLSearchParams(location.search);
  const qu=q.get('url');
  if(qu&&qu.includes('/thread/')){
    document.documentElement.classList.remove('restoring');
    $('#url').value=qu;doParse();return;
  }
  // 仅"本次会话正在浏览结果页"才恢复；主页绝不缓存链接——点过×、重开后刷新都是干净的空输入框
  try{
    const su=localStorage.getItem('dwc_lastUrl');
    if(su&&su.includes('/thread/')&&sessionStorage.getItem('dwc_active')==='1'){
      timeFilter=+localStorage.getItem('dwc_filter')||0;
      const raw=sessionStorage.getItem('dwc_data');
      if(raw){
        // 秒恢复：直接用会话快照重绘，不重新请求——无"解析中"过程、无主页闪烁
        DATA=JSON.parse(raw);
        (DATA.images||[]).forEach((it,i)=>it._fi=i);
        (DATA.videos||[]).forEach((it,i)=>it._fi=i);
        $('#url').value=su;
        renderChat();
        $('#chat').style.display='block';
        document.documentElement.classList.remove('restoring');
      }else{
        $('#url').value=su;
        document.documentElement.classList.remove('restoring');
        doParse(true); // 没有快照（如分享链接直达后刷新）才走重新解析，保留筛选档
      }
    }else{
      document.documentElement.classList.remove('restoring');
    }
  }catch(e){document.documentElement.classList.remove('restoring');}
});
// 前进/后退的页面缓存（bfcache）恢复时：若当前不是结果会话，强制回到干净主页
window.addEventListener('pageshow',e=>{
  if(e.persisted){try{if(sessionStorage.getItem('dwc_active')!=='1')clearUrl();}catch(err){}}
});

async function doParse(keep){
  let u=$('#url').value.trim();
  if(!u){clearUrl();return;}
  const urls=u.match(/https?:..[^ ]+/g)||[];
  const hit=urls.find(x=>x.includes('doubao.com'))||urls.find(x=>x.includes('/thread/'))||'';
  if(hit){u=hit.replace(/[^a-zA-Z0-9%._~=?&/-]+$/,'');}
  $('#url').value=u;
  if(!u.includes('/thread/')){showErr('链接格式不对','链接需包含 /thread/（请从豆包分享面板复制链接）');return;}
  try{localStorage.setItem('dwc_lastUrl',u);}catch(e){}
  // 时间筛选规则：刷新恢复解析时保留所选档位；主动解析新链接时重置为"全部"
  // （筛选档存在 localStorage 全局生效，残留档位会把新解析内容全部筛空）
  if(!keep&&timeFilter){timeFilter=0;try{localStorage.setItem('dwc_filter','0');}catch(e){}}
  const btn=$('#btnParse');
  btn.disabled=true;const oldTxt=btn.textContent;btn.textContent='解析中…';
  $('#chat').style.display='none';$('#chat').innerHTML='';$('#main').innerHTML='';$('#meta').style.display='none';
  try{
    const r=await fetch('/api/chat?url='+encodeURIComponent(u)+(forceFresh?'&fresh=1':''));
    forceFresh=false;
    DATA=await r.json();
    if(DATA.error){showErr('解析失败',DATA.error,true);return;}
    if(!DATA.messages||!DATA.messages.length||(!DATA.image_count&&!DATA.video_count)){
      if(!forceFresh){
        btn.textContent='内容没取全，自动重试中…';
        await new Promise(r=>setTimeout(r,1500));
        forceFresh=true;
        return doParse(keep);
      }
      showErr('⚠️ 没有解析到内容','豆包现在会间歇性返回"空壳页"（反爬策略），不是链接坏了——点「重试解析」一两次通常就能出来。'+(DATA.debug?('<br>诊断：'+esc(JSON.stringify(DATA.debug))):''));
      return;
    }
    (DATA.images||[]).forEach((it,i)=>it._fi=i);
    (DATA.videos||[]).forEach((it,i)=>it._fi=i);
    renderChat();
    $('#chat').style.display='block';
    try{sessionStorage.setItem('dwc_active','1');}catch(e){}
  }catch(e){showErr('网络异常',String(e),true);}
  finally{btn.disabled=false;btn.textContent=oldTxt;}
}

function showErr(title,msg,retry){
  try{sessionStorage.removeItem('dwc_active');}catch(e){}
  exitSel();
  document.body.classList.remove('hasdata');
  $('#batchbar').classList.remove('show');
  $('#main').innerHTML=`<div class="zero"><div class="zt">${title}</div><div class="zs">${msg}<br>豆包偶尔拦截抓取（反爬），重试一两次通常就能成功。</div>${retry?'<button class="ok" onclick="forceFresh=true;doParse()">↻ 重试解析（跳过缓存）</button>':''}</div>`;
}

function renderChat(){
  MEDIA=[];
  exitSel();
  const d0=DATA.messages.find(x=>x.time)||{};
  const dm=(d0.time||'').match(/^(\d{4})-(\d{2})-(\d{2})/);
  const dateStr=dm?(+dm[1])+' 年 '+(+dm[2])+' 月 '+(+dm[3])+' 日':'';
  const shead=`<div class="shead"><h1>${esc(DATA.share_name||'豆包分享对话')}</h1><div class="sdate">${dateStr?dateStr+' • ':''}AI 生成可能有误 注意核实</div></div><div class="sdiv"></div>`;
  // 时间筛选：隐藏早于档位的消息（无时间戳的消息保留）
  const cut=timeFilter?Date.now()-timeFilter*60000:0;
  const msgs=DATA.messages.filter(m=>!timeFilter||!m.epoch||m.epoch*1000>=cut);
  if(!msgs.length){
    MEDIA=[];
    $('#chat').innerHTML=shead+`<div class="emptyhint">时间筛选把 ${DATA.messages.length} 条消息全部筛掉了，<span onclick="setFilter(0)">点此查看全部</span></div>`;
    $('#meta').style.display='block';
    $('#meta').innerHTML=`共 <b>${DATA.messages.length}</b> 条消息，当前档位下 0 条`;
    $('#batchbar').classList.add('show');
    filterBar();
    return;
  }
  let html=shead,lastEpoch=0;
  for(const m of DATA.messages){
    if(m.epoch&&lastEpoch&&(m.epoch-lastEpoch)>300||(!lastEpoch&&m.time)){
      html+=`<div class="mtime">${m.time||''}</div>`;}
    if(m.epoch)lastEpoch=m.epoch;
    let inner='';
    const meds=[];
    const newMed=(html,ref)=>{meds.push({html,ref,mi:MEDIA.length+meds.length});};
    for(const b of m.blocks){
      if(b.t==='text'){inner+=`<div class="bubble">${m.role==='ai'?mdRender(b.text):mdLite(b.text)}</div>`;}
      else if(b.t==='up'){newMed(`<div class="med upimg" data-kind="up" data-mi="${MEDIA.length+meds.length}"><img loading="lazy" src="/proxy?u=${encodeURIComponent(b.thumb||b.url)}"></div>`,{kind:'image',url:b.url,thumb:b.thumb,time:m.time,format:'png',upload:1});}
      else if(b.t==='img'){
        const it=DATA.images[b.i];
        if(b.fail){inner+=`<div class="failchip">⛔ 该图片未生成成功（作废）</div>`;continue;}
        newMed(`<div class="med" data-kind="image" data-mi="${MEDIA.length+meds.length}"><img loading="lazy" src="/proxy?u=${encodeURIComponent(it.thumb||it.url)}"></div>`,it);
      }else if(b.t==='vid'){
        const it=DATA.videos[b.i];
        if(b.fail||!it.poster&&!it.fallback_api){inner+=`<div class="failchip">⛔ 该视频未生成成功（作废）</div>`;continue;}
        const land=(it.width&&it.height&&+it.width>=+it.height)?' l':'';
        newMed(`<div class="med v${land}" data-kind="video" data-mi="${MEDIA.length+meds.length}"><img loading="lazy" src="/proxy?u=${encodeURIComponent(it.poster||'')}"><div class="playbd"><i></i></div><span class="vdur">${fmtDur(it.duration)||'视频'}</span></div>`,it);
      }
    }
    if(meds.length){
      // 媒体统一渲染在文本块之后（豆包创作场景媒体几乎都在文本后）
      inner+=`<div class="meds">${meds.map(x=>x.html).join('')}</div>`;
      meds.forEach(x=>MEDIA.push(x.ref));
    }
    html+=`<div class="mrow ${m.role}"><div class="mbody">${inner}</div></div>`;
  }
  $('#chat').innerHTML=html;
  $('#meta').style.display='block';
  $('#meta').innerHTML=`图片 <b>${DATA.image_count}</b> · 视频 <b>${DATA.video_count}</b>`
    +(timeFilter?` · 时间筛选后 <b>${msgs.length}/${DATA.messages.length}</b> 条`:'')
    +`${DATA.stale?' · <b>缓存结果</b>':''}`;
  $('#batchbar').classList.add('show');
  filterBar();
  attachMedEvents();
  document.body.classList.add('hasdata');
  window.scrollTo(0,0);
  prefetchMedia();
  // 结果快照入会话缓存：刷新时直接重绘（无网络请求、无"解析中"闪烁）
  try{sessionStorage.setItem('dwc_data',JSON.stringify(DATA));}catch(e){}
}

/* 后台预取全部媒体 blob：点击「下载/分享」时秒取，保证在用户手势窗口内弹分享/下载 */
function prefetchMedia(){
  for(const it of MEDIA){
    (async()=>{try{
      if(it.kind==='image'){getBlobCached('/proxy?u='+encodeURIComponent(it.url));}
      else{const s=await pickVideoSrc(it);getBlobCached(s.u);}
    }catch(e){}})();
  }
}

/* ===== 长按进入多选 / 点击放大（正文媒体不叠任何按钮） ===== */
let lpTimer=null,lpFired=false;
function attachMedEvents(){
  document.querySelectorAll('#chat .med').forEach(el=>{
    const mi=el.dataset.mi;
    if(mi!==undefined){ // 生成的图片/视频/上传图：进 MEDIA
      el.addEventListener('click',()=>{if(lpFired){lpFired=false;return;}if(selMode){toggleSel(+mi);return;}showAct(+mi);});
    }
    el.addEventListener('contextmenu',e=>{if(isCoarse)e.preventDefault();});
    el.addEventListener('touchstart',e=>{
      e.stopPropagation();
      if(mi===undefined)return;
      const tx=e.touches[0].clientX,ty=e.touches[0].clientY;
      lpTimer=setTimeout(()=>{
        lpFired=true;setTimeout(()=>{lpFired=false;},600);
        if(navigator.vibrate)navigator.vibrate(15);
        enterSel(+mi);
      },550);
      const cancel=()=>{clearTimeout(lpTimer);};
      el.addEventListener('touchmove',ev=>{if(Math.abs(ev.touches[0].clientX-tx)>12||Math.abs(ev.touches[0].clientY-ty)>12)cancel();},{passive:true,once:true});
      el.addEventListener('touchend',cancel,{once:true});
      el.addEventListener('touchcancel',cancel,{once:true});
    },{passive:true});
  });
  // 长按消息（气泡/对话行）也可进入多选
  document.querySelectorAll('#chat .mrow').forEach(row=>{
    let t=null;
    row.addEventListener('touchstart',()=>{
      t=setTimeout(()=>{if(navigator.vibrate)navigator.vibrate(15);enterSel();},550);
      const c=()=>clearTimeout(t);
      row.addEventListener('touchmove',c,{once:true,passive:true});
      row.addEventListener('touchend',c,{once:true});
      row.addEventListener('touchcancel',c,{once:true});
    },{passive:true});
  });
}

/* ===== 多选模式：长按进入 → 打钩 → 底部 分享/下载 ===== */
let selMode=false,selSet=new Set();
function enterSel(pre){
  if(!DATA)return;
  if(selMode&&pre!=null){toggleSel(pre);return;}
  selMode=true;selSet=new Set();if(pre!=null)selSet.add(pre);
  document.body.classList.add('selmode');
  document.querySelectorAll('#chat .med').forEach(el=>{
    if(el.dataset.mi!==undefined&&!el.querySelector('.selck')){
      const s=document.createElement('span');s.className='selck';s.textContent='✓';el.appendChild(s);
    }
  });
  updateSelUI();
}
function exitSel(){
  selMode=false;selSet=new Set();
  document.body.classList.remove('selmode');
  const t=$('#selTip');if(t){t.style.display='none';t.innerHTML='';}
  document.querySelectorAll('#chat .med.sel-on').forEach(el=>el.classList.remove('sel-on'));
  updateSelUI();
}
function toggleSel(mi){selSet.has(mi)?selSet.delete(mi):selSet.add(mi);updateSelUI();}
function toggleSelAll(){
  if(selSet.size===MEDIA.length)selSet=new Set();
  else selSet=new Set(MEDIA.map((_,i)=>i));
  updateSelUI();
}
function updateSelUI(){
  document.querySelectorAll('#chat .med').forEach(el=>{
    if(el.dataset.mi===undefined)return;
    el.classList.toggle('sel-on',selMode&&selSet.has(+el.dataset.mi));
  });
  const n=selSet.size;
  $('#selTitle').textContent='多选 · 已选 '+n+' 项';
  $('#selShare').disabled=!n;$('#selDl').disabled=!n;
  $('#selShare').textContent=n?'📤 分享（'+n+'）':'📤 分享';
  $('#selDl').textContent=n?'⬇ 下载（'+n+'）':'⬇ 下载';
}

/* ===== 单击媒体操作菜单：放大 / 下载 / 复制 / 多选 ===== */
let actIdx=-1;
function showAct(i){actIdx=i;$('#act').style.display='flex';}
function hideAct(){$('#act').style.display='none';actIdx=-1;}
function actLb(){const i=actIdx;hideAct();showLb(i);}
function actDl(){const i=actIdx;hideAct();saveOne(i);}
function actCopy(){const i=actIdx;hideAct();copyOne(i);}
function actSel(){const i=actIdx;hideAct();enterSel(i);}
async function saveOne(i){
  const it=MEDIA[i];
  if(!it){toast('内容已过期，请重新解析');return;}
  try{
    toast('⏳ 正在拉取文件，请稍候…',0); /* 常驻提示，防止用户以为没反应 */
    let blob,name,type,u;
    if(it.kind==='image'){
      u='/proxy?u='+encodeURIComponent(it.url);
      name=(it.upload?'ref_':'img_')+(it._fi!==undefined?fnum(it._fi+1):'')+'_'+fstamp(it)+'.'+(it.format==='png'?'png':'jpg');
      blob=await getBlobCached(u);type=blob.type||'image/jpeg';
    }else{
      toast('⏳ 正在拉取视频（文件较大，可能要几秒）…',0);
      const s=await pickVideoSrc(it,i);u=s.u;name=s.name;
      blob=await getBlobCached(u);type='video/mp4';
    }
    const f=new File([blob],name,{type});
    // 关键：blob 已预取缓存，这里 await 的是已完成的 Promise（微任务），
    // navigator.share 仍处于用户手势窗口内——iOS 能正常弹出分享面板
    if(isIOS&&navigator.canShare&&navigator.canShare({files:[f]})){
      toast('✅ 拉取成功，正在拉起分享面板…',0);
      await navigator.share({files:[f]});
      toast('已弹出分享，点「存储图像/存储视频」存入相册',3000);
      return;
    }
    if(isIOS){toast('✅ 拉取成功',2000);window.open(u,'_blank');toast('浏览器不支持分享，已在新窗口打开，长按可存储',3000);}
    else{toast('✅ 拉取成功，开始下载',2000);dlBlob(blob,name);toast('已提交下载，在浏览器下载管理里查看',3000);}
  }catch(e){
    if(e&&e.name==='AbortError'){toast('已取消分享',1500);return;}
    if(e&&e.name==='NotAllowedError'){toast('系统没弹出分享面板，请再点一次「下载」',3500);return;}
    toast('保存失败，请重试：'+(e&&e.message||e),3500);
  }
}
function legacyCopy(t,okMsg){
  try{
    const ta=document.createElement('textarea');
    ta.value=t;ta.style.cssText='position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta);ta.focus();ta.select();
    const ok=document.execCommand('copy');ta.remove();
    toast(ok?(okMsg||'已复制'):'复制失败，请长按链接手动复制');
  }catch(e){toast('复制失败，请长按链接手动复制');}
}
function toPng(b){
  return new Promise(res=>{
    try{
      const im=new Image(),u=URL.createObjectURL(b);
      im.onload=()=>{const c=document.createElement('canvas');c.width=im.naturalWidth;c.height=im.naturalHeight;c.getContext('2d').drawImage(im,0,0);c.toBlob(x=>{URL.revokeObjectURL(u);res(x);},'image/png');};
      im.onerror=()=>{URL.revokeObjectURL(u);res(null);};
      im.src=u;
    }catch(e){res(null);}
  });
}
async function copyOne(i){
  const it=MEDIA[i];
  if(!it){toast('内容已过期，请重新解析');return;}
  if(it.kind==='image'){
    // 1) 同步（手势窗口内）复制无水印原图直链——最可靠
    let wrote=false;
    try{if(navigator.clipboard&&navigator.clipboard.writeText){await navigator.clipboard.writeText(it.url);wrote=true;}}catch(e){}
    if(wrote)toast('已复制无水印图片链接');
    else legacyCopy(it.url,'已复制无水印图片链接');
    // 2) 尝试升级：把图片本体写进剪贴板（可直接粘贴成图）
    try{
      if(window.ClipboardItem&&navigator.clipboard&&navigator.clipboard.write){
        const b=await getBlobCached('/proxy?u='+encodeURIComponent(it.url));
        const png=/png/i.test(b.type)?b:(await toPng(b));
        if(png){await navigator.clipboard.write([new ClipboardItem({'image/png':png})]);toast('图片已复制，可直接粘贴');}
      }
    }catch(e){}
  }else{
    let link=it.fallback_api||'';
    try{
      const vs=it._vs||await variantsOf(it);
      const best=(vs||[]).find(x=>/264/.test(x.codec||''))||(vs||[])[0];
      if(best&&best.url)link=best.url;
    }catch(e){}
    let wrote=false;
    try{if(navigator.clipboard&&navigator.clipboard.writeText){await navigator.clipboard.writeText(link);wrote=true;}}catch(e){}
    if(wrote)toast('已复制视频直链');
    else legacyCopy(link,'已复制视频直链');
  }
}
let toastT=null;
function toast(msg,ms){
  let t=$('#toastEl');
  if(!t){t=document.createElement('div');t.id='toastEl';
    t.style.cssText='position:fixed;left:50%;bottom:calc(env(safe-area-inset-bottom) + 40px);transform:translateX(-50%);background:rgba(0,0,0,.75);color:#fff;font-size:13px;padding:9px 16px;border-radius:20px;z-index:120;max-width:86vw;text-align:center;pointer-events:none';
    document.body.appendChild(t);}
  t.textContent=msg;t.style.display='block';
  clearTimeout(toastT);
  if(ms!==0)toastT=setTimeout(()=>{t.style.display='none';},ms||2600); /* ms=0：常驻提示，直到被下一条替换 */
}

/* ===== 下载通道（与已验证版本同源） ===== */
function fstamp(it){const t=(it.time||'').replace(/[-: ]/g,'');return t.length===14?t.slice(0,8)+'_'+t.slice(8):'unknown';}
function fnum(n){return String(n).padStart(3,'0');}
function dlBlob(b,name){
  const a=document.createElement('a');
  const url=URL.createObjectURL(b);
  a.href=url;a.download=name;
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>{try{URL.revokeObjectURL(url);}catch(e){}},60000);
}
async function shareSave(u,name){
  const r=await fetch(u);const b=await r.blob();
  const f=new File([b],name,{type:b.type||'application/octet-stream'});
  if(navigator.canShare&&navigator.canShare({files:[f]})){
    try{await navigator.share({files:[f]});return;}catch(e){if(e&&e.name==='AbortError')return;}
  }
  window.open(u,'_blank');
}
const blobCache=new Map();
function getBlobCached(u){
  if(!blobCache.has(u))blobCache.set(u,fetchBlob(u).catch(e=>{blobCache.delete(u);throw e;}));
  return blobCache.get(u);
}
async function fetchBlob(u){const r=await fetch(u);if(!r.ok)throw new Error('HTTP '+r.status);return await r.blob();}

/* ===== 视频变体（H.264 优先，同已验证版本） ===== */
const vvPromises=new Map();
function variantsOf(it){
  if(it._vs)return Promise.resolve(it._vs);
  if(vvPromises.has(it.fallback_api))return vvPromises.get(it.fallback_api);
  const p=(async()=>{
    const r=await fetch('/api/video?u='+encodeURIComponent(it.fallback_api));
    const j=await r.json();
    if(j.error)throw j.error;
    it._vs=j.variants;
    return j.variants;
  })().catch(e=>{vvPromises.delete(it.fallback_api);throw e;});
  vvPromises.set(it.fallback_api,p);
  return p;
}
async function pickVideoSrc(it,_i){
  const vs=await variantsOf(it);
  const h264=vs.find(x=>/264/.test(x.codec||''));
  const best=h264||vs[0];
  const num=(it._fi!==undefined?it._fi:0)+1;
  if(h264){return {u:'/proxy?u='+encodeURIComponent(best.url),name:'video_'+fnum(num)+'_'+fstamp(it)+'_'+(best.h||'')+'p.mp4'};}
  return {u:'/api/convert?u='+encodeURIComponent(best.url)+'&codec='+encodeURIComponent(best.codec||''),name:'video_'+fnum(num)+'_'+fstamp(it)+'_h264.mp4'};
}

/* ===== 灯箱：× 左上 / 下载右上 / 滑动切换 ===== */
let lbToken=0;
function showLb(i){
  lbIdx=i;updateLb();
  $('#lb').style.display='flex';
  document.body.classList.add('hidevid');
}
function hideLb(){
  const v=$('#lbVid');v&&v.pause&&v.pause();
  $('#lb').style.display='none';
  document.body.classList.remove('hidevid');
}
async function updateLb(){
  const it=MEDIA[lbIdx];
  const img=$('#lbImg'),vid=$('#lbVid');
  $('#lbCnt').textContent=(lbIdx+1)+' / '+MEDIA.length;
  $('#lbDl').textContent=it.kind==='video'?'⬇ 下载':'⬇ 下载';
  if(it.kind==='video'){
    img.style.display='none';
    vid.style.display='block';
    vid.poster=it.poster?('/proxy?u='+encodeURIComponent(it.poster)):'';
    vid.removeAttribute('src');
    const token=++lbToken;
    try{
      const vs=await variantsOf(it);
      if(token!==lbToken)return;
      const play=vs.find(x=>/264/.test(x.codec))||vs[0];
      vid.src='/proxy?u='+encodeURIComponent(play.url);
      vid.play().catch(()=>{});
    }catch(e){}
  }else{
    lbToken++;
    vid.style.display='none';vid.pause&&vid.pause();vid.removeAttribute('src');
    img.style.display='block';
    img.classList.add('loading');
    img.onload=()=>img.classList.remove('loading');
    img.src='/proxy?u='+encodeURIComponent(it.url);
    const n=MEDIA.length;
    [lbIdx+1,lbIdx-1].forEach(j=>{const p=MEDIA[(j+n)%n];if(p.kind==='image'){const im=new Image();im.src='/proxy?u='+encodeURIComponent(p.url);}});
  }
}
function lbNav(d){const n=MEDIA.length;lbIdx=(lbIdx+d+n)%n;updateLb();}
async function lbDl(){
  await saveOne(lbIdx);
}
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
document.addEventListener('keydown',e=>{if($('#lb').style.display==='flex'){if(e.key==='Escape')hideLb();if(e.key==='ArrowLeft')lbNav(-1);if(e.key==='ArrowRight')lbNav(1);}});

/* ===== 批量选择页（与已验证版本同源，列表固定为图片或视频） ===== */
function openPicker(kind){
  pickKind=kind;
  const list=DATA[kind==='image'?'images':'videos'].filter(x=>!x.fail);
  if(!list.length){toast(kind==='image'?'没有可下载的图片':'没有可下载的视频');return;}
  pickSel=new Set(list.map((_,i)=>i));
  document.body.classList.add('hidevid');
  document.querySelectorAll('#chat video').forEach(v=>{try{v.pause();}catch(e){}});
  $('#pkTip').style.display='none';$('#pkTip').innerHTML='';
  $('#picker').style.display='flex';
  pkRender();
  prefetchPicker();
}
function hidePicker(){$('#picker').style.display='none';document.body.classList.remove('hidevid');}
function pkList(){return DATA[pickKind==='image'?'images':'videos'].filter(x=>!x.fail);}
function togglePick(i){pickSel.has(i)?pickSel.delete(i):pickSel.add(i);pkRender();}
function toggleAll(){
  const list=pkList();
  if(list.length&&pickSel.size===list.length)pickSel.clear();
  else pickSel=new Set(list.map((_,i)=>i));
  pkRender();
}
function pkRender(){
  const list=pkList();
  $('#pkTitle').textContent=pickKind==='image'?'保存图片':'保存视频';
  const all=pickSel.size===list.length&&list.length>0;
  $('#pkAll').textContent=all?'取消全选':'全选';
  $('#pkGrid').innerHTML=list.map((it,i)=>{
    let media,dim=`${it.width||'?'}×${it.height||'?'}`;
    if(it.kind==='image'){media=`<img loading="lazy" src="/proxy?u=${encodeURIComponent(it.thumb||it.url)}">`;}
    else{media=it.poster?`<img loading="lazy" src="/proxy?u=${encodeURIComponent(it.poster)}">`:'<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:30px;background:#26282c">▶</div>';
      if(it.duration)dim+='·'+fmtDur(it.duration);}
    return `<div class="pktile ${pickSel.has(i)?'sel':''}" onclick="togglePick(${i})">${media}<span class="pkdim">${dim}</span><span class="pkck">${pickSel.has(i)?'✓':''}</span></div>`;
  }).join('');
  const n=pickSel.size,has=n>0;
  $('#pkShare').disabled=!has;$('#pkDl').disabled=!has;
  $('#pkShare').textContent=has?`📤 批量分享（${n}）`:'📤 批量分享';
  $('#pkDl').textContent=has?`⬇ 批量下载（${n}）`:'⬇ 批量下载';
}
function prefetchPicker(){
  for(const it of pkList()){
    (async()=>{try{
      const s=it.kind==='image'?{u:'/proxy?u='+encodeURIComponent(it.url)}:await pickVideoSrc(it);
      getBlobCached(s.u);
    }catch(e){}})();
  }
}
async function buildFiles(list,onp){
  let done=0;const total=list.length;
  const tasks=list.map(async it=>{
    let b,nm,ty;
    if(it.kind==='image'){
      b=await getBlobCached('/proxy?u='+encodeURIComponent(it.url));
      nm=(it.upload?'ref_':'img_')+(it._fi!==undefined?fnum(it._fi+1):'')+'_'+fstamp(it)+'.'+(it.format==='png'?'png':'jpg');
      ty=b.type||'image/jpeg';
    }else{
      const s=await pickVideoSrc(it);
      b=await getBlobCached(s.u);
      nm=s.name;ty='video/mp4';
    }
    done++;if(onp)onp(done,total);
    return new File([b],nm,{type:ty});
  });
  return await Promise.all(tasks);
}
async function runShare(list,btn){
  btn.disabled=true;btn.classList.add('busy');const old=btn.textContent;
  let ok=false;
  try{
    const files=await buildFiles(list,(d,t)=>{btn.textContent='取文件中 '+d+'/'+t+'…';});
    if(navigator.canShare&&navigator.canShare({files})){
      await navigator.share({files});ok=true;
    }else{
      alert('当前浏览器不支持多文件分享，请改用「下载」或减少勾选数量');
    }
  }catch(e){
    if(e&&e.name==='NotAllowedError'){
      alert('分享面板没有弹出（系统响应超时）。文件已准备好，请再点一次「分享」，弹出面板后立即点「存储图像 / 存储视频」。');
    }else if(!(e&&e.name==='AbortError')){
      alert('分享失败：'+(e&&e.message||e));
    }
  }
  btn.disabled=false;btn.classList.remove('busy');btn.textContent=old;
  return ok;
}
async function doPickShare(){
  const list=pkList().filter((_,i)=>pickSel.has(i));
  if(!list.length)return;
  const ok=await runShare(list,$('#pkShare'));
  if(ok)hidePicker();
}
async function shareOne(it){
  try{
    let u,name;
    if(it.kind==='image'){u='/proxy?u='+encodeURIComponent(it.url);name='img_'+fnum(it._fi+1)+'_'+fstamp(it)+'.'+(it.format==='png'?'png':'jpg');}
    else{const s=await pickVideoSrc(it);u=s.u;name=s.name;}
    const b=await getBlobCached(u);
    const f=new File([b],name,{type:it.kind==='image'?(b.type||'image/jpeg'):'video/mp4'});
    if(navigator.canShare&&navigator.canShare({files:[f]})){await navigator.share({files:[f]});return;}
    window.location.href=u+(u.includes('?')?'&':'?')+'dl='+encodeURIComponent(name);
  }catch(e){if(!(e&&e.name==='AbortError'))alert('保存失败：'+(e&&e.message||e));}
}
function pkRowTap(i){
  const r=pkRows[i];if(!r)return true;
  if(isIOS&&navigator.canShare){shareOne(r.it);return false;}
  return true;
}
async function shareRows(btn){
  const list=(pkRows||[]).map(r=>r.it).filter(Boolean);
  if(!list.length)return;
  await runShare(list,btn||$('#pkShare'));
}
async function runDownload(list,btn,tip){
  btn.disabled=true;btn.classList.add('busy');const old=btn.textContent;
  const rows=[];let auto=0;
  try{
    if(isIOS){
      for(let i=0;i<list.length;i++){
        const it=list[i];
        btn.textContent=`探测资源 ${i+1}/${list.length}…`;
        let u,name,bytes=null;
        if(it.kind==='image'){
          u='/proxy?u='+encodeURIComponent(it.url);
          name=(it.upload?'ref_':'img_')+(it._fi!==undefined?fnum(it._fi+1):'')+'_'+fstamp(it)+'.'+(it.format==='png'?'png':'jpg');
        }else{
          const s=await pickVideoSrc(it);
          u=s.u;name=s.name;
          if(it._vs){const h=it._vs.find(x=>/264/.test(x.codec||''))||it._vs[0];bytes=h.size||null;}
        }
        let ok=true;
        try{
          const ctl=new AbortController();
          const r=await fetch(u,{headers:{Range:'bytes=0-1'},signal:ctl.signal});
          ok=r.ok||r.status===206;
          const cr=r.headers.get('Content-Range');
          if(cr){const m=cr.match(/\/(\d+)/);if(m)bytes=+m[1];}
          else{const cl=r.headers.get('Content-Length');if(cl&&!bytes)bytes=+cl;}
          ctl.abort();
        }catch(e){if(e&&e.name!=='AbortError')ok=false;}
        rows.push({ok,name,bytes,u,it});
      }
    }else{
      for(let i=0;i<list.length;i++){
        const it=list[i];btn.textContent=`下载中 ${i+1}/${list.length}…`;
        let u,name;
        if(it.kind==='image'){u='/proxy?u='+encodeURIComponent(it.url);name=(it.upload?'ref_':'img_')+(it._fi!==undefined?fnum(it._fi+1):'')+'_'+fstamp(it)+'.'+(it.format==='png'?'png':'jpg');}
        else{const s=await pickVideoSrc(it);u=s.u;name=s.name;}
        try{dlBlob(await getBlobCached(u),name);auto++;}catch(e){}
        await new Promise(r=>setTimeout(r,400));
      }
    }
    pkRows=rows;
    const show=tip;
    show.style.display='block';
    show.innerHTML=isIOS
      ?('📱 iPhone：逐个点「⬇ 保存」→ 弹出分享面板 → 点「存储图像 / 存储视频」直接进相册：<br>'
        +rows.map((r,i)=>'<div class="pkrow"><span class="pkname">'+(r.ok?'✅':'⚠️')+' 📎 '+r.name+(r.bytes?' ('+fmtSize(r.bytes)+')':'')+'</span><a class="pkdlbtn" href="'+r.u+(r.u.includes('?')?'&':'?')+'dl='+encodeURIComponent(r.name)+'" onclick="return pkRowTap('+i+')">⬇ 保存</a></div>').join('')
        +'<button class="pkfb" onclick="shareRows(this)">📥 分享，一次存全部到相册</button>')
      :('✅ 已批量提交 '+auto+' 个下载任务，文件在浏览器的下载文件夹（相册/文件管理里能找到）'
        +(auto<list.length?'<br>⚠️ 有 '+(list.length-auto)+' 个文件没取到，请重试或用「分享」':'')
        +'<button class="pkfb" onclick="shareRows(this)">📥 想直接进相册？用「分享」</button>');
  }catch(e){
    if(!(e&&e.name==='AbortError'))alert('下载失败：'+(e&&e.message||e));
  }
  finally{btn.disabled=false;btn.classList.remove('busy');btn.textContent=old;}
}
async function doPickDownload(){
  const list=pkList().filter((_,i)=>pickSel.has(i));
  if(!list.length)return;
  await runDownload(list,$('#pkDl'),$('#pkTip'));
}
/* 多选模式：底部 分享 / 下载 */
async function doSelShare(){
  const list=[...selSet].sort((a,b)=>a-b).map(i=>MEDIA[i]).filter(Boolean);
  if(!list.length)return;
  await runShare(list,$('#selShare'));
}
async function doSelDownload(){
  const list=[...selSet].sort((a,b)=>a-b).map(i=>MEDIA[i]).filter(Boolean);
  if(!list.length)return;
  await runDownload(list,$('#selDl'),$('#selTip'));
}

/* ===== 清空 / 粘贴 / 悬浮按钮 ===== */
function clearUrl(){
  $('#url').value='';$('#chat').style.display='none';$('#chat').innerHTML='';
  $('#main').innerHTML='';DATA=null;$('#meta').style.display='none';
  document.body.classList.remove('hasdata');
  exitSel();
  window.scrollTo(0,0); /* iOS Safari 内容缩短时不自动回顶，必须显式滚回，否则看到的不是主页 */
  $('#batchbar').classList.remove('show');
  $('#fbar').classList.remove('show');timeFilter=0;
  try{sessionStorage.removeItem('dwc_active');sessionStorage.removeItem('dwc_data');localStorage.removeItem('dwc_lastUrl');localStorage.setItem('dwc_filter','0');}catch(e){}
  $('#url').focus();
}
async function pasteUrl(){
  try{
    if(!navigator.clipboard||!navigator.clipboard.readText)throw new Error('unsupported');
    const t=await navigator.clipboard.readText();
    if(t&&t.trim()){$('#url').value=t.trim();flashPaste('已粘贴✓');doParse();return;}
    throw new Error('empty');
  }catch(e){openPasteBox();}
}
function openPasteBox(){$('#ptext').value='';$('#pasteBox').style.display='flex';setTimeout(()=>$('#ptext').focus(),60);}
function hidePaste(){$('#pasteBox').style.display='none';}
function confirmPaste(){const t=$('#ptext').value.trim();if(t){$('#url').value=t;flashPaste('已粘贴✓');doParse();}hidePaste();}
function flashPaste(txt){const b=$('#btnPaste'),o='粘贴';b.textContent=txt;setTimeout(()=>b.textContent=o,1200);}
$('#url').addEventListener('keydown',e=>{if(e.key==='Enter')doParse();});
</script>
</body>
</html>"""


class ChatHandler(W.Handler):
    """复用 doubao_web.Handler 的 /proxy /api/video /api/convert /api/zip /api/sizes，
    仅替换首页为对话视图并新增 /api/chat /api/lan。"""

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/chat"):
                self._send(200, CHAT_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/chat":
                self._api_chat()
            elif path == "/api/lan":
                self._api_lan()
            else:
                super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))
            except Exception:
                pass

    def _api_lan(self):
        ip = ""
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("223.5.5.5", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:  # noqa: BLE001
            pass
        port = self.server.server_address[1]
        self._send(200, json.dumps({"lan": f"http://{ip}:{port}/" if ip else ""},
                                   ensure_ascii=False).encode("utf-8"))

    def _api_chat(self):
        u = self._q("url").strip()
        fresh = self._q("fresh") == "1"
        if "/thread/" not in u:
            self._send(400, json.dumps({"error": "链接需包含 /thread/"}).encode("utf-8"))
            return
        try:
            r, stale = _chat_cached(u, fresh)
        except Exception as e:  # noqa: BLE001
            self._send(200, json.dumps({"error": f"解析失败：{e}", "retry_hint": True},
                                       ensure_ascii=False).encode("utf-8"))
            return
        slim = _slim(r)
        if stale:
            slim["stale"] = True
        if not slim["messages"] and not slim["image_count"] and not slim["video_count"]:
            slim["debug"] = W._scan_debug(u)
        self._send(200, json.dumps(slim, ensure_ascii=False).encode("utf-8"))


def main():
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8766))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    srv = W.ThreadingHTTPServer((host, port), ChatHandler)
    print(f"豆包对话复刻: http://{host}:{port}  (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
