# Client recap texts

After a call is transcribed, analysed and approved, the agency can text the
client a short written recap of what was discussed and what changed. Every
draft and every send is kept, so months later you can show exactly what wording
went to which number and when.

## Why it is built the way it is

The recap is only worth anything in a dispute if it is accurate, so three rules
are baked into the code rather than left to habit:

**An agent reads it before it goes.** The transcript has to be approved first,
and the draft sits in an editable box until someone presses Send. An
unreviewed AI summary sent to a client creates exposure rather than closing it
— it becomes a written statement by the agency about a policy, made by nobody.
Auto-send exists (`CLIENT_RECAP_AUTO_SEND=true`) but ships off.

**It is written in the language the call was spoken in.** Whisper already
records the spoken language on each transcript, so a call taken in Spanish
produces a Spanish recap — including a Spanish closing disclaimer. The analysis
fields themselves are stored in English, so the translation happens at drafting
time, and the draft comes back with a plain-English rendering shown beside it so
an English-reading agent can check a message they cannot read before it goes.
That gloss is stored with the record but never sent.

**It never confirms coverage.** The draft is written from the analysis fields —
what was discussed, what options were presented, what the client chose, what
was recommended — and the model is instructed never to state that anything is
bound, added, removed, or in force. Every message closes with:

> This is a summary of our conversation, not confirmation of coverage. Reply if
> anything here is wrong. Reply STOP to opt out.

and in Spanish:

> Este es un resumen de nuestra conversación, no una confirmación de cobertura.
> Responda si algo aquí no es correcto. Responda STOP para no recibir más mensajes.

`STOP` stays in English in every language — carriers match that exact keyword to
process an opt-out, so translating it would break the opt-out itself.

That line is the point of the whole feature. A client who lets it stand has
been told in writing; a client who corrects it has told you something you needed
to know before the claim.

**What was sent is never rewritten.** Rows in `client_recaps` are immutable
once sent. Editing after a send creates a new row, so the record of what the
client actually received survives later edits.

## Configuration

Add to `.env`:

```ini
# --- sending ---------------------------------------------------------------
# RingCentral: an SMS-capable number on the extension the app authenticates as.
# It reuses the existing RINGCENTRAL_* JWT credentials — nothing else to set up.
RINGCENTRAL_SMS_FROM=+15097658839

# Twilio, as an alternative if the RingCentral app has no SMS scope:
# TWILIO_ACCOUNT_SID=ACxxxxxxxx
# TWILIO_AUTH_TOKEN=xxxxxxxx
# TWILIO_FROM_NUMBER=+15095550100
# TWILIO_MESSAGING_SERVICE_SID=MGxxxxxxxx   # instead of a From number

# ringcentral | twilio | auto (default: whichever is configured)
CLIENT_SMS_PROVIDER=auto

# Log messages instead of delivering them. Leave this ON until the wording has
# been read on a real handset — the recipients are real clients.
CLIENT_SMS_DRY_RUN=true

# --- wording ---------------------------------------------------------------
AGENCY_NAME=Columbia Basin Insurance
AGENCY_PHONE=509-765-8839
CLIENT_RECAP_MODEL=gpt-4.1-mini        # reuses OPENAI_API_KEY
CLIENT_RECAP_MAX_CHARS=480             # ~4 SMS segments, disclaimer included
CLIENT_RECAP_AUTO_SEND=false           # text on approval with no second look

# The closing line per language. English and Spanish are built in; add any other
# language the agency serves and recaps in it stop falling back to English.
# CLIENT_RECAP_DISCLAIMER_EN=...
# CLIENT_RECAP_DISCLAIMER_ES=...
# CLIENT_RECAP_DISCLAIMER_VI=...
```

Nothing else needs installing. The tables (`client_recaps`, `sms_opt_outs`) are
created by the startup migrations.

### Checking the setup before you need it

Two things stop a first send, and neither is visible until a message is
refused: the RingCentral app not having the **SMS** permission, and
`RINGCENTRAL_SMS_FROM` naming a number that does not belong to the extension the
app authenticates as.

**Check sending setup** on the transcript page (or `GET /api/sms/setup`) asks
RingCentral directly and answers both. It lists every number on the extension,
marks which can send texts, and says plainly whether the configured number is
one of them:

> (509) 765-8839 is not one of the numbers this extension can text from.
> RingCentral will refuse it. Numbers that will work: (509) 555-0143.

