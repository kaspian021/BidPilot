# راه‌اندازی روی سرور (24/7)

## گزینه ۱ — Docker Compose (پیشنهادی)

```bash
git clone https://github.com/<username>/bidpilot.git && cd bidpilot
cp .env.example .env && $EDITOR .env
$EDITOR config/profile.yaml
docker compose up -d --build
docker compose logs -f bidpilot
```

`docker-compose.yml` دو سرویس دارد: `bidpilot` (ربات) و `db` (Postgres). برای شروع کوچک می‌توانید SQLite را نگه دارید و سرویس `db` را حذف کنید.

## گزینه ۲ — systemd روی VPS

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo useradd -m -s /bin/bash bidpilot
sudo -iu bidpilot
git clone https://github.com/<username>/bidpilot.git ~/bidpilot && cd ~/bidpilot
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env && nano .env
exit

sudo cp /home/bidpilot/bidpilot/deploy/bidpilot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bidpilot
journalctl -u bidpilot -f
```

## گزینه ۳ — Raspberry Pi / لپ‌تاپ همیشه‌روشن

همان systemd کافی است. SQLite روی SD card را روی پارتیشن با کیفیت بگذارید و از `data/` بکاپ بگیرید. اگر کارت حافظه نگران‌کننده است، Postgres روی همان دستگاه یا یک VPS کوچک بگذارید.

## Postgres (برای چند worker)

```env
DATABASE_URL="postgresql+asyncpg://bidpilot:secret@localhost:5432/bidpilot"
```

```bash
pip install "bidpilot[postgres]"
```

unique index روی `(source, external_id)` تضمین می‌کند حتی با دو worker، پروژه دوبار بررسی نشود.

## پس از راه‌اندازی

```bash
bidpilot doctor          # همه‌ی چراغ‌ها سبز؟
bidpilot telegram-test   # پیام تست به تلگرام می‌رسد؟
bidpilot once --dry-run  # یک دور آزمایشی
# سپس در config: runtime.submit_mode: "live"  و ری‌استارت
```

## بکاپ

```bash
# SQLite
sqlite3 data/bidpilot.sqlite3 ".backup 'backup-$(date +%F).sqlite3'"

# Postgres
pg_dump -U bidpilot bidpilot | gzip > backup-$(date +%F).sql.gz
```

## پایش

- لاگ‌ها: `journalctl -u bidpilot -f` یا `docker compose logs -f`
- آمار: `bidpilot stats`
- هر خطای ارسال به تلگرام هم می‌رود، پس یک هشدار زودهنگام دارید.
- سلامت منبع: `bidpilot doctor` را با cron روزانه اجرا کنید (مثلاً `0 9 * * * cd ~/bidpilot && .venv/bin/bidpilot doctor`).

## به‌روزرسانی

```bash
cd ~/bidpilot && git pull && source .venv/bin/activate && pip install -e .
sudo systemctl restart bidpilot
```

## امنیت

- `.env` را `chmod 600` کنید و هرگز کامیت نکنید.
- دسترسی SSH با کلید، فایروال بسته به‌جز SSH.
- کوکی پارس‌کدرز = نشست فعال حساب؛ در صورت لورفتن، از حساب خارج شوید تا باطل شود.
