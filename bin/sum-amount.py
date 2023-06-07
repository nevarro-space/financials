#!/usr/bin/env python3

import sys
from collections import defaultdict
from typing import DefaultDict

category_column_num: int | None = None
if len(sys.argv) > 1:
    category_column_num = int(sys.argv[1])

category_totals: DefaultDict[str, float] = defaultdict(float)
subcategory_totals: DefaultDict[str, DefaultDict[str, float]] = defaultdict(
    lambda: defaultdict(float)
)
total = 0.0
for line in sys.stdin:
    parts = line.strip().split("|")
    amount_field = parts[-2].strip()
    if "=" in amount_field:
        amount_field = amount_field.split("=")[1].strip()
    amount = float(amount_field.strip("-").strip("$").replace(",", ""))
    if amount_field[0] == "-":
        amount *= -1
    total += amount

    if category_column_num is not None:
        category = parts[category_column_num].strip()
        category_parts = category.split("::")
        category_totals[category_parts[0]] += amount
        if len(category_parts) > 1:
            subcategory_totals[category_parts[0]][category_parts[1]] += amount

print(total)
print()
print("Category totals:")
for category, category_total in sorted(category_totals.items(), key=lambda x: -x[1]):
    if category_total > 0:
        print(f"| **{category}** | ${category_total:.2f} |")
    else:
        print(f"| **{category}** | -${-category_total:.2f} |")

    if sub := subcategory_totals.get(category):
        for subcategory, subcategory_total in sorted(sub.items(), key=lambda x: -x[1]):
            if subcategory_total > 0:
                print(f"| + {subcategory} | ${subcategory_total:.2f} |")
            else:
                print(f"| + {subcategory} | -${-subcategory_total:.2f} |")
