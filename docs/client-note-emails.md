# Client file-note emails

After a call is transcribed, analysed and approved, the agency can email the
client the file note kept on that call. Every draft and every send is kept, so
months later you can show exactly what wording went to which address and when.

> **The recap text messages were removed.** Sending by SMS is gone: the panel,
> the endpoints, `client_sms.py` and `client_recap.py`. The `client_recaps` and
> `sms_opt_outs` tables are deliberately left in place — texts that went to real
> clients are evidence, and dropping the record of them to tidy up the code
> would destroy the very thing the feature existed to create. The history below
> describes that feature as it was; it is kept because the reasoning still
> applies to the email, which works the same way.

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
Summary of your recent call with Columbia Basin Insurance. This is not confirmation of coverage. Reply if anything here is wrong.

Maria, here is what we went over:
Why you called: ...
Your decision: ...

Reply STOP to opt out.
```

and in Spanish:

> Resumen de su llamada reciente con Columbia Basin Insurance. No es una
> confirmación de cobertura: responda si algo aquí no es correcto.
>
> …
>
> Responda STOP para cancelar.

The agency is named there because a text from a number the client may not have
saved is otherwise unattributed, and an unattributed message about someone's
policy is one they are entitled to ignore. `AGENCY_NAME` sets it.

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

**The wording is read back before an agent can send it.** The model is told to
avoid anything that reads as confirming coverage, and an instruction is not a
guarantee — nor does it reach a message an agent typed by hand, or the analysis
fields the fallback draft copies verbatim. So every draft, however it was made,
is scanned for the phrasings that turn a record into a promise: "you're
covered", "fully covered", "all set", "guaranteed", "approved", "in force", and
their Spanish equivalents, accents or not.

A match raises an amber warning above the Send button naming the exact phrase.
It never edits and never blocks: silently rewording a message an agent is about
to put their name to would be worse than the risk it removes. A false positive
costs one sentence reread.

**What was sent is never rewritten.** Rows in `client_recaps` are immutable
once sent. Editing after a send creates a new row, so the record of what the
client actually received survives later edits.

## Configuration

Add to `.env`:

```ini
# --- sending ---------------------------------------------------------------
# The agency's main number. Every text goes out from this and nothing else.
# It reuses the existing RINGCENTRAL_* JWT credentials — nothing else to set up.
# Defaults to AGENCY_PHONE, so a missing value still sends from the main line.
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

# --- emailing the file note ------------------------------------------------
# The address the client sees and would write back to. The SMTP account must
# be allowed to send as it; see "The From address" below.
CLIENT_EMAIL_FROM=info@columbiabasininsurance.com
CLIENT_NOTE_EMAIL_SUBJECT=Notes Added to Your File
AGENCY_ADDRESS=21 D St SW, Suite A, Quincy, WA 98848
# CLIENT_EMAIL_SIGNATURE=...   # replaces the whole sign-off block

# The opening and closing lines for a language the code does NOT already carry.
# English and Spanish are written into client_recap.py and cannot be changed
# from here: those two lines do the legal work, so what they say belongs in the
# code, where it shows up in a diff and is the same everywhere the app runs.
# Setting one of these for a built-in language has no effect.
# CLIENT_RECAP_HEADER_VI=...
# CLIENT_RECAP_OPT_OUT_VI=...
```

Nothing else needs installing. The tables (`client_recaps`, `client_note_emails`,
`sms_opt_outs`) are created by the startup migrations.

### The sending number

Every recap goes out from the agency's main number, whichever agent took the
call and whichever extension it came in on. There is deliberately no per-agent
sender: a client should recognise the number, and one number means one inbox for
the replies the recap invites. The sender is stored on each recap row as it is
sent, so the record shows the number the client actually saw.

RingCentral's SMS endpoint is scoped to an extension and refuses a sender that
extension does not own:

> FeatureNotAvailable — Phone number doesn't belong to extension (MSG-304)

A main company number usually lives on the auto-receptionist rather than on the
user the app signs in as, which is exactly when this appears. The app handles it
without changing the sender: it sends as its own extension first, and on that
specific refusal looks up which extension actually holds the main number and
re-sends from there. The answer is cached, so it costs one lookup.

That lookup needs the **ReadAccounts** permission. Without it the app cannot
find the owning extension, and the send fails with a message saying so — the fix
is then either to grant ReadAccounts, or to assign the main number to the
extension the app signs in as. Sending itself needs the **SMS** permission; no
amount of configuration substitutes for it.

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

## Emailing the file note

The **Email the File Note** panel on the transcript page sends the CRM note
itself — the words that go on the file — so what the agency wrote down and what
the client was told are provably the same.

It goes from `info@columbiabasininsurance.com` (`CLIENT_EMAIL_FROM`) with the
subject "Notes Added to Your File" (`CLIENT_NOTE_EMAIL_SUBJECT`), and reads:

```
Hello Maria,

