/* Cursor / trail / click-ripple canvas rendering.
 * All coordinates are screen pixels; `scale` maps them to canvas pixels.
 */
(function () {
  'use strict';

  const ARROW = [
    [0, 0], [0, 18.6], [4.7, 14.2], [7.5, 20.6], [10.3, 19.3], [7.6, 13.1], [13.3, 13.1],
  ];
  const BUTTON_COLORS = { left: '#4da3ff', right: '#ff6b6b', middle: '#ffc857' };

  function cursorPath(ctx, x, y, s) {
    ctx.beginPath();
    ARROW.forEach(([ax, ay], i) => {
      const px = x + ax * s;
      const py = y + ay * s;
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.closePath();
  }

  function drawCursor(ctx, x, y, s, pressed) {
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,.55)';
    ctx.shadowBlur = 3;
    ctx.shadowOffsetY = 1;
    cursorPath(ctx, x, y, s * (pressed ? 0.94 : 1));
    ctx.fillStyle = pressed ? '#cfe6ff' : '#ffffff';
    ctx.fill();
    ctx.shadowColor = 'transparent';
    ctx.lineWidth = Math.max(1, 1.2 * s);
    ctx.strokeStyle = 'rgba(0,0,0,.85)';
    ctx.lineJoin = 'round';
    ctx.stroke();
    ctx.restore();
  }

  function drawTrail(ctx, points, s) {
    if (points.length < 2) return;
    const n = points.length;
    for (let i = 1; i < n; i++) {
      const a = (i / n);
      const p0 = points[i - 1], p1 = points[i];
      ctx.beginPath();
      ctx.moveTo(p0[0], p0[1]);
      ctx.lineTo(p1[0], p1[1]);
      ctx.strokeStyle = 'rgba(120,190,255,' + (0.08 + 0.5 * a * a).toFixed(3) + ')';
      ctx.lineWidth = Math.max(0.8, s * (1.0 + 3.2 * a));
      ctx.lineCap = 'round';
      ctx.stroke();
    }
  }

  function drawRipple(ctx, x, y, s, progress, button) {
    const color = BUTTON_COLORS[button] || BUTTON_COLORS.left;
    const r = s * (4 + 22 * progress);
    ctx.save();
    ctx.globalAlpha = 0.75 * (1 - progress);
    ctx.strokeStyle = color;
    ctx.lineWidth = Math.max(1, s * 2.2 * (1 - progress) + 0.5);
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.stroke();
    ctx.globalAlpha = 0.25 * (1 - progress);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, r * 0.55, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  function drawKeyChip(ctx, text, W, H, s, ttl) {
    if (!text) return;
    ctx.save();
    ctx.globalAlpha = Math.min(1, ttl / 250);
    ctx.font = `${Math.max(9, Math.round(13 * s))}px "Segoe UI", "Microsoft YaHei", system-ui, sans-serif`;
    const padX = 10 * s, padY = 6 * s;
    const w = ctx.measureText(text).width + padX * 2;
    const h = 26 * s;
    const x = (W - w) / 2, y = H - h - 18 * s;
    ctx.fillStyle = 'rgba(10,16,24,.78)';
    ctx.strokeStyle = 'rgba(120,190,255,.55)';
    ctx.lineWidth = Math.max(1, s);
    const r = 7 * s;
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = '#e6edf3';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, x + padX, y + h / 2 + 0.5 * s);
    ctx.restore();
  }

  window.Overlay = { drawCursor, drawTrail, drawRipple, drawKeyChip };
})();
