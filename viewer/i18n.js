/* D Studio annotator — bilingual (中文 / English) UI strings.
 *
 * Static markup is translated through attributes:
 *   data-i18n="key"              -> textContent
 *   data-i18n-title="key"        -> title
 *   data-i18n-placeholder="key"  -> placeholder
 *   data-i18n-aria="key"         -> aria-label
 * Dynamic text calls I18N.t('key', {param: value}) and re-renders on
 * I18N.onChange(cb).
 *
 * Language: ?lang=zh|en (wins, and is remembered) -> localStorage
 * 'dstudio.lang' -> navigator.language (zh* -> zh, else en).
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'dstudio.lang';
  const SUPPORTED = ['zh', 'en'];

  const DICT = {
    // ------------------------------------------------------------- top bar
    'app.title': { zh: 'D Studio · 标注台', en: 'D Studio · Annotator' },
    'brand.sub': { zh: '录屏数据标注', en: 'Recording annotation' },
    'top.dataset.title': { zh: '选择录制', en: 'Choose a recording' },
    'top.reload': { zh: '重新加载', en: 'Reload' },
    'top.reload.title': { zh: '重新读取录制列表和当前录制（新录制会出现在列表里）', en: 'Reload the recording list and the current recording (new recordings appear in the list)' },
    'top.lang.title': { zh: '切换语言：中文 / English', en: 'Switch language: 中文 / English' },

    // ----------------------------------------------------------- transport
    'tr.play.title': { zh: '播放 / 暂停 (Space)', en: 'Play / pause (Space)' },
    'tr.prev.title': { zh: '上一帧 (←)', en: 'Previous frame (←)' },
    'tr.next.title': { zh: '下一帧 (→)', en: 'Next frame (→)' },
    'tr.frame.title': { zh: '当前帧号', en: 'Current frame number' },
    'opt.cursor': { zh: '光标', en: 'Cursor' },
    'opt.cursor.title': { zh: '在视频上叠加绘制光标', en: 'Draw the cursor on top of the video' },
    'opt.cursor.inVideo': { zh: '（视频已含光标）', en: '(already in video)' },
    'opt.cursor.inVideo.title': { zh: '这段录制已把系统光标录进视频，所以叠加光标默认关闭，避免画两次。拖尾和点击涟漪仍可用。', en: 'This recording already has the system cursor drawn into the video, so the overlay cursor starts off to avoid drawing it twice. Trail and click ripples are still available.' },
    'opt.trail': { zh: '拖尾', en: 'Trail' },
    'opt.trail.title': { zh: '显示光标移动拖尾', en: 'Show the cursor movement trail' },
    'opt.events': { zh: '事件层', en: 'Events' },
    'opt.events.title': { zh: '显示点击涟漪和按键提示', en: 'Show click ripples and key presses' },
    'opt.speed': { zh: '速度', en: 'Speed' },

    // ----------------------------------------------------------- shortcuts
    'hint.play': { zh: '播放', en: 'play' },
    'hint.step': { zh: '逐帧', en: 'step a frame' },
    'hint.jump': { zh: '跳 1 秒', en: 'jump 1 s' },
    'hint.setStart': { zh: '设起点', en: 'set start' },
    'hint.setEnd': { zh: '设终点', en: 'set end' },
    'hint.new': { zh: '新建段', en: 'new segment' },
    'hint.delete': { zh: '删除', en: 'delete' },
    'hint.next': { zh: '跳到下一段', en: 'next segment' },

    // ------------------------------------------------------------ timeline
    'tl.title': { zh: '事件时间轴：点击跳转，按住拖动播放头，点下方色块选中段落', en: 'Event timeline: click to seek, drag to scrub, click a segment below to select it' },
    'lg.click': { zh: '点击', en: 'Click' },
    'lg.key': { zh: '按键', en: 'Key' },
    'lg.scroll': { zh: '滚动', en: 'Scroll' },
    'lg.drag': { zh: '拖拽', en: 'Drag' },
    'lg.cut': { zh: '录制断点', en: 'Recording cut' },
    'lg.cut.title': { zh: '暂停 / 自动暂停 / 中断处：这里的时间已从视频中剪掉', en: 'Pause / auto-pause / interruption: the time in between was cut from the video' },
    'lg.mask': { zh: '隐私遮挡 / 自动暂停', en: 'Privacy mask / auto-pause' },
    'lg.mask.title': { zh: '切到其他应用时自动暂停（privacy_mask_start / privacy_mask_end）', en: 'Recording paused automatically while another app was in front (privacy_mask_start / privacy_mask_end)' },
    'lg.segment': { zh: '标注段落', en: 'Segment' },
    'tip.time': { zh: '时间 {t} · 第 {f} 帧', en: 'Time {t} · frame {f}' },
    'tip.app': { zh: '应用：{app}', en: 'App: {app}' },
    'tip.segment': { zh: '段落 #{n}', en: 'Segment #{n}' },
    'tip.cut': { zh: '录制断点：片段 {n} 开始，前面剪掉 {gap} 秒', en: 'Recording cut: clip {n} starts, {gap} s removed before it' },
    'tip.cutClamped': { zh: '录制断点：片段 {n} 开始，前面剪掉超过 {gap} 秒', en: 'Recording cut: clip {n} starts, more than {gap} s removed before it' },
    'tip.autoPause': { zh: '自动暂停：切到了 {app}（已从视频中剪掉）', en: 'Auto-pause: {app} came to the front (cut from the video)' },
    'tip.mask': { zh: '隐私遮挡：{app}', en: 'Privacy mask: {app}' },
    'tip.unknownApp': { zh: '其他应用', en: 'another app' },

    // --------------------------------------------------------------- panel
    'panel.segments': { zh: '标注段落', en: 'Segments' },
    'btn.auto': { zh: '自动预分割', en: 'Auto-segment' },
    'btn.auto.title': { zh: '按空闲间隔、应用切换和录制断点自动预分割', en: 'Pre-split automatically at idle gaps, app switches and recording cuts' },
    'btn.new': { zh: '新建', en: 'New' },
    'btn.new.title': { zh: '在播放头新建段落 (N)', en: 'New segment at the playhead (N)' },
    'btn.split': { zh: '切分', en: 'Split' },
    'btn.split.title': { zh: '在播放头把选中段落切成两段', en: 'Split the selected segment at the playhead' },
    'btn.del': { zh: '删除', en: 'Delete' },
    'btn.del.title': { zh: '删除选中段落 (Del)', en: 'Delete the selected segment (Del)' },
    'editor.head': { zh: '段落详情', en: 'Segment details' },
    'editor.range': { zh: '时间段（秒）', en: 'Time range (seconds)' },
    'editor.setStart': { zh: '设起点', en: 'Start' },
    'editor.setStart.title': { zh: '把播放头位置设为起点 (S)', en: 'Set the start to the playhead (S)' },
    'editor.setEnd': { zh: '设终点', en: 'End' },
    'editor.setEnd.title': { zh: '把播放头位置设为终点 (E)', en: 'Set the end to the playhead (E)' },
    'field.action': { zh: 'action · 做了什么', en: 'action · what was done' },
    'field.intent': { zh: 'intent · 为什么', en: 'intent · why' },
    'field.app': { zh: 'app · 应用', en: 'app · application' },
    'field.details': { zh: 'details · 细节（可空）', en: 'details · specifics (optional)' },
    'field.summary': { zh: 'summary · 整段任务概述', en: 'summary · overall task' },
    'ph.start': { zh: '起点 start_s', en: 'start_s' },
    'ph.end': { zh: '终点 end_s', en: 'end_s' },
    'ph.action': { zh: '例：Opened the Color page and adjusted the sky mask …', en: 'e.g. Opened the Color page and adjusted the sky mask …' },
    'ph.intent': { zh: '例：Isolate the sky so the replacement grade only affects …', en: 'e.g. Isolate the sky so the replacement grade only affects …' },
    'ph.app': { zh: '例：SOLIDWORKS', en: 'e.g. SOLIDWORKS' },
    'ph.details': { zh: '例：界面上可见的状态、数值、失误 …', en: 'e.g. visible UI state, values, mistakes …' },
    'ph.summary': { zh: '一句话描述整个任务的目标', en: 'One sentence describing the goal of the whole task' },
    'btn.export': { zh: '导出 narration.json', en: 'Export narration.json' },
    'btn.export.title': { zh: '校验后写入录制目录的 narration.json，并下载一份副本', en: 'Validate, write narration.json into the recording folder and download a copy' },
    'btn.import': { zh: '导入', en: 'Import' },
    'btn.import.title': { zh: '从文件导入 narration.json（会替换当前段落）', en: 'Import a narration.json file (replaces the current segments)' },

    // ------------------------------------------------------- dynamic text
    'ds.none': { zh: '（没有录制）', en: '(no recordings)' },
    'ds.option': { zh: '{title} · {secs} 秒', en: '{title} · {secs} s' },
    'ds.annotated': { zh: '已标注 {n} 段', en: '{n} annotated' },
    'empty.loading': { zh: '加载中…', en: 'Loading…' },
    'empty.none': { zh: '数据目录里还没有已打包的录制。\n录制并停止（会自动打包）后，点「重新加载」。\n\n数据目录：{dir}', en: 'There are no packaged recordings in the data folder yet.\nRecord, stop (it is packaged automatically), then click “Reload”.\n\nData folder: {dir}' },
    'empty.server': { zh: '连不上标注台服务（{err}）。\n请从录制条打开标注台，或运行 START-VIEWER.bat，然后刷新本页。', en: 'Cannot reach the annotator server ({err}).\nOpen the annotator from the recorder bar or run START-VIEWER.bat, then refresh this page.' },
    'empty.loadFailed': { zh: '加载失败：{err}\n请确认 {dir}/ 下有 metadata.json、view/mouse.jsonl、view/events.jsonl 和 clip.mp4。', en: 'Could not load: {err}\nCheck that {dir}/ contains metadata.json, view/mouse.jsonl, view/events.jsonl and clip.mp4.' },
    'empty.noVideo': { zh: '这个录制里找不到视频文件（clip.mp4）。\n时间轴和标注仍可编辑。', en: 'This recording has no video file (clip.mp4).\nThe timeline and annotations can still be edited.' },
    'empty.videoError': { zh: '视频无法播放：{file}\n文件缺失、仍在写入，或浏览器不支持该编码（需要 H.264）。\n时间轴和标注仍可编辑。', en: 'The video cannot be played: {file}\nThe file is missing, still being written, or uses a codec this browser cannot play (H.264 is required).\nThe timeline and annotations can still be edited.' },
    'seg.noAction': { zh: '（未填写 action）', en: '(action not filled in)' },
    'seg.delete': { zh: '删除这一段', en: 'Delete this segment' },
    'seg.none': { zh: '未选中', en: 'None selected' },
    'v.endBeforeStart': { zh: '#{n} 结束时间 ≤ 开始时间', en: '#{n} end ≤ start' },
    'v.overlap': { zh: '#{n} 与前一段重叠', en: '#{n} overlaps the previous segment' },
    'v.noAction': { zh: '#{n} action 为空', en: '#{n} action is empty' },
    'v.tooLong': { zh: '#{n} 时长 {d} 秒 > 60 秒', en: '#{n} is {d} s long (> 60 s)' },
    'v.coverage': { zh: '覆盖率 {p}% < 95%', en: 'coverage {p}% < 95%' },
    'v.sep': { zh: '；', en: '; ' },
    'v.issues': { zh: '{n} 个问题：{list}', en: '{n} issue(s): {list}' },
    'v.ok': { zh: '校验通过 · 覆盖率 {p}%', en: 'Checks passed · coverage {p}%' },
    'save.saved': { zh: '已保存', en: 'Saved' },
    'save.dirty': { zh: '未保存…', en: 'Unsaved…' },
    'save.saving': { zh: '保存中…', en: 'Saving…' },
    'save.draftSaved': { zh: '草稿已保存', en: 'Draft saved' },
    'save.offline': { zh: '服务不可用（已暂存在浏览器）', en: 'Server unavailable (kept in this browser)' },
    'save.exported': { zh: '已导出 narration.json', en: 'narration.json exported' },
    'save.exportLocal': { zh: '服务不可用，只下载了文件', en: 'Server unavailable, file downloaded only' },
    'alert.exportBlocked': { zh: '还有 {n} 个段落不满足导出条件（action 为空 / 重叠 / 时间非法），请先修复。', en: '{n} segment(s) cannot be exported yet (empty action / overlap / invalid time). Please fix them first.' },
    'alert.importFailed': { zh: '导入失败：{err}', en: 'Import failed: {err}' },
    'alert.importEmpty': { zh: '文件里没有 annotations 列表。', en: 'The file has no annotations list.' },
    'confirm.auto': { zh: '自动预分割会替换当前的段落列表，继续吗？', en: 'Auto-segment will replace the current segment list. Continue?' },
    'confirm.import': { zh: '导入会替换当前的段落列表，继续吗？', en: 'Importing will replace the current segment list. Continue?' },
  };

  function norm(value) {
    const v = String(value || '').trim().toLowerCase();
    if (v.startsWith('zh')) return 'zh';
    if (v.startsWith('en')) return 'en';
    return null;
  }

  function storageGet() {
    try { return window.localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
  }
  function storageSet(value) {
    try { window.localStorage.setItem(STORAGE_KEY, value); } catch (e) { /* private mode */ }
  }

  function queryLang() {
    try { return norm(new URLSearchParams(window.location.search).get('lang')); } catch (e) { return null; }
  }

  function browserLang() {
    try {
      const nav = window.navigator || {};
      return String(nav.language || (nav.languages && nav.languages[0]) || '').toLowerCase().startsWith('zh') ? 'zh' : 'en';
    } catch (e) {
      return 'en';
    }
  }

  function initialLang() {
    const q = queryLang();
    if (q) { storageSet(q); return q; }
    return norm(storageGet()) || browserLang();
  }

  let lang = initialLang();
  const listeners = [];

  function t(key, params) {
    const entry = DICT[key];
    let text = entry ? (entry[lang] != null ? entry[lang] : entry.en) : key;
    if (params) {
      text = text.replace(/\{(\w+)\}/g, (m, k) => (Object.prototype.hasOwnProperty.call(params, k) ? String(params[k]) : m));
    }
    return text;
  }

  function each(root, selector, fn) {
    const nodes = root.querySelectorAll(selector);
    for (let i = 0; i < nodes.length; i++) fn(nodes[i]);
  }

  function apply(root) {
    if (typeof document === 'undefined') return;
    root = root || document;
    each(root, '[data-i18n]', (el) => { el.textContent = t(el.getAttribute('data-i18n')); });
    each(root, '[data-i18n-title]', (el) => { el.title = t(el.getAttribute('data-i18n-title')); });
    each(root, '[data-i18n-placeholder]', (el) => { el.placeholder = t(el.getAttribute('data-i18n-placeholder')); });
    each(root, '[data-i18n-aria]', (el) => { el.setAttribute('aria-label', t(el.getAttribute('data-i18n-aria'))); });
    each(root, '[data-lang]', (el) => { el.classList.toggle('on', el.getAttribute('data-lang') === lang); });
    if (root === document) {
      document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
      document.title = t('app.title');
    }
  }

  function syncUrl() {
    // keep an explicit ?lang= in step with the toggle so a reload keeps the choice
    try {
      const url = new URL(window.location.href);
      if (url.searchParams.has('lang') && url.searchParams.get('lang') !== lang) {
        url.searchParams.set('lang', lang);
        window.history.replaceState(window.history.state, '', url.toString());
      }
    } catch (e) { /* file:// or old browser */ }
  }

  function setLang(value) {
    const next = norm(value);
    if (!next) return lang;
    const changed = next !== lang;
    lang = next;
    storageSet(lang);
    syncUrl();
    apply();
    if (changed) {
      for (const cb of listeners.slice()) {
        try { cb(lang); } catch (e) { console.error(e); }
      }
    }
    return lang;
  }

  function toggle() { return setLang(lang === 'zh' ? 'en' : 'zh'); }
  function onChange(cb) { if (typeof cb === 'function') listeners.push(cb); }

  function init() {
    apply();
    const btn = document.getElementById('btn-lang');
    if (btn && !btn.dataset.bound) {
      btn.dataset.bound = '1';
      btn.addEventListener('click', () => toggle());
    }
  }

  window.I18N = {
    t, apply, setLang, toggle, onChange, init, DICT, SUPPORTED,
    get lang() { return lang; },
  };

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }
})();
