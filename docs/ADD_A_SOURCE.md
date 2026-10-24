# افزودن یک مارکت‌پلیس جدید

BidPilot از چند سایت فریلنسری هم‌زمان پشتیبانی می‌کند و فقط **یکی** در هر لحظه فعال
است (از تلگرام با `/site` عوض می‌شود). هر سایت حساب کاربری مستقل خودش را دارد:
`credentials.<site>.cookie` / `username` / `password` / `method`.

افزودن سایت معمولاً **فقط یک فایل YAML** است؛ کد لازم نیست.

---

## راه ۱ (پیشنهادی): فقط YAML

ساختار:

```
src/bidpilot/sources/mysite/
├── __init__.py
├── adapter.py       # ۱۲ خط
└── config.yaml      # سلکتورها، مسیر لیست، فرم پیشنهاد، نحوه‌ی ورود
```

`adapter.py`:

```python
from pathlib import Path

from ..html_market import HtmlMarketSource, load_yaml_settings
from ..registry import register_source

_CONFIG_PATH = Path(__file__).with_name("config.yaml")


def load_default_settings() -> dict:
    return load_yaml_settings(_CONFIG_PATH)


@register_source("mysite")
class MySiteSource(HtmlMarketSource):
    name = "mysite"
    config_file = _CONFIG_PATH
    default_base_url = "https://mysite.example"
```

`__init__.py`:

```python
from .adapter import MySiteSource

__all__ = ["MySiteSource"]
```

و ثبت در `src/bidpilot/sources/registry.py`:

```python
from . import karlancer, mock, mysite, parscoders, ponisha  # noqa: F401
```

### config.yaml

```yaml
base_url: "https://mysite.example"

list:
  path: "/jobs"
  page_param: "page"
  max_pages: 2                    # ۱ = فقط صفحه اول
  min_items_per_page: 1
  card_selectors:                 # به ترتیب امتحان می‌شوند
    - "article.job-card"
    - "li.job-item"
  fields:                         # برای هر فیلد چند سلکتور (اولی که پیدا شد)
    title: ["h2 a", "a.job-title"]
    link: ["h2 a", "a.job-title"]
    description: [".excerpt", "p.summary"]
    budget: [".budget", "[class*='price']"]
    skills: [".tags .tag", "li.skill"]
    employer: [".employer"]
    published_at: ["time", ".date"]
  id_regex: "/jobs/(\\d+)"        # شناسه‌ی یکتا از URL
  fetch_details: true             # باز کردن صفحه‌ی هر پروژه برای متن کامل
  detail_concurrency: 3

detail:
  path: "/jobs/{external_id}"     # یا "{url}" اگر آدرس کامل لازم است
  detail_fields:
    description: ["#job-description", ".job-body"]
    budget: [".budget"]

bid:
  form_selectors: ["form#bid-form", "form[action*='bid']"]
  fields:
    amount: ["input[name*='price']", "input[name*='amount']"]
    description: ["textarea[name*='message']", "textarea"]
    days: ["input[name*='day']"]
  include_hidden: true
  success_text_contains: ["با موفقیت", "ثبت شد"]
  failure_text_contains: ["قبلاً پیشنهاد", "وارد شوید", "خطا"]

auth:
  mode: "cookie"                  # cookie | credentials | none
  cookie_name: "PHPSESSID"
  login_path: "/login"
  submit_path: "/login"
  username_field: "username"
  password_field: "password"
  parse_hidden: true              # توکن CSRF فرم لاگین را خودکار پر می‌کند
```

> اگر سایت هیچ سلکتوری نداشته باشد، BidPilot یک **fallback محتوایی** اجرا می‌کند:
> لینک‌هایی که شبیه صفحه‌ی پروژه‌اند (id عددی در URL) پیدا و کارت‌بندی می‌شوند؛
> اما متن/بودجه‌ی دقیق فقط با سلکتور درست به دست می‌آید. `bidpilot inspect`
> می‌گوید کدام حالت فعال شده است.

### پیدا کردن سلکتورها بدون حدس زدن

