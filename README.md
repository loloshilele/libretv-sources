# libretv-sources

自动测活的 LibreTV 播放源订阅。每 6 小时重建一次，**只保留此刻真正能返回数据的采集接口**。

## 订阅地址

在 LibreTV 的「设置 → 数据源 → 数据源订阅」粘贴：

```
https://loloshilele.github.io/libretv-sources/libretv-sources.json
```

点「添加并同步」即可。地址永久固定，不需要再换。

备用（raw 直链，网络允许时可用）：

```
https://raw.githubusercontent.com/loloshilele/libretv-sources/main/dist/libretv-sources.json
```

## 为什么需要自己重建

公开的源清单几乎都是静态快照，而点播接口失效得非常快——本仓库首次构建时，监测站标为「在线」的 50 个源里只有 29 个真的能返回数据，另一小半已经死了。

所以这里不维护清单，维护的是**探测流程**：每次构建都重新验证，死的直接丢掉。

## 探测分两层

| 层 | 做法 | 能判断什么 |
|---|---|---|
| L1 | `GET {api}?ac=list` | 接口活不活、有多少资源 |
| L2 | `GET {api}?ac=detail&pg=1` → 提取 m3u8 → 校验 `#EXTM3U` → 下首个分片 | 接口活 ≠ 能播，这层才验真 |

只有 L1 通过的源才会进订阅文件；L2 结果写进报告，供你判断画质和速度。

## 产物

| 文件 | 说明 |
|---|---|
| `dist/libretv-sources.json` | LibreTV-SourceList 格式，直接订阅 |
| `dist/report.html` | 测活看板（延迟 / 资源量 / 播放验证） |
| `dist/report.json` | 同上，机器可读 |
| `build_libretv_sources.py` | 构建脚本，纯标准库无依赖 |

数据源：[ziyuanzhan](https://github.com/mylazily/ziyuanzhan) 每 6 小时更新的资源站监测 JSON。

## 本地手动跑一次

```bash
python build_libretv_sources.py --deep 15 --timeout 8 --drop-adult
```

常用参数：

```bash
--deep 20          # 深测前 20 个的 m3u8 可播性
--min-rating 4.0   # 只留高评分站
--merge <订阅URL>  # 合并别的 LibreTV 订阅一起探活
--no-probe         # 纯格式转换，不探测
--drop-adult       # 剔除成人源
```

## 自定义

改 `.github/workflows/build-sources.yml` 里 `Build subscription` 那一步的参数，推送即生效。
默认每 6 小时跑一次（整点 `0 */6 * * *`，UTC），也可以在 Actions 页面手动触发。

---

本仓库仅为接口可用性记录，不存储、不分发任何媒体文件。第三方服务与内容的权利归其权利人所有，公开接口或开源配置不代表取得内容授权，请仅使用获得许可的服务与内容。
