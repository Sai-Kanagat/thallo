# What Thallo needs from Sai

Everything in one place, in build order. Links checked on 12 Nov 2026; pages behind a sign-in could not be checked and are marked. The rule for every secret: it goes into `~/cortex-operator/.env` (or a file outside the repo) by you, and never into chat. After each step, run this and tell Claude "done":

```
cd ~/cortex-operator && .venv/bin/python scripts/check_env.py
```

It prints SET or MISSING, never a value.

Time estimates are for you, not for approvals that other companies control.

---

## Part A. Do today (about 45 minutes, unblocks waves 1 to 2)

### A1. Fix the Supabase line (2 min)
1. Supabase dashboard, your project, the green **Connect** button, then the **Direct** tile, then **Method: Session pooler**.
2. Copy the full URI. It must start with `postgresql://postgres.` and end with `.pooler.supabase.com:5432/postgres`.
3. In `.env`, replace the whole `DATABASE_ADMIN_URL=` line with it, putting your database password in place of `[YOUR-PASSWORD]`.

### A2. Set up the database (1 min)
```
cd ~/cortex-operator && .venv/bin/python scripts/provision_db.py
```
This applies all migrations and creates two restricted logins (`thallo_app` and `thallo_worker`), writing their connection lines into `.env`. If any line says "NOT SAFE" or "failed", paste that line to Claude. It contains no secrets.

### A3. Generate the two local keys (1 min)
```
cd ~/cortex-operator && .venv/bin/python scripts/gen_keys.py
```
Then open `.env`, copy the `THALLO_VAULT_KEY` and `THALLO_GATE_SIGNING_KEY` lines into your password manager, and keep that backup. Losing the vault key makes every stored token unreadable.

### A4. Anthropic API key (5 min)
1. Go to https://platform.claude.com/settings/keys (sign in, create an organisation if asked).
2. Add a card in billing, then **set a monthly spend limit** at https://platform.claude.com/settings/spend-limits (USD 20 is plenty for build and pilot).
3. **Create key**, name it `thallo-dev`, and paste it into `.env` as `ANTHROPIC_API_KEY=...`.

### A5. Gemini API key (3 min)
1. Go to https://aistudio.google.com/apikey and create a key in a new project named `thallo`.
2. Paste it into `.env` as `GEMINI_API_KEY=...`.
3. So that Gemini can do the cross-family code reviews, also run once in Terminal:
   ```
   gemini
   ```
   and choose "Use Gemini API key" when asked (it reads `GEMINI_API_KEY` from your shell; add `export GEMINI_API_KEY=...` to `~/.zshrc` if it does not find it).

### A6. Private GitHub repo (2 min, makes CI real)
```
cd ~/cortex-operator && gh repo create thallo --private --source=. --remote=origin
```
```
cd ~/cortex-operator && git push -u origin main
```
The first command creates an empty private repo. The second uploads the code; that is your publish step, so you run it. GitHub Actions then runs the full gate on every push. Nothing secret is in the repo, and gitleaks blocks any commit that would add one.

### A7. Platform access applications (20 min of forms, then weeks of waiting)
Send these today, because approval time is the longest wait in the whole plan. Manual mode covers the gap.

1. **Meta (WhatsApp Cloud API and Marketing API)**
   - Create a Meta Business portfolio: https://business.facebook.com/overview
   - Start **Business verification** in Business settings, then Security Centre (needs a business document and website). Use the business you will pilot with first, or yours as operator.
   - Create a developer app (type **Business**): https://developers.facebook.com/apps
   - Add the **WhatsApp** product. Meta gives you a free test number right away; that is enough for wave 3 testing.
2. **Google Ads API**
   - Create a Google Ads **manager account**: https://ads.google.com/home/tools/manager-accounts/
   - The old API Center developer-token flow was retired in September 2026. Now: in the `thallo` Cloud project (step 3 below creates it), enable the **Google Ads API**, then request access from the **Google Ads API Overview** page in the Cloud Console. Basic access needs brand verification. Current rules: https://developers.google.com/google-ads/api/docs/api-policy/developer-token
3. **Google Business Profile API**
   - Create a Google Cloud project named `thallo`: https://console.cloud.google.com/projectcreate
   - Open https://developers.google.com/my-business/content/prereqs, go to the section **Request access to the APIs**, and submit the form it links to. The form may only appear when you are signed in to Google; if the link lands on the Help Center home page, sign in and click it again from the prereqs page.
   - It asks for the **Project Number**, shown on the project Dashboard's "Project info" card.

Put nothing from these in `.env` yet. Tell Claude what was approved, and when.

---

## Part B. Before wave 2 (production starts)

