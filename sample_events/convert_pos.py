"""
Convert pos_transactions.csv (Purplle format) to normalised JSON records
suitable for seeding the API database directly.

Usage:
    python sample_events/convert_pos.py \
        --input data/pos_transactions.csv \
        --output data/pos_normalised.json

The normalised format groups line items into baskets by (store_id, order_date, order_time).
Each basket becomes one transaction record with total basket_value_inr.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime


def convert(input_path: str, output_path: str) -> None:
    baskets: dict[tuple, list[dict]] = defaultdict(list)

    with open(input_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (
                row["store_id"].strip(),
                row["order_date"].strip(),
                row["order_time"].strip(),
            )
            baskets[key].append({
                "product_id": row.get("product_id", "").strip(),
                "brand_name": row.get("brand_name", "").strip(),
                "line_amount": float(row.get("total_amount", 0) or 0),
            })

    transactions = []
    for (store_id, order_date, order_time), items in baskets.items():
        try:
            ts = datetime.strptime(f"{order_date} {order_time}", "%d-%m-%Y %H:%M:%S")
        except ValueError:
            continue

        basket_value = round(sum(i["line_amount"] for i in items), 2)
        transactions.append({
            "store_id": store_id,
            "transaction_ts": ts.isoformat(),
            "basket_value_inr": basket_value,
            "item_count": len(items),
            "brands": list({i["brand_name"] for i in items if i["brand_name"]}),
            "line_items": items,
        })

    transactions.sort(key=lambda t: t["transaction_ts"])

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(transactions, f, indent=2)

    print(f"Converted {len(transactions)} transactions from {len(baskets)} basket groups → {output_path}")
    print(f"Total basket value: ₹{sum(t['basket_value_inr'] for t in transactions):,.2f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/pos_transactions.csv")
    parser.add_argument("--output", default="data/pos_normalised.json")
    args = parser.parse_args()
    convert(args.input, args.output)


if __name__ == "__main__":
    main()
