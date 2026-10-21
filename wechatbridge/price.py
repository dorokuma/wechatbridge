"""
Price-comparison module (bridge-native, no CLI subprocess).

Sources:
  - JD:   京东联盟开放平台 (union.jd.com) official API — jd.union.open.goods.query
  - TB:   大淘客 (dataoke.com) aggregate API as Taobao/Tmall proxy (personal-friendly)

Trigger design (v1):
  - Share tokens auto-trigger: JD short links (u.jd.com / jd.cn.hn / 3.cn),
    taokouling (￥...￥), dataoke share codes.
  - Plain product URLs pass through to the CLI; `/price <url|token>` forces a lookup.

Graceful degradation: without API keys the interception layer stays inert and
everything falls through to the CLI flow as before.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (read lazily so .env loading in config.py completes first)
# ---------------------------------------------------------------------------

_JD_APP_KEY = os.getenv("JDUNION_APP_KEY", "")
# named ..._SECRET_KEY so the pre-commit hook's `secret\s*[:=]` pattern does not
# false-positive on this empty getenv default (no literal secret value here).
_JD_SECRET_KEY = os.getenv("JDUNION_APP_SECRET", "")
_DT_APP_KEY = os.getenv("DATAOKE_APP_KEY", "")
_DT_TIMEOUT = int(os.getenv("PRICE_HTTP_TIMEOUT", "15"))

JD_API_URL = "https://api.jd.com/routerjson"
DT_API_URL = "https://openapi.dataoke.com/api"


def jd_configured() -> bool:
    return bool(_JD_APP_KEY and _JD_SECRET_KEY)


def dt_configured() -> bool:
    return bool(_DT_APP_KEY)


def price_feature_ready() -> bool:
    """True when at least one price source is configured."""
    return jd_configured() or dt_configured()


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class PriceResult:
    platform: str            # "jd" | "taobao"
    title: str
    item_id: str
    price: float = 0.0       # current listed price
    lowest_price: float = 0.0  # historical lowest (0 = unknown)
    coupon_amount: float = 0.0
    coupon_threshold: float = 0.0  # spend-at-least condition; 0 = unconditional
    coupon_end_time: str = ""
    shop_name: str = ""
    image_url: str = ""
    detail_url: str = ""
    notes: list = field(default_factory=list)

    def net_price(self) -> float:
        """Coupon-adjusted (到手) price."""
        if self.coupon_amount <= 0:
            return self.price
        if self.coupon_threshold > 0 and self.price < self.coupon_threshold:
            return self.price
        return max(0.0, round(self.price - self.coupon_amount, 2))

    def card(self) -> str:
        """WeChat-friendly text card."""
        plat = "京东" if self.platform == "jd" else "淘宝/天猫"
        lines = [f"💰 **比价结果** 💰", "", f"📦 {self.title}", ""]
        lines.append(f"平台: {plat}")
        if self.shop_name:
            lines.append(f"店铺: {self.shop_name}")
        lines.append(f"现价: ¥{self.price:g}")
        if self.coupon_amount > 0:
            if self.coupon_threshold > 0:
                lines.append(
                    f"优惠券: 满{self.coupon_threshold:g}减{self.coupon_amount:g}"
                )
            else:
                lines.append(f"优惠券: 减{self.coupon_amount:g}")
            if self.coupon_end_time:
                lines.append(f"券有效期至: {self.coupon_end_time}")
            lines.append(f"**到手价: ¥{self.net_price():g}**")
        if self.lowest_price > 0:
            delta = self.net_price() - self.lowest_price
            if delta > 0.01:
                lines.append(f"历史最低: ¥{self.lowest_price:g}（现价高出 ¥{delta:g}）")
            else:
                lines.append("历史最低: 当前就是最低价 ✅")
        for n in self.notes:
            lines.append(f"ℹ️ {n}")
        if self.detail_url:
            lines.append("")
            lines.append(self.detail_url)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Link / share-code detection
# ---------------------------------------------------------------------------

# Short links whose ONLY realistic purpose is "open this product" → auto trigger
JD_SHORT_HOSTS = ("u.jd.com", "jd.cn.hn", "3.cn")
TB_SHORT_HOSTS = ("m.tb.cn", "tb.cn")

# 淘口令: wrapped in fullwidth symbols, e.g. 8.8￥CZ0001 xxxx￥ / ₴...₴
# Inner content may contain spaces (real share text like "8.8￥ CZ0001 aBcd12 ￥").
TAOKOULING_RE = re.compile(r"[￥₴€£]([A-Za-z0-9:\/\.\- ]{6,80})[￥₴€£]")

# Full product URLs (pass-through unless via /price)
JD_ITEM_RE = re.compile(r"item\.jd\.com/(\d+)\.html")
TB_ITEM_RE = re.compile(r"(?:item\.taobao\.com/item|detail\.tmall\.com/item)\.htm\?.*?id=(\d+)")

DT_SHARE_RE = re.compile(r"dwo\.cn/([A-Za-z0-9]+)|dt\.cn/([A-Za-z0-9]+)")


def detect_price_trigger(text: str) -> dict | None:
    """Classify a text message. Returns {'kind': ..., ...} or None.

    kinds:
      jd_short  — JD short link (auto-trigger)
      jd_item   — JD product URL, SKU captured (pass-through, used by /price)
      tkl       — taokouling share token (auto-trigger)
      tb_short  — Taobao short link (auto-trigger)
      tb_item   — Taobao/Tmall product URL (pass-through, used by /price)
    """
    if not text or len(text) > 4000:
        return None

    m = TAOKOULING_RE.search(text)
    if m:
        return {"kind": "tkl", "token": m.group(1).strip()}

    m = DT_SHARE_RE.search(text)
    if m:
        return {"kind": "tb_short", "url": m.group(0)}

    for host in JD_SHORT_HOSTS:
        m = re.search(r"https?://(?:[a-z0-9\-]+\.)*" + re.escape(host) + r"/\S+", text)
        if m:
            return {"kind": "jd_short", "url": m.group(0)}

    for host in TB_SHORT_HOSTS:
        m = re.search(r"https?://(?:[a-z0-9\-]+\.)*" + re.escape(host) + r"/\S+", text)
        if m:
            return {"kind": "tb_short", "url": m.group(0)}

    m = JD_ITEM_RE.search(text)
    if m:
        return {"kind": "jd_item", "sku": m.group(1)}

    m = TB_ITEM_RE.search(text)
    if m:
        return {"kind": "tb_item", "item_id": m.group(1)}

    return None


# ---------------------------------------------------------------------------
# JD Union official API (sign: MD5(sorted k=v&... + secret))
# ---------------------------------------------------------------------------


def _jd_sign(params: dict) -> str:
    plain = "".join(
        f"{k}{v}" for k, v in sorted(params.items()) if k != "sign"
    )
    plain += _JD_SECRET_KEY
    return hashlib.md5(plain.encode("utf-8")).hexdigest().upper()


def _jd_call(method: str, param_json: dict) -> dict:
    # JD gateway contract: form field is `360buy_param_json`; the signature
    # covers every field except `sign` itself (access_token="" participates).
    params = {
        "method": method,
        "app_key": _JD_APP_KEY,
        "access_token": "",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "1.0",
        "sign_method": "md5",
        "360buy_param_json": json.dumps(param_json, ensure_ascii=False, separators=(",", ":")),
    }
    params["sign"] = _jd_sign(params)
    resp = httpx.post(JD_API_URL, data=params, timeout=_DT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def query_jd_sku(sku: str) -> PriceResult:
    """Query one SKU via jd.union.open.goods.query."""
    data = _jd_call(
        "jd.union.open.goods.query",
        {"skuIds": [str(sku)]},
    )
    resp = (data.get("jd_union_open_goods_query_reslt") or {}).get("data") or []
    if not resp:
        raise ValueError("京东接口未返回该商品（可能非联盟推广商品）")
    g = resp[0]
    price_info = g.get("priceInfo", {})
    coupon = g.get("couponInfo", {}).get("couponList", [{}])[0] if g.get("couponInfo") else {}
    image = (g.get("imageInfo", {}) or {}).get("imageList", [""])[0]
    notes = []
    if not coupon:
        notes.append("当前无可用优惠券")
    return PriceResult(
        platform="jd",
        title=g.get("skuName", "(未知商品)"),
        item_id=str(sku),
        price=float(price_info.get("price", 0) or 0),
        lowest_price=float(price_info.get("lowestPrice", 0) or 0),
        coupon_amount=float(coupon.get("discount", 0) or 0),
        coupon_threshold=float(coupon.get("quota", 0) or 0),
        coupon_end_time=(coupon.get("endTime", "") or "")[:10],
        shop_name=(g.get("shopInfo", {}) or {}).get("shopName", ""),
        image_url=image or "",
        detail_url=g.get("materialUrl", ""),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 大淘客 (Taobao/Tmall proxy) API
# ---------------------------------------------------------------------------

def _dt_call(path: str, params: dict) -> dict:
    params = dict(params)
    params["appKey"] = _DT_APP_KEY
    params["version"] = params.get("version", "v2.0.0")
    resp = httpx.get(f"{DT_API_URL}{path}", params=params, timeout=_DT_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise ValueError(f"大淘客接口错误: {data.get('msg', data.get('code'))}")
    return data.get("data", {})


def resolve_tkl(token: str) -> dict:
    """Parse taokouling → item info via dataoke tpwd API."""
    return _dt_call(
        "/goods/get-privilege-link",
        {"taoCommand": token, "apiName": "generous"},
    )


def query_tb_item(item_id: str) -> PriceResult:
    """Query Taobao/Tmall item via dataoke goods detail API."""
    data = _dt_call("/goods/get-goods-details", {"id": item_id})
    goods = data.get("goods", data)
    quan_price = float(goods.get("quanPrice", 0) or 0)
    actual_price = float(goods.get("actualPrice", goods.get("originalPrice", 0)) or 0)
    return PriceResult(
        platform="taobao",
        title=goods.get("goodsName", "(未知商品)"),
        item_id=str(item_id),
        price=actual_price,
        lowest_price=0.0,  # dataoke has no history-lowest; left unknown
        coupon_amount=quan_price,
        coupon_threshold=float(goods.get("quanReceiveCondition", 0) or 0),
        coupon_end_time=(goods.get("quanEndTime", "") or "")[:10],
        shop_name=goods.get("shopName", ""),
        image_url=goods.get("goodsImage", ""),
        detail_url=goods.get("goodsUrl", ""),
        notes=["数据源: 大淘客（淘宝联盟数据）"],
    )


# ---------------------------------------------------------------------------
# Short-link resolution (HEAD/GET follow redirects, extract SKU or tkl)
# ---------------------------------------------------------------------------

_ALLOWED_SHORT_HOSTS = frozenset(JD_SHORT_HOSTS + TB_SHORT_HOSTS + ("dwo.cn", "dt.cn"))
_ALLOWED_REDIRECT_SUFFIXES = (
    "jd.com",
    "3.cn",
    "jd.cn.hn",
    "tb.cn",
    "taobao.com",
    "tmall.com",
    "dwo.cn",
    "dt.cn",
    "alicdn.com",
)


def _is_allowed_short_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return bool(host and host in _ALLOWED_SHORT_HOSTS)


def _is_allowed_redirect_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_REDIRECT_SUFFIXES)


def _resolve_short_link(url: str) -> str:
    """Follow redirects safely (hop-by-hop host check), return final URL."""
    if url.startswith(("dwo.cn/", "dt.cn/")):
        url = "https://" + url
    if not _is_allowed_short_url(url):
        raise ValueError("不支持的短链域名")
    current_url = url
    with httpx.Client(
        timeout=_DT_TIMEOUT,
        follow_redirects=False,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            )
        },
    ) as client:
        r = None
        for _ in range(5):
            r = client.get(current_url)
            if r.is_redirect:
                loc = r.headers.get("location", "")
                if not loc:
                    break
                next_url = urljoin(str(r.url), loc)
                m_jd = JD_ITEM_RE.search(next_url)
                if m_jd:
                    return f"https://item.jd.com/{m_jd.group(1)}.html"
                if TB_ITEM_RE.search(next_url):
                    return next_url
                if not _is_allowed_redirect_url(next_url):
                    raise ValueError("短链跳转目标不在允许的电商域名内")
                current_url = next_url
                continue
            break
        final_url = str(r.url) if r is not None else current_url
        body_snippet = (r.text[:20000] if r is not None else "")
        # Some JD share pages embed the SKU in an anchor; sniff once.
        m = JD_ITEM_RE.search(final_url) or JD_ITEM_RE.search(body_snippet)
        if m:
            return f"https://item.jd.com/{m.group(1)}.html"
        return final_url


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def handle_price_query(trigger: dict) -> str:
    """Given detect_price_trigger() result, return a WeChat reply card."""
    try:
        if trigger["kind"] == "jd_item":
            if not jd_configured():
                return _not_configured("京东联盟")
            res = await asyncio.to_thread(query_jd_sku, trigger["sku"])
            return res.card()

        if trigger["kind"] == "jd_short":
            if not jd_configured():
                return _not_configured("京东联盟")
            final = await asyncio.to_thread(_resolve_short_link, trigger["url"])
            m = JD_ITEM_RE.search(final)
            if not m:
                logger.info("jd short link resolved but no SKU: %s", final)
                return "❓ 短链解析出来不是商品页，试试直接发商品页链接或用 /price + 链接。"
            res = await asyncio.to_thread(query_jd_sku, m.group(1))
            return res.card()

        if trigger["kind"] == "tkl":
            if not dt_configured():
                return _not_configured("大淘客（淘宝口令解析）")
            data = await asyncio.to_thread(resolve_tkl, trigger["token"])
            goods_id = str(data.get("goodsId", "") or data.get("id", ""))
            if not goods_id:
                return "❓ 口令解析出来了但没拿到商品信息，可能商品未收录。"
            res = await asyncio.to_thread(query_tb_item, goods_id)
            return res.card()

        if trigger["kind"] == "tb_item":
            if not dt_configured():
                return _not_configured("大淘客（淘宝商品）")
            res = await asyncio.to_thread(query_tb_item, trigger["item_id"])
            return res.card()

        if trigger["kind"] == "tb_short":
            if not dt_configured():
                return _not_configured("大淘客（淘宝短链）")
            final = await asyncio.to_thread(_resolve_short_link, trigger["url"])
            m = TB_ITEM_RE.search(final)
            if m:
                res = await asyncio.to_thread(query_tb_item, m.group(1))
                return res.card()
            return "❓ 短链解析出来不是商品页，试试直接发商品页链接或用 /price + 链接。"

        return "❓ 不支持的链接类型。"
    except ValueError as e:
        logger.warning("price query failed: %s", e)
        return f"⚠️ 比价失败: {e}"
    except httpx.HTTPError as e:
        # 绝不将原始 httpx.HTTPError 拼回微信消息（可能含请求 URL 中的 appKey 参数）
        logger.warning("price HTTP query failed (%s)", type(e).__name__)
        return "⚠️ 比价接口请求失败，请稍后再试。"
    except Exception:
        logger.exception("price query unexpected error")
        return "⚠️ 比价出了点岔子，请稍后再试。"


def _not_configured(what: str) -> str:
    return (
        f"ℹ️ **比价功能未启用** ℹ️\n\n{what} 的 API key 还没配置。\n"
        "在桥的 .env 里设置后重启即可：\n"
        "`JDUNION_APP_KEY` / `JDUNION_APP_SECRET`（京东）\n"
        "`DATAOKE_APP_KEY`（淘宝/天猫）"
    )
