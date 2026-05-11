#!/bin/bash
# Quick setup helper for Pro Trading Journal
# Run this to see what's ready and what needs attention

echo "========================================="
echo "  Pro Trading Journal — Setup Status"
echo "========================================="
echo ""

# Check template exists
if [ -f "Pro_Trading_Journal_Template.xlsx" ]; then
    echo "✅ Template file: Ready"
    echo "   Path: $(pwd)/Pro_Trading_Journal_Template.xlsx"
    echo "   Size: $(du -h Pro_Trading_Journal_Template.xlsx | cut -f1)"
else
    echo "❌ Template file: Missing"
fi

echo ""

# Check delivery PDF
if [ -f "Delivery_Instructions.pdf" ]; then
    echo "✅ Delivery PDF: Ready (needs Google Sheets link)"
else
    echo "❌ Delivery PDF: Missing"
fi

echo ""

# Check thumbnail
if [ -f "thumbnail.svg" ]; then
    echo "✅ Thumbnail: Ready (SVG format)"
else
    echo "❌ Thumbnail: Missing"
fi

echo ""

# Check listing copy
if [ -f "GUMROAD_LISTING.md" ]; then
    echo "✅ Gumroad listing copy: Ready"
else
    echo "❌ Gumroad listing copy: Missing"
fi

echo ""

# Check GitHub
if [ -d ".git" ]; then
    echo "✅ GitHub repo: Pushed"
    echo "   URL: https://github.com/timoranjes/pro-trading-journal"
    echo "   Latest: $(git log --oneline -1)"
else
    echo "❌ GitHub repo: Not initialized"
fi

echo ""
echo "========================================="
echo "  NEXT STEPS (manual)"
echo "========================================="
echo ""
echo "1. Upload .xlsx to Google Drive → Import as Google Sheet"
echo "2. Share as template → Get the /template/preview link"
echo "3. Send me the link → I'll update the delivery PDF"
echo "4. Create Gumroad account → Copy listing from GUMROAD_LISTING.md"
echo "5. Upload PDF + thumbnail → Set price → Publish"
echo ""
echo "Total manual effort: ~15 minutes"
