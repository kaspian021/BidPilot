"""Multi-gateway abstraction — 4 Iranian gateways with light KYC.

User request: 4 options, none requires heavy verification (no company docs).
Chosen set (all شاپرک, all sandbox-capable):

  • zarinpal  — classic, moderate KYC (شناسه ملی + شبا) but trusted brand
  • zibal     — lightest: mobile + شبا, instant activation <2h, no logo needed
  • idpay     — light: mobile + شبا, <24h, API very clean
  • nextpay   — light: mobile + شبا, instant, good sandbox

All gateways implement the same interface so bot_ui/manager never cares
which one is used. If merchant_id/API key is missing, we fallback to
simulated payment (so user can test flow without any account).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayMeta:
    id: str
    label: str
    label_fa: str
    description: str
    website: str
    kyc_level: str  # نمایش به کاربر: سبک / متوسط
    logo_emoji: str
    docs_url: str


GATEWAYS_META: dict[str, GatewayMeta] = {
    "zarinpal": GatewayMeta(
        id="zarinpal",
        label="ZarinPal",
        label_fa="زرین‌پال",
        description="محبوب‌ترین، سندباکس عالی، تسویه شاپرک",
        website="zarinpal.com",
        kyc_level="متوسط — کارت ملی + شبا (چند ساعت)",
        logo_emoji="💛",
        docs_url="https://docs.zarinpal.com",
    ),
    "zibal": GatewayMeta(
        id="zibal",
        label="Zibal",
        label_fa="زیبال",
        description="سریع‌ترین فعال‌سازی — فقط موبایل + شبا، زیر ۲ ساعت",
        website="zibal.ir",
        kyc_level="سبک — موبایل + شبا، بدون لوگو/نماد",
        logo_emoji="💚",
        docs_url="https://docs.zibal.ir",
    ),
    "idpay": GatewayMeta(
        id="idpay",
        label="IDPay",
        label_fa="آی‌دی‌پی",
        description="API تمیز، وب‌هوک، احراز زیر ۲۴ ساعت",
        website="idpay.ir",
        kyc_level="سبک — موبایل + شبا + کارت ملی",
        logo_emoji="💙",
        docs_url="https://docs.idpay.ir",
    ),
    "nextpay": GatewayMeta(
        id="nextpay",
        label="NextPay",
        label_fa="نکست‌پی",
        description="فوری، بدون نیاز به وب‌سایت، مانده آنی",
        website="nextpay.ir",
        kyc_level="سبک — موبایل + شبا، فعال‌سازی آنی",
        logo_emoji="🧡",
        docs_url="https://nextpay.ir/docs",
    ),
}

# Canonical order shown to user (user picks)
GATEWAY_ORDER = ["zibal", "idpay", "nextpay", "zarinpal"]


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
    gateway: str = ""


@dataclass
class PaymentVerifyResult:
    ok: bool
    ref_id: str | None = None
    code: int | None = None
    message: str = ""
    card_pan: str | None = None


class GatewayError(RuntimeError):
    pass


def _simulated_ok(amount_toman: int, prefix: str, gateway: str) -> PaymentCreateResult:
    """Fallback when merchant not configured — keeps UX testable without account."""
    # deterministic but unique-ish fake authority so verify can succeed in sim mode
    h = hashlib.sha256(f"{prefix}:{amount_toman}:{gateway}".encode()).hexdigest()[:20].upper()
    fake_auth = f"SIM-{gateway[:2].upper()}-{h}"
    base = GATEWAYS_META[gateway].website
    return PaymentCreateResult(
        ok=True,
        authority=fake_auth,
        payment_url=f"https://{base}/pay/{fake_auth}",
        code=100,
        message=f"(شبیه‌سازی {GATEWAYS_META[gateway].label_fa}) — کلید درگاه تنظیم نشده، پرداخت تستی ایجاد شد.",
        gateway=gateway,
    )


class BaseGateway:
    id: str = ""

    def __init__(self, merchant_id: str, sandbox: bool = False, callback_url: str = ""):
        self.merchant_id = (merchant_id or "").strip()
        self.sandbox = sandbox
        self.callback_url = callback_url or "https://bidpilot.local/payment/callback"

    def is_configured(self) -> bool:
        return bool(self.merchant_id and self.merchant_id not in {"test", "demo", "YOUR_MERCHANT_ID"})

    async def create(self, req: PaymentRequest) -> PaymentCreateResult:
        raise NotImplementedError

    async def verify(self, authority: str, amount_toman: int) -> PaymentVerifyResult:
        raise NotImplementedError


# ── ZarinPal (keep existing logic, now as gateway) ─────────────────
class ZarinPalGateway(BaseGateway):
    id = "zarinpal"

    @property
    def base_url(self) -> str:
        return "https://sandbox.zarinpal.com" if self.sandbox else "https://api.zarinpal.com"

    @property
    def pay_prefix(self) -> str:
        return "https://sandbox.zarinpal.com/pg/StartPay/" if self.sandbox else "https://www.zarinpal.com/pg/StartPay/"

    async def create(self, req: PaymentRequest) -> PaymentCreateResult:
        if not self.is_configured():
            return _simulated_ok(req.amount_toman, req.order_id or "zp", self.id)
        url = f"{self.base_url}/pg/v4/payment/request.json"
        payload: dict[str, Any] = {
            "merchant_id": self.merchant_id,
            "amount": int(req.amount_toman * 10),
            "description": req.description,
            "callback_url": req.callback_url or self.callback_url,
        }
        if req.mobile:
            payload["mobile"] = req.mobile
        if req.email:
            payload["email"] = req.email
        if req.order_id:
            payload["metadata"] = {"order_id": req.order_id}
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, json=payload, headers={"Content-Type": "application/json", "Accept": "application/json"})
                data = r.json()
        except Exception as exc:
            return PaymentCreateResult(ok=False, message=f"اتصال زرین‌پال ناموفق: {exc}", gateway=self.id)
        d = data.get("data") or {}
        code = d.get("code")
        if code == 100 and d.get("authority"):
            return PaymentCreateResult(ok=True, authority=str(d["authority"]), payment_url=f"{self.pay_prefix}{d['authority']}", code=code, message="OK", gateway=self.id)
        err = data.get("errors") or d
        return PaymentCreateResult(ok=False, code=code, message=str(err.get("message") or "خطای زرین‌پال"), gateway=self.id)

    async def verify(self, authority: str, amount_toman: int) -> PaymentVerifyResult:
        if not self.is_configured():
            if authority.startswith("SIM-ZA"):
                return PaymentVerifyResult(ok=True, ref_id=f"SIM-{authority[-6:]}", code=100, message="تأیید شبیه‌سازی")
            return PaymentVerifyResult(ok=False, code=-1, message="Authority تستی نامعتبر")
        url = f"{self.base_url}/pg/v4/payment/verify.json"
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, json={"merchant_id": self.merchant_id, "amount": int(amount_toman * 10), "authority": authority})
                data = r.json()
        except Exception as exc:
            return PaymentVerifyResult(ok=False, message=f"خطا در تأیید: {exc}")
        d = data.get("data") or {}
        code = d.get("code")
        if code in (100, 101) and d.get("ref_id"):
            return PaymentVerifyResult(ok=True, ref_id=str(d["ref_id"]), code=code, message="OK", card_pan=d.get("card_pan"))
        err = data.get("errors") or d
        return PaymentVerifyResult(ok=False, code=code, message=str(err.get("message") or "تأیید ناموفق"), ref_id=str(d.get("ref_id") or "") if d.get("ref_id") else None)


# ── Zibal ───────────────────────────────────────────────────────
class ZibalGateway(BaseGateway):
    id = "zibal"

    async def create(self, req: PaymentRequest) -> PaymentCreateResult:
        if not self.is_configured():
            return _simulated_ok(req.amount_toman, req.order_id or "zb", self.id)
        # Zibal API: https://gateway.zibal.ir/v1/request
        url = "https://gateway.zibal.ir/v1/request"
        payload: dict[str, Any] = {
            "merchant": self.merchant_id,
            "amount": int(req.amount_toman * 10),  # ریال
            "callbackUrl": req.callback_url or self.callback_url,
            "description": req.description,
        }
        if req.order_id:
            payload["orderId"] = req.order_id
        if req.mobile:
            payload["mobile"] = req.mobile
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, json=payload)
                data = r.json()
        except Exception as exc:
            return PaymentCreateResult(ok=False, message=f"اتصال زیبال ناموفق: {exc}", gateway=self.id)
        if data.get("result") == 100 and data.get("trackId"):
            tid = str(data["trackId"])
            return PaymentCreateResult(ok=True, authority=tid, payment_url=f"https://gateway.zibal.ir/start/{tid}", code=100, message="OK", gateway=self.id)
        return PaymentCreateResult(ok=False, code=data.get("result"), message=str(data.get("message") or "خطای زیبال"), gateway=self.id)

    async def verify(self, authority: str, amount_toman: int) -> PaymentVerifyResult:
        if not self.is_configured():
            return PaymentVerifyResult(ok=True, ref_id=f"SIM-ZB-{authority[-6:]}", code=100, message="تأیید شبیه‌سازی") if authority.startswith("SIM-") else PaymentVerifyResult(ok=False, message="نامعتبر")
        # Zibal verify: POST https://gateway.zibal.ir/v1/verify {merchant, trackId}
        url = "https://gateway.zibal.ir/v1/verify"
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, json={"merchant": self.merchant_id, "trackId": authority})
                data = r.json()
        except Exception as exc:
            return PaymentVerifyResult(ok=False, message=f"خطا: {exc}")
        if data.get("result") in (100, 201):
            return PaymentVerifyResult(ok=True, ref_id=str(data.get("refId") or data.get("trackId") or authority), code=data.get("result"), message="OK")
        return PaymentVerifyResult(ok=False, code=data.get("result"), message=str(data.get("message") or "ناموفق"))


# ── IDPay ───────────────────────────────────────────────────────
class IDPayGateway(BaseGateway):
    id = "idpay"

    async def create(self, req: PaymentRequest) -> PaymentCreateResult:
        if not self.is_configured():
            return _simulated_ok(req.amount_toman, req.order_id or "id", self.id)
        url = "https://api.idpay.ir/v1.1/payment"
        headers = {"Content-Type": "application/json", "X-API-KEY": self.merchant_id, "X-SANDBOX": "1" if self.sandbox else "0"}
        payload: dict[str, Any] = {
            "order_id": req.order_id or f"bid-{req.amount_toman}",
            "amount": int(req.amount_toman * 10),
            "name": req.description[:80],
            "desc": req.description,
            "callback": req.callback_url or self.callback_url,
        }
        if req.mobile:
            payload["phone"] = req.mobile
        if req.email:
            payload["mail"] = req.email
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, json=payload, headers=headers)
                data = r.json()
        except Exception as exc:
            return PaymentCreateResult(ok=False, message=f"اتصال آی‌دی‌پی ناموفق: {exc}", gateway=self.id)
        if data.get("id") and data.get("link"):
            return PaymentCreateResult(ok=True, authority=str(data["id"]), payment_url=str(data["link"]), code=100, message="OK", gateway=self.id)
        return PaymentCreateResult(ok=False, code=data.get("status"), message=str(data.get("error_message") or data.get("error") or "خطای آی‌دی‌پی"), gateway=self.id)

    async def verify(self, authority: str, amount_toman: int) -> PaymentVerifyResult:
        if not self.is_configured():
            return PaymentVerifyResult(ok=True, ref_id=f"SIM-ID-{authority[-6:]}", code=100, message="تأیید شبیه‌سازی") if authority.startswith("SIM-") else PaymentVerifyResult(ok=False, message="نامعتبر")
        # IDPay verify needs id + order_id; we store authority as id
        url = "https://api.idpay.ir/v1.1/payment/verify"
        payload = {"id": authority, "order_id": authority}
        headers = {"Content-Type": "application/json", "X-API-KEY": self.merchant_id, "X-SANDBOX": "1" if self.sandbox else "0"}
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, json=payload, headers=headers)
                data = r.json()
        except Exception as exc:
            return PaymentVerifyResult(ok=False, message=f"خطا: {exc}")
        if data.get("status") in (100, 101, 200):
            return PaymentVerifyResult(ok=True, ref_id=str(data.get("track_id") or authority), code=data.get("status"), message="OK")
        return PaymentVerifyResult(ok=False, code=data.get("status"), message=str(data.get("error_message") or "ناموفق"))


# ── NextPay ─────────────────────────────────────────────────────
class NextPayGateway(BaseGateway):
    id = "nextpay"

    async def create(self, req: PaymentRequest) -> PaymentCreateResult:
        if not self.is_configured():
            return _simulated_ok(req.amount_toman, req.order_id or "np", self.id)
        url = "https://nextpay.org/nx/gateway/token"
        payload = {
            "api_key": self.merchant_id,
            "amount": int(req.amount_toman * 10),
            "order_id": req.order_id or f"bid-{req.amount_toman}",
            "callback_uri": req.callback_url or self.callback_url,
            "custom_json_fields": {"desc": req.description},
        }
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, json=payload)
                data = r.json()
        except Exception as exc:
            return PaymentCreateResult(ok=False, message=f"اتصال نکست‌پی ناموفق: {exc}", gateway=self.id)
        if str(data.get("code")) == "-1" and data.get("trans_id"):
            tid = str(data["trans_id"])
            return PaymentCreateResult(ok=True, authority=tid, payment_url=f"https://nextpay.org/nx/gateway/payment/{tid}", code=100, message="OK", gateway=self.id)
        return PaymentCreateResult(ok=False, code=data.get("code"), message=str(data.get("message") or "خطای نکست‌پی"), gateway=self.id)

    async def verify(self, authority: str, amount_toman: int) -> PaymentVerifyResult:
        if not self.is_configured():
            return PaymentVerifyResult(ok=True, ref_id=f"SIM-NP-{authority[-6:]}", code=100, message="تأیید شبیه‌سازی") if authority.startswith("SIM-") else PaymentVerifyResult(ok=False, message="نامعتبر")
        url = "https://nextpay.org/nx/gateway/verify"
        payload = {"api_key": self.merchant_id, "amount": int(amount_toman * 10), "trans_id": authority}
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, json=payload)
                data = r.json()
        except Exception as exc:
            return PaymentVerifyResult(ok=False, message=f"خطا: {exc}")
        if str(data.get("code")) == "0":
            return PaymentVerifyResult(ok=True, ref_id=str(data.get("Shaparak_Ref_Id") or authority), code=0, message="OK")
        return PaymentVerifyResult(ok=False, code=data.get("code"), message=str(data.get("message") or "ناموفق"))


GATEWAY_CLASSES: dict[str, type[BaseGateway]] = {
    "zarinpal": ZarinPalGateway,
    "zibal": ZibalGateway,
    "idpay": IDPayGateway,
    "nextpay": NextPayGateway,
}


def get_gateway(gateway_id: str, merchant_id: str, sandbox: bool = False, callback_url: str = "") -> BaseGateway:
    gid = (gateway_id or "zibal").strip().lower()
    cls = GATEWAY_CLASSES.get(gid) or ZibalGateway
    return cls(merchant_id=merchant_id, sandbox=sandbox, callback_url=callback_url)


def gateway_config_from_settings(settings: dict[str, str], gateway_id: str) -> tuple[str, bool, str]:
    """Read merchant_id/sandbox/callback for a gateway from settings dict."""
    # settings may store as payments.<gateway>.merchant_id etc.
    prefix = f"payments.{gateway_id}"
    mid = str(settings.get(f"{prefix}.merchant_id") or settings.get(f"{prefix}.api_key") or settings.get("payments.merchant_id") or settings.get("zarinpal.merchant_id") or "")
    sandbox = str(settings.get(f"{prefix}.sandbox") or settings.get("zarinpal.sandbox") or "").lower() in {"1", "true", "yes", "on"}
    cb = str(settings.get(f"{prefix}.callback_url") or settings.get("zarinpal.callback_url") or settings.get("payments.callback_url") or "")
    return mid, sandbox, cb


def all_gateways_with_meta() -> list[tuple[str, GatewayMeta]]:
    return [(gid, GATEWAYS_META[gid]) for gid in GATEWAY_ORDER]
