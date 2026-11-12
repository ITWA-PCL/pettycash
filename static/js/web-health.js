(() => {
  const root = document.querySelector('[data-web-health]');
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = 'true';
  let previousSample, timer, controller, loading = false, stopped = false;
  const find = name => root.querySelector(`[data-health-${name}]`);
  const bar = (name, percent) => {
    find(name + '-bar').style.width = `${percent ?? 0}%`;
    find(name + '-progress').setAttribute('aria-label', `${name === 'cpu' ? 'CPU' : 'Memory'} usage${percent == null ? '' : ` ${percent}%`}`);
  };
  const stop = () => {
    stopped = true;
    clearTimeout(timer);
    controller?.abort();
    document.removeEventListener('visibilitychange', onVisibility);
    document.removeEventListener('page:before-replace', stop);
  };
  const refresh = async () => {
    clearTimeout(timer);
    if (stopped || !root.isConnected) { stop(); return; }
    if (loading || document.visibilityState !== 'visible') return;
    loading = true;
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    let waiting = false;
    try {
      const response = await fetch(root.dataset.refreshUrl, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      if (response.redirected || response.status === 403) { find('cpu').textContent = 'ตรวจสอบไม่ได้'; stop(); return; }
      if (!response.ok) throw new Error('Health unavailable');
      const health = await response.json();
      if (stopped || !root.isConnected) return;
      let cpu = Number.isFinite(health.cpu_percent) ? health.cpu_percent : null;
      const sample = health.cpu_sample;
      if (sample && previousSample && sample.sampled_at > previousSample.sampled_at && sample.usage_usec >= previousSample.usage_usec && sample.available_cpus > 0) {
        cpu = Math.max(0, Math.min(100, Math.round((sample.usage_usec - previousSample.usage_usec) / 1000000 / (sample.sampled_at - previousSample.sampled_at) / sample.available_cpus * 100)));
      }
      previousSample = sample;
      waiting = cpu == null && !!sample;
      find('cpu').textContent = cpu == null ? (waiting ? 'กำลังวัด' : 'ตรวจสอบไม่ได้') : `${cpu}%`;
      bar('cpu', cpu);
      find('memory').textContent = health.memory_current;
      find('memory-limit').textContent = `จาก ${health.memory_limit}`;
      bar('memory', health.memory_percent);
      const database = find('db');
      database.textContent = health.database_ok ? 'เชื่อมต่อแล้ว' : 'ตรวจสอบไม่ได้';
      database.classList.toggle('is-on', !!health.database_ok);
      database.classList.toggle('is-off', !health.database_ok);
    } catch (_) {
      if (!stopped) {
        find('cpu').textContent = 'ตรวจสอบไม่ได้';
        bar('cpu', null);
        const database = find('db');
        database.textContent = 'ตรวจสอบไม่ได้';
        database.classList.remove('is-on');
        database.classList.add('is-off');
      }
    } finally {
      clearTimeout(timeout);
      loading = false;
      if (!stopped && root.isConnected) timer = setTimeout(refresh, waiting ? 500 : 5000);
    }
  };
  const onVisibility = () => { if (document.visibilityState === 'visible') refresh(); };
  document.addEventListener('visibilitychange', onVisibility);
  document.addEventListener('page:before-replace', stop, {once: true});
  refresh();
})();
