(function () {
  // 在所有「下载按钮」后插入真实下载次数徽章
  function fmt(n) {
    try { return Number(n).toLocaleString('en-US'); }
    catch (e) { return String(n); }
  }

  function render(btn, n) {
    if (btn.nextElementSibling && btn.nextElementSibling.classList.contains('dl-count')) return;
    var badge = document.createElement('span');
    badge.className = 'dl-count';
    badge.innerHTML = '🔽 已下载 <b>' + fmt(n) + '</b> 次';
    btn.insertAdjacentElement('afterend', badge);
  }

  function init() {
    var btns = document.querySelectorAll('a.btn-primary[href*="releases/download"]');
    if (!btns.length) return;
    // 读同站点 downloads.json（无跨域，国内可访问）
    fetch('downloads.json?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && typeof d.downloads === 'number') {
          btns.forEach(function (b) { render(b, d.downloads); });
        }
      })
      .catch(function () { /* 静默降级：读不到就不显示，不影响下载 */ });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
