# Design slop catalogue

The visual twin of the copy slop list (quality bar sections 2 and 3). The vision grader is shown
this list and may only FLAG; it never passes an image and never fails a build. Each entry has a
stable id (the part before the colon), which a grader reply must name. Maintained from real
rejections: add the pattern a reviewer removed, with the id it should carry.

Seeded by idea, not copied text, from public lists of tells in generated imagery and generic
interfaces (design review checklists such as the ones behind Impeccable, Hallmark and taste-skill,
named in docs/QUALITY_BAR.md section 10). None of those lists is vendored here.

## Composition and finish
- default_gradient: a stock two-colour or rainbow gradient used as the background with no reason from the brand
- generic_pill: generic rounded buttons, pills or badges that look like a default component library
- centred_everything: every element centred on one axis, no grid, no tension
- icon_soup: many small generic icons standing in for content
- fake_ui: invented app screens, dashboards or browser chrome that show nothing real
- decorative_emoji: emoji used as decoration or bullets
- lorem_filler: filler shapes or text that carry no message
- stock_photo_look: posed, glossy stock-photo look; generic people pointing at screens or shaking hands

## Artefacts of generation
- garbled_text: letters that do not form words, warped type, text inside the image that cannot be read
- warped_logo: a logo or brand mark that is distorted, misspelled or redrawn
- warped_product: a product whose shape, label or parts are wrong or melted
- malformed_hands: extra, missing or fused fingers; hands that bend the wrong way
- malformed_face: asymmetric or melted faces, wrong eyes or teeth, faces that are cut off
- impossible_scene: objects that merge, float or break physics; shadows and reflections that disagree

## Brand fit
- off_palette: colours that are not in the brand contract
- off_type: type that is not the brand's typeface or scale
- low_contrast: text that is hard to read against what is behind it
