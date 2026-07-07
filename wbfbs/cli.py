"""CLI: полный цикл сборки FBS-поставки WB.

    export WB_TOKEN=...
    wbfbs ping                                          # проверка токена
    wbfbs orders new                                    # что собирать
    wbfbs supply create --name "Поставка 06.07"        # -> WB-GI-...
    wbfbs supply add WB-GI-123 --orders 111,222,333    # bulk, new -> confirm
    wbfbs labels --orders 111,222,333 -o labels.pdf     # этикетки заказов
    wbfbs boxes create WB-GI-123 --count 3              # короба (для ПВЗ)
    wbfbs boxes stickers WB-GI-123 -o boxes.pdf         # стикеры коробов
    wbfbs supply deliver WB-GI-123                      # закрыть (необратимо)
    wbfbs supply qr WB-GI-123 -o supply_qr.pdf          # QR — только после deliver

Порядок именно такой: WB отдаёт QR поставки только после передачи в доставку.
Раскладку заказов по коробам API не поддерживает (спека 2026-07) — только
создание коробов и печать их стикеров.
"""

from __future__ import annotations

import json
import logging
import sys

import click

from . import pdf as pdfmod
from .client import WBClient, WBError, decode_sticker

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

SIZES = {"58x40": (58, 40), "40x30": (40, 30)}


def _ids(csv: str) -> list[int]:
    return [int(x) for x in csv.replace(" ", "").split(",") if x]


def _client() -> WBClient:
    try:
        return WBClient()
    except WBError as e:
        raise click.ClickException(str(e))


@click.group()
def main() -> None:
    """Сборка FBS-поставок Wildberries: заказы, короба, этикетки, QR."""


@main.command()
def ping() -> None:
    """Проверить токен и доступность API (лимит 3 запроса / 30 с)."""
    with _client() as wb:
        click.echo(json.dumps(wb.ping(), ensure_ascii=False))


# -- orders -------------------------------------------------------------------

@main.group()
def orders() -> None:
    """Сборочные задания."""


@orders.command("new")
def orders_new() -> None:
    """Новые сборочные задания."""
    with _client() as wb:
        rows = wb.new_orders()
    if not rows:
        click.echo("Новых заказов нет")
        return
    for o in rows:
        meta = ",".join(o.get("requiredMeta") or [])
        click.echo(
            f"{o.get('id')}\t{o.get('createdAt', '')}\t{o.get('article', '')}"
            f"\t{o.get('price', '')}" + (f"\tмаркировка:{meta}" if meta else "")
        )
    click.echo(f"-- всего: {len(rows)}")


@main.command("labels")
@click.option("--orders", "orders_csv", required=True, help="ID заказов через запятую")
@click.option("-o", "--out", default="labels.pdf", show_default=True)
@click.option(
    "--size", type=click.Choice(sorted(SIZES)), default="58x40", show_default=True,
    help="размер этикетки, мм (только эти пары поддерживает WB)",
)
@click.option("--a4", is_flag=True, help="сеткой на A4 вместо страницы-на-этикетку")
def labels(orders_csv: str, out: str, size: str, a4: bool) -> None:
    """Этикетки заказов -> PDF. Заказы должны быть в статусе confirm
    (добавлены в поставку) — для new WB этикетки не отдаёт."""
    ids = _ids(orders_csv)
    label_mm = SIZES[size]
    with _client() as wb:
        stickers = wb.order_stickers(ids, width=label_mm[0], height=label_mm[1])
    pngs = [decode_sticker(s) for s in stickers]
    fn = pdfmod.stickers_to_a4_pdf if a4 else pdfmod.stickers_to_pdf
    path = fn(pngs, out, label_mm=label_mm)
    click.echo(f"{len(pngs)} этикеток -> {path}")


# -- supply -------------------------------------------------------------------

@main.group()
def supply() -> None:
    """Поставки."""


@supply.command("create")
@click.option("--name", required=True)
def supply_create(name: str) -> None:
    with _client() as wb:
        sid = wb.create_supply(name)
    click.echo(sid)


@supply.command("list")
def supply_list() -> None:
    with _client() as wb:
        data = wb.list_supplies()
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


@supply.command("orders")
@click.argument("supply_id")
def supply_orders_cmd(supply_id: str) -> None:
    """ID заказов в поставке."""
    with _client() as wb:
        ids = wb.supply_order_ids(supply_id)
    click.echo(",".join(map(str, ids)) if ids else "пусто")


