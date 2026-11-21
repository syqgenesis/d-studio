# 数据格式规范（events_schema = 2）

本文档由真实样本反向推导而来，字段名/单位/合成规则与参考数据集
`general-computer-use/resolve-sky-replacement-935a9eec` 保持一致。

参考样本实测值：
- `metadata.json`：21 个 clip，104049 帧，30fps，3468300ms，1241421 个事件
- `events.json`：231,034,079 B ≈ 186 B/事件
- `frame_events.json`：68,245,762 B ≈ 656 B/帧
- `narration.json`：136,498 B，345 条标注

> 关键结论：**events.json / frame_events.json 都是 JSONL（每行一个 JSON 对象，没有外层数组）**，
> 只有 `metadata.json`、`narration.json` 是普通 JSON。这让 200MB+ 的文件可以流式解析。

---

## 1. 一个交付包的内容

```
<package>/
  clip.mp4             # 单文件成片，CFR，默认 30fps（可选 60），系统光标已画入；暂停/自动暂停/掉线导致的空洞已被剪掉（旧版名 video.mp4）
  metadata.json        # 目录：帧数、时长、clip 表、fps
  events.json          # JSONL，全速率原始事件（~1kHz 鼠标）
  frame_events.json    # JSONL，逐帧对齐后的事件（一帧一行）
  narration.json       # 人工标注：时间段 + 动作/意图/应用/细节
```

## 2. 时间基准

三层时间，全部是毫秒：

| 字段 | 含义 | 例子 |
|---|---|---|
| `timestamp` | 墙钟时间（epoch ms，浮点，微秒级精度） | `1793285297672.0981` |
| `osTime` | 开机以来的 tick 计数（`GetTickCount64`），抗墙钟跳变 | `2356656` |
| `video_t_ms` | 成片时间轴上的位置 | `1560703.0981` |

**映射规则**（成片会剪掉暂停/中断的空洞）：

```
video_t_ms(e) = clip.frame_start * 1000/fps + (e.timestamp - clip.source_start_ms)
frame_index(e) = floor(video_t_ms(e) * fps / 1000)        # 落在 [t, t+1000/fps) 内
```

`clip` 取 `source_start_ms <= e.timestamp < source_start_ms + frame_count*1000/fps` 的那一段。

实测校验：click 事件 `timestamp=1793285297672.0981` 落在 clip6，
`video_t_ms=1560703.0981`，与 `frame_events` 中的值**逐位一致**。

⚠️ 反向用 metadata.clips 推算会引入 ~50ms 级误差（因为 `frame_count` 是整数帧，
而 clip 的墙钟跨度不是整数帧）。**所以录制端必须落一份「逐帧墙钟时间戳」sidecar**，
打包时用它做映射，clips 表只作为目录/统计用途。参考数据里 `source_wall_ms` +
`sidecar_frame` 就是这份东西的残留。

## 3. events.json（全速率原始事件）

每行一个事件，按 `timestamp` 升序。公共字段：

```json
{"type":"click","timestamp":1793285192368.5796,"seq":541754,"osTime":2251343, ...}
```

`seq` 是录制端单调递增序号（用于去重/排序/丢包检测），`osTime` 是 QPC 相对值。

### 3.1 事件类型总览（样本中出现过的全部）

| type | 关键字段 |
|---|---|
| `screen_config` | `screenCount, screens[]{index,originX,originY,width,height,visibleWidth,visibleHeight,scaleFactor,clipIndex,deviceName,primary}, reason` |
| `active_app` | `name, bundleId, windowTitle, pid, isFullScreen, hwnd, windowClass` |
| `app_launch` / `app_terminate` | `name, bundleId, pid, source` |
| `app_hidden` / `app_unhidden` | `name, bundleId, pid` |
| `window_moved` / `window_resized` | `x,y,width,height, appName, bundleId, windowTitle` |
| `window_title_changed` | `title, appName, bundleId` |
| `mouse_move` | `x,y, appName, bundleId, screen, sourcePid` |
| `mouse_delta` | `dx,dy, packets, appName, bundleId, screen, sourcePid`（原始 HID 相对位移） |
| `click` | `x,y, button("left"/"right"/"middle"/"other_3"), isDown, clickCount, pressure, modifiers` |
| `drag` | `x,y, button, pressure, modifiers` |
| `scroll` | `x,y, deltaX, deltaY, phase, momentumPhase, isPrecise, channel("wheel"), modifiers` |
| `key` | `keyCode(VK), characters, isDown, isRepeat, scanCode, extended, hkl, dead, modifiers` |
| `modifier_change` | `modifiers` |
| `privacy_mask_start` / `privacy_mask_end` | `trigger, maskTrigger, maskDetail, appName, exeName, windowTitle, maskedDisplay` |
| `appearance_change` | `appearance("dark"/"light")` |
| `battery_status` / `power_source_change` | `isCharging / isOnAC, batteryLevel` |
| `network_change` / `wifi_change` | `isConnected, interfaceType, isExpensive, isConstrained` / `ssid` |
| `memory_pressure` | `pressureLevel` |

