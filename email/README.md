# Columbia Basin Insurance — interactive email

An HTML email built around the CBI logo and Fred's headshot. Two things in it
are genuinely interactive — a coverage tab switcher and an FAQ accordion — and
both are pure CSS, because email clients don't run JavaScript.

| File | What it is |
| --- | --- |
| `columbia-basin-interactive-email.html` | The template you send. Images point at `{{ASSET_BASE}}`. |
| `columbia-basin-interactive-email.txt` | Plain-text alternative. Attach it as the `text/plain` part — it keeps you out of spam filters. |
| `preview.html` | Self-contained preview with the images inlined. Open it in a browser to click through. Generated — don't hand-edit. |
| `build_preview.py` | Regenerates `preview.html` from the template. Run it after any edit. |
| `../static/email/` | The image assets, derived from the original logo and headshot. |

## Before you send

**1. Point `{{ASSET_BASE}}` at a real URL.** Every image in the template is
written as `{{ASSET_BASE}}/filename.png`. Replace that token with wherever the
files in `static/email/` are publicly reachable. If this app is deployed, they
are already served from its `/static` mount:

```
https://your-domain.com/static/email
```

For a quick test send you can use the copies in this repo:

```
https://raw.githubusercontent.com/fredosaguilar/cbitranscripts/master/static/email
```

Images must be hosted at an https URL. Data URIs (what `preview.html` uses) are
blocked by Gmail and Outlook, so the preview file is for looking at, not sending.

**2. Fill the merge tags.** `{{FIRST_NAME}}`, `{{VIEW_IN_BROWSER_URL}}`,
`{{UNSUBSCRIBE_URL}}`, `{{PREFERENCES_URL}}`. Most senders (Mailchimp,
Constant Contact, Brevo, HubSpot) have their own tag syntax — swap ours for
theirs. If you send without a real unsubscribe link, delete that line rather
than leaving a dead `#`; the physical address in the footer is already there
because CAN-SPAM requires it.

**3. Check the details we guessed.** "Fred Aguilar" and the coverage bullets are
placeholders written from the logo's four lines of business. Edit the copy to
match what you actually want to say — the layout doesn't care how long the
sentences are.

## How the interactivity works (and what happens when it doesn't)

Both widgets are driven by hidden `<input>` elements and the CSS `:checked`
pseudo-class. Labels sit on top as the clickable surface.

The important part is the fallback. The CSS is written so the *broken* state is
the readable one:

- **Coverage tabs.** By default every panel is `display:block` and the tab bar is
  `display:none`. Only the rule `.tab:checked ~ .tabbar { display:block }` turns
  the tabs on — and it can only match if the radio inputs survived the client's
  sanitizer *and* `:checked` is supported. A client that strips inputs shows all
  four coverages stacked, each under its own heading, with no dead buttons.
- **FAQ accordion.** `#faq-support` is a checkbox that ships pre-checked, so it
  doubles as the support detector. Where it works, the answers collapse and the
  +/− markers appear. Where it doesn't, all four answers are simply visible.
- **Outlook (Word rendering engine)** gets a separate static version of the tabs
  through `<!--[if mso]>` conditional comments, so it never sees the CSS at all.

Roughly where you land:

| Client | Result |
| --- | --- |
| Apple Mail (macOS, iOS, iPadOS) | Full interactivity — tabs and accordion |
| Samsung Mail, Thunderbird | Full interactivity |
| Gmail (web, iOS, Android) | Static fallback: all coverages and answers visible |
| Outlook 2016–2021, Microsoft 365 desktop | Static MSO version, table-based |
| Outlook.com / Outlook mobile | Static fallback |
| Yahoo / AOL | Static fallback |

Dark mode is handled through `prefers-color-scheme` plus `[data-ogsc]` for
Outlook.com, including a light/dark swap of the footer logo so the teal wordmark
doesn't disappear on a dark background. Below 620px the layout stacks to one
column and the buttons go full width.

## Assets

Generated from the two source images:

- `cbi-logo.png` — trimmed, transparent background, brand colors intact. For
  light backgrounds.
- `cbi-logo-reversed.png` — teal wordmark knocked out to white, gold mark kept,
  transparent background. For the teal header and dark mode.
- `fred-headshot.png` — 400×400, circular alpha mask baked in, displayed at
  130px. The circle is part of the image rather than a `border-radius`, since
  Outlook ignores the CSS.

All three are palette-quantized PNGs, ~25–30 KB each, so the whole email is
around 85 KB of images.

## Editing

Change the template, then:

```bash
python3 email/build_preview.py
```

and reload `email/preview.html`. Keep an eye on total HTML size — Gmail clips
messages past 102 KB, and the template is currently well under that.
