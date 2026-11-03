"""Price module contract tests (stdlib unittest only).

These pin the *external* contracts that cannot be exercised without real
API keys: the JD gateway signature rule (empty values excluded), the
Dataoke signRan rule, the correct Dataoke endpoints/params/versions for
taokouling parsing and goods detail, and response parsing against sample
payloads shaped like the real APIs.

Credentials are injected by patching the module-level constants so the
tests never read the host's real JDUNION_*/DATAOKE_* environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import unittest
from unittest import mock

from wechatbridge import price as price_mod


class _PriceCredentialsMixin:
    """Patch price.py's module-level credentials; never touch os.environ."""

    def setUp(self):
        super().setUp()
        for name, value in (
            ("_JD_APP_KEY", "test_jd_key"),
            ("_JD_SECRET_KEY", "test_jd_secret"),
            ("_DT_APP_KEY", "test_dt_key"),
            ("_DT_APP_SECRET", "test_dt_secret"),
        ):
            patcher = mock.patch.object(price_mod, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestJdSign(_PriceCredentialsMixin, unittest.TestCase):
    def test_secret_wraps_both_ends_and_skips_empty_values(self):
        params = {
            "method": "jd.union.open.goods.query",
            "app_key": "test_jd_key",
            "access_token": "",
            "timestamp": "2026-11-01 19:00:00",
            "format": "json",
            "v": "1.0",
            "sign_method": "md5",
            "360buy_param_json": '{"skuIds":["123"]}',
        }
        # Official SDK areNotEmpty semantic: empty values are excluded from
        # the signature input entirely (not concatenated as "k").
        plain = "".join(
            f"{k}{v}" for k, v in sorted(params.items()) if v != ""
        )
        expected = hashlib.md5(
            ("test_jd_secret" + plain + "test_jd_secret").encode("utf-8")
        ).hexdigest().upper()
        self.assertEqual(price_mod._jd_sign(dict(params)), expected)

    def test_jd_call_drops_access_token_and_sends_gmt8_timestamp(self):
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True}

        def fake_post(url, data=None, timeout=None):
            captured["url"] = url
            captured["data"] = dict(data or {})
            return FakeResp()

        with mock.patch.object(price_mod.httpx, "post", side_effect=fake_post):
            price_mod._jd_call("jd.union.open.goods.query", {"skuIds": ["123"]})
        sent = captured["data"]
        self.assertNotIn("access_token", sent)
        # GMT+8 Beijing time, "yyyy-MM-dd HH:mm:ss"
        from datetime import datetime, timedelta, timezone

        ts = datetime.strptime(sent["timestamp"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=8))
        )
        delta = abs(
            (datetime.now(timezone.utc) - ts).total_seconds()
        )
        self.assertLess(delta, 60)
        self.assertIn("sign", sent)

    def test_jd_query_parses_sample_payload(self):
        sample = {
            "jd_union_open_goods_query_reslt": {
                "code": "0",
                "data": [
                    {
                        "skuName": "Sample Widget 2000",
                        "priceInfo": {"price": 129.0, "lowestPrice": 99.0},
                        "couponInfo": {
                            "couponList": [
                                {"discount": 20.0, "quota": 100.0, "endTime": "2026-11-11 23:59:59"}
                            ]
                        },
                        "imageInfo": {"imageList": ["https://img.example/x.jpg"]},
                        "shopInfo": {"shopName": "Sample Shop"},
                        "materialUrl": "https://item.jd.com/123.html",
                    }
                ],
            }
        }
        with mock.patch.object(price_mod, "_jd_call", return_value=sample):
            res = price_mod.query_jd_sku("123")
        self.assertEqual(res.title, "Sample Widget 2000")
        self.assertEqual(res.price, 129.0)
        self.assertEqual(res.lowest_price, 99.0)
        self.assertEqual(res.net_price(), 109.0)
        card = res.card()
        self.assertIn("到手价: ¥109", card)
        self.assertIn("历史最低: ¥99", card)


