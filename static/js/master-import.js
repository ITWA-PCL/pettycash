(() => {
  const root = document.querySelector('.import-workspace');
  if (!root || root.dataset.importBound) return;
  root.dataset.importBound = 'true';
  const input = root.querySelector('#id_workbook');
  const zone = root.querySelector('#master-dropzone');
  const selected = root.querySelector('#master-selected-file');
  const error = root.querySelector('#master-file-error');
  const preview = root.querySelector('#master-preview-panel');
  const stale = root.querySelector('#master-preview-stale');
  let changedFile = false;
  const updatePreview = () => {
    if (!preview) return;
    const mode = root.querySelector('[name=mode]:checked')?.value;
    const dirty = changedFile || mode !== preview.dataset.mode;
    preview.hidden = dirty;
    stale.hidden = !dirty;
  };
  const showFile = () => {
    const file = input.files[0];
    changedFile = true;
    updatePreview();
    selected.hidden = !file;
    error.hidden = true;
    input.setCustomValidity('');
    if (!file) return;
    root.querySelector('#master-file-name').textContent = file.name;
    root.querySelector('#master-file-size').textContent = (file.size / 1024 < 1024 ? (file.size / 1024).toFixed(1) + ' KB' : (file.size / 1024 / 1024).toFixed(1) + ' MB') + ' · พร้อมตรวจไฟล์';
    if (!/\.xlsx$/i.test(file.name) || file.size > 5 * 1024 * 1024) {
      error.textContent = 'เลือกไฟล์ .xlsx ขนาดไม่เกิน 5 MB';
      error.hidden = false;
      input.setCustomValidity(error.textContent);
    }
  };
  input.addEventListener('change', showFile);
  root.querySelector('#master-change-file').addEventListener('click', () => input.click());
  root.querySelectorAll('[name=mode]').forEach(option => option.addEventListener('change', updatePreview));
  ['dragenter', 'dragover'].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); zone.classList.add('is-dragging'); }));
  ['dragleave', 'drop'].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); zone.classList.remove('is-dragging'); }));
  zone.addEventListener('drop', event => {
    if (!event.dataTransfer.files.length) return;
    const transfer = new DataTransfer();
    transfer.items.add(event.dataTransfer.files[0]);
    input.files = transfer.files;
    showFile();
  });
  const data = root.querySelector('#master-preview-data');
  if (!data) return;
  const rows = JSON.parse(data.textContent);
  const search = root.querySelector('#master-preview-search');
  const size = root.querySelector('#master-preview-size');
  const previous = root.querySelector('#master-preview-prev');
  const next = root.querySelector('#master-preview-next');
  let current = 1;
  const render = () => {
    const query = search.value.trim().toLocaleLowerCase();
    const filtered = rows.map((values, index) => ({values, index})).filter(row => row.values.join(' ').toLocaleLowerCase().includes(query));
    const count = Number(size.value);
    const pages = Math.max(1, Math.ceil(filtered.length / count));
    current = Math.min(current, pages);
    const start = (current - 1) * count;
    const fragment = document.createDocumentFragment();
    filtered.slice(start, start + count).forEach(row => {
      const tr = document.createElement('tr');
      [row.index + 1, ...row.values].forEach((value, index) => {
        const td = document.createElement('td');
        td.textContent = value;
        if (!index) td.className = 'import-row-number';
        tr.append(td);
      });
      fragment.append(tr);
    });
    root.querySelector('#master-preview-rows').replaceChildren(fragment);
    root.querySelector('#master-preview-empty').hidden = filtered.length !== 0;
    root.querySelector('#master-preview-summary').textContent = filtered.length ? `${start + 1}–${Math.min(start + count, filtered.length)} จาก ${filtered.length} รายการ` : '0 รายการ';
    root.querySelector('#master-preview-page').textContent = `${current} / ${pages}`;
    previous.disabled = current === 1;
    next.disabled = current === pages;
  };
  search.addEventListener('input', () => { current = 1; render(); });
  size.addEventListener('change', () => { current = 1; render(); });
  previous.addEventListener('click', () => { current -= 1; render(); });
  next.addEventListener('click', () => { current += 1; render(); });
  render();
})();