`modifiers` 位掩码（本项目定义，需与训练侧约定）：`1=Shift 2=Ctrl 4=Alt 8=Win 16=CapsLock`。
样本中 `key` `keyCode=90`（Z）+ `characters="\u001a"` + `modifiers=2` 与此一致（Ctrl+Z）。

### 3.2 真实样本

```json
{"type":"mouse_move","x":1676,"y":893,"appName":"ShellHost","bundleId":"ShellHost.exe","screen":0,"sourcePid":0,"timestamp":1793283736969.0,"seq":2546,"osTime":772687}
{"type":"mouse_delta","dx":-2,"dy":1,"packets":1,"appName":"ShellHost","bundleId":"ShellHost.exe","screen":0,"sourcePid":0,"timestamp":1793283736969.0,"seq":2549,"osTime":772687}
{"type":"click","x":1361,"y":344,"button":"right","isDown":true,"clickCount":1,"pressure":1,"appName":"DaVinci Resolve","bundleId":"Resolve.exe","screen":0,"sourcePid":0,"timestamp":1793285192368.5796,"modifiers":0,"seq":541754,"osTime":2251343}
{"type":"key","keyCode":90,"characters":"\u001a","isDown":true,"modifiers":2,"appName":"DaVinci Resolve","bundleId":"Resolve.exe","isRepeat":false,"sourcePid":0,"timestamp":1793285191119.2673,"scanCode":44,"extended":false,"hkl":"40090409","dead":false,"seq":541436,"osTime":2250093}
{"type":"scroll","x":1465,"y":268,"deltaX":0,"deltaY":-1,"appName":"DaVinci Resolve","bundleId":"Resolve.exe","screen":0,"phase":0,"momentumPhase":0,"isPrecise":false,"sourcePid":0,"timestamp":1793285189052.705,"channel":"wheel","modifiers":0,"seq":540837,"osTime":2248031}
{"type":"drag","x":1336,"y":457,"button":"left","pressure":1,"appName":"Brave Browser","bundleId":"brave.exe","screen":0,"sourcePid":0,"timestamp":1793285196593.41,"modifiers":0,"seq":543039,"osTime":2255562}
```

## 4. frame_events.json（逐帧对齐）

一行一帧，共 `total_frames` 行（没有外层数组、无尾部汇总行）：

```json
{"frame":46821,"video_t_ms":1560700.0,"events":[ ... ]}
```

`video_t_ms` 恒等于 `frame * 1000/fps`（样本实测 `46821*1000/30 = 1560700`）。

每帧的 `events[]` 由三类构成：

1. **合成的光标状态**（每帧必有一条，`t_in_frame_ms=0`）
   ```json
   {"type":"mouse_move","x":1479,"y":305,"screen":0,
    "buttons":{"left":true,"right":false,"middle":false,"other_3":false,"other_4":false},
    "coordinate_space":"screen_px_top_left","synthetic":true,
    "source":"frame_timestamps_sidecar","source_wall_ms":1793285320939,
    "sidecar_frame":5770,"timestamp":1560700.0,"video_t_ms":1560700.0,"t_in_frame_ms":0}
   ```
   - 坐标取该帧起点时刻的**最后已知**位置（由 1kHz `mouse_move` 流插值/保持）
   - `source_wall_ms`：该帧的墙钟时间；`sidecar_frame`：clip 内帧号
   - 注意：这类事件不带 `seq`/`osTime`

