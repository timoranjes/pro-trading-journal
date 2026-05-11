# GOOGLE SHEETS SETUP INSTRUCTIONS
# ============================================================
# You need to do these 3 steps manually (I can't access your Google account):

STEP 1: Create the Google Sheet
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Go to https://sheets.google.com
2. Click "Blank" spreadsheet
3. Name it: "Pro Trading Journal Template"
4. Import the Excel template:
   - File → Import → Upload tab
   - Select: ~/.hermes/data/trading-journal-template/Pro_Trading_Journal_Template.xlsx
   - Choose: "Replace spreadsheet"
   - Click "Import data"

STEP 2: Make it a shareable template
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Click the "Share" button (top right)
2. Click "Get link"
3. Change from "Restricted" to "Anyone with the link"
4. Set role to: "Viewer"
5. Copy the share link
6. Modify the URL: change `/edit?usp=sharing` at the end to `/template/preview`
   - Example: https://docs.google.com/spreadsheets/d/1abc123.../template/preview

STEP 3: Update the delivery PDF
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Once you have the template link, I'll update the PDF with the real link.
Just send me the link and I'll regenerate the PDF automatically.

ALTERNATIVE: If you want me to handle the upload via Google Drive API,
you'd need to run the Google OAuth setup first:

python ~/.hermes/skills/productivity/google-workspace/scripts/setup.py --check
# If not authenticated, follow the OAuth flow to authorize

But the 3-step manual process above is faster.
