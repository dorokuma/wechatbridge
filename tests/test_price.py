"""Price module contract tests (stdlib unittest only).

These pin the *external* contracts that cannot be exercised without real
API keys: the JD gateway signature rule, the Dataoke signature rule, the
correct Dataoke endpoints/params for taokouling parsing and goods detail,
and response parsing against sample payloads shaped like the real APIs.
"""

from __future__ import annotations

import hashlib
import os
import unittest
from unittest import mock

os.environ.setdefault("JDUNION_APP_KEY", "test_jd_key")
os.environ.setdefault("JDUNION_APP_SECRET", "test_jd_secret")
os.environ.setdefault("DATAOKE_APP_KEY", "test_dt_key")
os.environ.setdefault("DATAOKE_APP_SECRET", "test_dt_secret")

from wechatbridge import price as price_mod


class TestJdSign(unittest.TestCase):
    def test_secret_wraps_both_ends(self):
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
        plain = "".join(f"{k}{v}" for k, v in sorted(params.items()))
        expected = hashlib.md5(
            ("test_jd_secret" + plain + "test_jd_secret").encode("utf-8")
        ).hexdigest().upper()
        self.assertEqual(price_mod._jd_sign(dict(params)), expected)

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


class TestDataokeSignAndCalls(unittest.TestCase):
    def test_dt_sign_matches_sdk_algorithm(self):
        params = {"goodsId": "789", "appKey": "test_dt_key", "version": "v2.0.0"}
        plain = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        expected = hashlib.md5(
            (plain + "&key=test_dt_secret").encode("utf-8")
        ).hexdigest().upper()
        self.assertEqual(price_mod._dt_sign(dict(params)), expected)

    def test_dt_call_attaches_appkey_version_and_sign(self):
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
        self.assertEqual(captured["params"]["appKey"], "test_dt_key")
        self.assertEqual(captured["params"]["version"], "v2.0.0")
        unsigned = {k: v for k, v in captured["params"].items() if k != "sign"}
        self.assertEqual(captured["params"]["sign"], price_mod._dt_sign(unsigned))

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
        self.assertEqual(captured["params"], {"goodsId": "789"})
        self.assertEqual(res.title, "TB Sample")
        self.assertEqual(res.price, 59.9)
        self.assertEqual(res.net_price(), 49.9)

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
