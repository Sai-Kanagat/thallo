# Gaps and contradictions: Sai Kanagat brand pack

Read 2026-11-16 from /Users/saikanagat/code/portfolio-live (read only) and checked against https://saikanagat.com. The CSS files, favicon.svg and llms.txt are byte-identical to the live ones. The HTML pages differ only in how Cloudflare rewrites the email address.

## 1. Does brand.json load?

Update: it loads now. The missing values are filled as labelled proposals in `PROPOSED.md`, for Sai to approve. The text below is the state when the pack was first written.

No, on purpose. `load_brand("demo_brands/sai")` fails with exactly these errors, because the site states none of them:

- `logo.width`: rendered width of the logo on a 1080 px canvas
- `logo.min_width`: smallest allowed width on a 1080 px canvas
- `logo.clear_space`: share of logo height. The site says "minimum padding = x · height of the mark" and never gives x.
- `script/latin.json` also fails on `line_height`: the site has one per style (1.0, 1.05, 1.10, 1.20, 1.55, 1.50), not one per script.

Everything else in brand.json validates. Decisions needed from Sai (not facts), then the file loads:

| Field | What the site gives | What is missing |
|-------|---------------------|-----------------|
| logo width, min_width | screen mark at least 16 px square, full lockup at least 20 px tall (web px) | a size on the 1080 canvas |
| logo clear_space | "x · height of the mark" | the value of x |
| line_height | body 1.55, display 1.0 | one number for the script |
| type_scale | display 64, h1 48, h2 36, body 18, small 14 | these are web px. The loader treats them as px on a 1080 canvas, where 18 px body is far too small. A scale for the canvas is a design decision. |
| spacing | grid outer 48, gutter 24 | the site never names margin or gap. Mapping is mine. |

## 2. What a brand contract needs that the site does not say

- **Colour hex.** The brand book defines every colour in oklch only. Hex literals exist in three places and disagree (see G11). brand.json uses published literals only. For reference, these are conversions I computed from the site's oklch values (sRGB, clipped). They are derived, not published, and the site does not endorse them: ink #0A0D12, paper #F5F7F9, accent #1238BD, accent-ink #0D067F, accent-up #1E64EF, accent-soft #DBE8FF, n-500 #70757C, positive #38853E, negative #B32228, warning #D58D25. The full oklch set is in brand.extras.json.
- **muted and secondary colours.** n-500 is described as hints and captions only, and has no published hex.
- **Logo files.** Published and copied: favicon.svg, favicon-32.png, favicon-16.png, apple-touch-icon.png (180 x 180). Not published as files: a wordmark, a reversed or on-dark version, a stacked lockup. The brand book builds those in HTML and CSS, so `file_on_dark` is empty. `/favicon.ico`, `/icon-192.png` and `/icon-512.png` are referenced by the pages and the web manifest but return an HTML page, not an image. og-image.jpg (1200 x 630) exists and is a share card, not a logo, so it was not copied.
- **Photo treatment.** The brand book gives rules in words (one light source, no filter, no text overlays, crop tight or wide). There is no photograph of Sai on the pages read and no library of approved photos. The schema has only `photo_scrim`. The only scrim is the work-card gradient in the live CSS (rgba(16, 18, 28, 0.92) at the base), which sits against the brand book's "No text overlays." The rules are in brand.extras.json for a human to translate.
- **Fonts.** Display and body are Bricolage Grotesque. Newsreader (italic accent) and IBM Plex Mono (labels) have no slot in `script/latin.json`. Fonts come from Google Fonts, so there are no font files to put in the brand folder.
- **Tamil.** He speaks Tamil natively. The site is English and Italian only, so there is no Tamil copy and no Tamil type choice.
- **Italian copy.** The index, about and process pages carry Italian text in data-it attributes. It was not reviewed here, so there are no Italian voice quotes.
- **Offer and pricing.** There is no public price list or package. The brand book's "14 days" brand book, the invoice amounts and the "Pricing" link in the sample search ad are specimen content. His stated offer is a job search (C19) and project work, not a priced service.
- **Audience.** The only stated audience is "a hiring manager in Italy with seven minutes between meetings" (blog: Portfolios Are Products). No client ideal profile is published.
- **Channels with real activity.** Only LinkedIn and GitHub are linked from the live pages. Instagram, TikTok and X formats are specimens in the brand book. No past posts, liked references or rejected references are on the site.
- **Contrast thresholds.** Only four measured pairs are published (ink on paper 17.2, paper on ultramarine 8.4, ultramarine-deep on paper 11.6, n-500 on paper 4.6). Normal and large thresholds are not stated, so brand.json relies on the loader defaults.
- **Template family rules.** The brand book shows carousel, quote, stat and case-study layouts as specimens. It gives no rules that map to Thallo's families beyond the type, colour and logo rules.
- **Claims policy and disclosure.** Not stated anywhere.