### B1. Postiz, publishing (10 min, costs money: your call)
1. Sign up at https://postiz.com. The plan says the Standard tier (about USD 29 a month) is enough. Buying it is your decision.
2. Connect **one** channel first (a page you control, ideally the first pilot's).
3. In Postiz settings, find the **Public API** key (Postiz documents the API at https://docs.postiz.com/public-api/introduction; the exact menu name was not verifiable without signing in) and paste it into `.env` as `POSTIZ_API_KEY=...`.

### B2. Brand files for the first pilot (15 min)
Put these in `~/thallo-brands/<pilot-name>/`, outside the repo, since client data stays out of git: logo (SVG or large PNG), font files or font names, brand colours, 20 past posts (screenshots are fine), 10 liked and 10 disliked reference posts, and about 30 real photos.

---

## Part C. Before wave 3 (leads and messaging)

### C1. WhatsApp Cloud API token (after A7 step 1)
In your Meta app, go to WhatsApp, then **API Setup**. Copy the **Phone number ID** and the **Messaging account ID** (the WhatsApp Business Account) into `.env` as `WHATSAPP_PHONE_NUMBER_ID=` and `WHATSAPP_WABA_ID=`. For the token, create a **System user** in Business settings, give it the app and the WhatsApp account, and generate a permanent token with `whatsapp_business_messaging` and `whatsapp_business_management`. Put it in `.env` as `WHATSAPP_TOKEN=`. Do not use the temporary token from API Setup; it expires quickly. Docs: https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started

### C2. Email sending: Brevo (free tier)
Sign up at https://www.brevo.com, verify a sender domain (DNS records on the sending domain), then find **API keys** in your account settings (the menu name was not verifiable without signing in) and put the key in `.env` as `BREVO_API_KEY=`.

### C3. Booking: Cal.com
Sign up at https://cal.com, create one event type per pilot ("Demo" or "Showroom visit"), go to **Settings, then Security**, create an API key, and put it in `.env` as `CALCOM_API_KEY=`.

### C4. Each pilot business (Claude prepares the questions; you run the conversations)
- The 45-minute kickoff interview with each owner.
- Verify the first 40 claim candidates (30 seconds each, on your phone).
- The business WhatsApp number, and whether it is on the WhatsApp Business app today. Moving a number to the API can remove it from the app, so decide that before C1 with a real number.

---

## Part D. Before wave 4 (measurement)

### D1. Google Analytics 4 and Search Console
1. In the `thallo` Cloud project, enable the **Google Analytics Data API** and the **Google Search Console API**: https://console.cloud.google.com/apis/library
2. Create a **service account** (IAM, then Service accounts), then **Keys, Add key, JSON**. Save the JSON as `~/thallo-secrets/google-sa.json` (outside the repo) and put `GOOGLE_APPLICATION_CREDENTIALS=/Users/saikanagat/thallo-secrets/google-sa.json` in `.env`.
3. Add the service account's email as **Viewer** on each pilot's GA4 property (Admin, then Property access management), and as a **user** in each Search Console property (Settings, then Users and permissions).

### D2. Notion mirror (optional)
Create an internal integration at https://app.notion.com/profile/integrations, share the calendar database with it, and put the token in `.env` as `NOTION_TOKEN=`.

---

## Part E. Before wave 5 (money)
- **Google Ads**: once Google Ads API access is approved, create an OAuth client (Desktop app) in the `thallo` Cloud project (APIs and services, then Credentials). Claude will then give you a one-time local command that produces the refresh token.
- **Meta Marketing API**: in the same Meta app, add **Marketing API**, give the system user the pilot's ad account, and add `ads_management` and `ads_read` to its token. Put the ad account id in `.env` as `META_AD_ACCOUNT_ID=`.
- **Spend caps per pilot**: a monthly cap and a daily cap, in each business's currency.
- **Growth model numbers per pilot**: average margin per sale, rough close rate, show rate, and where enquiries come from today.

---

## Decisions only Sai can make
Claude's recommendation comes first in each.

| # | Decision | Recommendation | Needed by |
|---|---|---|---|
| 1 | Which pilot goes first | The business with the most enquiries per week, since play A (dealer) shows value fastest through the lead desk | Wave 2 |
| 2 | How long an approval lasts | 7 days by default, 30 at most (as built) | Now, or keep as built |
| 3 | Whether a change of posting time voids an approval | No, timing is outside the hash (as built) | Now, or keep as built |
| 4 | Loss reasons list | Price, finance refused, chose a competitor, no budget, timing, no response, not a fit, other | Wave 3 |
| 5 | Whether the repo stays private | Private until the first pilot result, then make a cleaned public copy for the build log | Wave 4 |
| 6 | Hosting for the worker and renderer | The smallest Hetzner cloud server in the EU, from EUR 5.49 a month excluding VAT (price read on 11 Nov) | Wave 2 |
| 7 | Short domain for tracked links | `go.saikanagat.com` costs nothing and works now; a short dedicated domain can come later | Wave 3 |
| 8 | A native Tamil reader | Someone who can audit about 100 Tamil sentences once | Before any Tamil post |
| 9 | Region policy answers | A short Q and A with a lawyer or adviser for India and Italy (consent, disclosure labels) | Before wave 3 sends anything |
| 10 | Product name check | Thallo collides with usethallo.com and a pending US filing; EU search not done | Before spending on the brand |

---

## Connectors for Claude (optional, saves time)
- **GitHub**: authorise it in this app (the engineering GitHub plugin, via `/mcp` in a terminal session, or the app's connector settings). Claude can then open PRs and read CI without you.
- Notion is already connected.
- No other connector is needed. Supabase, Postiz and the ad platforms are reached through the code, with keys in `.env`.
