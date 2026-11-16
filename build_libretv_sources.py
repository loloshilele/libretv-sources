#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
libretv_sources_build.py
========================
把资源站监测数据转成 LibreTV-SourceList 订阅格式，并逐个探测接口 / 首个 m3u8 是否真的能播，
只把「此刻还活着」的源写进订阅文件。

数据来源（默认）：https://mylazily.github.io/ziyuanzhan/data/online.json
输出格式        ：LibreTV「设置 → 数据源 → 数据源订阅」可用的 JSON

用法
----
    python libretv_sources_build.py                      # 只用 ziyuanzhan 数据，全部探测一层
    python libretv_sources_build.py --deep 10            # 额外对前 10 个做 m3u8 深度探测
    python libretv_sources_build.py --merge <订阅URL>    # 合并别人的 LibreTV 订阅一起探活
    python libretv_sources_build.py --no-probe           # 不探测，纯转换（离线整理用）

依赖：仅标准库。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import io
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SOURCE = "https://mylazily.github.io/ziyuanzhan/data/online.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 采集站里通常归到「成人」的标签
ADULT_TAGS = ("成人", "伦理", "福利", "三级", "禁片", "18+")

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url: str, timeout: float = 8.0) -> tuple[int, bytes, float, str]:
    """返回 (状态码, 响应体, 耗时ms, 错误)。任何异常都不抛出，交给调用方判断。"""
    t0 = time.perf_counter()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, identity",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip" and raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return r.status, raw, (time.perf_counter() - t0) * 1000, ""
    except urllib.error.HTTPError as e:
        return e.code, b"", (time.perf_counter() - t0) * 1000, f"HTTP {e.code}"
    except Exception as e:  # 超时 / DNS / TLS / 连接重置
        return 0, b"", (time.perf_counter() - t0) * 1000, type(e).__name__ + ": " + str(e)[:70]


def normalize_api(url: str) -> str:
    """统一采集接口写法：补 scheme、去掉多余的尾部斜杠与空白。"""
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    u = u.split("#")[0].strip()
    while u.endswith("/"):
        u = u[:-1]
    return u


