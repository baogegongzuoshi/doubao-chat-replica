#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
doubao_engine.py — 豆包分享链接无水印解析/下载引擎（纯标准库，无第三方依赖）
原理参考 ihmily/doubao-nomark（MIT/学习用途）：
  - 图片: 抓取 thread 页面 -> 提取内嵌路由 JSON -> creation_block -> image_ori_raw
  - 视频: 提取 fallback_api -> 改参数 logo_type=unwatermarked&codec_type=8 请求
          -> 用 key_seed 解码 main_url 得到无水印直链
子命令:
  scan     解析分享链接，输出清单 JSON（按生成时间从旧到新排序）
  download 按 scan 的结果下载（支持时间过滤/类型过滤/指定 id）
  mp4      无损规整为 mp4（ffmpeg remux；可选转码 H.264 提升兼容性）
"""
import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0")
HEADERS = {"user-agent": UA, "accept-language": "zh-CN,zh;q=0.9,en;q=0.8"}
JIAGEV = "https://www.jiagev.com"

# ---------------------------------------------------------------- HTTP


def http_get(url, timeout=40, headers=None, binary=False):
    req = urllib.request.Request(url, headers=dict(HEADERS, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", "replace")


def http_post_json(url, payload, timeout=40):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=dict(HEADERS, **{"content-type": "application/json"}), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def download_file(url, path, timeout=300):
    req = urllib.request.Request(url, headers=HEADERS)
    tmp = path + ".part"
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, path)
    return os.path.getsize(path)


# ------------------------------------------------------- 页面数据提取

# 兼容单/双引号属性（豆包 2026-09 起把大数据块改用单引号包裹：data-fn-args='[&quot;...&quot;]'）
FN_ARGS_RE = re.compile(r'data-fn-args=(["\'])(.*?)\1', re.DOTALL)


def _extract_blocks(html):
    """从页面 HTML 中解析出所有内嵌 fn-args JSON（list 形式），兼容单/双引号属性。"""
    blocks = []
    for m in FN_ARGS_RE.finditer(html):
        try:
            data = json.loads(htmllib_unescape(m.group(2)))
        except Exception:
            continue
        blocks.append(data)
    return blocks


def _extract_blocks_multi(html):
    """多策略提取：豆包会变换 JSON 嵌入方式（双引号/单引号属性、一层或多层 HTML 实体转义）。
    原文提取 + 逐层反转义后再提取，全部合并（后续 _dedupe 会去重，多余块无害）。"""
    blocks = _extract_blocks(html)
    cur = html
    for _ in range(3):  # 最多剥 3 层实体转义
        nxt = htmllib_unescape(cur)
        if nxt == cur:
            break
        cur = nxt
        blocks += _extract_blocks(cur)
    return blocks


def htmllib_unescape(s):
    import html as _html
    return _html.unescape(s)


def walk(obj, depth=0):
    if depth > 30:
        return
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, depth + 1)
    elif isinstance(obj, str) and obj.lstrip()[:1] in ("{", "["):
        # 页面数据存在多层嵌套 JSON 字符串（routerDataFnArgs 等），递归解析
        try:
            yield from walk(json.loads(obj), depth + 1)
        except Exception:
            return


def find_message_lists(blocks):
    """从所有内嵌 JSON 中找出 message_snapshot.message_list。"""
    lists = []
    for data in blocks:
        for node in walk(data):
            if isinstance(node, dict) and isinstance(node.get("message_snapshot"), dict):
                ml = node["message_snapshot"].get("message_list")
                if isinstance(ml, list) and ml:
                    lists.append(ml)
    return lists


def _parse_block_content(cb):
    c = cb.get("content_v2") or cb.get("content")
    if isinstance(c, str):
        try:
            return json.loads(c)
        except Exception:
            return {}
    return c if isinstance(c, dict) else {}


def _first_text(messages, upto_idx):
    """找 upto_idx 之前最近一条用户文字消息（作为生成语境）。"""
    for m in reversed(messages[:upto_idx]):
        for cb in (m.get("content_block") or []):
            cj = _parse_block_content(cb)
            tb = cj.get("text_block") if isinstance(cj, dict) else None
            if tb and tb.get("text"):
                return tb["text"][:600]
    return ""


def collect_items(messages):
    """遍历消息，收集图片/视频创作，附时间戳与语境。返回 (images, videos)。"""
    images, videos = [], []
    for mi, msg in enumerate(messages):
        epoch = int(msg.get("create_time") or 0)
        for cb in (msg.get("content_block") or []):
            if cb.get("block_type") != 2074:
                continue
            cj = _parse_block_content(cb)
            block = cj.get("creation_block") if isinstance(cj, dict) else None
            if not block:
                continue
            context = _first_text(messages, mi)
            for cre in block.get("creations") or []:
                gd = cre.get("gen_detail") or {}
                base = {
                    "id": str(cre.get("id") or ""),
                    "msg_index": mi,
                    "epoch": epoch,
                    "time": datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch else None,
                    "status": None, "tips": "",
                    "prompt": "", "context": context,
                    "ref_images": len(gd.get("ref_images") or []),
                }
                if cre.get("type") == 1 and isinstance(cre.get("image"), dict):
                    img = cre["image"]
                    ori = img.get("image_ori_raw") or {}
                    item = dict(base, kind="image",
                                status=img.get("status"), tips=img.get("tips") or "",
                                width=ori.get("width"), height=ori.get("height"),
                                format=ori.get("format") or "jpeg",
                                url=(ori.get("url") or "").replace("&amp;", "&"))
                    gp = img.get("gen_params") or {}
                    item["prompt"] = (gp.get("prompt") or "")[:600]
                    for key, attr in (("image_preview", "preview"), ("image_thumb", "thumb")):
                        d = img.get(key) or {}
                        u = d.get("url") if isinstance(d, dict) else None
                        if not u and isinstance(d, dict):
                            for v in d.values():
                                if isinstance(v, str) and v.startswith("http"):
                                    u = v
                                    break
                        item[attr] = (u or "").replace("&amp;", "&") if u else ""
                    images.append(item)
                elif cre.get("type") == 2 and isinstance(cre.get("video"), dict):
                    v = cre["video"]
                    model = {}
                    if isinstance(v.get("video_model"), str):
                        try:
                            model = json.loads(v["video_model"])
                        except Exception:
                            model = {}
                    elif isinstance(v.get("video_model"), dict):
                        model = v["video_model"]
                    item = dict(base, kind="video",
                                status=v.get("status"), tips=v.get("tips") or "",
                                vid=v.get("vid"), width=v.get("width"), height=v.get("height"),
                                duration=v.get("duration"),
                                poster=(model.get("poster_url") or v.get("cover") or "").replace("&amp;", "&"),
                                fallback_api=(model.get("fallback_api") or "").replace("&amp;", "&"),
                                model_status=(model.get("status"), model.get("message")))
                    item["model_status"] = model.get("status")
                    item["model_message"] = model.get("message") or ""
                    videos.append(item)
    return images, videos


# ------------------------------------------------------- 视频无水印直链

FALLBACK_PARAMS = {"codec_type": "8", "logo_type": "unwatermarked"}
TRUSTED_SUFFIX = (".snssdk.com", ".douyinvod.com", ".dola.com", ".byteintlapi.com")


def build_unwatermarked_api(fallback_api):
    p = urllib.parse.urlsplit(fallback_api)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not any(host == s[1:] or host.endswith(s) for s in TRUSTED_SUFFIX):
        raise ValueError("fallback_api 域名不受信任")
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if k not in FALLBACK_PARAMS]
    q.extend(FALLBACK_PARAMS.items())
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), p.fragment))


# ---- 纯 Python AES-128-CBC 解密（仅用于解码 main_url，数据量极小）

_SBOX = None
_INV_SBOX = None
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _init_aes_tables():
    global _SBOX, _INV_SBOX
    if _SBOX:
        return
    p, q = 1, 1
    sbox = [0] * 256
    sbox[0] = 0x63
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1) | (q >> 7)) & 0xFF ^ ((q << 2) | (q >> 6)) & 0xFF \
            ^ ((q << 3) | (q >> 5)) & 0xFF ^ ((q << 4) | (q >> 4)) & 0xFF
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    _SBOX = sbox
    _INV_SBOX = [0] * 256
    for i, v in enumerate(sbox):
        _INV_SBOX[v] = i


def _xtime(a):
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a, b):
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        b >>= 1
        a = _xtime(a)
    return r & 0xFF


def _expand_key(key):
    _init_aes_tables()
    nk, nr = 4, 10
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // nk - 1]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w


def _aes_decrypt_block(block, w):
    state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
    nr = 10
    # AddRoundKey
    def ark(state, rnd):
        for c in range(4):
            for r in range(4):
                state[r][c] ^= w[rnd * 4 + c][r]
    ark(state, nr)
    for rnd in range(nr - 1, -1, -1):
        # InvShiftRows
        for r in range(1, 4):
            state[r] = state[r][-r:] + state[r][:-r]
        # InvSubBytes
        for r in range(4):
            for c in range(4):
                state[r][c] = _INV_SBOX[state[r][c]]
        # AddRoundKey
        ark(state, rnd)
        if rnd:  # InvMixColumns（轮密钥加后、下一轮前）
            for c in range(4):
                a = [state[r][c] for r in range(4)]
                state[0][c] = _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9)
                state[1][c] = _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13)
                state[2][c] = _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11)
                state[3][c] = _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)
    return bytes(state[r][c] for c in range(4) for r in range(4))


def aes_cbc_decrypt(data, key, iv):
    if len(data) % 16:
        return b""
    w = _expand_key(key)
    out, prev = b"", iv
    for i in range(0, len(data), 16):
        blk = _aes_decrypt_block(data[i:i + 16], w)
        out += bytes(a ^ b for a, b in zip(blk, prev))
        prev = data[i:i + 16]
    return out


QAAB_SALT = bytes.fromhex(
    "4dd4c2e6b83162090e52b3c7a6733ba4"
    "1cb2462b829ab58a196b39db57177524"
    "f49baf7f08e8d68d26a72e37c1a95a2f"
    "1f05a51892aef2949732b62a38aadd58")


def _b64_loose(text):
    text = str(text or "").strip()
    for cand in (text,
                 text.translate(str.maketrans({"$": "_", "@": "/", "#": "."})),
                 text.translate(str.maketrans({"$": "+", "@": "/", "#": "="}))):
        if not cand:
            continue
        n = cand.replace("-", "+").replace("_", "/")
        n += "=" * (-len(n) % 4)
        try:
            return base64.b64decode(n, validate=True)
        except Exception:
            continue
    return b""


def _strip_pkcs7(b):
    if not b:
        return b""
    pad = b[-1]
    if pad < 1 or pad > 16 or pad > len(b):
        return b
    if b[-pad:] != bytes([pad]) * pad:
        return b
    return b[:-pad]


def _url_from_bytes(b):
    if not b or any(byte != 9 and byte != 10 and byte != 13 and not 32 <= byte <= 126 for byte in b):
        return ""
    try:
        u = b.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""
    return u if u.lower().startswith(("http://", "https://")) else ""


def decode_main_url(token, key_seed=""):
    token = str(token or "").strip()
    if token.lower().startswith(("http://", "https://")):
        return token
    u = _url_from_bytes(_b64_loose(token))
    if u:
        return u
    if token.startswith("qAAB") and key_seed:
        data, seed = _b64_loose(token), _b64_loose(key_seed)
        if data and seed:
            material = __import__("hashlib").sha512(
                __import__("hashlib").sha512(seed[:32]).digest() + QAAB_SALT).digest()
            key, iv = material[:16], material[16:32]
            attempts = []
            if data.startswith(b"\xa8\x00\x01\x00"):
                attempts = [(data[4:], key, iv), (data[4:], iv, key)]
                if len(data) > 36:
                    attempts += [(data[36:], key, data[20:36]), (data[36:], key, iv)]
            else:
                attempts = [(data, key, iv)]
            for payload, k, i in attempts:
                out = aes_cbc_decrypt(payload, k, i)
                u = _url_from_bytes(out) or _url_from_bytes(_strip_pkcs7(out))
                if u:
                    return u
    return ""


def parse_share(share_url):
    """主入口：本地解析分享链接。返回 dict(share_name, images, videos)。
    三级策略：结构化提取 -> 泛走查提取 -> 正则碎片兜底，页面结构变化时尽量不断链。"""
    html_text = http_get(share_url)
    # 兼容新旧页面结构：原文 + 逐层反转义多轮提取（豆包 2026-09 起部分分享改用单引号属性+实体转义）
    blocks = _extract_blocks_multi(html_text)
    if not blocks or "message_snapshot" not in htmllib_unescape(html_text):
        # 页面被风控/网络抖动/结构变更时明确报错，而不是静默返回 0
        raise ValueError("页面未返回可解析数据（可能被风控拦截或网络抖动），请稍后重试")
    mlists = find_message_lists(blocks)
    images, videos = [], []
    for ml in mlists:
        i2, v2 = collect_items(ml)
        images += i2
        videos += v2
    share_name = _find_share_name(blocks)
    if not mlists and not images:
        # 兜底：页面结构大改时，正则直接捞创作碎片
        images, videos = _regex_fallback(html_text)
    images = _dedupe(images)
    videos = _dedupe(videos)
    # 时间从旧到新排序（生成时间早的在前；无时间的排最后）
    images.sort(key=lambda x: x.get("epoch") or 0)
    videos.sort(key=lambda x: x.get("epoch") or 0)
    return {"share_name": share_name, "share_url": share_url,
            "image_count": len(images), "video_count": len(videos),
            "images": images, "videos": videos}


def _dedupe(items):
    seen, out = set(), []
    for it in items:
        k = it.get("id") or it.get("url") or it.get("vid")
        if k and k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _find_share_name(blocks):
    for data in blocks:
        for node in walk(data):
            if isinstance(node, dict) and isinstance(node.get("share_info"), dict):
                return node["share_info"].get("share_name") or ""
    return ""


def _regex_fallback(html_text):
    """最后兜底：直接在 HTML 里找 image_ori_raw / video 的 JSON 碎片。"""
    images, videos = [], []
    for m in re.finditer(r'\{"url":"https:?[^"]*rc_gen_image[^"]*"[^{}]*\}', html_text):
        try:
            d = json.loads(m.group(0).replace("\\/", "/"))
        except Exception:
            continue
        if d.get("url"):
            images.append({"kind": "image", "id": d.get("key", ""), "url": d["url"],
                           "width": d.get("width"), "height": d.get("height"),
                           "epoch": 0, "time": None, "status": None, "tips": "",
                           "prompt": "", "context": "", "preview": "", "thumb": "",
                           "format": d.get("format") or "jpeg", "msg_index": -1})
    return images, videos


def resolve_video_variants(fallback_api):
    """解码全部清晰度变体，按分辨率从高到低返回：[{url, codec, w, h, size, bitrate}]。"""
    api = build_unwatermarked_api(fallback_api)
    payload = json.loads(http_get(api, timeout=30))
    vi = payload.get("video_info") or (payload.get("data") or {}).get("video_info") or payload
    data = vi.get("data") or vi if isinstance(vi, dict) else {}
    vlist = data.get("video_list") if isinstance(data, dict) else None
    entries = [e for e in (vlist.values() if isinstance(vlist, dict) else (vlist or []))
               if isinstance(e, dict) and (e.get("main_url") or e.get("play_url"))]
    key_seed = data.get("key_seed") or vi.get("key_seed") or ""
    out = []
    for e in entries:
        url = decode_main_url(str(e.get("main_url") or e.get("play_url")).strip(), key_seed)
        if not url:
            continue
        out.append({"url": url, "codec": str(e.get("codec_type") or "").lower(),
                    "w": int(e.get("vwidth") or 0), "h": int(e.get("vheight") or 0),
                    "size": int(e.get("size") or 0),
                    "bitrate": int(e.get("real_bitrate") or e.get("bitrate") or 0)})
    out.sort(key=lambda x: -(x["w"] * x["h"]))
    return out


def resolve_video_url(video, prefer_h264=True):
    """通过 fallback_api 拿无水印直链；prefer_h264=True 时优先 H.264（播放兼容性最好）。"""
    api = build_unwatermarked_api(video["fallback_api"])
    payload = json.loads(http_get(api, timeout=30))
    vi = payload.get("video_info") or (payload.get("data") or {}).get("video_info") or payload
    data = vi.get("data") or vi if isinstance(vi, dict) else {}
    vlist = data.get("video_list") if isinstance(data, dict) else None
    entries = [e for e in (vlist.values() if isinstance(vlist, dict) else (vlist or []))
               if isinstance(e, dict) and (e.get("main_url") or e.get("play_url"))]
    if not entries:
        raise ValueError("fallback_api 响应中没有 main_url")
    key_seed = data.get("key_seed") or vi.get("key_seed") or ""

    def score(e):
        codec = str(e.get("codec_type") or "")
        res = int(e.get("vwidth") or 0) * int(e.get("vheight") or 0)
        return ((0 if codec == "h264" else 1) if prefer_h264 else 0, -res,
                -int(e.get("real_bitrate") or e.get("bitrate") or 0))
    entries.sort(key=score)
    for e in entries:
        url = decode_main_url(str(e.get("main_url") or e.get("play_url")).strip(), key_seed)
        if url:
            return url, e
    raise ValueError("main_url 解码失败")


# ---------------------------------------------------------------- 时间过滤

def _parse_since(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    raise ValueError(f"无法识别时间: {s}")


def filter_by_time(items, since=None, until=None):
    out = []
    for it in items:
        ep = it.get("epoch") or 0
        if not ep:  # 无时间信息的不在时间过滤中丢弃
            out.append(it)
            continue
        if since and ep < since:
            continue
        if until and ep > until:
            continue
        out.append(it)
    return out


# ---------------------------------------------------------------- CLI

def _brief(items, kind):
    rows = []
    for i, it in enumerate(items, 1):
        fail = ""
        if kind == "image" and it.get("status") not in (2, None) or (it.get("tips") and kind == "image"):
            fail = f"  ⚠status={it.get('status')} tips={it.get('tips')}"
        rows.append(f"{i:02d}. [{it.get('time') or '时间未知'}] {kind}"
                    f" {it.get('width')}x{it.get('height')} id={it.get('id')}"
                    f" 提示词: {(it.get('prompt') or it.get('context') or '')[:60]}{fail}")
    return rows


def cmd_scan(args):
    r = parse_share(args.url)
    print(f"分享标题: {r['share_name']}")
    print(f"图片: {r['image_count']} 张 | 视频: {r['video_count']} 个（已按生成时间从旧到新排序）")
    if r["images"]:
        print("--- 图片清单（近 -> 远）---")
        print("\n".join(_brief(r["images"], "image")))
    if r["videos"]:
        print("--- 视频清单（近 -> 远）---")
        print("\n".join(_brief(r["videos"], "video")))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
        print(f"完整清单已写入: {args.json_out}")


def cmd_download(args):
    r = parse_share(args.url)
    items = []
    if args.type in ("all", "image"):
        items += r["images"]
    if args.type in ("all", "video"):
        items += r["videos"]
    since = until = None
    now = datetime.datetime.now()
    if args.today:
        since = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    elif args.hours:
        since = int((now - datetime.timedelta(hours=args.hours)).timestamp())
    elif args.since:
        since = _parse_since(args.since)
    if args.until:
        until = _parse_until(args.until)
    items = filter_by_time(items, since, until)
    if args.ids:
        want = {x.strip() for x in args.ids.split(",")}
        items = [it for it in items if it.get("id") in want]
    if args.limit:
        items = items[:args.limit]
    if not items:
        print("没有符合条件的内容可下载")
        return
    os.makedirs(args.out, exist_ok=True)
    print(f"计划下载 {len(items)} 项 -> {args.out}")
    manifest = []
    for i, it in enumerate(items, 1):
        stamp = (it.get("time") or "").replace("-", "").replace(":", "").replace(" ", "_") or "unknown"
        if it["kind"] == "image":
            fname = f"{i:03d}_{stamp}_img_{(it.get('id') or 'x')[-8:]}.{it.get('format') or 'jpg'}"
        else:
            fname = f"{i:03d}_{stamp}_vid_{(it.get('vid') or it.get('id') or 'x')[-8:]}.mp4"
        path = os.path.join(args.out, fname)
        try:
            if it["kind"] == "image":
                size = download_file(it["url"], path)
            else:
                url, entry = resolve_video_url(it, prefer_h264=not args.best)
                size = download_file(url, path)
                _maybe_remux(path)
            print(f"  ✔ {fname} ({size / 1048576:.2f} MB)")
            manifest.append({"file": fname, "kind": it["kind"], "id": it.get("id"),
                             "time": it.get("time"), "prompt": it.get("prompt")})
        except Exception as e:
            print(f"  ✘ {fname} 失败: {e}")
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"share_url": args.url, "share_name": r["share_name"],
                   "downloaded": manifest}, f, ensure_ascii=False, indent=1)
    print(f"完成 {len(manifest)}/{len(items)}，清单见 {os.path.join(args.out, 'manifest.json')}")


def _parse_until(s):
    return _parse_since(s)


def _maybe_remux(path):
    """用 ffprobe 检查容器；非规范 mp4 时 ffmpeg -c copy 无损重封装为 mp4。"""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=60)
        fmt = (p.stdout or "").strip().split(",")[0]
    except Exception:
        return
    if fmt == "mov,mp4,m4a,3gp,3g2,mj2":
        return
    tmp = path + ".remux.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-c", "copy",
                    "-movflags", "+faststart", tmp], timeout=600, check=True)
    os.replace(tmp, path)


def cmd_mp4(args):
    for f in args.files:
        if not os.path.exists(f):
            print(f"  ✘ 文件不存在: {f}")
            continue
        if args.transcode:
            out = os.path.splitext(f)[0] + "_h264.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", f,
                            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out],
                           check=True, timeout=1800)
            print(f"  ✔ 转码完成: {out}")
        else:
            before = os.path.getsize(f)
            _maybe_remux(f)
            after = os.path.getsize(f)
            print(f"  ✔ {f} 已规整为 mp4（无损，{before}->{after} 字节）")


def main():
    ap = argparse.ArgumentParser(description="豆包分享链接无水印解析/下载")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="解析链接，输出清单（不下载）")
    p.add_argument("url")
    p.add_argument("--json-out", default="")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("download", help="下载（先 scan 报数确认，再加 --yes 执行）")
    p.add_argument("url")
    p.add_argument("--out", required=True)
    p.add_argument("--type", choices=["all", "image", "video"], default="all")
    p.add_argument("--today", action="store_true", help="只下载今天的")
    p.add_argument("--hours", type=float, help="只下载最近 N 小时的")
    p.add_argument("--since", help='起始时间 "2026-09-17 08:00"')
    p.add_argument("--until", help="截止时间")
    p.add_argument("--ids", help="仅下载指定 id，逗号分隔")
    p.add_argument("--limit", type=int, help="只下前 N 个（按生成时间从旧到新）")
    p.add_argument("--best", action="store_true", help="视频选最高分辨率（可能为 H.265）")
    p.add_argument("--yes", action="store_true", help="确认执行（配合先 scan 后 download 的流程使用，直接下载）")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("mp4", help="视频无损规整为 mp4 / 可选转码 H.264")
    p.add_argument("files", nargs="+")
    p.add_argument("--transcode", action="store_true", help="强制转码 H.264（有损但兼容性最好）")
    p.set_defaults(func=cmd_mp4)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