class TestDataokeSignAndCalls(_PriceCredentialsMixin, unittest.TestCase):
    def test_dt_call_sends_signran_and_no_legacy_sign(self):
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"code": 0, "data": {"ok": True}}

        def fake_get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = dict(params or {})
            return FakeResp()

        with mock.patch.object(price_mod.httpx, "get", side_effect=fake_get):
            out = price_mod._dt_call("/goods/get-goods-details", {"goodsId": "789"})
        self.assertEqual(out, {"ok": True})
        sent = captured["params"]
        self.assertEqual(sent["appKey"], "test_dt_key")
        self.assertIn("timer", sent)
        self.assertIn("nonce", sent)
        self.assertIn("signRan", sent)
        self.assertNotIn("sign", sent)
        expected = hashlib.md5(
            (
                f"appKey=test_dt_key&timer={sent['timer']}"
                f"&nonce={sent['nonce']}&key=test_dt_secret"
            ).encode("utf-8")
        ).hexdigest().upper()
        self.assertEqual(sent["signRan"], expected)

    def test_dt_call_raises_on_nonzero_code(self):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"code": 1, "msg": "sign error"}

        with mock.patch.object(price_mod.httpx, "get", return_value=FakeResp()):
            with self.assertRaises(ValueError):
                price_mod._dt_call("/goods/get-goods-details", {"goodsId": "1"})

    def test_resolve_tkl_uses_parse_content_endpoint(self):
        captured = {}

        def fake_call(path, params):
            captured["path"] = path
            captured["params"] = dict(params)
            return {"goodsId": "789456"}

        with mock.patch.object(price_mod, "_dt_call", side_effect=fake_call):
            out = price_mod.resolve_tkl("CZ0001 aBcd12")
        self.assertEqual(captured["path"], "/tb-service/parse-content")
        self.assertEqual(captured["params"]["content"], "CZ0001 aBcd12")
        self.assertEqual(captured["params"]["version"], "v1.0.0")
        self.assertEqual(out["goodsId"], "789456")

    def test_query_tb_item_sends_goods_id_and_parses(self):
        captured = {}
        sample_data = {
            "goods": {
                "goodsName": "TB Sample",
                "actualPrice": 59.9,
                "originalPrice": 79.9,
                "quanPrice": 10.0,
                "quanReceiveCondition": 50.0,
                "quanEndTime": "2026-11-11 00:00:00",
                "shopName": "TB Shop",
                "goodsImage": "https://img.example/y.jpg",
                "goodsUrl": "https://detail.tmall.com/item.htm?id=789",
            }
        }

        def fake_call(path, params):
            captured["path"] = path
            captured["params"] = dict(params)
            return sample_data

        with mock.patch.object(price_mod, "_dt_call", side_effect=fake_call):
            res = price_mod.query_tb_item("789")
        self.assertEqual(
            captured["params"], {"goodsId": "789", "version": "v1.2.4"}
        )
        self.assertEqual(res.title, "TB Sample")
        self.assertEqual(res.price, 59.9)
        self.assertEqual(res.net_price(), 49.9)

    def test_handle_price_query_ignores_internal_id_field(self):
        # parse-content returns goodsId/itemId; a bare dataoke-internal `id`
        # must NOT be used as a Taobao item id — reply with the
        # "no goods info" fallback instead of querying garbage.
        with mock.patch.object(
            price_mod, "resolve_tkl", return_value={"id": "12345"}
        ), mock.patch.object(
            price_mod, "query_tb_item"
        ) as mock_query:
            reply = asyncio.run(
                price_mod.handle_price_query({"kind": "tkl", "token": "tk"})
            )
        self.assertIn("没拿到商品信息", reply)
        mock_query.assert_not_called()

    def test_dt_configured_requires_secret(self):
        with mock.patch.object(price_mod, "_DT_APP_SECRET", ""):
            self.assertFalse(price_mod.dt_configured())
        with mock.patch.object(price_mod, "_DT_APP_SECRET", "x"):
            self.assertTrue(price_mod.dt_configured())


class TestDetectTrigger(unittest.TestCase):
    def test_samples(self):
        cases = [
            ("看看 https://u.jd.com/AbCdEf 不错", "jd_short"),
            ("8.8￥CZ0001 aBcd12￥ 打开淘宝", "tkl"),
            ("https://item.jd.com/100012043978.html", "jd_item"),
            ("https://detail.tmall.com/item.htm?id=789456123", "tb_item"),
            ("https://m.tb.cn/h.abc123", "tb_short"),
            ("今天天气不错", None),
        ]
        for text, kind in cases:
            got = price_mod.detect_price_trigger(text)
            if kind is None:
                self.assertIsNone(got, msg=text)
            else:
                self.assertIsNotNone(got, msg=text)
                self.assertEqual(got["kind"], kind, msg=text)


if __name__ == "__main__":
    unittest.main()
