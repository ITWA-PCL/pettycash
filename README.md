# Petty Cash

Repository: https://github.com/ITWA-PCL/pettycash

งาน Process Flow พัฒนาบน `process-flow` และส่ง PR เข้า `main` ให้เจ้าของโครงการกับโก้ตรวจตาม [ขอบเขตการทำงาน](PETTYCASH-DEV-HANDOFF.md) งาน Security แยกบน `security` การ merge และ deploy ต้องได้รับอนุมัติเป็นรายครั้ง

Current UAT checklist: [UAT_CHECKLIST.md](UAT_CHECKLIST.md)

ระบบเบิกเงินสดย่อยภายในองค์กร พัฒนาด้วย Django + Django Templates/HTMX + PostgreSQL

## เริ่มใช้งานบนเครื่อง

1. สร้าง virtual environment: `python -m venv .venv`
2. เปิดใช้งาน: `.\.venv\Scripts\Activate.ps1`
3. ติดตั้ง package: `python -m pip install -r requirements.txt`
4. สร้างฐานข้อมูล local: `python manage.py migrate`
5. สร้างผู้ดูแล: `python manage.py createsuperuser`
6. รัน: `python manage.py runserver`

เปิด `http://127.0.0.1:8000/` แล้ว Login ด้วยผู้ดูแลที่สร้างไว้

## Docker

1. คัดลอก `.env.example` เป็น `.env` และแทนค่าทุก placeholder
2. เปิด Docker Desktop
3. รัน `docker compose up --build`

ห้ามใส่ AD Secret, Telegram Bot Token หรือรหัสผ่านจริงใน repository, `.env.example` หรือเอกสารที่ Commit

## นำเข้า BU / Budget และตั้งค่าการเชื่อมต่อ

- หน้า **Budget และ BU → นำเข้า BU / นำเข้า Budget** แยกส่วนกัน BU ใช้ชีต `Business unit` (Distribution Rule, Distribution Rule Name) ส่วน Budget ใช้ชีต `Budget` (Expenses/Asset, Description, Department) มีแม่แบบไฟล์ชีตเดียวให้แต่ละส่วน หรือใช้ไฟล์เดิมที่มีสองชีตได้โดยอ่านเฉพาะส่วนที่เลือก ตรวจตัวอย่างและค้นหา/แบ่งหน้าก่อนยืนยัน เลือกเพิ่ม/อัปเดตหรือแทนที่เฉพาะ master ส่วนนั้น อีกส่วนและข้อมูลคำขอเดิมคงอยู่
- ผู้ดูแลระบบ บัญชี ผจก.บัญชี และ CFO ใช้การนำเข้าได้ตามสิทธิ์จัดการ master เดิม ผู้ใช้ทั่วไปนำเข้าไม่ได้
- หน้า **ตั้งค่าระบบ → การเชื่อมต่อ AD และ Telegram** บันทึก URL เว็บจริง, AD URL/App ID/Secret/Origin IP, Bot Token, Group Chat ID และ Webhook Secret ได้ ใช้กับการทำงานใหม่ทันที ช่องรหัสเว้นว่างเพื่อเก็บค่าเดิม ไม่แสดงค่าที่บันทึกกลับบนหน้าเว็บ
- หน้าปกติแสดงค่าที่ใช้ประจำ: URL เว็บ, AD (Gateway URL/App ID/Secret) และ Telegram (Bot Token/Webhook Secret) ส่วน AD Origin IP และ Group Chat ID เป็นตัวเลือกเพิ่มเติม ค่าฐานข้อมูล/Redis/Secret Key/Traefik อยู่ในส่วนขั้นสูงที่พับไว้ ไม่ต้องกรอกซ้ำเพื่อใช้งานทั่วไป สถานะ “ตั้งค่าครบแล้ว” ตรวจเฉพาะค่าที่มีอยู่ ไม่ใช่ผลทดสอบล็อกอินหรือส่งข้อความจริง
- หลังบันทึก กด **เชื่อมต่อ Webhook กับเว็บนี้** เพื่อย้าย webhook ของ bot มา `https://pettycash.windowasia.com/telegram/webhook/` การบันทึกค่าอย่างเดียวไม่เรียก Telegram หรือย้าย webhook ข้อความใหม่และปุ่มเปิดเว็บจะใช้ URL ที่ตั้งไว้ ข้อความเก่าที่ส่งไปแล้วไม่เปลี่ยน URL เอง
- หน้า **ค่าตั้งต้นเซิร์ฟเวอร์** แก้ PostgreSQL, Redis, Django Secret Key, Allowed Hosts/CSRF และค่าของ Traefik เป็นชุดค่ารอใช้งานได้ บันทึกแล้วดาวน์โหลด `production.env` ให้ผู้ดูแล VPS นำไปใช้ตามกระบวนการ deploy ที่อนุมัติ หน้านี้ไม่แก้ environment ของ process ที่กำลังรัน ไม่ restart container หรือย้ายฐานข้อมูลให้เอง
- ไฟล์ที่ดาวน์โหลดมีค่าลับ เก็บไว้นอก Git และส่งให้ผู้ดูแล VPS โดยตรง การสร้างระบบครั้งแรกยังต้องมีค่าตั้งต้นให้เว็บเชื่อมฐานข้อมูลได้ก่อน ค่าจากเว็บอยู่ในฐานข้อมูล ไม่ได้ฝังใน image หรือส่งขึ้น Git

ค่าลับในฐานข้อมูลเข้ารหัสด้วยกุญแจที่ผูกกับ `DJANGO_SECRET_KEY` ของเซิร์ฟเวอร์ เก็บคีย์นี้ให้คงเดิมระหว่าง rebuild และ backup ทั้งฐานข้อมูลกับคีย์ หากเปลี่ยนคีย์ผ่านชุดค่ารอใช้งาน ไฟล์ export จะเพิ่ม `DJANGO_SECRET_KEY_FALLBACKS` เพื่อให้อ่านค่าที่เข้ารหัสด้วยคีย์เดิมได้ ต้องรักษา fallback จนผู้ดูแลจัดการหมุนกุญแจครบแล้ว

การอัปเดตชุดนี้ต้องติดตั้ง dependency ใน `requirements.txt` และใช้ migration `core.0026`/`core.0027` กับฐานเป้าหมายผ่านขั้นตอน deploy ไม่ต้องคัดลอก `.env` จากเครื่องพัฒนาไปทับค่าฐานข้อมูล VPS
