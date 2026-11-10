// Fetch fresh pages while retaining the shell. Never prefetch/cache workflow data.
(() => {
  let content = document.getElementById('page-content');
  if (!content || !window.fetch || !window.history.pushState) return;
  const progress = document.getElementById('navigation-progress');
  const status = document.getElementById('navigation-status');
  let activeRequest, sequence = 0, finishTimer, statusTimer, selectedLink;
  if (!history.state?.partialNavigation) history.replaceState({ partialNavigation: true, scrollY: window.scrollY }, '', location.href);
  const saveScroll = () => history.replaceState({ ...(history.state || {}), partialNavigation: true, scrollY: window.scrollY }, '', location.href);
  const clearFeedback = () => {
    clearTimeout(statusTimer);
    document.body.classList.remove('is-page-navigating');
    content?.removeAttribute('aria-busy');
    selectedLink?.classList.remove('is-navigation-target');
    selectedLink = null;
    if (status) { status.hidden = true; status.textContent = ''; }
  };
  const begin = (link) => {
    clearTimeout(finishTimer);
    clearFeedback();
    document.body.classList.add('is-page-navigating');
    content.setAttribute('aria-busy', 'true');
    selectedLink = link;
    selectedLink?.classList.add('is-navigation-target');
    if (progress) {
      progress.hidden = false;
      progress.classList.remove('is-finishing');
    }
    // Fast responses need only the unobtrusive progress line.
    statusTimer = setTimeout(() => {
      if (status) { status.textContent = 'กำลังเปิดหน้า…'; status.hidden = false; }
    }, 350);
  };
  const finish = () => {
    clearFeedback();
    if (progress) {
      progress.classList.add('is-finishing');
      finishTimer = setTimeout(() => { progress.hidden = true; progress.classList.remove('is-finishing'); }, 160);
    }
  };
  const runScripts = (container) => container.querySelectorAll('script').forEach((oldScript) => {
    const script = document.createElement('script');
    [...oldScript.attributes].forEach((attribute) => script.setAttribute(attribute.name, attribute.value));
    script.textContent = oldScript.textContent;
    oldScript.replaceWith(script);
  });
  const loadPage = async (url, { push = false, restoreScroll = 0, link } = {}) => {
    const current = ++sequence;
    activeRequest?.abort();
    const controller = new AbortController();
    activeRequest = controller;
    begin(link);
    let timeout;
    try {
      // A failed connection must not leave the page permanently waiting.
      timeout = setTimeout(() => controller.abort(), 20000);
      const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store',
        signal: controller.signal, headers: { 'X-Partial-Navigation': '1' } });
      if (current !== sequence) return;
      if (!response.ok || !response.headers.get('Content-Type')?.includes('text/html')) throw new Error('Unable to load page');
      const nextDocument = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (current !== sequence) return;
      const nextContent = nextDocument.getElementById('page-content');
      const destination = response.url || url;
      // Login/session redirects need a fresh shell as well as new content.
      if (!nextContent || !!nextDocument.querySelector('.topbar') !== !!document.querySelector('.topbar')) {
        location.assign(destination);
        return;
      }
      document.dispatchEvent(new CustomEvent('page:before-replace'));
      content.replaceWith(nextContent);
      content = nextContent;
      document.title = nextDocument.title || document.title;
      runScripts(content);
      if (push) history.pushState({ partialNavigation: true, scrollY: 0 }, '', destination);
      if (push) window.__pageRouteHistory?.push(destination); else window.__pageRouteHistory?.visit(destination);
      const target = new URL(destination, location.href).hash;
      if (target) {
        try { document.getElementById(decodeURIComponent(target.slice(1)))?.scrollIntoView(); } catch (_) {}
      } else window.scrollTo(0, restoreScroll);
      if (!matchMedia('(prefers-reduced-motion: reduce)').matches && content.animate) {
        content.animate([{ opacity: 0.78, transform: 'translateY(3px)' }, { opacity: 1, transform: 'translateY(0)' }],
          { duration: 140, easing: 'ease-out' });
      }
      document.dispatchEvent(new CustomEvent('page:loaded'));
    } catch (_) {
      if (current === sequence) location.assign(url);
    } finally {
      clearTimeout(timeout);
      if (current === sequence) { activeRequest = null; finish(); }
    }
  };
  const isPageLink = (link) => link && !link.target && !link.hasAttribute('download') &&
    link.dataset.noPartialNavigation === undefined && link.origin === location.origin &&
    ['http:', 'https:'].includes(link.protocol) &&
    !/(?:\/export|\/download|\/preview|\/print|\/slip|\/template)\/?$/.test(link.pathname);
  window.__navigateToPage = (url) => { saveScroll(); loadPage(url, { push: true }); };
  document.addEventListener('click', (event) => {
    const link = event.target.closest('a[href]');
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || !isPageLink(link)) return;
    if (link.pathname === location.pathname && link.search === location.search && link.hash) return;
    if (document.body.classList.contains('is-page-loading')) return;
    event.preventDefault();
    saveScroll();
    loadPage(link.href, { push: true, link });
  });
  window.addEventListener('popstate', (event) => {
    if (event.state?.partialNavigation) loadPage(location.href, { restoreScroll: event.state.scrollY || 0 });
  });
  window.addEventListener('pageshow', () => { clearTimeout(finishTimer); clearFeedback(); if (progress) progress.hidden = true; });
})();