def host_key(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return url.lower()


# --------------------------------------------------------------------------- #
# 输入解析
# --------------------------------------------------------------------------- #
def load_json_url(url: str, timeout: float) -> dict | list:
    code, body, _, err = http_get(url, timeout=timeout)
    if code != 200 or not body:
        raise SystemExit(f"[x] 拉取失败 {url}\n    {err or ('HTTP ' + str(code))}")
    try:
        return json.loads(body.decode("utf-8", "ignore"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"[x] JSON 解析失败 {url}: {e}")


def parse_ziyuanzhan(obj: dict) -> list[dict]:
    """资源站监测数据 → 内部结构。只保留 status=ok 且带 api 的条目。"""
    out = []
    for it in obj.get("data") or []:
        api = normalize_api(it.get("api") or "")
        if not api:
            continue
        if (it.get("status") or "").lower() not in ("ok", "online"):
            continue
        tags = it.get("tags") or []
        out.append({
            "name": (it.get("name") or host_key(api)).strip(),
            "url": api,
            "detail": (it.get("link") or "").rstrip("/"),
            "isAdult": any(t in ADULT_TAGS for t in tags)
                        or any(a in (it.get("name") or "") for a in ADULT_TAGS),
            # 以下仅用于排序与报告，不写进订阅文件
            "_rating": float(it.get("rating") or 0),
            "_rt": int(it.get("responseTime") or 9999),
            "_count": int(it.get("resource_count") or 0),
            "_today": int(it.get("todayUpdates") or 0),
            "_tags": tags,
        })
    return out


def parse_subscription(obj: dict | list, origin: str) -> list[dict]:
    """别人的 LibreTV 订阅 → 内部结构（只取 sources，忽略 liveSources）。"""
    srcs = []
    if isinstance(obj, dict):
        srcs = obj.get("sources") or []
    elif isinstance(obj, list):
        srcs = obj
    out = []
    for it in srcs:
        if not isinstance(it, dict):
            continue
        api = normalize_api(it.get("url") or "")
        if not api:
            continue
        out.append({
            "name": (it.get("name") or host_key(api)).strip(),
            "url": api,
            "detail": (it.get("detail") or "").rstrip("/"),
            "isAdult": bool(it.get("isAdult")),
            "_rating": 0.0, "_rt": 9999, "_count": 0, "_today": 0,
            "_tags": [f"来源:{origin}"],
        })
    return out


def dedupe(items: list[dict]) -> list[dict]:
    """按 主机名 + 接口路径 去重，保留先出现的那条。"""
    seen, out = set(), []
    for it in items:
        k = host_key(it["url"]) + urllib.parse.urlparse(it["url"]).path.rstrip("/").lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
def probe_l1(entry: dict, timeout: float) -> dict:
    """一层：采集接口 ?ac=list 是否返回有效数据（快，判断接口活不活）。"""
    api = entry["url"]
    res = {"ok": False, "api_ms": None, "total": 0, "sample": "", "error": ""}
    for qs in ("?ac=list", "?ac=videolist"):  # 少数站只认 videolist
        code, body, ms, err = http_get(api + qs, timeout=timeout)
        res["api_ms"] = round(ms)
        if code != 200 or not body:
            res["error"] = err or f"HTTP {code}"
            continue
        try:
            j = json.loads(body.decode("utf-8", "ignore"))
        except json.JSONDecodeError:
            res["error"] = "非 JSON 响应"
            continue
        lst = j.get("list") or []
        if isinstance(lst, dict):
            lst = [lst]
        if not lst:
            res["error"] = j.get("msg") or "list 为空"
            continue
        res["ok"] = True
        res["total"] = int(j.get("total") or len(lst) or 0)
        res["sample"] = str(lst[0].get("vod_name") or "")[:30]
        res["error"] = ""
        return res
    return res


_M3U8_RE = re.compile(r"(https?://[^\s#$'\"]+?\.m3u8[^\s#$'\"]*)", re.I)


def probe_l2(entry: dict, timeout: float) -> dict:
    """二层：取详情页第一条 m3u8，验证索引能解析、首个分片能下（判断真的能不能播）。"""
    res = {"play_ok": False, "ttfb_ms": None, "m3u8": "", "error": ""}
    code, body, _, err = http_get(entry["url"] + "?ac=detail&pg=1", timeout=timeout)
    if code != 200 or not body:
        res["error"] = err or f"HTTP {code}"
        return res
    try:
        j = json.loads(body.decode("utf-8", "ignore"))
    except json.JSONDecodeError:
        res["error"] = "详情非 JSON"
        return res
    lst = j.get("list") or []
    if isinstance(lst, dict):
        lst = [lst]
    play = ""
    for vod in lst:
        m = _M3U8_RE.search(str(vod.get("vod_play_url") or ""))
        if m:
            play = m.group(1)
            break
    if not play:
        res["error"] = "无 m3u8 播放地址"
        return res
    res["m3u8"] = play

    code, body, ms, err = http_get(play, timeout=timeout)
    if code != 200 or not body:
        res["error"] = "m3u8 " + (err or f"HTTP {code}")
        return res
    text = body.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        res["error"] = "m3u8 内容异常"
        return res
    res["ttfb_ms"] = round(ms)

    # 取第一个分片再验一次（相对路径要拼回绝对地址）
    seg = ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            seg = urllib.parse.urljoin(play, line)
            break
    if seg:
        scode, sbody, _, serr = http_get(seg, timeout=timeout)
        res["play_ok"] = scode == 200 and len(sbody) > 1000
        if not res["play_ok"]:
            res["error"] = "分片 " + (serr or f"HTTP {scode} / {len(sbody)}B")
    else:
        res["play_ok"] = True  # 主列表(master)情况下不强制取分片
    return res


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def to_subscription(items: list[dict], deep_map: dict) -> dict:
    sources = []
    for it in items:
        d = deep_map.get(it["url"]) or {}
        s = {"name": it["name"], "url": it["url"], "isAdult": it["isAdult"]}
        if it["detail"]:
            s["detail"] = it["detail"]
        sources.append(s)
    return {
        "name": "LibreTV 自动测活源",
        "version": 2,
        "snapshotDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generatedBy": "libretv_sources_build.py",
        "sources": sources,
    }


def render_html(items: list[dict], deep_map: dict, meta: dict) -> str:
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    rows = []
    for i, it in enumerate(items, 1):
        d = deep_map.get(it["url"]) or {}
        if "play_ok" not in d:          # 只做过一层探测，没验播放
            deep = '<span class="dim">未深测</span>'
        elif d.get("play_ok"):
            deep = f'<span class="ok">可播 {d.get("ttfb_ms")}ms</span>'
        else:
            deep = f'<span class="bad">{esc(d.get("error") or "不可播")}</span>'
        tags = " ".join(f'<span class="tag">{esc(t)}</span>' for t in it["_tags"][:3])
        adult = '<span class="tag adult">成人</span>' if it["isAdult"] else ""
        rows.append(f"""<tr>
      <td class="idx">{i}</td>
      <td><div class="nm">{esc(it['name'])} {adult}</div><div class="tags">{tags}</div></td>
      <td class="mono sm">{esc(it['url'])}</td>
      <td class="num">{it['_rating'] or '-'}</td>
      <td class="num">{it['_count'] or '-'}</td>
      <td class="num">{d.get('api_ms') if d else '-'}ms</td>
      <td class="num">{it['_today'] or 0}</td>
      <td>{deep or '<span class="dim">未深测</span>'}</td>
    </tr>""")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LibreTV 源 · 测活报告</title>
<style>
 :root{{--bg:#0d1117;--card:#161b22;--line:#232b36;--tx:#e6edf3;--tx2:#9aa8b8;--tx3:#6b7889;--ok:#7ee787;--bad:#ff7b72;--warn:#d29922}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--tx);font-family:-apple-system,"Segoe UI","PingFang SC",sans-serif;font-size:14px}}
 .wrap{{max-width:1180px;margin:0 auto;padding:40px 24px 70px}}
 h1{{font-size:23px;margin:0 0 8px;font-weight:650;letter-spacing:-.2px}}
 .sub{{color:var(--tx2);font-size:13px;line-height:1.7;margin-bottom:26px}}
 .kpis{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:26px}}
 .kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;min-width:132px}}
 .kpi b{{display:block;font-size:22px;font-weight:650;margin-bottom:3px}}
 .kpi span{{font-size:11.5px;color:var(--tx3)}}
 .kpi.g b{{color:var(--ok)}} .kpi.r b{{color:var(--bad)}} .kpi.y b{{color:var(--warn)}}
 table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
 th{{text-align:left;font-size:11px;letter-spacing:.9px;text-transform:uppercase;color:var(--tx3);font-weight:600;
     padding:11px 12px;border-bottom:1px solid var(--line);background:#131920;white-space:nowrap}}
 td{{padding:11px 12px;border-bottom:1px solid #1b222c;vertical-align:middle}}
 tr:last-child td{{border-bottom:0}} tr:hover td{{background:#182029}}
 .idx{{color:var(--tx3);font-variant-numeric:tabular-nums;width:38px}}
 .nm{{font-weight:600;margin-bottom:4px}}
 .mono{{font-family:ui-monospace,Consolas,monospace}} .sm{{font-size:11.5px;color:var(--tx2)}}
 .num{{text-align:right;font-variant-numeric:tabular-nums;color:var(--tx2)}}
 .ok{{color:var(--ok);font-size:12.5px}} .bad{{color:var(--bad);font-size:12.5px}} .dim{{color:var(--tx3);font-size:12.5px}}
 .tags{{display:flex;gap:5px;flex-wrap:wrap}}
 .tag{{font-size:10.5px;color:var(--tx3);border:1px solid var(--line);border-radius:4px;padding:1px 5px}}
 .tag.adult{{color:var(--warn);border-color:rgba(210,153,34,.35)}}
 footer{{margin-top:26px;font-size:11.5px;color:var(--tx3);line-height:1.8}}
</style></head><body><div class="wrap">
<h1>LibreTV 源 · 测活报告</h1>
<div class="sub">生成于 {meta['time']} · 数据源 <span class="mono">{esc(meta['source'])}</span><br>
只保留采集接口真正返回数据的源；深度探测项额外验证了 m3u8 索引与首个分片是否可达。</div>
<div class="kpis">
  <div class="kpi g"><b>{len(items)}</b><span>存活点播源</span></div>
  <div class="kpi r"><b>{meta['dropped']}</b><span>探测失败已剔除</span></div>
  <div class="kpi y"><b>{meta['deep_n']}</b><span>深度探测</span></div>
  <div class="kpi"><b>{meta['ms']}ms</b><span>平均接口延迟</span></div>
</div>
<table><thead><tr>
<th>#</th><th>名称 / 标签</th><th>采集接口</th><th>评分</th><th>资源数</th><th>延迟</th><th>今日更新</th><th>播放验证</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
<footer>
本页仅为接口可用性记录，不存储、不分发任何媒体文件。第三方服务与内容的权利归其权利人所有，
公开接口或开源配置不代表取得内容授权，请仅使用获得许可的服务与内容。
</footer></div></body></html>"""


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="资源站数据 → LibreTV 订阅（带测活）")
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="资源站监测 JSON 地址")
    ap.add_argument("--merge", action="append", default=[], help="额外合并的 LibreTV 订阅地址，可多次")
    ap.add_argument("--out", default="dist", help="输出目录")
    ap.add_argument("--deep", type=int, default=0, help="对前 N 个做 m3u8 深度探测")
    ap.add_argument("--no-probe", action="store_true", help="跳过探测，只做格式转换")
    ap.add_argument("--timeout", type=float, default=8.0, help="单请求超时秒数")
    ap.add_argument("--workers", type=int, default=12, help="并发数")
    ap.add_argument("--min-rating", type=float, default=0.0, help="只保留评分不低于此值的源")
    ap.add_argument("--keep-adult", action="store_true", help="保留成人源（默认保留，加 --drop-adult 剔除）")
    ap.add_argument("--drop-adult", action="store_true", help="剔除成人源")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] 拉取资源站数据 {args.source}")
    raw = load_json_url(args.source, args.timeout)
    items = parse_ziyuanzhan(raw) if isinstance(raw, dict) else []
    print(f"      解析到 {len(items)} 条在线源")

    for m in args.merge:
        print(f"[1/4] 合并订阅 {m}")
        sub = load_json_url(m, args.timeout)
        extra = parse_subscription(sub, host_key(m))
        print(f"      +{len(extra)} 条")
        items.extend(extra)

    items = dedupe(items)
    if args.min_rating > 0:
        items = [x for x in items if x["_rating"] >= args.min_rating]
    if args.drop_adult:
        items = [x for x in items if not x["isAdult"]]
    # 排序：评分优先，其次响应时间，再次资源量
    items.sort(key=lambda x: (-x["_rating"], x["_rt"], -x["_count"]))
    print(f"      去重/过滤后 {len(items)} 条")

    deep_map: dict[str, dict] = {}
    if args.no_probe:
        print("[2/4] 跳过探测")
        alive, dropped = items, 0
    else:
        print(f"[2/4] 探测采集接口（{args.workers} 并发，超时 {args.timeout}s）")
        t0 = time.time()
        alive, dropped = [], 0
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(probe_l1, it, args.timeout): it for it in items}
            done = 0
            for f in cf.as_completed(futs):
                it = futs[f]
                r = f.result()
                done += 1
                deep_map[it["url"]] = {"api_ms": r["api_ms"], **r}
                if r["ok"]:
                    it["_total"] = r["total"]
                    if r["total"] > it["_count"]:
                        it["_count"] = r["total"]
                    if r["sample"]:
                        it["_sample"] = r["sample"]
                    alive.append(it)
                else:
                    dropped += 1
                if done % 10 == 0 or done == len(items):
                    print(f"      {done}/{len(items)} 完成，存活 {len(alive)}")
        alive.sort(key=lambda x: (-x["_rating"], x["_rt"]))
        print(f"      用时 {time.time()-t0:.1f}s，存活 {len(alive)} / {len(items)}")

    if args.deep > 0 and alive:
        n = min(args.deep, len(alive))
        print(f"[3/4] 深度探测前 {n} 个的 m3u8 可播性")
        with cf.ThreadPoolExecutor(max_workers=min(8, n)) as ex:
            futs = {ex.submit(probe_l2, it, args.timeout): it for it in alive[:n]}
            for f in cf.as_completed(futs):
                it = futs[f]
                deep_map.setdefault(it["url"], {}).update(f.result())
        ok = sum(1 for it in alive[:n] if (deep_map.get(it["url"]) or {}).get("play_ok"))
        print(f"      可播 {ok} / {n}")
    else:
        print("[3/4] 跳过深度探测（加 --deep N 开启）")

    sub = to_subscription(alive, deep_map)
    sub_path = outdir / "libretv-sources.json"
    sub_path.write_text(json.dumps(sub, ensure_ascii=False, indent=2), encoding="utf-8")

    rep_path = outdir / "report.json"
    rep_path.write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": args.source,
        "alive": len(alive),
        "dropped": dropped,
        "items": [{
            "name": it["name"], "url": it["url"], "detail": it["detail"],
            "rating": it["_rating"], "resources": it["_count"],
            "todayUpdates": it["_today"], "tags": it["_tags"],
            "apiMs": (deep_map.get(it["url"]) or {}).get("api_ms"),
            "playOk": (deep_map.get(it["url"]) or {}).get("play_ok"),
            "playMs": (deep_map.get(it["url"]) or {}).get("ttfb_ms"),
        } for it in alive],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    ms_list = [v["api_ms"] for v in deep_map.values() if v.get("api_ms")]
    html = render_html(alive, deep_map, {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": args.source, "dropped": dropped,
        "deep_n": min(args.deep, len(alive)) if alive else 0,
        "ms": round(sum(ms_list) / len(ms_list)) if ms_list else 0,
    })
    (outdir / "report.html").write_text(html, encoding="utf-8")

    print(f"[4/4] 完成")
    print(f"      订阅文件 {sub_path}  ({len(sub['sources'])} 个点播源)")
    print(f"      报告     {outdir / 'report.html'}")
    print(f"      明细     {rep_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