2. **聚合的原始输入**（每帧最多各一条）
   ```json
   {"type":"mouse_delta","dx":69.0,"dy":14.0,"packets":7,"aggregated_count":4,"screen":0,"sourcePid":0,
    "timestamp":1560700.0,"video_t_ms":1560700.0,"t_in_frame_ms":0,
    "source":"wm_input_frame_sum","coordinate_space":"raw_hid_relative"}
   {"type":"drag","x":1479,"y":305,"screen":0,"buttons":{...},"button":"left","pressure":1,
    "aggregated_count":26,"sourcePid":0,"timestamp":1560700.0,"video_t_ms":1560700.0,"t_in_frame_ms":0,
    "source":"raw_drag_frame_state","coordinate_space":"screen_px_top_left",
    "appName":"Brave Browser","bundleId":"brave.exe"}
   ```
   `mouse_delta` 是帧内所有 delta 之和，`aggregated_count` 是合并条数。

3. **原样透传的离散事件**（click / key / scroll / window / app ...），保留原始 `seq`、`osTime`，
   并补充 `video_t_ms`、`t_in_frame_ms`：
   ```json
   {"type":"click","x":1463,"y":302,"button":"left","isDown":true,"clickCount":1,"pressure":1,
    "appName":"Brave Browser","bundleId":"brave.exe","screen":0,"sourcePid":0,
    "timestamp":1793285297672.0981,"modifiers":0,"seq":558581,"osTime":2356656,
    "video_t_ms":1560703.0981,"t_in_frame_ms":3.0981}
   ```
   `t_in_frame_ms = video_t_ms - frame*1000/fps`。

**分桶规则**：`floor(video_t_ms * fps / 1000) == frame`，跨 clip 边界的尾事件归入该 clip 的最后一帧。
`mouse_move` 原始事件**不进** frame_events（否则体积 ×30），只保留每帧一条合成状态。

## 5. metadata.json

```json
{
  "workflow_id": "935a9eec-8e34-5991-983a-781685403e26",
  "category": "video-editing",
  "platform": "windows",
  "title": "Resolve Sky Replacement",
  "description": "Develop the cityscape sky-replacement composite in DaVinci Resolve ...",
  "total_duration_ms": 3468300,
  "total_frames": 104049,
  "event_count": 1241421,
  "fps": 30,
  "events_schema": 2,
  "clips": [
    {"clip_index":0,"segment_index":0,"frame_start":0,"frame_count":9007,
     "source_start_ms":1793283736969,"gap_before_ms":0,"gap_before_clamped":false}
  ]
}
```

clip 生成规则（本项目实现）：

```
gap_before_ms = clamp(source_start_ms - prev_source_end_ms, 0, MAX_GAP_MS)
gap_before_clamped = (raw gap > MAX_GAP_MS)      # 超长空洞按会话截断
frame_start = 累计帧数（整数，保证 clip 无缝相接）
```

`metadata.json` 不承载录制端信息；我们的实现额外输出 `capture.json`（分辨率、编码器、后端、
暂停次数、隐私遮罩时长等），不污染训练侧 schema。

## 6. narration.json（人工标注产物）

```json
{
  "summary": "Create and refine a DaVinci Resolve city-sky composite by sourcing replacement footage, ...",
  "annotations": [
    {
      "start_s": 0.0,
      "end_s": 5.188,
      "action": "Opened and adjusted Quick settings, then switched among desktop windows ...",
      "intent": "Navigate between desktop windows and applications.",
      "app": "Windows Shell / Quick settings",
      "details": "Several app switches and launch/hide events occurred; no specific setting change is visible."
    }
  ]
}
```

- 时间单位是**秒**（浮点），与 `frame_events` 的 ms 不同，注意换算
- `annotations` 按 `start_s` 升序、**不重叠**、尽量无缝覆盖全片（样本 345 条覆盖 3468s）
- 一条标注 = 一个「语义动作段」，不是一次点击

## 7. 光标轨迹（需求 1）

参考数据里**没有**单独的轨迹文件：轨迹由 `events.json` 的 ~1kHz `mouse_move` 重建。
本项目额外输出 `view/mouse.jsonl`（每帧 `f,x,y,buttons`）+ `view/events.jsonl`（离散事件），
供标注平台和叠加烧录工具低带宽读取；`events.json` 仍是唯一真源。

轨迹渲染参数（与参考站观感一致）：

```
trail_window_ms   = 600      # 拖尾长度
trail_points      = 12~24    # 采样点（按距离重采样，避免密集点堆叠）
trail_width_px    = 3 → 0.5  # 线性变细
alpha             = 0.35 → 0 # 线性衰减
click_ripple_ms   = 350      # 按下涟漪
```
