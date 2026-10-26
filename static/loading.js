"use strict";
// Keep activity outside the rendered page so refreshes cannot erase it.
window.MerchantLoading = (() => {
  const active = new Set();
  let indicator = null, timer = null;
  const placeholder = () => '<div class="data-loading-placeholder" role="status" aria-label="正在加载"><span class="data-spinner" aria-hidden="true"></span></div>';
  function begin() {
    let parentEnd = null;
    if (window.parent !== window) {
      try { parentEnd = window.parent.MerchantLoading?.begin(); } catch {}
    }
    const end = () => {
      if (!active.delete(end)) return;
      parentEnd?.();
      if (!active.size) {
        clearTimeout(timer); timer = null;
        if (indicator) indicator.hidden = true;
      }
    };
    active.add(end);
    if (!parentEnd && !timer && (!indicator || indicator.hidden)) {
      // Local status polling normally finishes immediately; avoid flicker.
      timer = setTimeout(() => {
        timer = null;
        if (!active.size) return;
        if (!indicator || !indicator.isConnected) {
          indicator = document.createElement('div');
          indicator.id = 'data-loading';
          indicator.setAttribute('role', 'status');
          indicator.setAttribute('aria-live', 'polite');
          indicator.innerHTML = '<span class="data-spinner" aria-hidden="true"></span><span>加载中</span>';
          document.body.append(indicator);
        }
        indicator.hidden = false;
      }, 120);
    }
    return end;
  }
  // A removed embedded page must not leave the parent spinner running.
  window.addEventListener('pagehide', () => [...active].forEach(end => end()));
  return {begin, placeholder};
})();
