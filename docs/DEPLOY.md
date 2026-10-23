# راه‌اندازی روی سرور (24/7)

> **هیچ فایل `.env` لازم نیست.** هر چیزی که ربات لازم دارد (توکن تلگرام، کلید رایگان
> هوش مصنوعی، حساب سایت، پروفایل و لحن) با `bidpilot setup` یا از داخل تلگرام پرسیده
> می‌شود و در `data/settings.json` (فایل `0600`، پوشه `0700`، در `.gitignore`) می‌ماند.
> تنظیمات بعدی هم از تلگرام قابل تغییر است و بدون ری‌استارت، در دور بعدی رصد اعمال می‌شود.

## گزینه ۱ — Docker Compose (پیشنهادی)

```bash
git clone https://github.com/<username>/bidpilot.git && cd bidpilot

# یک بار، تعاملی: توکن/کلید/حساب/لحن را می‌پرسد و در volume «./data» ذخیره می‌کند
docker compose run --rm bidpilot setup

docker compose up -d --build
docker compose logs -f bidpilot
```

`docker-compose.yml` دو سرویس دارد: `bidpilot` (رصد/ارسال) و `db` (Postgres). برای شروع
کوچک می‌توانید SQLite را نگه دارید و سرویس `db` را حذف کنید. اگر می‌خواهید ربات کنترل
تلگرام هم در همین کانتینر بالا باشد:

```yaml
# docker-compose.yml  →  سرویس bidpilot
command: ["bot", "--run"]
```

## گزینه ۲ — systemd روی VPS

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo useradd -m -s /bin/bash bidpilot
sudo -iu bidpilot
git clone https://github.com/<username>/bidpilot.git ~/bidpilot && cd ~/bidpilot
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

bidpilot setup          # همان‌جا پاسخ بده؛ در ~/bidpilot/data/settings.json ذخیره می‌شود
exit

sudo cp /home/bidpilot/bidpilot/deploy/bidpilot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bidpilot
journalctl -u bidpilot -f
```

برای مدیریت از تلگرام، در `deploy/bidpilot.service` خط `ExecStart` را به
`bidpilot bot --run` تغییر دهید (ربات + رصد در یک فرآیند).

## گزینه ۳ — Raspberry Pi / لپ‌تاپ همیشه‌روشن

همان systemd کافی است. SQLite روی SD card را روی پارتیشن با کیفیت بگذارید و از `data/`
بکاپ بگیرید. اگر کارت حافظه نگران‌کننده است، Postgres روی همان دستگاه یا یک VPS کوچک بگذارید.

## Postgres (برای چند worker)

فقط زیرساخت است و از محیط هم قابل تنظیم است:

```env
DATABASE_URL="postgresql+asyncpg://bidpilot:secret@localhost:5432/bidpilot"
```

```bash
pip install "bidpilot[postgres]"
```

unique index روی `(source, external_id)` تضمین می‌کند حتی با دو worker، پروژه دوبار بررسی نشود.

## پس از راه‌اندازی

```bash
bidpilot setup --status  # چه چیزی تنظیم شده؟
bidpilot doctor          # همه‌ی چراغ‌ها سبز؟
bidpilot preview --limit 2   # متن نمونه را بخوانید
bidpilot once --dry-run      # یک دور آزمایشی
# رفتن به حالت واقعی: bidpilot go-live  یا در تلگرام /mode → «واقعی»
```

## بکاپ

```bash
# SQLite
sqlite3 data/bidpilot.sqlite3 ".backup 'backup-$(date +%F).sqlite3'"

# Postgres
pg_dump -U bidpilot bidpilot | gzip > backup-$(date +%F).sql.gz

# تنظیمات و اعتبارنامه‌ها (فایل حساس است؛ جای امن نگه دارید)
cp data/settings.json data/settings-$(date +%F).json
```

## پایش

- لاگ‌ها: `journalctl -u bidpilot -f` یا `docker compose logs -f`
- آمار: `bidpilot stats`
- هر خطای ارسال به تلگرام هم می‌رود، پس یک هشدار زودهنگام دارید.
- سلامت منبع: `bidpilot doctor` را با cron روزانه اجرا کنید (مثلاً `0 9 * * * cd ~/bidpilot && .venv/bin/bidpilot doctor`).
- تنظیمات تغییر کرد؟ رصد‌کننده در دور بعدی خودش فایل تنظیمات را دوباره می‌خواند؛ نیازی به ری‌استارت نیست.

## به‌روزرسانی

```bash
cd ~/bidpilot && git pull && source .venv/bin/activate && pip install -e .
sudo systemctl restart bidpilot
```

## امنیت

- `data/settings.json` معادل `.env` است؛ `chmod 600` نگهش دارید و هرگز کامیت نکنید
  (پوشه‌ی `data/` در `.gitignore` است).
- اگر از `.env` استفاده می‌کنید (اختیاری — Docker/CI)، آن را هم `chmod 600` کنید.
- دسترسی SSH با کلید، فایروال بسته به‌جز SSH.
- کوکی پارس‌کدرز = نشست فعال حساب؛ در صورت لورفتن، از حساب خارج شوید تا باطل شود.
- فقط chat_id خودتان در تنظیمات باشد؛ اولین چتی که ربات را استارت می‌کند «مالک» می‌شود
  و بقیه پیام «مجاز نیست» می‌گیرند.
