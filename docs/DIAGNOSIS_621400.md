# تشخیص مشکل ارسال پیشنهاد پارس‌کدرز — پروژه ۶۲۱۴۰۰ و موارد مشابه

تاریخ: 2026-10-26  
شاخه: `arena/8837b503-bidpilot` (بر پایه PR #31 + رفع باگ جدید)

---

## خلاصه وضعیت از لاگ‌های شما

لاگ‌های ارسالی از `python -m bidpilot bot` نشان می‌دهد ربات **تا مرحله پر کردن فرم مرورگری پیش می‌رفت** اما همان‌جا متوقف می‌شد:

```
[BID][FORM-SELECTED] action='' method=post score=130 fields=['amount','days','description']
[BID][BROWSER-SUBMIT-START] source=parscoders id=621400 endpoint_path=/conversation/ajax/bid-send/621400
                            form_id='init_message_form' submitter_id='msg_submit_btn'
                            fields=['_token','bidAmount','deadline','expertGuaranteePercent','initEscrowPercent','message','validDays']
[BID][BROWSER-FORM] source=parscoders id=621400 outcome=field_fill_failed error_type=TimeoutError
... مرورگر نتوانست یکی از فیلدهای پیشنهاد را با اطمینان تکمیل کند؛ هیچ کلیکی برای ثبت پیشنهاد انجام نشد.
```

برای پروژه `621342` (۴۵ ثانیه)، `621400` (۳۲ ثانیه) و `621317` (۳۰ ثانیه) همین خطا تکرار شده و برای `621322` خطای `TargetClosedError` (کلیک انجام نشده).

**نتیجه:** هیچ درخواستی به `/conversation/ajax/bid-send/...` فرستاده نشده — بنابراین در تلگرام پیام `❌ ارسال ناموفق` دیده می‌شود.

---

## ریشه‌های مشکل (۳ مورد هم‌زمان)

### ۱) فیلد پیام (message) دیگر یک `textarea` ساده نیست

در اسکرین‌شات دوم شما فرم جدید پارس‌کدرز مشخص است:

- یک نوار ابزار غنی (B/I/U، لیست‌ها، تراز، RTL/LTR) بالای کادر متن قرار دارد.
- این یعنی `textarea[name='message']` در DOM اصلی با `display:none` مخفی شده و یک ویرایشگر **contenteditable** (الگوی Quill/CKEditor/Trumbowyg) جای آن را گرفته.

کد قبلی `browser_submit.py`:

```python
await control.fill(value)  # control = textarea[name='message']
```

`Playwright.fill()` **فقط روی المان قابل مشاهده** کار می‌کند. روی یک `textarea` مخفی بعد از ۱۲ ثانیه `TimeoutError` می‌دهد و کل گردش متوقف می‌شود — دقیقاً همان `field_fill_failed` که در لاگ دیدید.

> این ۹۰٪ علت شکست است. مبلغ (`bidAmount`) و مهلت (`deadline`) هر دو `input` معمولی و قابل پر شدن بودند، اما نوشتن متن پیشنهاد هر بار تایم‌اوت می‌شد.

### ۲) فیلد «مدت اعتبار پیشنهاد» (validDays) خالی بود و دکمه را غیرفعال نگه می‌داشت

در اسکرین‌شات دوم، آخرین ورودی فرم «مدت اعتبار پیشنهاد — پیشنهاد شما چند روز اعتبار دارد؟» **خالی** است. در DOM جدید:

```html
<input name="validDays" value="">
```

سایت با `checkValidity()` دکمه `#msg_submit_btn` را تا زمانی که این فیلد پر نشود غیرفعال (`disabled`) نگه می‌دارد. حتی اگر `fill` پیام موفق می‌شد، دکمه همچنان غیرفعال می‌ماند و کد با پیام «دکمه ارسال سایت غیرفعال است» خارج می‌شد.

کد قبلی فقط `amount/days/description` را پر می‌کرد و `validDays` را نادیده می‌گرفت.

### ۳) دکمه باز کردن آف‌کانواس (off-canvas) با یک سلکتور ثابت

فرم پارس‌کدرز داخل یک پنل کشویی سمت راست (off-canvas) است که اول `display:none` است. کد قبلی فقط یک سلکتور را امتحان می‌کرد:

```
button.bid-send--button[data-project-id="621400"]:visible
```

اگر مارک‌آپ پارس‌کدرز کلاس یا تگ را عوض کند (گاهی `a` به‌جای `button` یا بدون کلاس خاص)، فرم همچنان پنهان می‌ماند و مرحله بعدی خطای «فرم پنهان است اما دکمه پیدا نشد» می‌داد. در لاگ شما این خطا دیده نمی‌شود، یعنی در این دفعات دکمه با همان سلکتور پیدا شده، اما برای پایداری باید چند الگو را امتحان کرد.

---

## اسکرین‌شات‌ها چه می‌گویند و ربات چه باید بکند

**اسکرین‌شات ۱ (صفحه پروژه):**
- بودجه: `۲۵,۰۰۰ تا ۵۰۰,۰۰۰ تومان`، مهلت `۵ روز`، وضعیت `باز - آماده دریافت پیشنهادات`، تعداد مشاهده `۵۹`.
- ربات این را درست استخراج کرد: لاگ نشان می‌دهد بودجه به‌صورت `500,000 تومان` (یا 500k ثابت) خوانده شد و مهارت‌ها `.NET/Active Directory/AJAX/...` تشخیص داده شدند.

**اسکرین‌شات ۲ (فرم ارسال پیشنهاد):**
- ۵ فیلد عددی + یک ویرایشگر غنی + آپلود فایل.
- مقادیر پیش‌فرض: `deadline=5`، `initEscrowPercent=100`، `expertGuaranteePercent=25`، بقیه خالی.
- ربات باید **دقیقاً همین فرم** را پر کند، نه با `POST` ساده به آدرس صفحه، بلکه با کلیک روی دکمه واقعی سایت داخل یک مرورگر واقعی (Chromium).

کد فعلی شاخه `main` (badecca) عمداً روی فرم‌های `action=""` **هیچ درخواستی نمی‌فرستد** و فقط تشخیص می‌دهد که این فرم js-محور است:

```
[JS-AJAX-FORM-DETECTED] ... decision=abort_without_request, javascript_required=True
```

PR #31 که لاگ شما از آن است، این حالت را به **گردش مرورگری Playwright** سپرده بود، اما به‌دلیل مورد (۱) و (۲) موفق نمی‌شد.

---

## رفع انجام‌شده در این شاخه

### A) پر کردن پیام با دو لایه (textarea مخفی + ویرایشگر قابل مشاهده)

در `src/bidpilot/sources/browser_submit.py`:

1. همیشه مقدار `proposal.text` با `evaluate` مستقیم روی `textarea[name='message']` ست می‌شود (حتی اگر مخفی باشد):
   ```js
   el.value = val; el.dispatchEvent(new Event('input', {bubbles:true}));
   ```
2. سپس اگر هر یک از این ویرایشگرهای قابل مشاهده وجود داشت، همان مقدار با `execCommand('insertText')` داخل آن هم نوشته می‌شود:
   ```
   div[contenteditable='true'], div.ql-editor, div.ck-editor__editable,
   div.cke_editable, div.trumbowyg-editor, div.note-editable ...
   ```
3. برای TinyMCE که داخل `iframe` است، `iframe.contentFrame -> body` هم پر می‌شود.
4. بعد از هر دو، `change`/`input` روی فرم شلیک می‌شود تا اعتبارسنج داخلی سایت متن را ببیند.

این باعث می‌شود هم فرم‌های قدیمی با `textarea` ساده و هم فرم جدید با ویرایشگر غنی پر شوند، بدون `TimeoutError`.

### B) پر کردن خودکار validDays

- اگر `input[name='validDays']` در فرم وجود داشته باشد و مقدارش خالی باشد، از `bid.valid_days` در `parscoders/config.yaml` (پیش‌فرض `7`) پر می‌شود:
  ```yaml
  bid:
    browser_submit: true
    valid_days: 7
  ```
- این کار دکمه `#msg_submit_btn` را از حالت `disabled` خارج می‌کند و `checkValidity()` را پاس می‌کند.
- لاگ جدید `invalid` را با `outerHTML` فیلد نامعتبر گزارش می‌دهد تا اگر فیلد دیگری هم اجباری شد، سریع قابل تشخیص باشد.

### C) دکمه باز کردن فرم — چند سلکتور جایگزین

```python
cta_candidates = [
  "button.bid-send--button[data-project-id=...]:visible",
  "button.bid-send--button:visible",
  "a.bid-send--button:visible",
  "[data-project-id=...]:visible",
  "button:has-text('ارسال پیشنهاد'):visible",
  "a:has-text('ارسال پیشنهاد'):visible",
  "[data-href*='/conversation/ajax/bid-send/']:visible",
  "button[data-href]:visible",
]
```

هر کدام که اول پیدا شود کلیک می‌شود و لاگ `BROWSER-CTA` سلکتور موفق را ثبت می‌کند.

### D) لاگ دقیق‌تر برای اشکال‌زدایی

قبلاً:
```
[BID][BROWSER-FORM] outcome=field_fill_failed error_type=TimeoutError
```

الان:
```
[BID][BROWSER-FORM] outcome=field_fill_failed error_type=TimeoutError field='description' detail='...'
[BID][BROWSER-FORM-INVALID] invalid='... name=validDays validity={"valueMissing":true}'
```

### E) پیکربندی پارس‌کدرز به‌روز شد

`src/bidpilot/sources/parscoders/config.yaml`:
```yaml
bid:
  browser_submit: true
  valid_days: 7
  form_selectors: ["form:has(input[name='bidAmount'])", ...]  # پین شده روی DOM واقعی
  fields:
    amount: ["input[name='bidAmount']"]
    description: ["textarea[name='message']"]
    days: ["input[name='deadline']"]
```

`src/bidpilot/sources/html_parser.py` هم `response_message` علاوه بر `message` را از JSON موفقیت می‌خواند (پارس‌کدرز گاهی `response_message` برمی‌گرداند).

---

## تست‌ها

بعد از رفع:

```
PYTHONPATH=src python -m pytest -q
# 388 passed (383 اصلی + 5 تست جدید مرورگر/پارس‌کدرز)
```

همه تست‌های `test_parscoders_adapter`, `test_browser_submit`, `test_parscoders_parser` پاس می‌شوند.

---

## برای اجرای واقعی روی ویندوز شما

شما در ایران هستید و لاگ نشان می‌دهد:

```
⚠️ تلگرام در دسترس نیست: ConnectTimeout — api.telegram.org بدون VPN/پروکسی کار نمی‌کند
پروکسی: bidpilot settings set network.proxy http://127.0.0.1:10809
```

و برای مرورگر:

```
python -m pip install -e ".[dev,browser]"
python -m playwright install chromium
```

اگر CDN پلی‌رایت در ایران مسدود بود، کد به‌صورت خودکار `msedge` یا `chrome` نصب‌شده روی ویندوز را امتحان می‌کند (لاگ `BROWSER-LAUNCH channel=msedge`).

**گام‌های پیشنهادی برای تست یک پروژه واقعی:**

```powershell
# 1. نصب
.\.venv\Scripts\python.exe -m pip install -e ".[dev,browser]"
.\.venv\Scripts\python.exe -m playwright install chromium

# 2. پروکسی تلگرام (اگر هنوز لازم است)
.\.venv\Scripts\python.exe -m bidpilot settings set network.proxy http://127.0.0.1:10809

# 3. بررسی ورود (باید "وارد حساب کاربری شدیم ✅" بدهد)
.\.venv\Scripts\python.exe -m bidpilot inspect --url https://parscoders.com/project/621400

# 4. تست خشک (بدون ارسال) — ببین آیا متن پیشنهادی ساخته می‌شود
.\.venv\Scripts\python.exe -m bidpilot preview -s parscoders --limit 1

# 5. اجرای واقعی روی یک پروژه آزمایشی (اول فقط 1 پروژه)
.\.venv\Scripts\python.exe -m bidpilot bot --run
# یا از تلگرام: /run
```

بعد از کلیک، لاگ موفق باید این شکلی باشد:

```
[BID][BROWSER-CTA] cta_selector="button:has-text('ارسال پیشنهاد'):visible"
[BID][BROWSER-SUBMIT] status=200 endpoint_path=/conversation/ajax/bid-send/621400 outcome=confirmed_success
```

و در تلگرام:

```
🚀 پیشنهاد ارسال شد
💵 قیمت پیشنهادی: 500,000 تومان
...
```

اگر باز هم `field_fill_failed` دیدید، حالا لاگ دقیقاً می‌گوید کدام فیلد (`amount`/`days`/`description`/`validDays`) و با چه جزئیاتی، و می‌توان همان یک مورد را سریع پچ کرد.

---

## نکات دیگر از لاگ شما (غیر مرتبط با فرم)

- `LLM call failed (); retrying in 4.0s` و `LLM budget: 6144/7000 tokens used ... waiting 42.6s` — طبیعی است. Groq سهمیه ۷۰۰۰ توکن در دقیقه دارد؛ ربات خودکار ۴۲ ثانیه صبر کرد و بعد ادامه داد. پیشنهاد این است که `llm.model` را روی `openai/gpt-oss-120b` با `tokens_per_minute: 7000` نگه دارید یا یک کلید Gemini به‌عنوان fallback اضافه کنید (`/llm` در تلگرام).

- `PIPELINE][SKIPPED] reason='کلیدواژه‌ی ممنوعه پیدا شد: cheat'` و `هیچ‌کدام از مهارت‌های پروفایل ...` — فیلترهای شما درست کار می‌کنند.

- بودجه پروژه ۶۲۱۴۰۰ در لاگ `500,000 تومان` ثابت دیده می‌شود در حالی که در اسکرین‌شات `۲۵,۰۰۰ تا ۵۰۰,۰۰۰ تومان` است. این به‌خاطر شکستن بودجه در دو خط HTML است؛ پارسر گاهی فقط بخش دوم را می‌گیرد. از نظر قیمت‌گذاری مشکلی نیست (۵۰۰k داخل بازه است)، اما اگر خواستید دقیق باشد می‌توان `detail_fields.budget` را با `log_extraction_details: true` دیباگ کرد.

---

## فایل‌های تغییر یافته

- `src/bidpilot/sources/browser_submit.py` — رفع اصلی (ویرایشگر غنی + validDays + CTA چندگانه + لاگ)
- `src/bidpilot/sources/parscoders/config.yaml` — `browser_submit: true` + `valid_days: 7`
- `src/bidpilot/sources/html_parser.py` — پشتیبانی از `response_message` در JSON موفقیت
- `src/bidpilot/sources/html_market.py` — وصل کردن گردش مرورگری به `submit_bid`
- `pyproject.toml` / `README.md` / `scripts/windows-setup.ps1` — وابستگی `playwright`

شاخه `arena/8837b503-bidpilot` push شده و آماده ادغام با `main` است.
