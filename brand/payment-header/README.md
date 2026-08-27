# Payment page branding — Columbia Basin Insurance

**Simply Easier Payments' "Payment Headers" are text, not images** — two
plain-text blocks you type in, shown on the payment pages and receipts you tick
on the grid. That copy is in **`payment-header-text.md`**, which is what to use
for the header boxes.

The images below are for the separate branding settings, where a logo can be
uploaded. Open `preview.html` to see them on the page background each one
suits.

| File | Size | Use |
| --- | --- | --- |
| `cbi-payment-header-light-1200x250` | 1200 × 250 | Wide header on a white page |
| `cbi-payment-header-teal-1200x250` | 1200 × 250 | Wide header on a grey or tinted page |
| `cbi-payment-header-light-760x150` | 760 × 150 | Narrower page, or a portal that scales down |
| `cbi-payment-header-teal-760x150` | 760 × 150 | Same, on a tinted page |
| `cbi-payment-logo-light-600x200` | 600 × 200 | Where only a logo is wanted, no contact details |
| `cbi-payment-logo-teal-600x200` | 600 × 200 | Same, reversed |

Every file has an `@2x` twin at double the pixels. Upload the `@2x` if the
portal accepts it — it stays sharp on retina screens and scales down cleanly.
Use the plain one if there is an upload size limit.

## Sizes

Simply Easier Payments does not publish a header spec, so these are standard
banner proportions rather than their exact requirement. If they give you one,
the artboards regenerate at any size — nothing here is hand-placed.

## What's in the design

- The logo is the real artwork, not a re-typeset version, so the wordmark is
  correct. The light files use the brand-colour logo on a transparent
  background; the teal files use a knockout with the wordmark in white and the
  mark left gold.
- Phone number is the most prominent text after the logo. On a payment page the
  useful thing for someone who is stuck is a number to call, not an address.
- "Secure Online Payment" sits above it in gold. It says what the page is
  without claiming anything about how payments are processed — that is Simply
  Easier's to state, not ours.
- A gold rule closes the bottom edge so the header separates from the payment
  form whatever colour the page behind it is.

## Regenerating

The artboards are built from `static/email/cbi-logo.png` and
`cbi-logo-reversed.png` and rendered headlessly, so a change to the wording,
the phone number, or the dimensions is a change to the build rather than an
edit in a graphics program. Ask and it will be re-cut.
