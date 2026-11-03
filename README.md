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
