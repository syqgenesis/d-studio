# 架构设计

## 1. 目标拆解

| # | 需求 | 对应模块 | 产物 |
|---|---|---|---|
| 1 | 录屏 + 光标轨迹（光标本体 + 拖尾） | `recorder` 采集 / `viewer` 叠加渲染 / `tools/burn_overlay.py` 烧录 | 视频 + `events.json` |
| 2 | 记录所有输入并与视频时钟同步 | `recorder`（LL 钩子 + QPC 时钟） | `events.json` |
| 3 | 工程师标注平台 | `viewer`（浏览器 SPA） | `narration.json` |
| 4 | 帧级对齐每个输入 | `packager`（分桶 + 合成） | `frame_events.json` |
| 5 | 元数据 | `packager` | `metadata.json` |

## 2. 数据流

```
                ┌──────────────── recorder (Windows, Python) ────────────────┐
                │  WH_MOUSE_LL / WH_KEYBOARD_LL  → 事件队列(seq/QPC 时间戳)   │
  屏幕帧 ───────▶│  GetForegroundWindow 轮询      → active_app/window_*       │
                │  GetCursorPos 轮询(兜底)       → mouse_move               │
                │  ffmpeg gdigrab → H.264/HEVC   → segments/*.mp4 + 逐帧墙钟 │
                └───────────────────────────┬───────────────────────────────┘
                                            ▼
                              raw/  (events.jsonl, segments.json, capture.json)
                                            ▼
                ┌──────────────── packager (纯离线, 可重跑) ─────────────────┐
                │  concat 片段 → video.mp4 (CFR)                            │
                │  clip 切分 → metadata.json                                │
                │  墙钟→video_t_ms → frame 分桶 → frame_events.json (JSONL) │
                │  视图索引 → view/mouse.jsonl, view/events.jsonl           │
                │  校验：帧数/事件数/时间单调/坐标越界                       │
                └───────────────────────────┬───────────────────────────────┘
                                            ▼
                ┌──────────── viewer (标注平台, 无构建步骤) ────────────────┐
                │  视频 + canvas 光标/拖尾/点击涟漪                          │
                │  事件密度时间轴 + 自动预分割 + 段落编辑                    │
                │  导出 narration.json（+ 草稿自动保存）                    │
                └──────────────────────────────────────────────────────────┘
```

## 3. 关键设计决策

### 3.1 光标画进视频（v0.2 起默认，与 Markov 一致）
Markov 的样例成片里有系统光标，因此 v0.2 默认把光标画进母带：DXGI 帧在采集线程里混合当前光标位图
（`recorder/cursor_draw.py`，支持透明/反色光标），`gdi` 后端用 `DrawIconEx`，`gdigrab` 用 `-draw_mouse 1`。
`capture.json` / `metadata.json` 的 `cursor_in_video` 记录是否画入；标注台据此默认关闭自身的光标叠加（拖尾和点击涟漪仍保留）。
需要「无光标」母带时用 `--no-draw-mouse-cursor` 录制，再用 `tools/burn_overlay.py` 从事件确定性地渲染。

### 3.1b “仅应用”模式（v0.2）
视频仍是整块显示器，`recorder/app_mask.py` 按 z 序枚举顶层窗口（DWM 扩展边框、cloaked、最小化、所属进程），
把不属于目标程序的区域算成一组黑色矩形，在编码前涂黑（DXGI：numpy 切片；GDI：`PatBlt`）。前台切到其他程序
≥300ms 时自动暂停（`privacy_mask_start/end`，时间从成片剪掉）；其他程序的窗口标题和按键从不记录。

### 3.2 采集与打包解耦
`recorder` 只保证「原始信号 + 逐帧墙钟时间戳」不丢不改；一切推导（clip 表、帧分桶、
事件裁剪、隐私打码）都在 `packager` 里做。好处：schema 改了、对齐策略改了，**不用重录**，
直接重跑 packager（几十秒）。这也是参考数据里 `source_wall_ms`/`sidecar_frame` 存在的原因。

### 3.3 大文件全部 JSONL + 流式
58 分钟数据的 `events.json` 是 231MB。用一行一对象的 JSONL，标注平台按需流式读、
训练侧按需 `itertools` 迭代、Linux `wc -l` 就能核对数量；`view/*.jsonl` 再把 UI 需要的
子集（每帧光标 1.5MB + 离散事件 1MB）单独抽出来，前端不用加载 231MB。

### 3.4 时间戳一律 QPC（`perf_counter`），墙钟只做映射
墙钟会被 NTP 校正跳变。事件同时记 `timestamp`（墙钟，用于与 clip 对齐）
和 `osTime`（`GetTickCount64`，单调，用于内部计算）。分桶只依赖「逐帧墙钟 sidecar」，
所以任何一侧的漂移都不会跨 clip 累积。

