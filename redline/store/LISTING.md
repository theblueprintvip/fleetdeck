# Chrome Web Store submission: Redline

Upload `redline-extension-0.1.0.zip` at https://chrome.google.com/webstore/devconsole → **New item**.
Then fill in each tab below, copying the text as written.

## Store listing

**Name:** Redline: contract review (from the manifest)

**Summary (132 characters max):**
Spots risky clauses in any contract, terms of service or offer letter, and rewrites them. Read it before you regret it.

**Description:**
Redline reads the fine print so you don't sign something you regret.

Open any contract, lease, offer letter, creator deal or terms of service page and click "Redline this". In about 20 seconds you get:

• A risk score from 0 to 99
• Every risky clause, quoted from the document and explained in plain English
• A safe rewrite you can propose instead
• A "time bomb" timeline of renewals, notice periods and deadlines

Works where contracts actually show up:
• Web pages: a "Redline this" button appears on pages that read like a contract
• PDFs: right-click a PDF link and choose "Redline this PDF"
• Gmail: a Redline button next to every PDF attachment
• Any text: select it, right-click, "Redline selected text"

Every finding is checked against Redline's clause library of 66 common traps across employment, rental, creator, NIL, entertainment, mortgage and everyday contracts. Anything outside the library is marked "review recommended".

Private by design: Redline only reads a page when you ask it to. PDFs are converted to text inside your browser. No tracking, no ads, no selling data.

Redline flags common contract risks. It is not a law firm and does not give legal advice.

**Category:** Productivity
**Language:** English

**Graphics:**
- Store icon: `extension/icons/icon-128.png`
- Screenshots (1280×800): `screenshots/1-pill.png`, `screenshots/2-drawer.png`, `screenshots/3-rewrites.png`
- Small promo tile (440×280): `screenshots/promo-440x280.png`
- Marquee promo tile (1400×560): `screenshots/marquee-1400x560.png`

**Official URL / homepage:** https://redline-mpr16m.v2.appdeploy.ai/
**Support URL:** https://redline-mpr16m.v2.appdeploy.ai/

## Privacy practices tab

**Single purpose:**
Review contracts the user is reading (web pages, selected text, PDFs and Gmail PDF attachments) and show risky clauses with suggested rewrites.

**Permission justifications:**
- **activeTab:** Reads the current page's text when the user clicks the toolbar button so it can be reviewed.
- **contextMenus:** Adds "Redline selected text", "Redline this page" and "Redline this PDF" to the right-click menu.
- **scripting:** Reads the page text on request, and fills the user's contract into the Redline site when they choose "Open in Redline".
- **storage:** Holds the current review job in session storage (cleared when the browser closes) so the review panel can read it.
- **Host permission (all sites):** Contracts appear on any website, so the extension checks pages locally for contract language to offer the "Redline this" button, and downloads a PDF the user asks to review. Page content is sent to Redline only when the user clicks to review it.

**Remote code:** No, I am not using remote code. (pdf.js is bundled inside the package.)

**Data usage:** tick **Website content** only (the contract text the user chooses to review).
Then tick all three certifications:
- I do not sell or transfer user data to third parties, outside of the approved use cases
- I do not use or transfer user data for purposes that are unrelated to my item's single purpose
- I do not use or transfer user data to determine creditworthiness or for lending purposes

**Privacy policy URL:** https://redline-mpr16m.v2.appdeploy.ai/privacy.html

## Distribution tab
- Visibility: **Public** (or **Unlisted** for a soft launch: only people with the link can install)
- Regions: all regions
- Pricing: free (payments happen on the Redline site through Stripe)

## Before you submit
- Set a support email on your developer account (Account tab). The privacy policy points people there.
- Review usually takes a few days. Extensions with "all sites" access sometimes take longer.
