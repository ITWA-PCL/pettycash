(() => {
  const months = ['มกราคม','กุมภาพันธ์','มีนาคม','เมษายน','พฤษภาคม','มิถุนายน','กรกฎาคม','สิงหาคม','กันยายน','ตุลาคม','พฤศจิกายน','ธันวาคม'];
  document.querySelectorAll('[data-thai-month-picker]').forEach(root => {
    const value = root.querySelector('input[type=hidden]');
    const display = root.querySelector('.thai-month-display');
    const toggle = root.querySelector('.thai-month-open');
    const selectedYear = () => /^\d{4}-(0[1-9]|1[0-2])$/.test(value.value) && Number(value.value.slice(0,4)) > 0
      ? Number(value.value.slice(0,4)) : new Date().getFullYear();
    let year = selectedYear();
    const panel = document.createElement('div');
    panel.className = 'thai-month-calendar'; panel.hidden = true;
    panel.setAttribute('role','dialog'); panel.setAttribute('aria-label','เลือกเดือนและปีค่าใช้จ่าย');
    root.append(panel); panel.id = value.id + '_calendar';
    toggle.setAttribute('aria-controls', panel.id);
    display.setAttribute('aria-controls', panel.id); display.setAttribute('aria-haspopup','dialog');
    const close = () => { panel.hidden=true; toggle.setAttribute('aria-expanded','false'); display.setAttribute('aria-expanded','false'); };
    const choose = month => {
      value.value = String(year).padStart(4,'0') + '-' + String(month+1).padStart(2,'0');
      display.value = months[month] + ' ' + (year+543);
      display.setCustomValidity(''); value.dispatchEvent(new Event('change',{bubbles:true}));
      close(); display.focus();
    };
    const render = () => {
      panel.replaceChildren();
      const head=document.createElement('div'); head.className='thai-month-year';
      const label=document.createElement('strong'); label.textContent='พ.ศ. ' + (year+543);
      const nav = (text,offset,title) => {
        const button=document.createElement('button'); button.type='button'; button.textContent=text; button.setAttribute('aria-label',title);
        button.disabled=year+offset<1 || year+offset>9999;
        button.addEventListener('click',()=>{year+=offset;render();panel.querySelector('.thai-month-year button')?.focus();});
        return button;
      };
      head.append(nav('‹',-1,'ปีก่อนหน้า'),label,nav('›',1,'ปีถัดไป')); panel.append(head);
      const grid=document.createElement('div'); grid.className='thai-month-grid';
      months.forEach((name,index)=>{
        const button=document.createElement('button'); button.type='button'; button.textContent=name;
        const selected=value.value===String(year).padStart(4,'0')+'-'+String(index+1).padStart(2,'0');
        button.classList.toggle('is-selected',selected); button.setAttribute('aria-pressed',String(selected));
        button.addEventListener('click',()=>choose(index)); grid.append(button);
      }); panel.append(grid);
      const current=document.createElement('button'); current.type='button'; current.className='thai-month-current'; current.textContent='เดือนนี้';
      current.addEventListener('click',()=>{const today=new Date();year=today.getFullYear();choose(today.getMonth());}); panel.append(current);
    };
    const open = () => {
      if (!panel.hidden) {close(); return;}
      year=selectedYear();
      render(); panel.hidden=false; toggle.setAttribute('aria-expanded','true'); display.setAttribute('aria-expanded','true');
      (panel.querySelector('.is-selected') || panel.querySelector('.thai-month-grid button')).focus();
    };
    toggle.addEventListener('click',open); display.addEventListener('click',open);
    display.addEventListener('beforeinput',event=>event.preventDefault());
    display.addEventListener('paste',event=>event.preventDefault());
    display.addEventListener('keydown',event=>{if(['Enter',' ','ArrowDown'].includes(event.key)){event.preventDefault();open();}});
    display.addEventListener('invalid',()=>display.setCustomValidity('กรุณาเลือกเดือนค่าใช้จ่าย'));
    root.addEventListener('keydown',event=>{if(event.key==='Escape'){close();display.focus();}});
    document.addEventListener('pointerdown',event=>{if(!root.contains(event.target))close();});
    root.closest('form')?.addEventListener('reset',()=>setTimeout(()=>{value.value=value.defaultValue;display.value=display.defaultValue;display.setCustomValidity('');close();}));
  });
})();
