#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ataix_lab08.py — Автоматизация ЛР для ATAIX.

Функционал:
- Читает баланс USDT из /api/user/balances/{currency}, поле result.available
- Загружает /api/symbols, строго ищет запись по symbol==PAIR (например, TRX/USDT)
- Получает цену: bid/last/price из /api/symbols, иначе берет top bid из /api/cmc/v1/orderbook/{pair}
- Считает 3 лимитных BUY (-2%, -5%, -8%) ИЛИ одну покупку, если средств мало
- Округляет price по pricePrecision, quantity по lotSize (floor)
- Пропускает слишком маленькие заявки (проверки: lotSize, minQty, minNotional)
- side строго в нижнем регистре ('buy'/'sell'), type='limit'
- Сохраняет ответы и состояние в orders.json
- Делает один проход проверки статусов и при FILLED создает SELL +2%
- Умеет извлекать orderID из верхнего уровня или result.orderID
- Имеется заготовка для отмены незаполненных ордеров (отключена по умолчанию)

Запуск:
  python ataix_lab08.py --api-key ABC... --symbol TRX/USDT --usdt-amount 1.85 --out orders.json
"""
import argparse
import time
import json
import math
from typing import Optional, Dict, Any, Tuple
import requests

# ====== Константы ======
BASE_URL = "https://api.ataix.kz"
SAVE_FILE_DEFAULT = "orders.json"
REQUEST_TIMEOUT = 15  # seconds
RETRY_DELAY = 0.5

# ====== HTTP / Вспомогательное ======
def try_request(method: str, path: str, api_key: Optional[str], json_body=None, params=None, extra_headers=None) -> Tuple[Optional[requests.Response], Dict[str, str]]:
    """
    Универсальный HTTP-вызов с перебором популярных вариантов авторизации.
    Возвращает (response, headers_used).
    """
    url = BASE_URL.rstrip("/") + "/" + path.lstrip("/")
    base_extra = (extra_headers or {}).copy()

    headers_variants = []
    if api_key:
        headers_variants.append({**base_extra, "X-API-KEY": api_key})
        headers_variants.append({**base_extra, "Authorization": f"Bearer {api_key}"})
        headers_variants.append({**base_extra, "api_key": api_key})
        headers_variants.append({**base_extra, "Api-Key": api_key})
    # последний вариант — без ключа (для публичных эндпоинтов)
    headers_variants.append({**base_extra})

    for headers in headers_variants:
        try:
            if method.upper() == "GET":
                resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            elif method.upper() == "POST":
                resp = requests.post(url, headers=headers, json=json_body, params=params, timeout=REQUEST_TIMEOUT)
            elif method.upper() == "DELETE":
                resp = requests.delete(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            else:
                raise ValueError(f"Unsupported method: {method}")
        except requests.RequestException as e:
            print(f"[WARN] Сетевая ошибка {method} {url}: {e}")
            time.sleep(RETRY_DELAY)
            continue
        return resp, headers
    return None, {}

def load_saved(filename: str) -> Dict[str, Any]:
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"orders": []}
    except Exception as e:
        print(f"[ERROR] Не удалось прочитать {filename}: {e}")
        return {"orders": []}

def save_saved(filename: str, data: Dict[str, Any]):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ====== Обёртки API ======
def get_balance(api_key: str, currency: str = "USDT") -> Dict[str, Any]:
    """
    /api/user/balances/{currency}
    Ожидается ответ: {"status": true, "result": {"available": "1.23", ...}}
    """
    path = f"/api/user/balances/{currency}"
    resp, _ = try_request("GET", path, api_key)
    if resp is None:
        raise RuntimeError("Network error when requesting balance")
    if resp.status_code in (401, 403):
        raise PermissionError(f"Permission denied when requesting balance (HTTP {resp.status_code}). Проверь разрешение DATA у ключа.")
    if not resp.ok:
        raise RuntimeError(f"Error fetching balance: {resp.status_code} {resp.text}")
    return resp.json()

def extract_available_usdt(balance_json: Dict[str, Any], fallback: float) -> float:
    """
    Корректно извлекаем result.available (строка -> float).
    Если нет — используем fallback (из аргумента --usdt-amount).
    """
    if isinstance(balance_json, dict):
        res = balance_json.get("result") or {}
        if "available" in res:
            try:
                return float(res["available"])
            except:
                pass
        for key in ("free", "total"):
            if key in res:
                try:
                    return float(res[key])
                except:
                    pass
    print("[WARN] Не удалось автоматически прочитать available из ответа баланса. Использую значение из --usdt-amount.")
    return float(fallback)

def get_symbols(api_key: Optional[str]) -> Dict[str, Any]:
    """
    /api/symbols — список символов c параметрами (pricePrecision, lotSize, minQty, minNotional и т.п.)
    """
    path = "/api/symbols"
    resp, _ = try_request("GET", path, api_key)
    if resp is None:
        raise RuntimeError("Network error when requesting symbols")
    if not resp.ok:
        raise RuntimeError(f"Error fetching symbols: {resp.status_code} {resp.text}")
    return resp.json()

def find_symbol_record(symbols_json: Dict[str, Any], pair: str) -> Dict[str, Any]:
    """
    Возвращает запись из result[], где symbol == pair (например, 'TRX/USDT').
    """
    entries = symbols_json.get("result") if isinstance(symbols_json, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("Неожиданный формат /api/symbols (ожидался {'result': [...]})")
    for e in entries:
        if isinstance(e, dict) and e.get("symbol") == pair:
            return e
    raise RuntimeError(f"Пара {pair} не найдена в /api/symbols")

def get_symbol_limits(srec: Dict[str, Any]) -> Dict[str, float]:
    """
    Возвращает лимиты инструмента: lotSize (шаг количества),
    minQty (минимум по количеству), minNotional (минимум по сумме сделки, в QUOTE).
    Если каких-то полей нет — задаём мягкие дефолты.
    """
    lot_size = float(srec.get("lotSize", 0.000001))      # шаг количества
    # у некоторых реализаций lotSize = minQty; если minQty нет, берём lotSize
    min_qty = float(srec.get("minQty", lot_size))        # минимум по количеству
    # разумный дефолт для минимальной суммы (0.5 USDT); при наличии поля — используем его
    min_notional = float(srec.get("minNotional", 0.5))   # минимум по сумме в QUOTE
    return {"lotSize": lot_size, "minQty": min_qty, "minNotional": min_notional}

def get_public_best_bid(pair: str) -> float:
    """
    Публичный стакан: /api/cmc/v1/orderbook/{pair}
    Ожидается: {"status": true, "result": {"bids": [[price, qty], ...], "asks": ...}}
    """
    path = f"/api/cmc/v1/orderbook/{pair}"
    resp, _ = try_request("GET", path, None)
    if resp is None or not resp.ok:
        raise RuntimeError(f"Не удалось получить публичный ордербук для {pair}: {None if resp is None else resp.text}")
    data = resp.json()
    res = data.get("result", {})
    bids = res.get("bids") or []
    if not bids:
        raise RuntimeError(f"В публичном стакане нет заявок на покупку для {pair}")
    top = bids[0]
    if isinstance(top, list) and len(top) >= 1:
        return float(top[0])
    if isinstance(top, dict) and "price" in top:
        return float(top["price"])
    raise RuntimeError("Неожиданный формат bids[0] в публичном стакане")

def find_best_bid_price(api_key: Optional[str], pair: str) -> float:
    """
    1) Пытается взять bid/last/price из /api/symbols для конкретного pair.
    2) Если в /api/symbols цены нет — берёт bid из публичного ордербука.
    """
    symbols = get_symbols(api_key)
    srec = find_symbol_record(symbols, pair)
    for key in ("bid", "bestBid", "last", "price"):
        v = srec.get(key)
        if v is not None:
            try:
                price = float(v)
                if price > 0:
                    return price
            except:
                pass
    return get_public_best_bid(pair)

def extract_order_id(resp_json: Dict[str, Any]) -> Optional[str]:
    """
    Унифицированно вытаскивает идентификатор ордера из ответа:
    - orderID / orderId / id / clientOrderId / dataId
    - те же поля внутри result.{...}
    """
    if not isinstance(resp_json, dict):
        return None
    for key in ("orderID", "orderId", "id", "clientOrderId", "dataId"):
        if key in resp_json and resp_json[key]:
            return str(resp_json[key])
    res = resp_json.get("result")
    if isinstance(res, dict):
        for key in ("orderID", "orderId", "id", "clientOrderId", "dataId"):
            if key in res and res[key]:
                return str(res[key])
    return None

def place_order(api_key: str, symbol: str, side: str, price: float, quantity: float, srec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Создание ордера: POST /api/orders
    - side в нижнем регистре ('buy'/'sell')
    - type='limit'
    - price округляется по pricePrecision
    - quantity приводится к шагу lotSize (floor)
    """
    path = "/api/orders"

    price_prec = int(srec.get("pricePrecision", 8))
    lot_size = float(srec.get("lotSize", 0.00000001))

    p = float(f"{price:.{price_prec}f}")
    steps = int(quantity / lot_size)
    q = steps * lot_size
    q = float(f"{q:.8f}")

    body = {
        "symbol": symbol,
        "side": side.lower(),      # ВАЖНО: нижний регистр
        "type": "limit",
        "price": str(p),
        "quantity": str(q)
    }

    resp, _ = try_request("POST", path, api_key, json_body=body)
    if resp is None:
        raise RuntimeError("Network error when placing order")
    if resp.status_code in (401, 403):
        raise PermissionError(f"Permission denied when placing order ({resp.status_code}): {resp.text}")
    if not resp.ok:
        raise RuntimeError(f"Order rejected {resp.status_code}: {resp.text}")

    try:
        return resp.json()
    except Exception:
        return {"raw_text": resp.text}