Here is a summary of your conversation with Fred Aguilar and the notes we added to your file for our record retention:

Client called to make a payment of $287.12 on the liability coverage. ...

Columbia Basin Insurance
Phone: 509-765-8839
Email: info@columbiabasininsurance.com
Office: 21 D St SW, Suite A, Quincy, WA 98848

Licensed in Washington. Coverage descriptions here are summaries only - your policy language governs.
```

The agent named is the one the call is assigned to, by the name recorded against
that user; failing that, the name on the RingCentral extension the call came in
on, which is still who the client spoke to. A call with neither drops the clause
rather than trailing off — "a summary of the notes we added to your file".

The sign-off matches the one on the agency's other client mail. It is built from
`AGENCY_NAME`, `AGENCY_PHONE`, `CLIENT_EMAIL_FROM` and `AGENCY_ADDRESS`, so
changing the phone number in one place changes it here too;
`CLIENT_EMAIL_SIGNATURE` replaces the whole block. The licensing line is not
decoration — this email carries a summary of somebody's policy file, and it says
in the same breath that the policy language governs.

**Read it before it goes.** The note was written for the file, not for the
client. A note meant for internal use can carry an assessment of the caller, a
doubt about something they said, or an E&O flag raised for the agency's own
benefit — none of which improves for being emailed to them. Nothing sends on its
own: the note is composed into a draft, sits in an editable box, and goes only
when someone has read it and pressed Send. Subject, body and address are all
editable first.

Send is blocked, with the reason shown, when the transcript is not approved,
when the call has no CRM note, when there is no email address, or when SMTP is
not configured.

The client's address is not stored on a transcript, so it is prefilled from the
linked Agency Zoom record when there is one and typed by the agent when there is
not. Guessing an address for a letter about somebody's policy is not a thing to
do quietly.

Every draft and send is kept in `client_note_emails`, the same way recap texts
are kept — a refused send is recorded as `failed` with the reason, because what
was attempted is part of the record too.

### The From address

`CLIENT_EMAIL_FROM` only sets the header. The SMTP account still has to be
permitted to send as that address; a provider that refuses an unauthorised From
returns an error, which the page shows rather than silently rewriting the sender:

> The mail server refused to send as info@columbiabasininsurance.com. The SMTP
> account has to be allowed to send from that address.

In Google Workspace or Microsoft 365 that means adding the address as a
send-as alias on the mailbox in `SMTP_USER`, or authenticating as that mailbox
directly.

### Previewing from the transcripts list

Every row on the transcripts page has a **Preview email** link beside *View
Details*. It shows the From, To and Subject, and the message itself, without
saving anything or sending anything — so a whole afternoon of calls can be
checked without opening each one.

What it shows depends on where that call has got to:

| State | Shown |
| --- | --- |
| Already sent | Exactly what the client received, and when |
| An unsent draft exists | The draft, edits included — what would go if sent |
| Nothing drafted yet | What drafting would produce |
| No CRM note on the call | Nothing to send, and says so |

A sent email shows the stored body rather than recomposing it. Recomposing
would display words nobody was ever sent, and the note may well have been
edited since it went.
