"""ZarinPal PG v4 client — works with real merchant or sandbox.

Docs: https://docs.zarinpal.com/paymentGateway/
Sandbox: https://sandbox.zarinpal.com/pg/v4/payment/request.json
Prod:    https://api.zarinpal.com/pg/v4/payment/request.json
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class ZarinpalConfig:
    merchant_id: str
    sandbox: bool = False
    callback_url: str = "https://bidpilot.local/payment/callback"
    description_prefix: str = "BidPilot - اشتراک"

    @property
    def base_url(self) -> str:
        return "https://sandbox.zarinpal.com" if self.sandbox else "https://api.zarinpal.com"

    @property
    def payment_url_prefix(self) -> str:
        return "https://sandbox.zarinpal.com/pg/StartPay/" if self.sandbox else "https://www.zarinpal.com/pg/StartPay/"


@dataclass
class PaymentRequest:
    amount_toman: int
    description: str
    callback_url: str
    mobile: str | None = None
    email: str | None = None
    order_id: str | None = None


@dataclass
class PaymentCreateResult:
    ok: bool
    authority: str | None = None
    payment_url: str | None = None
    code: int | None = None
    message: str = ""


@dataclass
class PaymentVerifyResult:
    ok: bool
    ref_id: str | None = None
    code: int | None = None
    message: str = ""
    card_pan: str | None = None


class ZarinpalClient:
    def __init__(self, config: ZarinpalConfig, *, timeout: float = 20.0) -> None:
        self.config = config
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Accept": "application/json"}

    async def create_payment(self, req: PaymentRequest) -> PaymentCreateResult:
        if not self.config.merchant_id or self.config.merchant_id in {"test", "demo", "YOUR_MERCHANT_ID"}:
            # Simulated payment for dev / when merchant not configured
            fake_auth = f"A00000000000000000000000000000{req.amount_toman%100:02d}"
            log.warning("ZarinPal merchant_id not set — returning simulated authority %s", fake_auth)
            return PaymentCreateResult(
                ok=True,
                authority=fake_auth,
                payment_url=f"{self.config.payment_url_prefix}{fake_auth}",
                code=100,
                message="(شبیه‌سازی) — مرچنت‌کد واقعی تنظیم نشده. پرداخت تستی ایجاد شد.",
            )
        url = f"{self.config.base_url}/pg/v4/payment/request.json"
        payload: dict[str, Any] = {
            "merchant_id": self.config.merchant_id,
            "amount": int(req.amount_toman * 10),  # ZarinPal expects Rial, we store Toman → ×10
            "description": req.description or f"{self.config.description_prefix}",
            "callback_url": req.callback_url or self.config.callback_url,
        }
        if req.mobile:
            payload["mobile"] = req.mobile
        if req.email:
            payload["email"] = req.email
        if req.order_id:
            payload["metadata"] = {"order_id": req.order_id}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
                data = resp.json()
        except Exception as exc:  # network
            log.error("ZarinPal request failed: %s", exc)
            return PaymentCreateResult(ok=False, message=f"خطا در اتصال به زرین‌پال: {exc}")

        # v4 shape: {data: {code, message, authority, fee_type, fee}, errors: ...}
        d = data.get("data") or {}
        code = d.get("code")
        if code == 100 and d.get("authority"):
            auth = str(d["authority"])
            return PaymentCreateResult(
                ok=True,
                authority=auth,
                payment_url=f"{self.config.payment_url_prefix}{auth}",
                code=code,
                message=d.get("message") or "OK",
            )
        # error branch
        err = data.get("errors") or d
        msg = str(err.get("message") or data.get("message") or "خطای نامشخص از زرین‌پال")
        return PaymentCreateResult(ok=False, code=code, message=msg)

    async def verify_payment(self, authority: str, amount_toman: int) -> PaymentVerifyResult:
        if not self.config.merchant_id or self.config.merchant_id in {"test", "demo", "YOUR_MERCHANT_ID"}:
            # Simulated verify — succeed if authority looks like fake one
            log.warning("ZarinPal verify simulated for %s", authority)
            if authority.startswith("A000000000"):
                return PaymentVerifyResult(ok=True, ref_id=f"SIM-{authority[-6:]}", code=100, message="تأیید شبیه‌سازی‌شده")
            return PaymentVerifyResult(ok=False, code=-1, message="Authority نامعتبر (حالت تست)")

        url = f"{self.config.base_url}/pg/v4/payment/verify.json"
        payload = {
            "merchant_id": self.config.merchant_id,
            "amount": int(amount_toman * 10),
            "authority": authority,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
                data = resp.json()
        except Exception as exc:
            return PaymentVerifyResult(ok=False, message=f"خطا در تأیید زرین‌پال: {exc}")

        d = data.get("data") or {}
        code = d.get("code")
        # ZarinPal success codes: 100 (verified) , 101 (already verified)
        if code in (100, 101) and d.get("ref_id"):
            return PaymentVerifyResult(ok=True, ref_id=str(d["ref_id"]), code=code, message=d.get("message") or "OK", card_pan=d.get("card_pan"))
        err = data.get("errors") or d
        msg = str(err.get("message") or data.get("message") or "تأیید ناموفق")
        return PaymentVerifyResult(ok=False, code=code, message=msg, ref_id=str(d.get("ref_id") or "") if d.get("ref_id") else None)

    @staticmethod
    def setup_guide() -> str:
        return (
            "🔧 <b>راهنمای اتصال زرین‌پال</b>\n\n"
            "۱) به <a href=\"https://www.zarinpal.com\">zarinpal.com</a> برو → ثبت‌نام (حقیقی با کد ملی/شماره شبا)\n"
            "۲) از پنل: <b>درگاه پرداخت → ایجاد درگاه → وب‌سرویس</b> → عنوان: BidPilot\n"
            "۳) دامنه موقت: <code>https://bidpilot.local</code> و شماره تماس خودت\n"
            "۴) پس از تأیید (چند ساعت)، از <b>تنظیمات درگاه → مرچنت‌کد</b> مقدار ۳۶ کاراکتری را کپی کن (مثلاً <code>xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx</code>)\n"
            "۵) در ربات: <code>/settings</code> یا فایل <code>data/settings.json</code> → <code>zarinpal.merchant_id</code> را بگذار\n"
            "۶) برای تست بدون پول واقعی: در تنظیمات ربات <code>zarinpal.sandbox = true</code> بگذار (از <code>sandbox.zarinpal.com</code> استفاده می‌شود)\n\n"
            "• <b>Callback URL لازم نیست</b> — BidPilot پرداخت را با دکمه «بررسی پرداخت» تأیید می‌کند (نیاز به دامنه عمومی ندارد).\n"
            "• هزینه زرین‌پال: ~۱٪ تا سقف ۴ هزار تومان به عهده خریدار؛ تسویه شاپرکی روزانه.\n"
            "• بعد از تنظیم مرچنت‌کد، با <code>/plans</code> → خرید → لینک واقعی زرین‌پال ساخته می‌شود.\n"
        )