@supply.command("add")
@click.argument("supply_id")
@click.option("--orders", "orders_csv", required=True, help="ID заказов через запятую")
def supply_add(supply_id: str, orders_csv: str) -> None:
    """Массово добавить заказы в поставку (bulk по 100, new -> confirm)."""
    ids = _ids(orders_csv)
    with _client() as wb:
        failed = wb.add_orders_to_supply(supply_id, ids)
    click.echo(f"добавлено {len(ids) - len(failed)}/{len(ids)}")
    if failed:
        click.echo(f"не добавлены: {','.join(map(str, failed))}", err=True)
        click.echo(
            "409 обычно значит: смешение габаритных типов/складов или закрытая поставка",
            err=True,
        )
        sys.exit(1)


@supply.command("deliver")
@click.argument("supply_id")
@click.confirmation_option(prompt="Передать поставку в доставку? Это необратимо")
def supply_deliver(supply_id: str) -> None:
    """Закрыть поставку. После этого доступен QR (supply qr)."""
    with _client() as wb:
        try:
            wb.deliver_supply(supply_id)
        except WBError as e:
            if e.status == 409:
                raise click.ClickException(
                    f"Маркировка не прошла проверку, детали от WB:\n{e.body[:2000]}"
                )
            raise
    click.echo("передана в доставку; теперь можно печатать QR: wbfbs supply qr " + supply_id)


@supply.command("qr")
@click.argument("supply_id")
@click.option("-o", "--out", default="supply_qr.pdf", show_default=True)
def supply_qr(supply_id: str, out: str) -> None:
    """QR поставки -> PDF (A6). Доступен только ПОСЛЕ supply deliver."""
    with _client() as wb:
        try:
            png = wb.supply_barcode(supply_id)
        except WBError as e:
            if e.status == 409:
                raise click.ClickException(
                    "409: QR доступен только после передачи поставки в доставку "
                    f"(wbfbs supply deliver {supply_id})"
                )
            raise
    pdfmod.stickers_to_pdf([png], out, label_mm=(105, 148))
    click.echo(out)


# -- boxes --------------------------------------------------------------------

@main.group()
def boxes() -> None:
    """Короба (trbx). Только для поставок с отгрузкой на ПВЗ."""


@boxes.command("create")
@click.argument("supply_id")
@click.option("--count", type=click.IntRange(1, 1000), required=True)
def boxes_create(supply_id: str, count: int) -> None:
    """Создать короба (макс. = число товаров в поставке + 1)."""
    with _client() as wb:
        ids = wb.add_boxes(supply_id, count)
    for tid in ids:
        click.echo(tid)


@boxes.command("list")
@click.argument("supply_id")
def boxes_list(supply_id: str) -> None:
    with _client() as wb:
        rows = wb.list_boxes(supply_id)
    for b in rows:
        click.echo(b.get("id", ""))
    click.echo(f"-- всего: {len(rows)}")


@boxes.command("delete")
@click.argument("supply_id")
@click.option("--box", "trbx_csv", required=True, help="ID коробов через запятую")
def boxes_delete(supply_id: str, trbx_csv: str) -> None:
    with _client() as wb:
        wb.delete_boxes(supply_id, [x for x in trbx_csv.replace(" ", "").split(",") if x])
    click.echo("удалены")


@boxes.command("stickers")
@click.argument("supply_id")
@click.option("--box", "trbx_csv", default="", help="ID коробов через запятую; пусто = все")
@click.option("-o", "--out", default="boxes.pdf", show_default=True)
def boxes_stickers(supply_id: str, trbx_csv: str, out: str) -> None:
    """Стикеры коробов -> PDF (580x400, размер фиксирован WB)."""
    with _client() as wb:
        if trbx_csv:
            trbx_ids = [x for x in trbx_csv.replace(" ", "").split(",") if x]
        else:
            trbx_ids = [b["id"] for b in wb.list_boxes(supply_id) if b.get("id")]
        if not trbx_ids:
            raise click.ClickException("Не передан --box и в поставке нет коробов")
        stickers = wb.box_stickers(supply_id, trbx_ids)
    pngs = [decode_sticker(s) for s in stickers]
    pdfmod.stickers_to_pdf(pngs, out)
    click.echo(f"{len(pngs)} стикеров -> {out}")


# -- sandbox --------------------------------------------------------------

@main.group()
def sandbox() -> None:
    """Песочница WB: эмуляция полного цикла без боевого кабинета."""


