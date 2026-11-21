# D Studio 0.2 — real-PC test checklist / 真机测试清单

GitHub's Windows test machines already check that recording, masking, auto-pause, crash recovery,
export, install and uninstall work, but only on a 1024×768 virtual screen with Notepad. These checks
need a real CAD workstation. About 30 minutes.
GitHub 的 Windows 测试机已验证录制、遮黑、自动暂停、崩溃恢复、导出、安装和卸载，但只在 1024×768 虚拟屏和记事本上。
下面这些需要在真实的 CAD 工作站上确认，约 30 分钟。

| # | Do / 操作 | Expect / 预期 |
|---|---|---|
| 1 | Install `DStudio-Setup-0.2.1.exe` (SmartScreen: More info → Run anyway). Start menu → **D Studio Self-test**. / 安装后运行「D Studio 自检」 | All OK, `DXGI desktop duplication works`. / 全部 OK，DXGI 可用 |
| 2 | Open **D Studio**, toggle 中/EN. / 切换中英文 | Every label switches; bar not visible in recordings. / 所有文字切换；悬浮条不会出现在录像中 |
| 3 | Full screen, 30 fps, record 2 min of real SolidWorks/NX work, F9 pause/resume once, F10 stop. / 全屏 30fps 录 2 分钟，F9 暂停一次，F10 停止 | Bar shows "Packaged". `↓ Export recording` → 5 files. Video smooth, cursor visible, only the main display. / 显示已打包；导出 5 个文件；视频流畅、有光标、只录主屏 |
| 4 | Same with 60 fps. / 60fps 再录一次 | Bar shows ~60 fps; a warning that Markov uses 30. / 显示约 60fps，并提示 Markov 为 30 |
| 5 | App only → Choose app → SOLIDWORKS/NX. Record; meanwhile get a WeChat/QQ/Teams message, open the Start menu, hover a taskbar thumbnail. / 仅应用模式录 CAD，期间收消息、开开始菜单、悬停任务栏缩略图 | In the video everything except the CAD window is black, incl. pop-ups and the taskbar; CAD menus/dialogs visible. / 除 CAD 外全部黑色（含弹窗、任务栏），CAD 菜单和对话框可见 |
| 6 | During 5: maximize/restore the CAD window, minimize it, click WeChat, come back. / 最大化/还原、最小化 CAD，切到微信再切回 | No flash of other windows during animations; "Auto-paused" while WeChat is in front; that time cut from the video. / 动画期间不漏出其他窗口；微信在前台时自动暂停并剪掉 |
| 7 | Type in a Chinese IME inside CAD (e.g. a part name). / 在 CAD 中用中文输入法输入 | IME candidate box visible; text logged. / 候选框可见，按键已记录 |
| 8 | If you use a password manager: open it while recording. / 录制时打开密码管理器 | Recording pauses ("Privacy pause"), nothing of it in video or events. / 自动隐私暂停，视频与事件中都没有它 |
| 9 | Ctrl+F9 / F9 inside CAD while **not** recording; Ctrl+F9 while recording. / 不录制时按 F9，录制时按 Ctrl+F9 | CAD shortcuts still work; Ctrl+F9 does not pause. / CAD 快捷键不受影响 |
| 10 | Record, then kill the bar in Task Manager (pythonw.exe), reopen D Studio. / 录制中在任务管理器结束 pythonw.exe，再打开 | "Found 1 recording(s) that did not finish… Recover?" → Yes → recording packaged (last few seconds lost). / 提示找回，选是后可打包 |
| 11 | If the CAD app runs as administrator: record with D Studio normal, then as admin. / CAD 以管理员运行时分别测试 | Normal: CAD keys/clicks may be missing (expected, shown in app picker as "(admin)"). Admin: all logged. / 普通权限可能缺键鼠事件；管理员运行则完整 |
| 12 | `↗ Open annotator`, write one narration segment, save; `Verify & export Markov CAD` with a task-set folder. / 打开标注台写一段解说并保存；用任务集导出 Markov CAD | Annotator in the bar's language; export makes 10 files. / 标注台语言一致；导出 10 个文件 |
| 13 | Multi-monitor / 125–150 % scaling (if available). / 多显示器、125–150% 缩放 | Full screen = main display only; app-only follows the CAD's monitor; cursor at the right spot. / 全屏只录主屏；仅应用跟随 CAD 所在屏；光标位置正确 |
| 14 | With 360安全卫士 or 火绒 running: install, open D Studio, record 1 min. / 在装有 360 或火绒的电脑上安装并录 1 分钟 | No "键盘记录 / keylogger" alert and nothing quarantined; if there is, add the D Studio folder to the trust list and report it. / 无"键盘记录"拦截或隔离；若有，将 D Studio 文件夹加入信任区并反馈 |
| 15 | Record one real 45–60 min session at 30 fps, watch Task Manager. / 正常录一段 45–60 分钟 | Memory of pythonw.exe stays flat (no climbing); fps stays ~30; Stop & package finishes in a few minutes. / pythonw.exe 内存不持续上涨；帧率约 30；停止并打包几分钟内完成 |

Send back / 回传：the `rec-…` folders of steps 3, 5, 6 (from `Documents\DStudio`) and
`Documents\DStudio\dstudio-gui.log` if anything went wrong.