## 3. Brand-book specimen content, not fact

brand-identity.html is a template library. These items appear in it and must not become claims: the client "Northwind Co." and the founder "Maria" with her two testimonials, the invoice (lines, totals, bank details), the telephone number, the "14 days" and "18 days" turnaround claims, "Milan, autumn 2022", "11 stores", "Sold out in seven", the sample CV (including "BFA · Industrial Design" and its dates), the follower and like counts on the sample reels, the sample poll results, and the sample "3.2 × ROAS" tied to a Carlos Mota campaign in "Q4 2024". None of them is on a page that states it as his record.

## 4. Contradictions between pages

No winner is picked. Each needs a call from Sai.

**G01. Carlos Mota internship.**
- /about.html: dated May to July 2022, "Furniture & Set Design Intern", location Mumbai, India.
- /blog/carlos-mota-studio-lesson.html: "In 2022 I spent six months as a design intern with Carlos Mota" and "the New York interior stylist".
- /blog.html: "Six months inside Carlos Mota's NYC studio on the Verde Collection".
Three months in Mumbai against six months in New York.

**G02. What Nishta is.**
- /about.html: "Founder & Digital Marketing Strategist", dated January 2024 to present, "Founded a digital marketing practice specialising in social media advertising, web design, branding and content strategy."
- /blog/building-nishta.html: "I was working as a designer in India in 2023", "Nishta was my attempt to close that loop on a single product line", "For the first nine months Nishta and I were the same brand in my head".
- /blog/carlos-mota-studio-lesson.html: "my own product line at Nishta".
A marketing practice for clients, or his own product brand, or both. Do not describe Nishta in the demo until this is settled.

**G03. Italian level.**
- /about.html: "am developing Italian" and, in the skills list, "Italian (Basic → Developing)".
- /blog/why-italy-and-why-now.html: "I'm fluent in English, conversational in Italian".

**G04. Degree.**
- /about.html: "B.Des" at DJ Academy of Design, dated 2019 to 2023, "Four-year undergraduate programme".
- /brand-identity.html sample CV: "BFA · Industrial Design" with dates ending 2022. This is specimen content that disagrees with the real page. Use the about page and fix the sample.

**G05. The 3.2x ROAS: which channels, which clients.**
- /about.html: "across Meta and Google Ads campaigns over a 6-month period" with "€15,000".
- /blog/what-15k-meta-ads-taught-me.html: "a Meta Ads account for a portfolio of lifestyle brands" and "We hit 3.2× ROAS consistently."
- /brand-identity.html samples attach 3.2x to a Carlos Mota campaign (see section 3).
- /index.html shows "3.2×" as "Average ROAS on Paid Campaigns" with no period.
Safe wording: 3.2x over six months on 15,000 euros. Not safe: naming the channel mix or the client.

**G06. Where the 40% organic growth came from.**
- /about.html: "+40%" from "a structured 6-month SEO content programme".
- /brand-identity.html samples: "organic growth · Q3" and a Juicy Chemistry card with "+40% ORG". Both are specimens, but the Juicy Chemistry card pairs it with a different project from the about page, which pairs Juicy Chemistry with "+35%" sales conversion.

