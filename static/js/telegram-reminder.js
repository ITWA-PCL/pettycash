(() => {
  const check = async () => {
    const reminder = document.querySelector('[data-telegram-check-url]');
    if (!reminder || reminder.dataset.checking) return;
    reminder.dataset.checking = 'true';
    const form = reminder.querySelector('[data-telegram-reminder-dismiss]');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(reminder.dataset.telegramCheckUrl, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Accept': 'application/json'}, body: new FormData(form), signal: controller.signal,
      });
      if (!response.ok || response.redirected) return;
      const status = await response.json();
      if (!reminder.isConnected) return;
      if (status.show_reminder === true) reminder.hidden = false;
      else reminder.remove();
    } catch (_) {
      // Do not call an unknown connection "not started" during an API outage.
    } finally {
      clearTimeout(timeout);
    }
  };
  document.addEventListener('page:loaded', check);
  check();
  document.addEventListener('submit', async event => {
    const form = event.target.closest('[data-telegram-reminder-dismiss]');
    if (!form) return;
    event.preventDefault();
    const reminder = form.closest('[data-telegram-reminder]');
    const button = form.querySelector('button');
    if (button.disabled) return;
    button.disabled = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(form.action, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Accept': 'application/json'}, body: new FormData(form), signal: controller.signal,
      });
      if (!response.ok || response.redirected || !(await response.json()).ok) throw new Error('Dismiss failed');
      reminder.remove();
    } catch (_) {
      button.disabled = false;
      reminder.querySelector('.telegram-reminder-error').hidden = false;
    } finally {
      clearTimeout(timeout);
    }
  }, true);
})();
