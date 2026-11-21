# 路线图

## 阶段 0（本次已交付，可跑通）

- [x] 采集端：鼠标/键盘/滚动/拖拽/窗口/系统状态 + 逐帧墙钟 sidecar
- [x] 打包：clip 切分、空洞丢弃、`video_t_ms` 映射、帧分桶、合成光标状态
- [x] 四个交付文件 + `view/` 索引 + `capture.json` + JSON Schema
- [x] 标注平台：光标/拖尾/涟漪叠加、事件时间轴、自动预分割、校验、草稿、导出
- [x] 光标烧录工具（流式，无临时 PNG 序列）
- [x] 合成演示数据 + 12 条端到端不变量测试

## 阶段 1：采集质量（1～2 周）

- [x] DXGI Desktop Duplication 后端（`bettercam`）：采集线程自己打时间戳，
      `FramePacer` 把任意采集时刻映射到 CFR 槽位；掉帧复制上一帧并计数，
      落后 >5s 判定 stall 自动切 clip；未装依赖时自动回退 gdigrab
- [ ] 采集线程与编码线程解耦（有界队列 + 背压），避免编码慢时阻塞 `get_latest_frame`
- [ ] 多显示器：DXGI output 索引与 Windows 监视器索引的自动映射（目前需 `--dxcam-output`）
- [ ] `SetWinEventHook` 精确窗口事件（`EVENT_OBJECT_LOCATIONCHANGE`、
      `EVENT_SYSTEM_FOREGROUND`）替代 100/250ms 轮询，去掉 `app_hidden/unhidden` 的缺失
- [ ] 应用友好名：从 PE 版本资源取 `FileDescription`（现在只有内置映射表）
- [ ] 多显示器 DPI 混合（125% + 100%）：验证 `gdigrab` 与 `GetCursorPos` 同一坐标空间
- [ ] 音频：WASAPI loopback 不依赖虚拟声卡（可用 `soundcard`/`pyaudiowpatch`）
- [ ] 长时间稳定性：8h 会话、事件队列背压、磁盘写满保护、断点续录
- [x] 采集端悬浮窗：开始/暂停/停止+自动打包、实时计数、窗口排除出画面
- [ ] 托盘图标 + 最小化到托盘 + 热键可配置 + 麦克风静音检测

## 阶段 2：隐私与合规（与阶段 1 并行）

- [ ] 前台命中黑名单时可选「暂停录制」而非仅打标记
- [ ] 打包阶段按 `privacy_mask_start/end` 区间做 `boxblur`/马赛克，逐帧追踪窗口矩形
- [ ] 密码框检测（`EM_SETPASSWORDCHAR` / UI Automation `IsPassword`）→ 区域遮罩
- [ ] 上传前 PII 扫描（正则：邮箱、身份证、密钥前缀）+ 人工抽检队列
- [ ] 数据保留策略与删除审计（谁在什么时候删了哪一段）

## 阶段 3：标注规模化（2～4 周）

- [ ] 服务端：Postgres 存段落 + 乐观锁 + 多人协作（当前是单文件 JSON + 后写覆盖）
- [ ] 标注任务分配：按 app/category 排队、领取锁、工作量统计、一致性抽检（双标 Kappa）
- [ ] 快捷键工作流打磨：`J/K/L` 变速、`A/B` 打点、段落内跳转、语音转写辅助
- [ ] LLM 预填：把段落内关键帧缩略图 + 事件摘要喂给多模态模型生成
      `action/intent` 草稿，人工只做 accept/edit（预计省 60% 时间）
- [ ] 自动预分割 v2：用前台 app + 窗口标题变化 + 点击簇做语义边界，替代纯空闲间隔
- [ ] 质量看板：覆盖率/段长分布/空字段率/按工程师的返工率

## 阶段 4：训练对接（与训练侧共同定义，1 周）

- [ ] 导出器：`narration.json` + `frame_events.json` → LeRobot / RLDS / WebDataset shard
- [ ] 动作空间约定：`(x, y)` 归一化坐标 vs 屏幕像素；点击/拖动/滚轮的 token 化方案
- [ ] 采样策略：关键帧（点击前后 ±3 帧）+ 均匀帧；负样本（空闲段）配比
- [ ] 训练用光标图：可参数化（大小/颜色/拖尾/是否绘制）并固化到 `metadata.cursor_style`
- [ ] 数据版本：`workflow_id` + 内容哈希，训练 config 里锁定版本

## 阶段 5：跨数据集互通

- [ ] MCAP 适配器：读 `open-world-agents/ocap` 的 `.mcap`（自描述、可逐条读），
      映射到本 schema，直接复用标注平台与打包校验
- [ ] 参考站数据集导入器：给定 `events.json`/`frame_events.json`/`video.mp4`，
      生成 `view/*.jsonl` 与 `geometry.json`，用于对比/迁移
- [ ] 格式版本迁移脚本（`events_schema` 2 → 3，字段重命名走映射表）

## 待定的开放问题

1. **滚轮符号**：本项目定义 `deltaY > 0 = 向上/远离用户`；参考数据用 macOS 风格增量，
   需与训练侧确认方向约定（否则模型学到相反的滚动语义）。
2. **暂停期间是否继续记录输入**：当前继续记录、打包丢弃（利于审计与状态延续）；
   若合规要求，应改为暂停时卸载钩子。
3. **空闲数据是否入训练集**：目前完整保留（含 `memory_pressure` 等系统事件），
   建议在导出器按策略裁剪，而不是在采集端丢。
4. **frame_events 的体积**：58 分钟 ≈ 68MB；若要做 10 万小时，需要 shard + 列存
   （parquet：`frame, video_t_ms, cursor_x, cursor_y, buttons` 一行一帧）。
