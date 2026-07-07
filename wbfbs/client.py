"""Клиент WB Marketplace API (FBS): поставки, короба (trbx), этикетки.

Эндпоинты сверены с официальной OpenAPI-спекой (зеркало
github.com/eslazarev/wildberries-sdk, specs/03-orders-fbs.yaml, снимок
2026-07-02; первоисточник dev.wildberries.ru).

Важное из спеки:
- Единый rate-limit на все FBS-методы: 300 req/мин, интервал 200 мс,
  всплеск 20. Ответ 4XX списывает лимит как 10 запросов, поэтому 4XX
  НЕ ретраим — только 429 и 5xx.
- 409 — семантическая ошибка (смешение cargoType/crossBorderType в поставке,
  закрытая поставка, непрошедшая маркировка на deliver) — ретрай бесполезен,
  тело ответа содержит причину.
- Одиночные методы добавления/чтения заказов поставки удалены 17-18.12.2025;
  используется bulk PATCH /api/marketplace/v3/... (до 100 ID).
- Метода раскладки заказов по коробам в API нет: короба можно создать,
  получить их стикеры и удалить.
- QR поставки доступен только ПОСЛЕ передачи в доставку (deliver).

Токен: личный API-токен селлера с категорией «Маркетплейс»
(seller.wildberries.ru: Профиль → Настройки → Доступ к API).
Передаётся в Authorization без префикса Bearer.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("wbfbs")

BASE_URL = "https://marketplace-api.wildberries.ru"
SANDBOX_URL = "https://marketplace-api-sandbox.wildberries.ru"

RETRYABLE = {429, 500, 502, 503, 504}
# спека: минимальный интервал 200 мс между запросами
MIN_INTERVAL = 0.21
# допустимые размеры этикеток заказов, мм (спека: только эти пары)
LABEL_SIZES = {(58, 40), (40, 30)}
BULK_LIMIT = 100  # max ID в bulk-методах (добавление в поставку, стикеры)


def keychain_token(item: str) -> str | None:
    """Явное чтение токена из Keychain по имени записи."""
    return _keychain_token(item)


def _keychain_token(item: str | None = None) -> str | None:
    """macOS Keychain fallback. Имя записи — из WB_KEYCHAIN_ITEM или wb-fbs-token."""
    if sys.platform != "darwin":
        return None
    item = item or os.environ.get("WB_KEYCHAIN_ITEM", "wb-fbs-token")
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", item, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None


class WBError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Endpoints:
    ping: str = "/ping"
    new_orders: str = "/api/v3/orders/new"
    orders: str = "/api/v3/orders"
    order_stickers: str = "/api/v3/orders/stickers"
    supplies: str = "/api/v3/supplies"
    supply: str = "/api/v3/supplies/{supply_id}"
    # bulk-методы (старые одиночные удалены WB 17-18.12.2025)
    supply_add_orders: str = "/api/marketplace/v3/supplies/{supply_id}/orders"
    supply_order_ids: str = "/api/marketplace/v3/supplies/{supply_id}/order-ids"
    supply_deliver: str = "/api/v3/supplies/{supply_id}/deliver"
    supply_barcode: str = "/api/v3/supplies/{supply_id}/barcode"
    trbx: str = "/api/v3/supplies/{supply_id}/trbx"
    trbx_stickers: str = "/api/v3/supplies/{supply_id}/trbx/stickers"


EP = Endpoints()


class WBClient:
    def __init__(
        self,
        token: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 5,
    ):
        token = token or os.environ.get("WB_TOKEN") or _keychain_token()
        if not token:
            raise WBError(
                "Нет токена: передай token=, задай WB_TOKEN или положи в Keychain: "
                "security add-generic-password -a wbfbs -s wb-fbs-token -w '<токен>'"
            )
        base_url = base_url or os.environ.get("WB_API_BASE") or BASE_URL
        self.max_retries = max_retries
        self._last_request = 0.0
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": token},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "WBClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        wait = self._last_request + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        delay = 1.0
        last: httpx.Response | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                r = self._http.request(method, path, **kw)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise WBError(f"{method} {path}: сеть — {e}") from e
                log.warning("%s %s: %s, ретрай %d", method, path, e, attempt)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if r.status_code in RETRYABLE and attempt < self.max_retries:
                wait = float(r.headers.get("Retry-After") or delay)
                log.warning(
                    "%s %s -> %d, ретрай %d через %.0fс", method, path, r.status_code, attempt, wait
                )
                time.sleep(wait)
                delay = min(delay * 2, 30)
                last = r
                continue
            if r.is_error:
                # 4XX не ретраим: по спеке WB такой ответ списывает
                # rate-limit как 10 запросов; причина — в теле
                raise WBError(
                    f"{method} {path} -> {r.status_code}: {r.text[:1000]}",
                    status=r.status_code,
                    body=r.text,
                )
            return r
        assert last is not None
        raise WBError(
            f"{method} {path}: ретраи исчерпаны ({last.status_code})",
            status=last.status_code,
            body=last.text,
        )

    def _json(self, method: str, path: str, **kw: Any) -> Any:
        r = self._request(method, path, **kw)
        return r.json() if r.content else None

    # -- сервис -----------------------------------------------------------

    def ping(self) -> dict:
        """Проверка токен+сервис. Лимит: 3 запроса за 30 с."""
        return self._json("GET", EP.ping)

    # -- сборочные задания -------------------------------------------------

    def new_orders(self) -> list[dict]:
        """Новые сборочные задания (без пагинации, все на момент запроса)."""
        return self._json("GET", EP.new_orders).get("orders", [])

    def order_stickers(
        self,
        order_ids: list[int],
        fmt: str = "png",
        width: int = 58,
        height: int = 40,
    ) -> list[dict]:
        """Этикетки заказов: [{orderId, partA, partB, barcode, file(base64)}].

        Только для заказов в статусе confirm/complete (new -> confirm
        происходит автоматически при добавлении заказа в поставку).
        """
        if (width, height) not in LABEL_SIZES:
            raise WBError(f"Размер {width}x{height} не поддерживается WB; допустимо: 58x40, 40x30")
        out: list[dict] = []
        for i in range(0, len(order_ids), BULK_LIMIT):
            chunk = order_ids[i : i + BULK_LIMIT]
            data = self._json(
                "POST",
                EP.order_stickers,
                params={"type": fmt, "width": width, "height": height},
                json={"orders": chunk},
            )
            out.extend(data.get("stickers", []))
        return out

    # -- поставки -----------------------------------------------------------

    def create_supply(self, name: str) -> str:
        if not 1 <= len(name) <= 128:
            raise WBError("Имя поставки: 1..128 символов")
        data = self._json("POST", EP.supplies, json={"name": name})
        return data["id"]

    def list_supplies(self, limit: int = 1000, next_: int = 0) -> dict:
        return self._json("GET", EP.supplies, params={"limit": limit, "next": next_})

    def supply(self, supply_id: str) -> dict:
        return self._json("GET", EP.supply.format(supply_id=supply_id))

    def supply_order_ids(self, supply_id: str) -> list[int]:
        data = self._json("GET", EP.supply_order_ids.format(supply_id=supply_id))
        return data.get("orderIds", [])

    def add_orders_to_supply(self, supply_id: str, order_ids: list[int]) -> list[int]:
        """Bulk-добавление заказов в поставку (чанками по 100).

        Заказы переходят new -> confirm. 409 = смешение cargoType /
        crossBorderType / разные склады / закрытая поставка — весь чанк
        отклонён, id попадают в возвращаемый список failed.
        """
        failed: list[int] = []
        for i in range(0, len(order_ids), BULK_LIMIT):
            chunk = order_ids[i : i + BULK_LIMIT]
            try:
                self._request(
                    "PATCH",
                    EP.supply_add_orders.format(supply_id=supply_id),
                    json={"orders": chunk},
                )
            except WBError as e:
                log.error("чанк из %d заказов не добавлен: %s", len(chunk), e)
                failed.extend(chunk)
        return failed

    def deliver_supply(self, supply_id: str) -> None:
        """Передать поставку в доставку (закрыть). Необратимо.

        409 = маркировка (uin/imei/gtin/sgtin) не закреплена или не прошла
        проверку: тело содержит data.orders[].metaDetails с решением по
        каждому заказу.
        """
        self._request("PATCH", EP.supply_deliver.format(supply_id=supply_id))

    def supply_barcode(self, supply_id: str, fmt: str = "png") -> bytes:
        """QR поставки (580x400). Доступен только ПОСЛЕ deliver, иначе 409."""
        data = self._json(
            "GET", EP.supply_barcode.format(supply_id=supply_id), params={"type": fmt}
        )
        return base64.b64decode(data["file"])

    # -- короба (trbx) --------------------------------------------------------

    def list_boxes(self, supply_id: str) -> list[dict]:
        data = self._json("GET", EP.trbx.format(supply_id=supply_id))
        return data.get("trbxes", []) if data else []

    def add_boxes(self, supply_id: str, amount: int) -> list[str]:
        """Создать короба. Только для поставок с отгрузкой на ПВЗ
        (isPickupPointShipmentAllowed), максимум коробов = товаров + 1."""
        if not 1 <= amount <= 1000:
            raise WBError("amount: 1..1000")
        data = self._json(
            "POST", EP.trbx.format(supply_id=supply_id), json={"amount": amount}
        )
        return data.get("trbxIds", [])

    def delete_boxes(self, supply_id: str, trbx_ids: list[str]) -> None:
        self._request(
            "DELETE", EP.trbx.format(supply_id=supply_id), json={"trbxIds": trbx_ids}
        )

    def box_stickers(self, supply_id: str, trbx_ids: list[str], fmt: str = "png") -> list[dict]:
        """Стикеры коробов (580x400): [{barcode, file(base64)}].

        В элементах нет ID короба — порядок соответствует trbx_ids запроса.
        """
        out: list[dict] = []
        for i in range(0, len(trbx_ids), BULK_LIMIT):
            chunk = trbx_ids[i : i + BULK_LIMIT]
            data = self._json(
                "POST",
                EP.trbx_stickers.format(supply_id=supply_id),
                params={"type": fmt},
                json={"trbxIds": chunk},
            )
            out.extend(data.get("stickers", []))
        return out


def decode_sticker(sticker: dict) -> bytes:
    return base64.b64decode(sticker["file"])
