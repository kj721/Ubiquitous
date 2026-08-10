"""
End-to-end Pandas walkthrough: read -> inspect -> clean -> group -> merge -> reshape -> export
Run:  python3 full_pipeline.py
"""
import pandas as pd

# 1. READ
df = pd.read_csv("orders_messy.csv", parse_dates=["order_date"])

# 2. INSPECT (uncomment to explore)
# print(df.shape); print(df.isna().sum()); print(df["city"].value_counts())

# 3. CLEAN
clean = df.copy()
clean["city"] = clean["city"].str.strip().str.title()
clean["revenue"] = (
    clean["revenue"]
    .str.replace("$", "", regex=False)
    .str.replace(",", "", regex=False)
    .astype("float")
)
clean = clean.dropna(subset=["city"])
clean["revenue"] = clean["revenue"].fillna(0)
clean = clean.sort_values("revenue", ascending=False)
clean = clean.drop_duplicates(subset=["order_id"], keep="first")
clean = clean.sort_values("order_id").reset_index(drop=True)
clean["margin"] = clean["revenue"] - clean["cost"]

# 4. GROUP
by_city = clean.groupby("city", as_index=False).agg(
    n_orders=("order_id", "count"),
    total_revenue=("revenue", "sum"),
    total_margin=("margin", "sum"),
)

# 5. MERGE
regions = pd.read_csv("regions.csv")
enriched = by_city.merge(regions, on="city", how="left")

# 6. RESHAPE (long -> wide): region x month grid of revenue
clean["month"] = clean["order_date"].dt.month_name()
wide = clean.merge(regions, on="city", how="left").pivot_table(
    index="region", columns="month", values="revenue", aggfunc="sum", fill_value=0
)

# 7. EXPORT
clean.to_csv("orders_clean.csv", index=False)
enriched.to_csv("city_summary.csv", index=False)

print("=== enriched city summary ===")
print(enriched)
print("\n=== reshaped (region x month revenue) ===")
print(wide)
