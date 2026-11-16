# Proposed values: Sai Kanagat brand pack

Every value below is a proposal for Sai to approve or change by eye. The renders are in `docs/demo_screens/sai/`. Nothing here is stated by saikanagat.com. Everything the site does state (colours, fonts, weights, radii, spacing, tagline, names) is unchanged, except the type scale, which the brief asked to be re-derived for the 1080 px canvas (the web scale stays in `brand.extras.json`).

With these values `load_brand("demo_brands/sai")` loads and every render passes the design lint.

## 1. Type scale for the 1080 px canvas: one ratio, 2.0

| token | web px (site) | canvas px (proposed) |
|-------|---------------|----------------------|
| display | 64 | 128 |
| h1 | 48 | 96 |
| h2 | 36 | 72 |
| body | 18 | 36 |
| small | 14 | 28 |

Why 2.0: body must be at least 28 on a 1080 canvas, which needs a ratio of at least 1.56. The renderer also holds every text box to the channel minimum (24 px on LinkedIn), and the smallest web size, 14, needs a ratio of at least 1.72 for that. 2.0 is the next clean number above both, and the site's own size scale already has 128 px (its 6xl step, twice its 64 px display).

## 2. Logo

| field | value | why |
|-------|-------|-----|
| width | 128 | the brand book draws the mark at 64 px in its stacked lockup; times the ratio 2.0 |
| min_width | 32 | the brand book's screen minimum for the mark is 16 px square; times 2.0 |
| clear_space | 1.0 | the brand book says "minimum padding = x · height of the mark" and never gives x, so the default x = 1.0 times the mark height |

Only the mark exists as a published file (`assets/favicon.svg`, square), so the "full lockup at least 20 px tall" rule does not apply to any file here.

Open question on clear space: the brand book's clear-space specimen draws 48 px of padding around a lockup whose mark is about 34 px tall, which would read as x of about 1.4. I did not adopt it, because 48 px is also the book's grid outer margin and the specimen never labels x with a number. If Sai reads it as 1.4, change `clear_space` to 1.4; the free area around the logo grows by 0.4 of its height (about 51 px) and text keeps further from it.

## 3. Line height: 1.3, not 1.2

The brief suggested 1.2. The renderer's own overflow check rejects it. The script has one `line_height`; the templates use it as is for body and multiply it by 0.92 for display and 0.95 for h1. Bricolage Grotesque needs a line box of about 1.17 times the font size, so at 1.2 the 128 px display figure and 96 px headlines fail with "is clipped". Measured on the real templates:

| line_height | single-01 headline (96 px) | proof-01 figure (128 px) |
|-------------|----------------------------|--------------------------|
| 1.20 | fails | fails |
| 1.25 | passes | fails |
| 1.28 | passes | passes |
| 1.30 | passes | passes |

1.3 is the smallest round value that passes with a little margin. It gives display 1.20, h1 1.24, body 1.30 on the canvas. The site's web values are display 1.0, h1 1.05, body 1.55 (1.4 for short marketing lines), so the canvas is tighter than the web for body and looser for headlines. A different headline leading would need a template change, not a token.

## 4. Font files

`script/latin.json` still names Bricolage Grotesque, weights 700 (display) and 400 (body), as the site states. Bricolage Grotesque is not installed on this machine and the site loads it from Google Fonts, so the renderer's font check failed. I added the font files to the brand folder: `fonts/BricolageGrotesque-Regular.ttf`, `fonts/BricolageGrotesque-Bold.ttf` and `fonts/BricolageGrotesque-OFL.txt` (SIL Open Font License 1.1). They are static TTFs copied from a copy already on this machine; nothing was downloaded. The installed-font name stays first in the list, so an installed copy wins. No fallback font was added: a substitute would make the renders say nothing about his brand.

His site also serves its own variable `fonts/bricolage-latin.woff2`. I did not use it because weight handling of a variable font under a single declared weight is unverified here.

## Left open on purpose

- **Muted colour.** The site publishes no hex for it (n-500 is described as hints and captions only), so none is added. Soft text such as the card attribution renders in ink.
- **Logo on dark.** There is no reversed or on-dark mark file. On the ink and accent layouts one half of the mark disappears into the background (see `gap-dark-background.png`). The demo pieces use paper layouts only.
- **Spacing.** The site's grid values (48 outer, 24 gutter) are kept as web px, not rescaled. On a 1080 canvas they sit inside the renderer's own 5 percent safe zone, so the margin you see is the safe zone, and the gap between a headline and its body is tight. A proposed alternative is margin 96 and gap 48 (the same ratio 2.0); it is not applied.
- **Photos.** There are no approved photographs, and the site's rule is "No text overlays". The photo layouts show the repository's specimen shape only.
- **Offer family.** He publishes no offer or price, so the offer family renders its specimen wording only.
- **Landscape LinkedIn size (1200 x 627).** The renderer scales type down to fit a short canvas, so the 28 px small text lands at about 23 px against a floor of about 27 px and the lint refuses it. The demo uses 1080 x 1350. A smaller landscape scale would need its own proposal.