def get_order_status(api_key: str, order_id: str) -> Dict[str, Any]:
    """
    Проверка статуса (перебор вероятных путей).
    """
    candidates = [
        f"/api/orders/{order_id}",
        f"/api/user/orders/{order_id}",
        f"/api/orders?id={order_id}"
    ]
    for p in candidates:
        resp, _ = try_request("GET", p, api_key)
        if resp is None:
            continue
        if resp.status_code in (401, 403):
            raise PermissionError(f"Permission denied when checking order {order_id}: {resp.status_code}")
        if resp.ok:
            try:
                return resp.json()
            except:
                return {"raw_text": resp.text}
    raise RuntimeError(f"Не удалось получить статус ордера {order_id}")

# ====== Отмена ордеров (опционально) ======
def cancel_order(api_key: str, order_id: str) -> Dict[str, Any]:
    """
    Отмена ордера. Часто DELETE /api/orders/{id} либо /api/user/orders/{id}.
    """
    paths = [f"/api/orders/{order_id}", f"/api/user/orders/{order_id}"]
    last_err = None
    for p in paths:
        resp, _ = try_request("DELETE", p, api_key)
        if resp is not None and resp.ok:
            try:
                return resp.json()
            except:
                return {"raw_text": resp.text}
        last_err = None if resp is None else resp.text
    raise RuntimeError(f"Не удалось отменить ордер {order_id}: {last_err}")

