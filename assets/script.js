// 导航交互：移动端菜单 + 滚动高亮当前页
(function () {
  const toggle = document.querySelector('.nav-toggle');
  const links = document.querySelector('.nav-links');
  if (toggle && links) {
    toggle.addEventListener('click', () => links.classList.toggle('open'));
    links.querySelectorAll('a').forEach(a =>
      a.addEventListener('click', () => links.classList.remove('open'))
    );
  }

  // 当前页高亮
  const path = location.pathname.split('/').pop() || 'index.html';
  document.querySelectorAll('.nav-links a').forEach(a => {
    const href = a.getAttribute('href');
    if (href === path || (path === '' && href === 'index.html')) a.classList.add('active');
  });

  // 年份
  const y = document.querySelector('[data-year]');
  if (y) y.textContent = new Date().getFullYear();
})();

// ---------------------------------------------------------------------------
// 下载按钮动态化（2026-09-15 修 H4）
// 背景：三个页面里的下载按钮原本是「写死 v1.0.1 的地址」，而发布脚本
// tools/publish_update.py 只更新 version.json、**不会**改 HTML。结果每发一版
// 都得手工改 5 处链接，一旦漏改官网就挂着旧包地址（实测确实存在这个风险）。
//
// 现在：按钮带 data-download 标记，本脚本读取同目录的 version.json 后自动
//       改写下载地址与文案版本号。HTML 里保留的地址降级为「JS/网络不可用时的兜底」。
// ---------------------------------------------------------------------------
(function () {
  const links = document.querySelectorAll('[data-download]');
  if (!links.length) return;

  const REPO_RELEASE = (host, ver, file) =>
    `${host}/releases/download/v${ver}/${file}`;

  // version.json 的候选来源，按顺序尝试，取第一个可用的。
  //
  // ① Gitee raw（首选）：每次发版必更新，且国内直连可达 —— 即使本站（GitHub Pages）
  //    的内容因为网络原因没能及时推送，按钮也能指向最新版本，不会挂旧包地址。
  // ② 同目录 ./version.json（兜底）：站点自身携带的那份，可能滞后。
  const SOURCES = [
    'https://gitee.com/kuai061102/kuai-release/raw/master/version.json',
    `version.json?_=${Date.now()}`,
  ];

  function fetchFirst(candidates) {
    return candidates.reduce(
      (chain, url) => chain.catch(() =>
        fetch(url, { cache: 'no-store' })
          .then(r => (r.ok ? r.json() : Promise.reject(new Error('bad status'))))
          .then(info => {
            if (!info || !info.version) throw new Error('no version');
            return info;
          })
      ),
      Promise.reject(new Error('start'))
    );
  }

  fetchFirst(SOURCES)
    .then(info => {
      if (!info || !info.version) return;             // 读不到就保留硬编码兜底
      const ver = String(info.version).trim().replace(/^v/i, '');
      const file = `VideoPromptAssistant-Setup-${ver}.exe`;
      const primary = String(info.url || '').trim()
        || REPO_RELEASE('https://gitee.com/kuai061102/kuai-release', ver, file);
      const mirror = String(info.url_mirror || '').trim()
        || REPO_RELEASE('https://github.com/yichenzi016-spec/KuAi', ver, file);

      links.forEach(a => {
        a.href = a.getAttribute('data-download') === 'mirror' ? mirror : primary;
        // 同步按钮文案里的版本号（如「⬇ 免费下载 V1.0.1」→ V1.0.2）
        a.innerHTML = a.innerHTML.replace(/V\d+\.\d+\.\d+/gi, `V${ver}`);
      });

      // 页面上其它标注了版本号的位置。
      // 注意用「替换版本号子串」而不是整段 textContent —— 像
      // 「V1.0.1 正式版 · Windows 桌面应用」这类元素，整段赋值会把后缀文案抹掉。
      document.querySelectorAll('[data-version]').forEach(el => {
        el.innerHTML = el.innerHTML.replace(/V\d+\.\d+\.\d+/g, `V${ver}`);
      });

      // 浏览器标签页标题里的版本号（<title> 不是 DOM 元素，单独处理）
      document.title = document.title.replace(/V\d+\.\d+\.\d+/g, `V${ver}`);
    })
    .catch(() => { /* 静默：保留 HTML 里写死的兜底链接 */ });
})();
