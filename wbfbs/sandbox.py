"""Песочница WB: методы эмуляции FBS + прекондишены полного цикла.

Источник: dev.wildberries.ru/docs/openapi-other/sandbox-environment
(раздел «Маркетплейс FBS», скопирован вручную 2026-07-07 — страница за
антиботом).

Статусная модель: supplierStatus меняет продавец штатными методами,
wbStatus в песочнице двигается методами эмуляции:
  make -> waiting/new -> (add to supply) waiting/confirm ->
  (deliver supply) waiting/complete -> (test close) sorted ->
  (test deliver) ready_for_pickup -> (test receive) sold
  ветки: decline (отмена покупателем, 1 час), reject (отказ на ПВЗ),
  defect (брак).

Прекондишены make: баркод из карточки, созданной в ПЕСОЧНИЦЕ контента
(content-api-sandbox), остаток на FBS-складе >= количества, цена != 0
(discounts-prices-api-sandbox). ID песочницы: WB-GI-SAND-*, WB-TRBX-SAND-*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .client import SANDBOX_URL, WBClient, WBError

log = logging.getLogger("wbfbs")

CONTENT_SANDBOX_URL = "https://content-api-sandbox.wildberries.ru"
PRICES_SANDBOX_URL = "https://discounts-prices-api-sandbox.wildberries.ru"


@dataclass(frozen=True)
class TestEP:
    make: str = "/api/v3/test/fbs/orders/make"
    decline: str = "/api/v3/test/fbs/orders/{order_id}/decline"
    deliver: str = "/api/v3/test/fbs/orders/{order_id}/deliver"
    receive: str = "/api/v3/test/fbs/orders/{order_id}/receive"
    reject: str = "/api/v3/test/fbs/orders/{order_id}/reject"
    defect: str = "/api/v3/test/fbs/orders/{order_id}/defect"
    close_supply: str = "/api/v3/test/fbs/supplies/{supply_id}/close"
    # штатные методы marketplace, нужные для прекондишенов
    warehouses: str = "/api/v3/warehouses"
    offices: str = "/api/v3/offices"
    stocks: str = "/api/v3/stocks/{warehouse_id}"


TEP = TestEP()


class SandboxClient(WBClient):
    """Клиент песочницы: наследует штатные методы, добавляет эмуляцию."""

    def __init__(self, token: str | None = None, **kw):
        kw.setdefault("base_url", SANDBOX_URL)
        super().__init__(token=token, **kw)

    # -- эмуляция покупателя/WB ------------------------------------------

    def make_test_orders(self, items: list[dict]) -> None:
        """Создать тестовые СЗ (одна корзина, единый orderUid). 204 без тела —
        ID заказов забирать через new_orders().

        items: [{"sku": "<баркод из карточки>", "amount": <число СЗ, <=10>}],
        1..10 объектов. ВАЖНО: sku = баркод (не chrtId — он только для
        остатков), amount = количество сборочных заданий, не единиц товара.
        Прекондишены: остаток по chrtId >= amount, цена != 0."""
        if not 1 <= len(items) <= 10:
            raise WBError("make: 1..10 позиций")
        self._request("POST", TEP.make, json={"orders": items})

    def test_decline(self, order_id: int) -> None:
        self._request("PATCH", TEP.decline.format(order_id=order_id))

    def test_deliver(self, order_id: int) -> None:
        """Заказ поступил на ПВЗ -> ready_for_pickup."""
        self._request("PATCH", TEP.deliver.format(order_id=order_id))

    def test_receive(self, order_id: int, code: str = "0000") -> None:
        """Покупатель получил -> sold. Код в песочнице любой."""
        self._request("PATCH", TEP.receive.format(order_id=order_id), json={"code": code})

    def test_reject(self, order_id: int, code: str = "0000") -> None:
        self._request("PATCH", TEP.reject.format(order_id=order_id), json={"code": code})

    def test_defect(self, order_id: int) -> None:
        self._request("PATCH", TEP.defect.format(order_id=order_id))

    def test_close_supply(self, supply_id: str, reshipment_order_ids: list[int] | None = None) -> None:
        """Закрыть поставку: перечисленные orderIds -> повторная отгрузка,
        остальные -> sorted."""
        self._request(
            "PATCH",
            TEP.close_supply.format(supply_id=supply_id),
            json={"orderIds": reshipment_order_ids or []},
        )

    # -- прекондишены: склад и остатки ------------------------------------

    def offices(self) -> list[dict]:
        return self._json("GET", TEP.offices) or []

    def warehouses(self) -> list[dict]:
        return self._json("GET", TEP.warehouses) or []

    def create_warehouse(self, name: str, office_id: int) -> dict:
        return self._json("POST", TEP.warehouses, json={"name": name, "officeId": office_id})

    def set_stocks(self, warehouse_id: int, stocks: list[dict]) -> None:
        """stocks: [{"chrtId": <ID размера>, "amount": N}].

        С миграции 09.02.2026 остатки принимаются ТОЛЬКО по chrtId
        (ID размера из карточки), не по баркоду — sku-загрузка отключена."""
        self._request("PUT", TEP.stocks.format(warehouse_id=warehouse_id), json={"stocks": stocks})


class ContentSandbox(WBClient):
    """Песочница контента: карточки, из которых берутся баркоды для make."""

    def __init__(self, token: str | None = None, **kw):
        kw.setdefault("base_url", CONTENT_SANDBOX_URL)
        super().__init__(token=token, **kw)

    def generate_barcodes(self, count: int = 1) -> list[str]:
        data = self._json("POST", "/content/v2/barcodes", json={"count": count})
        return data.get("data", [])

    def find_subject(self, name: str = "Футболки") -> int:
        data = self._json("GET", "/content/v2/object/all", params={"name": name, "top": 1})
        objects = data.get("data", [])
        if not objects:
            raise WBError(f"Предмет «{name}» не найден в песочнице контента")
        return objects[0]["subjectID"]

    def upload_card(self, subject_id: int, vendor_code: str, barcode: str, title: str) -> None:
        card = [{
            "subjectID": subject_id,
            "variants": [{
                "vendorCode": vendor_code,
                "title": title,
                "description": f"{title} (тестовая карточка wbfbs)",
                "dimensions": {"length": 20, "width": 15, "height": 5, "weightBrutto": 0.3},
                "sizes": [{"techSize": "0", "wbSize": "", "skus": [barcode]}],
                "characteristics": [],
            }],
        }]
        self._request("POST", "/content/v2/cards/upload", json=card)

    def list_cards(self, limit: int = 20) -> list[dict]:
        data = self._json(
            "POST",
            "/content/v2/get/cards/list",
            json={"settings": {"cursor": {"limit": limit}, "filter": {"withPhoto": -1}}},
        )
        return data.get("cards", [])


class PricesSandbox(WBClient):
    def __init__(self, token: str | None = None, **kw):
        kw.setdefault("base_url", PRICES_SANDBOX_URL)
        super().__init__(token=token, **kw)

    def set_price(self, nm_id: int, price: int, discount: int = 0) -> dict:
        return self._json(
            "POST",
            "/api/v2/upload/task",
            json={"data": [{"nmID": nm_id, "price": price, "discount": discount}]},
        )