```bash
bidpilot inspect -s mysite                    # صفحه‌ی لیست سایت فعال/انتخابی
bidpilot inspect -s mysite -u https://mysite.example/jobs
bidpilot inspect -s mysite -f ~/Downloads/jobs.html   # فایل ذخیره‌شده از مرورگر
```

خروجی می‌گوید هر سلکتور چند مورد پیدا می‌کند، چند لینک پروژه‌مانند وجود دارد و
تشخیص خودکار چند پروژه پیدا کرده است. اولین سلکتوری که تعداد معقولی (چند کارت)
پیدا می‌کند را در `config.yaml` بگذارید.

### تنظیمات و ورود

- `config/config.yaml` لازم نیست تغییر کند؛ اگر خواستید، یک بلوک
  `- type: "mysite"` اضافه کنید (پیش‌فرض `enabled: false`).
- کوکی/رمز **در YAML نوشته نمی‌شود**. از تلگرام `/market` یا ترمینال:

```bash
bidpilot settings use mysite
bidpilot settings set credentials.mysite.cookie -v (Get-Clipboard -Raw).Trim()   # ویندوز
bidpilot settings set credentials.mysite.method -v cookie
```

سپس:

```bash
bidpilot selftest -s mysite          # حساب + لیست + فرم پیشنهاد (بدون ارسال)
bidpilot preview -s mysite --limit 2
```

---

## راه ۲ (پیشرفته): آداپتور سفارشی

اگر سایت JSON API دارد یا رفتارش با فرم HTML جور نیست، مستقیم از `Source` ارث ببرید
(نمونه‌ی کامل و تست با `respx`: `src/bidpilot/sources/mock/`). قرارداد:

- `fetch_projects()` → `list[Project]` با `external_id` پایدار از URL.
- `submit_bid(project, proposal, amount, delivery_days)` → همیشه `SubmissionResult`
  برمی‌گرداند (هرگز exception انتظاری بالا نمی‌دهد).
- `healthcheck()` → `(ok, پیام)` و وقتی سلکتور/سایت شکست، `False`.
- `login()` اگر سایت ورود با رمز دارد.
- ورود/نشست از `bidpilot.credentials.site_credentials(self.name)` خوانده شود تا
  حساب هر سایت جدا بماند.

ثبت در `registry._load_builtin` هم مثل راه ۱.

---

## تست

```python
import httpx, respx
from bidpilot.sources.mysite.adapter import MySiteSource

@respx.mock
async def test_fetch():
    respx.get("https://mysite.example/jobs").mock(
        return_value=httpx.Response(200, text=(FIXTURES / "mysite_list.html").read_text())
    )
    source = MySiteSource(settings={}, config=make_config(), secrets=Secrets(_env_file=None))
    projects = await source.fetch_projects()
    assert projects[0].external_id
```

نمونه‌های موجود: `tests/test_credentials.py`، `tests/test_multi_site.py`،
`tests/test_parscoders_parser.py` و `tests/fixtures/`.

## چک‌لیست کیفیت آداپتور

- [ ] `external_id` پایدار است (از URL، نه از عنوان).
- [ ] پروژه‌های تکراری دوباره ثبت نمی‌شوند.
- [ ] `submit_bid` همیشه `SubmissionResult` برمی‌گرداند (شامل «قبلاً پیشنهاد داده‌اید»).
- [ ] نشست منقضی/بلاک تشخیص داده می‌شود و پیامش به `/market` راهنمایی می‌کند.
- [ ] `healthcheck` وقتی سلکتورها می‌شکنند `False` برمی‌گرداند.
- [ ] `credentials.<site>.*` تنها منبع اعتبارنامه است (نه YAML، نه `.env` اجباری).
- [ ] برای هر رفتار سایت یک تست با `respx` وجود دارد.

## نکته‌ی حقوقی

برای بعضی سایت‌ها ارسال خودکار خلاف قوانین است یا API رسمی وجود دارد. اگر API رسمی
هست، همان را ترجیح دهید (سریع‌تر، پایدارتر و بی‌ریسک‌تر).