### 3.5 参考 ocap 的做法，但不直接依赖它
[`open-world-agents/ocap`](https://github.com/open-world-agents/ocap)（MIT）已经很完整：
单个 `recorder.py`、GStreamer + NVENC/HEVC、MKV + MCAP、含隐私遮罩与多显示器。
本项目与它的差异：

| | ocap | 本项目 |
|---|---|---|
| 输出 | `.mkv` + `.mcap`（OWAMcap） | 参考站同构的 `video.mp4` + 4 个 JSON |
| 依赖 | GStreamer bundle + NVIDIA 硬编 | ffmpeg（已在 PATH），无 Python 三方依赖 |
| 标注 | 无 | 内置浏览器标注平台 |
| 光标 | 可选烧录 | 母带干净 + canvas 实时叠加 + 确定性烧录 |

若已有 ocap 录制资产，`packager` 可以加一个 mcap 适配器（MCAP 是自描述格式，
`mcap` Python 库可逐条读），映射到本文档 schema 即可复用标注平台 —— 见 `docs/roadmap.md`。

## 4. 录制端要点（Windows）

- **鼠标**：`WH_MOUSE_LL` 拿到 `mouse_move / click / drag / scroll`。LL 钩子回调必须极轻
  （只入队 + 返回 `CallNextHookEx`），否则会拖慢全局输入，用户能感知。
  用 250Hz `GetCursorPos` 轮询做兜底（某些注入/远程输入不触发 LL 钩子）。
- **键盘**：`WH_KEYBOARD_LL` + `ToUnicodeEx` 还原字符；`isRepeat` 由「同 vk + 40ms 内」判定；
  修饰键单独发 `modifier_change`。
- **窗口**：`GetForegroundWindow` 100ms 轮询（比 `SetWinEventHook` 简单且够用），
  前后台变化发 `active_app`，矩形变化发 `window_moved/window_resized`（250ms 去抖）。
- **隐私**：前台进程命中黑名单 → `privacy_mask_start`，离开 → `privacy_mask_end`。
  打包阶段可选对这段时间区间做 `boxblur` 打码（事件里保留区间，便于审计）。
- **暂停/恢复**：每次恢复开一个新的 ffmpeg 片段，同时产生一个 clip；空洞自然被剪掉，
  这正是 `gap_before_ms` 的来源。
- **控制面**：`recorder/gui.py` 是 Tkinter 悬浮条，录制线程与 UI 线程分离，
  UI 只轮询 `Recorder.snapshot()`；`Recorder.stop()` 做成幂等且阻塞，
  保证「停止」返回时 `segments.json` 已落盘，自动打包不会读到半成品。
- **先 CFR 再 showinfo**：滤镜链是 `pad,fps=N,showinfo`，`showinfo` 拿到的是**最终视频的帧序号**。
  这一步很关键：gdigrab 启动时可能卡顿几百毫秒，ffmpeg 会用重复帧补齐，
  若在补帧前计数，sidecar 的帧号会比成片少若干帧，整段事件就会错位（实测差 10 帧）。
- **同步误差预算**：gdigrab + showinfo 锚定，典型 ±1 帧（30fps 下 ±33ms），
  用 `--sync-latency-ms` 标定（录制时做几次点击，在标注平台里看涟漪是否对得上点击反馈）。
- **双采集后端**：默认 `auto` 先用 DXGI Desktop Duplication（`bettercam`）——
  采集线程直接拿到帧缓冲并自己打时间戳；不可用（无 GPU/锁屏/安装了旧版依赖）则回退
  `gdi`（v0.2 新增，纯 ctypes BitBlt，同样逐帧进 Python，可做“仅应用”涂黑）或 `gdigrab`。
  各后端交付格式完全一致，切换不需要改打包/标注。
  DXGI 的节奏控制用 `FramePacer`：把任意采集时刻映射到固定 CFA 槽位，落后就复制上一帧
  （`duplicated_slots` 计数），落后超过 5s 判定 stall，由 recorder 切新 clip。
- **编码器要实测**：`ffmpeg -encoders` 里列出的编码器不代表能用（本机有 `h264_nvenc`
  但没有 `nvcuda.dll`），所以 `pick_encoder` 会先用 3 帧做一次试编码再决定，
  失败自动回退 `h264_amf → h264_qsv → libx264`。

## 5. 标注平台要点

- 无需构建：`index.html + app.js + style.css`，`python tools/serve.py` 打开即可。
- 每个段落 4 个字段，与参考数据一致：`action`（做了什么）/`intent`（为什么）/
  `app`（在哪个应用）/`details`（细节，可空）。校验：时间段不为空、不重叠、按序。
- 自动预分割：空闲 > 2s 或前台应用切换处切段，工程师只做合并/微调/填写，不手搓时间轴。
- 质量闸门（导出前）：覆盖率 ≥ 95%、每段至少 `action` 非空、段长 ≤ 60s、
  无重叠、无 < 0.5s 的碎片段。
- 导出走后端 `POST /api/save`，同时落 `drafts/` 与 `narration.json`，避免标注员丢稿。

## 6. 目录结构

```
destudio/
  recorder/     采集端（Windows）：clock, rawlog, win_input, win_window, videocap, recorder
  packager/     离线打包：build（clip/映射/分桶/校验/视图索引）
  viewer/       标注平台（静态站）
  tools/        serve.py（静态站 + 保存 API）, burn_overlay.py（烧录光标）, make_demo.py（演示数据）
  schemas/      4 个交付文件的 JSON Schema
  docs/         数据格式、架构、路线图
  data/<id>/    一个交付包 = 一个目录
```
