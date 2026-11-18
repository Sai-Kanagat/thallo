# Design pass v2: before and after

Screens of `scripts/demo.sh --brand sai`, named `{before|after}-{phone|desktop}-{light|dark}-NN-state.png`.
Phone is 375x812 at 2x and desktop 1440x900 at 1x. Login and code screens are captured once (phone, light).
`*-metrics.json` lists, per screen, the page width and every link, button, summary or field under 44 px.
After: none under 44 px (only the skip link, which sits off screen until focused) and no horizontal scroll at 375 px.

`before-*-06-card-fail-forced-approve` shows the old blocked card. Neither version lets a disabled Approve be clicked.
`after-*-16-approve-refused` shows a real refusal: the card is opened, the piece is edited in a second tab, then
Approve is pressed. The toast names the checks in plain words and gives the next step.

## Contrast (WCAG AA, 4.5:1 for text)
Lowest text pair: 5.5:1 in light and 7.3:1 in dark. Pairs checked: ink and muted text on background, surface
and raised surface; links; text on the cobalt button; each status colour on its own tint; muted text on the red tint.
