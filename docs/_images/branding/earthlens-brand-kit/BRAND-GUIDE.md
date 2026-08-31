# earthlens — Brand & Media Kit

Everything you need to represent **earthlens** in docs, on GitHub/PyPI, and on social media.

## The mark
A satellite captures Earth-observation data, which resolves down through a pyramid of
raster overview tiles (the *pyramids* lineage) onto a living, rotating globe. The name is
the story: **earth + lens**.

## Colors
| Role | Hex |
|------|-----|
| Navy (primary surface) | `#0B2036` |
| Deep navy | `#061220` |
| Ocean (globe) | `#3A6E96` / `#21486A` / `#0E2A42` |
| Gold (accent — the "lens") | `#F2C879` |
| Aqua (grid / atmosphere) | `#8FD8D2` |
| Green (land) | `#3EA863` |
| River / data | `#29B6E6` |
| Light text | `#EAF6FA` |
| Muted text | `#96ACC0` |

## Typography
- **Wordmark & headings:** Outfit (Bold). The wordmark is `earth` (light/navy) + `lens` (gold).
- **Taglines & code:** Geist Mono / DM Mono.

## Files
```
logo/       full logo (SVG + PNG, transparent, on-white, on-navy) and wordmark lockups
icon/       square globe icon (transparent) + rounded app-badge
favicon/    favicon.ico + 16–512 PNGs + apple-touch-icon
social/     github-social-preview (1280×640) · announcement-card (1200×675)
docs/       docs-hero (1600×520) · readme-banner (1280×300)
animation/  orbit GIF (hero) · spinning-globe GIF + CSS SVG
```

## Usage snippets
README header:
```md
<p align="center"><img src="docs/_images/branding/earthlens-brand-kit/docs/readme-banner.png" width="820"></p>
```
Favicon (HTML `<head>`):
```html
<link rel="icon" href="favicon/favicon.ico" sizes="any">
<link rel="icon" type="image/png" href="favicon/favicon-32.png">
<link rel="apple-touch-icon" href="favicon/apple-touch-icon.png">
```
Set `social/github-social-preview.png` as the repo's **Social preview** (Settings → General).
Post `social/announcement-card.png` + `animation/earthlens-logo-orbit.gif` for the launch.

## Clear space & don't
Keep clear space ≥ the height of the satellite around the mark. Don't recolor the wordmark,
stretch the mark, or place the transparent logo on a busy photo without a navy scrim.

## Overlay variant — `earthlens-lockup-stacked-overlay`
For placing the mark over **arbitrary imagery** (photos, satellite scenes, video) at watermark scale,
down to **80 px wide**. Unlike the transparent lockup, it carries its **own rounded navy scrim**
(`#091c30` @ 0.82, 14% corner radius) with an optional gold inset hairline — so the "navy-scrim"
rule is already satisfied and it reads on anything from pure black to pure white.

It is a reduced form: enlarged wordmark (`earth` / `lens` stacked), a simplified high-contrast globe,
**no satellite and no tagline** (both alias away at watermark scale). Brand ink values are unchanged;
only the globe fill is lightened to meet WCAG non-text contrast against the plate.

Files: `logo/earthlens-lockup-stacked-overlay.svg` (vector source) · `logo/earthlens-lockup-stacked-overlay.png` (512 px RGBA).
Minimum width **80 px**. Do not use it below that, and don't disable the plate when placing over imagery.
