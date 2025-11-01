import json, os

PATH = "orders.json"

with open(PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

changed = 0
for o in data.get("orders", []):
    if not o.get("order_id"):
        # orderID может лежать здесь:
        rid = None
        cr = o.get("created_raw_response", {})
        if isinstance(cr, dict):
            # прямой уровень
            rid = cr.get("orderID") or cr.get("orderId")
            # внутри result
            if not rid and isinstance(cr.get("result"), dict):
                rid = cr["result"].get("orderID") or cr["result"].get("orderId")
        if rid:
            o["order_id"] = str(rid)
            changed += 1

with open(PATH, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"Patched orders: {changed}")
