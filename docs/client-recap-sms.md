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
produces a Spanish recap — opening line and opt-out included. The analysis
fields themselves are stored in English, so the translation happens at drafting
time, and the draft comes back with a plain-English rendering shown beside it so
an English-reading agent can check a message they cannot read before it goes.
That gloss is stored with the record but never sent.

**It never confirms coverage.** The draft is written from the analysis fields —
what was discussed, what options were presented, what the client chose, what
was recommended — and the model is instructed never to state that anything is
bound, added, removed, or in force.

Every message goes out in three parts, and only the middle one is the agent's:

```
Summary of our recent call. This is not confirmation of coverage. Reply if anything here is wrong.

Maria, here is what we went over:
Why you called: ...
Your decision: ...

Reply STOP to opt out.
```

and in Spanish:

> Resumen de nuestra llamada reciente. No es una confirmación de cobertura:
> responda si algo aquí no es correcto.
>
> …
>
> Responda STOP para cancelar.

The opening line is where the legal work is done, and it opens the message
rather than closing it because that is where it is read. A text that starts with
policy details reads like a confirmation no matter what the small print says at
the bottom; one that names itself a summary of the call in its first six words
cannot be mistaken for one. The opt-out is left at the end and kept to a single
short sentence — it is a carrier requirement, not a message, and every character
it costs comes out of what the client is actually being told.

`STOP` stays in English in every language — carriers match that exact keyword to
process an opt-out, so translating it would break the opt-out itself.

That opening line is the point of the whole feature. A client who lets it stand
has been told in writing; a client who corrects it has told you something you
needed to know before the claim.

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
CLIENT_RECAP_MAX_CHARS=480             # ~4 SMS segments, fixed lines included
CLIENT_RECAP_AUTO_SEND=false           # text on approval with no second look

# The opening and closing lines for a language the code does NOT already carry.
# English and Spanish are written into client_recap.py and cannot be changed
# from here: those two lines do the legal work, so what they say belongs in the
# code, where it shows up in a diff and is the same everywhere the app runs.
# Setting one of these for a built-in language has no effect.
# CLIENT_RECAP_HEADER_VI=...
# CLIENT_RECAP_OPT_OUT_VI=...
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

Every recap goes out from the single number in `RINGCENTRAL_SMS_FROM`, whichever
agent took the call and whichever extension it came in on — there is deliberately
no per-agent sender. A client should recognise the number, and one number means
one inbox for the replies the recap invites. The sender is stored on each recap
row as it is sent, so the record shows the number the client actually saw.

The two permissions are separate, and only one of them blocks sending:

- **SMS** is what sending needs. Without it every send is refused, and the check
  says so from the token's own scopes without calling anything.
- **ReadAccounts** is what *listing the numbers* needs. An app set up for call
  recordings will not have it, and the check then says it cannot confirm the
  sender belongs to the extension rather than reporting a failure — sending may
  well work anyway. A test send with dry run off is the remaining check.

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
2. Edit the wording and the destination number. You are writing the middle of
   the message; the opening and the opt-out are shown below the box and added
   for you. The counter shows the length with both included, and how many SMS
   segments that costs.
3. **Send Text**. The message is delivered, the row is marked sent, and it drops
   into the history below with the timestamp, number, provider, and who sent it.

Send is blocked, with the reason shown, when the transcript is not approved,
when there is no textable mobile number, when the number has opted out, or when
no provider is configured.

### Languages

English and Spanish are complete: recap, opening line and opt-out. Whisper's
detected language decides, and the page says which language the call was in
before the agent drafts. Their two fixed lines live in `_BUILT_IN_HEADERS` and
`_BUILT_IN_OPT_OUTS` in `client_recap.py` and are edited there — no environment
variable can change them, deliberately, so what a client is told cannot drift
from what the code says.

For any other language, the recap itself is written in that language but the
opening and opt-out fall back to English, and the page says so in an amber
warning before the agent can send. Setting `CLIENT_RECAP_HEADER_<CODE>` and
`CLIENT_RECAP_OPT_OUT_<CODE>` for that language clears the warning; writing it
into the two dictionaries instead is better, and makes it settled the same way.

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
| `body` | The wording. After a send, exactly what the client received, opening and opt-out included |
| `language` | The language the body is actually written in |
| `english_gloss` | Plain English rendering of the body, for an English reader later. Never sent |
| `to_number` / `from_number` | E.164. The sender is recorded per message, so the record shows which number the client was actually texted from rather than which one was configured at the time |
| `status` | `draft`, `sent`, or `failed` |
| `source` | `ai`, `template`, or `manual` — how the wording was arrived at |
| `provider`, `provider_message_id`, `provider_status` | For tracing a message back to RingCentral or Twilio |
| `error` | Why a failed send failed |
| `sent_by`, `sent_at` | Who pressed Send, and when |

A failed send is kept, not discarded. "We tried and the carrier rejected it" is
part of the record too.
