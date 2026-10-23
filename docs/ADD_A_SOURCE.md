# افزودن یک مارکت‌پلیس جدید

هدف: در ۳۰ دقیقه یک سایت جدید را به BidPilot وصل کنید.

## ۱) ساخت آداپتور

```bash
mkdir -p src/bidpilot/sources/mysite
touch src/bidpilot/sources/mysite/__init__.py
```

```python
# src/bidpilot/sources/mysite/adapter.py
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ...models import Project, Proposal, SubmissionResult, SubmissionStatus
from ...pricing.budget import parse_budget
from ...text import clean_text
from ..base import Source
from ..registry import register_source


@register_source("mysite")
class MySiteSource(Source):
    async def fetch_projects(self) -> list[Project]:
        html = await self.get_html(self.settings.get("list_path", "/jobs"))
        soup = BeautifulSoup(html, "lxml")
        projects: list[Project] = []

        for card in soup.select(self.settings.get("card_selector", "article.job")):
            link = card.select_one("a")
            if link is None:
                continue
            href = urljoin(self.base_url + "/", link["href"])
            external_id = re.search(r"/jobs/(\d+)", href).group(1)

            projects.append(
                Project(
                    source=self.name,
                    external_id=external_id,
                    title=clean_text(link.get_text(" ", strip=True)),
                    url=href,
                    description=clean_text(card.select_one(".desc").get_text(" ", strip=True))
                    if card.select_one(".desc")
                    else "",
                    budget=parse_budget(card.select_one(".budget").get_text() if card.select_one(".budget") else ""),
                    skills=[clean_text(s.get_text()) for s in card.select(".tag")],
                )
            )
        return projects

    async def submit_bid(self, project, proposal: Proposal, amount: int, delivery_days: int | None):
        client = await self.client()
        response = await client.post(
            f"/jobs/{project.external_id}/bids",
            data={
                "price": amount,
                "message": proposal.text,
                "delivery_days": delivery_days or "",
            },
            headers={"Referer": project.url},
        )
        if response.status_code >= 400:
            return self.fail(f"HTTP {response.status_code}", response.status_code)
        if "موفق" not in clean_text(response.text):
            return self.fail("پاسخ موفقیت در صفحه پیدا نشد.")
        return SubmissionResult(status=SubmissionStatus.SUBMITTED, message="ثبت شد", http_status=response.status_code)

    async def healthcheck(self) -> tuple[bool, str]:
        projects = await self.fetch_projects()
        return (bool(projects), f"{len(projects)} پروژه پیدا شد")
```

```python
# src/bidpilot/sources/mysite/__init__.py
from .adapter import MySiteSource

__all__ = ["MySiteSource"]
```

برای اینکه ماژول در زمان import ثبت شود، در `src/bidpilot/sources/registry.py` به `_load_builtin` اضافه کنید:

```python
from . import mock, mysite, parscoders  # noqa: F401
```

## ۲) تنظیمات

```yaml
# config/config.yaml
sources:
  - type: "mysite"
    enabled: true
    settings:
      base_url: "https://mysite.example"
      list_path: "/jobs"
      card_selector: "article.job"
      headers: { "X-Requested-With": "XMLHttpRequest" }
      cookies: { "session": "..." }   # یا از SettingsStore / محیط با متغیر خودتان
```

کلیدهای امنیتی (کوکی/توکن) را در `config.Secrets` فیلد اضافه کنید — مقدارش از
`data/settings.json` (کلیدهایی مثل `credentials.<source>.cookie`) یا `.env` می‌آید، نه YAML.
برای اینکه ویزارد/ربات هم آن را بپرسند، یک `Question` در `onboarding.py` اضافه کنید.

## ۳) بررسی بدون ارسال

```bash
bidpilot doctor                       # آیا صفحه باز می‌شود و پروژه‌ای پیدا می‌شود؟
bidpilot preview -s mysite --limit 2  # متن و قیمت‌ها را ببینید
bidpilot once -s mysite --dry-run     # یک دور کامل بدون ارسال
```

## ۴) تست

```python
# tests/test_mysite_adapter.py
import httpx, respx
from bidpilot.sources.mysite.adapter import MySiteSource

@respx.mock
async def test_fetch(monkeypatch):
    respx.get("https://mysite.example/jobs").mock(
        return_value=httpx.Response(200, text=(FIXTURES / "mysite_list.html").read_text())
    )
    source = MySiteSource(settings={}, config=make_config(), secrets=Secrets(_env_file=None))
    projects = await source.fetch_projects()
    assert projects[0].external_id
```

## چک‌لیست کیفیت آداپتور

- [ ] `external_id` پایدار است (از URL، نه از عنوان).
- [ ] تاریخ انتشار پروژه‌های تکراری باعث ثبت دوباره نمی‌شود.
- [ ] `submit_bid` هرگز exception «انتظاری» بالا نمی‌دهد؛ همیشه `SubmissionResult` برمی‌گرداند.
- [ ] نشست منقضی/بلاک/قبلاً ارسال‌شده تشخیص داده می‌شود.
- [ ] `healthcheck` وقتی سلکتورها می‌شکنند `False` برمی‌گرداند.
- [ ] برای هر رفتار سایت یک تست با `respx` وجود دارد.

## نکته‌ی حقوقی

برای بعضی سایت‌ها ارسال خودکار خلاف قوانین است یا API رسمی وجود دارد. اگر API رسمی هست، همان را ترجیح دهید (سریع‌تر، پایدارتر و بی‌ریسک‌تر).