**G07. MSc dates and status.**
- /about.html timeline dates the MSc 2025 to present. The credentials list dates it 2025 to 2026.
- /blog/bologna-business-school-marketing-reality.html is titled "A Year Inside BBS" and says "I'm halfway through the MSc".
- /about.html says he is "finishing my MSc".

**G08. Contact address.** The live pages link a Gmail address. The brand book sample stationery shows an address at saikanagat.com. Only the Gmail one is on a real contact link. The profile does not repeat either.

**G09. Domain and handles.** README.md in the repo says "Live: https://saikanagat.netlify.app". The live site and llms.txt use saikanagat.com. The brand book shows the handle "@saikanagat" for Instagram and X, and a LinkedIn path "/saikanagat" on the CV sample. The live links are linkedin.com/in/sai-kanagat and github.com/Sai-Kanagat. No Instagram or X account is linked.

**G10. Font names.**
- /css/brand-identity-system.css header comment: "Bodoni Moda + Hanken Grotesk + JetBrains Mono".
- Same file's tokens, the brand book and the live overlay: Bricolage Grotesque, Newsreader, IBM Plex Mono.
The comment is stale. About.html inline styles also use `font-weight: 900` on a display heading, which matches neither.

**G11. Colour values.**
- Ink: oklch(0.16 0.012 260) in CSS, #0E1116 in favicon.svg.
- Paper: oklch(0.975 0.004 260) in the brand book CSS, #f4f3ef in brand-overlay.css (the file the live pages load last) and the web manifest, #F4F5F7 in favicon.svg.
- Accent: oklch(0.42 0.21 265) in CSS, #1F40C7 in the theme-color meta tag, the web manifest and favicon.svg. My conversion of the oklch value is #1238BD, which differs.
brand.json uses the published hex literals: ink #0E1116, paper #F4F3EF, accent #1F40C7.

**G12. Radii, shadows and neutrals.**
- Radius r-4 is 12 px in brand-identity-system.css and 14 px in brand-overlay.css. r-5 is 20 px and 22 px.
- /brand-identity.html says cards and inputs use r-3 (8 px) and then shows a card with "A 1 px rule, 12 px radius."
- Shadows are tinted rgba(8,12,28,...) in one file and rgba(20,14,8,...) in the other. The brand book says "All shadows are cast cool."
- n-300 is oklch 0.85 in one file and 0.86 in the other.

**G13. Heading weight.** Brand book specimens: display and h1 at weight 700, h2 and h3 at 600. The live overlay CSS sets h1, h2, h3 and the work card titles to weight 500. script/latin.json uses 700 for display, which is the brand book value.

**G14. Logo and gradients.** The brand book says "no gradients" for the logo and bans "gradients on brand surfaces". The published favicon.svg draws the diagonal split with a hard-stop linear gradient, and the live overlay uses several soft gradients.

**G15. Project naming.** "ARABELLA" in the about page text and "ARABellA" in the index card and the brand book. The index also lists "Buddy" (a sustainable cigarette redesign), which llms.txt and the about page do not mention.

**G16. Counts.** The index shows "12+ Design Projects Completed" and "5 Brand Identities & Campaigns". The brand book sample CV says "Five brand systems consumer & furniture" under independent work from 2024. Nothing lists the twelve or the five.

**G17. Voice rules against the repo rules.** The brand book allows em dashes "sparingly" and one emoji in a Bologna joke. This repository bans em dashes and emoji in copy. Demo copy for Sai follows the repo rule, which means the demo voice is slightly stricter than his own.

## 5. Not read or not usable

- `thallo/index.html` in the portfolio repo has an uncommitted change that is not part of this work. It was not read. The Thallo description in claims.json comes from llms.txt only.
- `work.html`, `cortex.html` and the other project pages were outside the source list and were not read, so project facts beyond llms.txt, the blog and the index cards are not here.
- The brand book states "Next review: November 2026". It is November 2026 now, so the brand book may be about to change.