def cancel_all_new_buys(api_key: str, saved: Dict[str, Any]):
    """
    Отменяет все buy-ордера в статусах NEW/OPEN.
    """
    for o in saved.get("orders", []):
        if o.get("side") == "buy" and o.get("status") in (None, "NEW", "OPEN") and o.get("order_id"):
            try:
                r = cancel_order(api_key, o["order_id"])
                print(f"[CANCEL] Отменён {o['order_id']}: {r}")
            except Exception as e:
                print(f"[WARN] Не удалось отменить {o['order_id']}: {e}")

# ====== Бизнес-логика ЛР ======
def run_lab(api_key: str, pair: str, usdt_amount: float, out_file: str) -> Dict[str, Any]:
    print("[INFO] Получаем баланс USDT...")
    bal_json = get_balance(api_key, "USDT")
    print("[INFO] Баланс (raw):", bal_json)
    available_usdt = extract_available_usdt(bal_json, fallback=usdt_amount)
    print(f"[INFO] Доступно USDT: {available_usdt}")

    use_usdt = min(available_usdt, usdt_amount)
    if use_usdt <= 0:
        raise RuntimeError("Недостаточно USDT для выставления ордеров.")

    print(f"[INFO] Получаем параметры символа и цену для {pair} ...")
    symbols = get_symbols(api_key)
    srec = find_symbol_record(symbols, pair)
    limits = get_symbol_limits(srec)
    best_bid = find_best_bid_price(api_key, pair)
    print(f"[INFO] Лучшая цена покупки (best bid) для {pair}: {best_bid}")

    # Логика распределения: 3 заявки или 1, если средств мало
    deltas = [0.98, 0.95, 0.92]
    per_order_usdt = use_usdt / 3.0
    if per_order_usdt < limits["minNotional"]:
        # слишком мало для трёх — поставим одну «консервативную» заявку
        deltas = [0.98]
        per_order_usdt = use_usdt
        print(f"[INFO] Недостаточно средств для 3 заявок. Переключаюсь на 1 заявку всей суммой: ~{per_order_usdt:.8f} USDT")

    # Формируем кандидатов
    to_place = []
    for d in deltas:
        price = best_bid * d
        qty_raw = per_order_usdt / price

        # Фильтры минимальных лимитов инструмента
        if (qty_raw < limits["minQty"]) or (per_order_usdt < limits["minNotional"]):
            print(f"[SKIP] Слишком маленькая заявка: price≈{price:.10f}, qty≈{qty_raw:.10f}, "
                  f"usdt≈{per_order_usdt:.8f} (minQty={limits['minQty']}, minNotional={limits['minNotional']}). Пропускаю.")
            continue

        to_place.append((price, qty_raw))

    saved = load_saved(out_file)
    if "orders" not in saved:
        saved["orders"] = []

    # Если нужно освободить средства — включи строку ниже:
    #cancel_all_new_buys(api_key, saved)

    print(f"[INFO] Выставляем {len(to_place)} лимитных покупок для {pair} ...")
    for price, qty in to_place:
        try:
            resp_json = place_order(api_key, pair, "buy", price, qty, srec)
            order_id = extract_order_id(resp_json)

            entry = {
                "side": "buy",
                "price": float(f"{price:.10f}"),
                "quantity": float(f"{qty:.10f}"),
                "pair": pair,
                "order_id": order_id,
                "status": "NEW",
                "created_raw_response": resp_json,
                "linked_sell_order": None,
                "created_at": int(time.time())
            }
            saved["orders"].append(entry)
            save_saved(out_file, saved)
            print(f"[OK] Покупка выставлена. order_id={entry['order_id']} price={entry['price']} qty≈{entry['quantity']}")
        except PermissionError:
            raise
        except Exception as e:
            print(f"[ERROR] Не удалось выставить покупку: {e}")

    print("[INFO] Сохраняем результаты в", out_file)

    # Один проход проверки статусов (для отчёта)
    print("[INFO] Проверяем статусы buy-ордеров (один проход)...")
    for idx, entry in enumerate(saved["orders"]):
        if entry["side"] != "buy":
            continue
        if entry.get("status") in ("FILLED", "CLOSED"):
            continue
        oid = entry.get("order_id")
        if not oid:
            print(f"[WARN] У ордера нет order_id, пропускаем: {entry.get('created_raw_response')}")
            continue
        try:
            st = get_order_status(api_key, oid)
            status = None
            filled_amount = None
            avg_price = None
            if isinstance(st, dict):
                status = st.get("status") or st.get("orderStatus")
                avg_price = st.get("avgPrice") or st.get("averagePrice")
                filled_amount = st.get("filledAmount") or st.get("filledQty") or st.get("filled")
                if "result" in st and isinstance(st["result"], dict):
                    status = st["result"].get("status") or status
                    avg_price = st["result"].get("avgPrice") or avg_price
                    filled_amount = st["result"].get("filledAmount") or filled_amount

            norm = (status or "").lower()
            if norm in ("filled", "done", "closed", "executed"):
                entry["status"] = "FILLED"
            elif norm in ("new", "open", "partially_filled", "partiallyfilled"):
                entry["status"] = norm.upper()
            else:
                try:
                    if filled_amount is not None and float(filled_amount) >= float(entry["quantity"]) - 1e-9:
                        entry["status"] = "FILLED"
                    else:
                        entry["status"] = "NEW"
                except:
                    entry["status"] = "NEW"

            entry["status_raw_response"] = st
            saved["orders"][idx] = entry
            save_saved(out_file, saved)
            print(f"[INFO] Order {oid} status -> {entry['status']}")

            # Если покупка FILLED — создаём продажу +2%
            if entry["status"] == "FILLED" and not entry.get("linked_sell_order"):
                bought_price = float(avg_price) if avg_price else float(entry["price"])
                sell_price = bought_price * 1.02
                sell_qty = float(entry["quantity"])

                try:
                    sell_resp = place_order(api_key, pair, "sell", sell_price, sell_qty, srec)
                    sell_id = extract_order_id(sell_resp)

                    sell_entry = {
                        "side": "sell",
                        "price": float(f"{sell_price:.10f}"),
                        "quantity": float(f"{sell_qty:.10f}"),
                        "pair": pair,
                        "order_id": sell_id,
                        "status": "NEW",
                        "created_raw_response": sell_resp,
                        "linked_buy_order": entry.get("order_id"),
                        "created_at": int(time.time())
                    }
                    saved["orders"].append(sell_entry)
                    entry["linked_sell_order"] = sell_entry["order_id"]
                    saved["orders"][idx] = entry
                    save_saved(out_file, saved)
                    print(f"[OK] Создан ордер на продажу: order_id={sell_entry['order_id']} price={sell_entry['price']}")
                except Exception as e:
                    print(f"[ERROR] Не удалось создать продажу: {e}")

        except PermissionError:
            raise
        except Exception as e:
            print(f"[ERROR] Ошибка при проверке статуса ордера {oid}: {e}")

    print("[DONE] Один проход проверки завершён. Для постоянного мониторинга — запускайте периодически (или в цикле).")
    return saved

# ====== CLI ======
def main():
    parser = argparse.ArgumentParser(description="ATAIX Lab08 automation (исправленная версия с лимитами)")
    parser.add_argument("--api-key", required=True, help="Ваш API ключ ATAIX")
    parser.add_argument("--symbol", required=False, default="TRX/USDT", help="Пара вида BASE/QUOTE, по умолчанию TRX/USDT")
    parser.add_argument("--usdt-amount", type=float, required=False, default=10.0, help="Сколько USDT использовать (лимит расходов)")
    parser.add_argument("--out", default=SAVE_FILE_DEFAULT, help="Файл для сохранения ордеров JSON")
    args = parser.parse_args()

    try:
        result = run_lab(args.api_key, args.symbol, args.usdt_amount, args.out)
        print("[RESULT] Сохранённые записи:", json.dumps(result, ensure_ascii=False, indent=2)[:2000])
    except PermissionError as e:
        print("[FATAL] Permission error:", e)
        print("Подсказка: добавьте разрешение DATA для вашего API-ключа в кабинете ATAIX.")
    except Exception as e:
        print("[FATAL] Ошибка:", e)

if __name__ == "__main__":
    main()
