(() => {
  if (window.enhanceSearchableSelects) return;
  let active = null, sequence = 0;
  document.addEventListener('pointerdown', event => {
    if (active && !active.wrapper.contains(event.target)) active.close();
  });
  window.enhanceSearchableSelects = root => root.querySelectorAll('select').forEach(select => {
    if (select.dataset.searchableReady) return;
    select.dataset.searchableReady = 'true';
    const wrapper = document.createElement('div');
    wrapper.className = 'searchable-select';
    select.before(wrapper); wrapper.append(select);
    select.classList.add('searchable-native');
    select.setAttribute('aria-hidden', 'true'); select.tabIndex = -1;
    const caption = select.closest('.form-field')?.querySelector('label')?.textContent.replace(/[＊*:]/g, '').trim() || 'ตัวเลือก';
    const trigger = document.createElement('button');
    trigger.type = 'button'; trigger.className = 'searchable-trigger';
    trigger.setAttribute('aria-label', caption);
    trigger.setAttribute('aria-haspopup', 'listbox'); trigger.setAttribute('aria-expanded', 'false');
    const panel = document.createElement('div');
    panel.className = 'searchable-panel'; panel.hidden = true;
    const search = document.createElement('input');
    search.type = 'search'; search.className = 'searchable-query'; search.placeholder = 'พิมพ์เพื่อค้นหา…'; search.autocomplete = 'off';
    search.setAttribute('aria-label', 'ค้นหา ' + caption);
    search.setAttribute('role', 'combobox'); search.setAttribute('aria-autocomplete', 'list');
    const list = document.createElement('div');
    list.id = 'searchable-options-' + (++sequence); list.className = 'searchable-options';
    list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', caption);
    search.setAttribute('aria-controls', list.id); trigger.setAttribute('aria-controls', list.id);
    panel.append(search, list); wrapper.append(trigger, panel);
    let choices = [], index = -1;
    const close = () => {
      panel.hidden = true; trigger.setAttribute('aria-expanded', 'false'); search.setAttribute('aria-expanded', 'false');
      search.removeAttribute('aria-activedescendant');
      if (active?.wrapper === wrapper) active = null;
    };
    const choose = option => {
      select.value = option.value; select.dispatchEvent(new Event('change', { bubbles: true }));
      refresh(); close(); trigger.focus();
    };
    const render = () => {
      const query = search.value.trim().toLocaleLowerCase();
      choices = [...select.options].filter(option => !option.disabled && !option.hidden && option.textContent.toLocaleLowerCase().includes(query));
      list.replaceChildren(); search.removeAttribute('aria-activedescendant'); index = -1;
      choices.forEach((option, position) => {
        const button = document.createElement('button');
        button.type = 'button'; button.tabIndex = -1; button.id = list.id + '-' + position;
        button.setAttribute('role', 'option'); button.setAttribute('aria-selected', String(option.selected));
        button.textContent = option.textContent; button.addEventListener('click', () => choose(option)); list.append(button);
      });
      if (!choices.length) {
        const empty = document.createElement('p'); empty.className = 'searchable-empty'; empty.textContent = 'ไม่พบรายการที่ค้นหา'; list.append(empty);
      }
    };
    const refresh = () => {
      trigger.textContent = select.selectedOptions[0]?.textContent || 'เลือก ' + caption;
      trigger.disabled = select.disabled; trigger.classList.toggle('is-placeholder', !select.value);
      trigger.setAttribute('aria-required', String(select.required));
      if (select.disabled || select.closest('.form-field')?.hidden) close(); else if (!panel.hidden) render();
    };
    const open = () => {
      if (select.disabled) return;
      if (active) active.close(); active = { wrapper, close };
      search.value = ''; panel.hidden = false; trigger.setAttribute('aria-expanded', 'true'); search.setAttribute('aria-expanded', 'true'); render(); search.focus();
    };
    trigger.addEventListener('click', () => panel.hidden ? open() : close());
    trigger.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); open(); }
    });
    search.addEventListener('input', render);
    search.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (choices.length) {
          index = index < 0 ? (event.key === 'ArrowDown' ? 0 : choices.length - 1) : (index + (event.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length;
          [...list.children].forEach((button, position) => {
            button.classList.toggle('is-highlighted', position === index);
            if (position === index) { search.setAttribute('aria-activedescendant', button.id); button.scrollIntoView({ block: 'nearest' }); }
          });
        }
      } else if (event.key === 'Enter') {
        event.preventDefault(); if (choices.length) choose(choices[index < 0 ? 0 : index]);
      } else if (event.key === 'Escape') { event.preventDefault(); close(); trigger.focus(); }
      else if (event.key === 'Tab') close();
    });
    wrapper.addEventListener('focusout', () => setTimeout(() => { if (!wrapper.contains(document.activeElement)) close(); }, 0));
    select.addEventListener('change', refresh); select.addEventListener('searchable:refresh', refresh);
    select.addEventListener('focus', () => trigger.focus());
    select.addEventListener('invalid', event => { event.preventDefault(); open(); });
    new MutationObserver(refresh).observe(select, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled', 'required', 'selected'], characterData: true });
    refresh();
  });
})();