@sandbox.command("cycle")
@click.option(
    "--keychain-item", default="wb-fbs-token-2", show_default=True,
    help="Keychain-запись с ТЕСТОВЫМ токеном (тип «Тестовый контур»)",
)
@click.option("--out-dir", default=".", show_default=True)
@click.option("--skip-make", is_flag=True, help="остановиться перед созданием тестовых заказов")
def sandbox_cycle(keychain_item: str, out_dir: str, skip_make: bool) -> None:
    """Полный цикл: карточка -> остатки -> цена -> тестовый заказ ->
    поставка -> этикетки -> короб -> стикеры -> deliver -> QR -> close."""
    import os
    import time as _t

    from .client import keychain_token
    from .sandbox import ContentSandbox, PricesSandbox, SandboxClient

    token = os.environ.get("WB_TEST_TOKEN") or keychain_token(keychain_item)
    if not token:
        raise click.ClickException(f"Тестовый токен не найден (Keychain: {keychain_item})")

    wb = SandboxClient(token=token)
    content = ContentSandbox(token=token)
    prices = PricesSandbox(token=token)

    def step(msg: str) -> None:
        click.echo(click.style(f"-> {msg}", bold=True))

    try:
        # 1. склад
        step("склад FBS в песочнице")
        whs = wb.warehouses()
        if not whs:
            offices = wb.offices()
            if not offices:
                raise click.ClickException("В песочнице нет офисов WB — некуда привязать склад")
            office = offices[0]
            wh = wb.create_warehouse("wbfbs-test", office.get("id"))
            click.echo(f"   создан склад: {wh}")
            whs = wb.warehouses()
        wh_id = whs[0]["id"]
        click.echo(f"   склад id={wh_id}")

        # 2. карточка с баркодом
        step("карточка в песочнице контента")
        cards = content.list_cards()
        if not cards:
            subject = content.find_subject()
            barcode = content.generate_barcodes(1)[0]
            content.upload_card(subject, "wbfbs-test-1", barcode, "Тестовая футболка wbfbs")
            click.echo(f"   карточка отправлена (barcode={barcode}), жду появления…")
            for _ in range(12):
                _t.sleep(5)
                cards = content.list_cards()
                if cards:
                    break
        if not cards:
            raise click.ClickException(
                "Карточка не появилась в списке — проверь ошибки: /content/v2/cards/error/list"
            )
        card = cards[0]
        nm_id = card["nmID"]
        size = card["sizes"][0]
        chrt_id = size["chrtID"]
        sku = size["skus"][0]
        click.echo(f"   nmID={nm_id} chrtID={chrt_id} sku={sku}")

        # 3. остатки и цена
        step("остаток на складе (по chrtId — миграция 02.2026)")
        wb.set_stocks(wh_id, [{"chrtId": chrt_id, "amount": 10}])
        step("цена")
        try:
            prices.set_price(nm_id, 1000)
        except WBError as e:
            if "already set" not in (e.body or ""):
                raise
            click.echo("   цена уже установлена (повторный прогон)")

        if skip_make:
            click.echo("остановлено перед make (--skip-make)")
            return

        # 4. тестовые заказы
        step("тестовые сборочные задания (make)")
        wb.make_test_orders([{"sku": sku, "amount": 1}])
        _t.sleep(3)
        orders = wb.new_orders()
        if not orders:
            raise click.ClickException("make прошёл, но orders/new пуст — подожди и повтори")
        ids = [o["id"] for o in orders]
        click.echo(f"   заказы: {ids}")

        # 5. поставка + этикетки
        step("поставка")
        sid = wb.create_supply("wbfbs full cycle")
        failed = wb.add_orders_to_supply(sid, ids)
        click.echo(f"   {sid}, добавлено {len(ids) - len(failed)}/{len(ids)}")
        step("этикетки заказов -> PDF")
        stickers = wb.order_stickers(ids)
        from .pdf import stickers_to_pdf
        p = stickers_to_pdf([decode_sticker(s) for s in stickers], f"{out_dir}/sandbox_labels.pdf")
        click.echo(f"   {p}")

        # 6. короба
        step("короб + стикер -> PDF")
        trbx = wb.add_boxes(sid, 1)
        bst = wb.box_stickers(sid, trbx)
        p = stickers_to_pdf([decode_sticker(s) for s in bst], f"{out_dir}/sandbox_boxes.pdf")
        click.echo(f"   {trbx} -> {p}")

        # 7. deliver + QR
        step("передача в доставку + QR -> PDF")
        wb.deliver_supply(sid)
        png = wb.supply_barcode(sid)
        stickers_to_pdf([png], f"{out_dir}/sandbox_supply_qr.pdf", label_mm=(105, 148))
        click.echo(f"   {out_dir}/sandbox_supply_qr.pdf")

        # 8. эмуляция приёмки WB
        step("эмуляция: закрытие поставки (sorted) -> ПВЗ -> выдан")
        wb.test_close_supply(sid)
        for oid in ids:
            wb.test_deliver(oid)
            wb.test_receive(oid)
        click.echo(click.style("ПОЛНЫЙ ЦИКЛ ПРОЙДЕН", fg="green", bold=True))
    finally:
        wb.close(); content.close(); prices.close()


if __name__ == "__main__":
    main()