Every recap goes out from the single number in `RINGCENTRAL_SMS_FROM` — there is
no per-agent sender — so that one value decides what every client sees.

### Before the first real send

- **Turn off `CLIENT_SMS_DRY_RUN`** only once you have drafted a few recaps and
  read them. In dry run the app records the send with provider `dry-run`, so
  nobody can mistake a test for a delivered message later.
- **Check A2P registration.** Carriers filter unregistered business texting in
  the US. RingCentral and Twilio both register numbers under 10DLC; if the
  agency's numbers are not registered, messages are accepted by the API and then
  quietly dropped by the carrier.
- **Confirm consent.** Texting clients about their policies needs their consent
  on file. The recap is a service message about a call they made, which is the
  strongest footing you can be on, but the agency's own consent language should
  cover it.

## Using it

On the transcript detail page, under **Client Recap Text**:

1. **Draft from call** writes a recap from the call analysis. If `OPENAI_API_KEY`
   is set it drafts with the model; if not — or if the API fails — it falls back
   to a plainer recap assembled from the same fields, so drafting never breaks.
2. Edit the wording and the destination number. The counter shows the length
   with the disclaimer included, and how many SMS segments that costs.
3. **Send Text**. The message is delivered, the row is marked sent, and it drops
   into the history below with the timestamp, number, provider, and who sent it.

Send is blocked, with the reason shown, when the transcript is not approved,
when there is no textable mobile number, when the number has opted out, or when
no provider is configured.

### Languages

English and Spanish are complete: recap and disclaimer both. Whisper's detected
language decides, and the page says which language the call was in before the
agent drafts.

For any other language, the recap itself is written in that language but the
closing disclaimer falls back to English, and the page says so in an amber
warning before the agent can send. Setting `CLIENT_RECAP_DISCLAIMER_<CODE>` for
that language clears the warning.

Two cases produce a deliberate English draft rather than a wrong one:

- **The model could not be reached.** The fallback recap is assembled from the
  English analysis fields, so it can only be English. The page says "This call
  was in Spanish, but the draft is in English" and leaves it to the agent.
- **Auto-send is on and the draft came back in the wrong language.** It is left
  as a draft instead of being sent, because that is exactly the case a person
  should look at.

Accented languages cost more per text: one character outside GSM-7 puts the
whole message into UCS-2, dropping a segment from 153 characters to 67. The
counter under the draft shows the real segment count as you type, so a Spanish
recap at 480 characters reads as 7 segments rather than 4. Lower
`CLIENT_RECAP_MAX_CHARS` if that matters to the bill.

### Which number gets texted

Inbound calls text the caller; outbound calls text the number dialled. Landlines,
extensions, and `Unknown` are rejected rather than dialled. The agent can always
override the number in the box before sending.

### Opt-outs

`POST /api/sms/opt-out` with `phone` (and an optional `note`) records a number
that has asked not to be texted; sends to it are refused from then on. Carrier
STOP handling does not notify the app, so opt-outs heard on the phone or seen in
the RingCentral inbox need recording here by hand.

## API

All routes require an admin session.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/transcripts/{id}/client-recap` | Draft, history, and whether it can be sent |
| `POST` | `/api/transcripts/{id}/client-recap/draft` | Write a draft from the call analysis |
| `PUT` | `/api/transcripts/{id}/client-recap` | Save edits (`body`, `to_number`) |
| `POST` | `/api/transcripts/{id}/client-recap/send` | Send it and record the outcome |
| `GET` | `/api/sms/setup` | Which numbers RingCentral will let this app send from |
| `POST` | `/api/sms/opt-out` | Record a number that must not be texted |

## What is stored

`client_recaps` — one row per draft, carried through to what was sent:

| Column | Meaning |
| --- | --- |
| `body` | The wording. After a send, exactly what the client received, disclaimer included |
| `language` | The language the body is actually written in |
| `english_gloss` | Plain English rendering of the body, for an English reader later. Never sent |
| `to_number` / `from_number` | E.164 |
| `status` | `draft`, `sent`, or `failed` |
| `source` | `ai`, `template`, or `manual` — how the wording was arrived at |
| `provider`, `provider_message_id`, `provider_status` | For tracing a message back to RingCentral or Twilio |
| `error` | Why a failed send failed |
| `sent_by`, `sent_at` | Who pressed Send, and when |

A failed send is kept, not discarded. "We tried and the carrier rejected it" is
part of the record too.
