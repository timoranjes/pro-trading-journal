import json
from datetime import datetime, timezone

tracker_path = "/Users/zichengliao/.hermes/data/passive-income-tracker.json"

with open(tracker_path) as f:
    tracker = json.load(f)

tracker["projects"].append({
    "id": "proj-002",
    "name": "Freelancer Finance Dashboard & Tax Estimator",
    "category": "spreadsheet-template",
    "status": "deployed",
    "evidence": {
        "competitors": [
            {"name": "Ultimate Annual Budget Spreadsheet", "platform": "Etsy", "price": "$11.95", "reviews": "10.3k", "url": "https://www.etsy.com/listing/1826328945"},
            {"name": "PLR Spreadsheet Bundle", "platform": "Etsy", "price": "£33.66", "reviews": "1,484 seller reviews", "url": "https://www.etsy.com/uk/listing/1557400138"}
        ],
        "market_size": "Budget/finance spreadsheets: top Etsy seller 10.3k reviews at $11.95 (16 sales/24h). Gumroad Business & Money: $15.4M total, avg $49.49/product, 247 avg sales. Spreadsheet templates on Etsy: $80K-$125K/year for top sellers. [sources: etsy.com, insightraider.com, rupa.pro]",
        "sources": [
            "https://www.etsy.com/listing/1826328945",
            "https://insightraider.com/en/answers/what-sells-best-on-gumroad",
            "https://www.earninglivingonline.com/digital-product-ideas-2026/",
            "https://rupa.pro/blog/most-profitable-digital-products"
        ]
    },
    "startup_cost": "$0 (Google Sheets + Gumroad free tier)",
    "estimated_revenue": "$500-2000/mo within 6 months (freelancer niche, $29-39 pricing)",
    "progress_pct": 95,
    "next_task": "User: upload .xlsx to Google Drive, get share link, update delivery PDF, list on Gumroad",
    "created_at": "2026-05-11",
    "deployed_url": "https://github.com/timoranjes/freelancer-finance-dashboard",
    "build_log": [
        "2026-05-11 16:40: Research completed. Freelancer finance = underserved niche, 73M+ US freelancers.",
        "2026-05-11 16:41: Built 4-sheet Excel template: Setup, Income (50 rows), Expenses (90 rows), Dashboard",
        "2026-05-11 16:42: 12 expense categories, tax deductibility flags, quarterly tax estimation",
        "2026-05-11 16:42: Dashboard: 8 KPI cards, monthly trends, expense breakdown, top clients, tax schedule",
        "2026-05-11 16:43: Pre-launch audit passed - all formulas valid, correct sheet references",
        "2026-05-11 16:43: Pushed to GitHub: github.com/timoranjes/freelancer-finance-dashboard",
        "2026-05-11 16:44: Created delivery PDF + thumbnail (1280x720 PNG) + Gumroad listing copy + README"
    ],
    "notes": "Ready to list on Gumroad. Needs: Google Sheet upload + share link + Gumroad listing.",
    "revenue_tracking": {
        "total_sales": 0,
        "total_revenue": 0,
        "listing_url": None,
        "listing_date": None
    }
})

tracker["last_updated"] = datetime.now(timezone.utc).isoformat()

with open(tracker_path, 'w') as f:
    json.dump(tracker, f, indent=2, ensure_ascii=False)

print("Tracker updated with proj-002")
