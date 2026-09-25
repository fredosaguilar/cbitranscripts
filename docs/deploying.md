# Deploying

The app runs on Railway. There are two ways it gets there, and it is worth
knowing which one is live, because they fail differently.

## What starts the app

```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

That is the `Procfile`, and it is the only place in the repository that says
how to start the app. It was added when auto-deploy was set up: before that
the start command existed solely as a setting inside the Railway service,
invisible to anyone reading the code and lost the day the service is recreated.

A **Custom Start Command** set on the Railway service still overrides the
Procfile. So adding this changed nothing about how the app runs today; it
means a service built from scratch would run it correctly.

## Auto-deploy from GitHub

Railway watches `master`. Merging a pull request is the deploy — there is no
separate step and no approval gate, so a merge reaches clients' email.

Railway service → **Settings → Source** → connect `fredosaguilar/cbitranscripts`,
branch `master`.

Environment variables live on the Railway service, not in the repository, and
are untouched by changing the source.

## Deploying by hand

`deploy.ps1` is the fallback, and the way to ship without merging. It runs on
Windows and needs the Railway CLI (`npm install -g @railway/cli`, then
`railway login` once):

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

It clones `master` fresh into `C:\cbi-deploy` and runs `railway up`. It clones
rather than uploading a working copy on purpose: an earlier version uploaded
whatever was on disk, which twice deployed stale code while reporting the right
commit.

## Confirming what is actually running

```
https://cbitranscripts.up.railway.app/healthz
```

```json
{"status": "ok", "build": "15af9aa7b087"}
```

`build` is a hash of `main.py`, `client_note_email.py`, `transcription.py`,
`ringcentral_utils.py` and `templates/transcript_detail.html`, so it changes
whenever the source does and needs no version to be remembered and bumped. Line
endings are normalised first, so a Windows clone and the repository agree.

To work out what the value *should* be for a commit:

```bash
git fetch origin master
python3 - <<'PY'
import hashlib, subprocess
digest = hashlib.sha256()
for name in ("main.py", "client_note_email.py", "transcription.py",
             "ringcentral_utils.py", "templates/transcript_detail.html"):
    digest.update(subprocess.check_output(["git", "show", f"origin/master:{name}"]).replace(b"\r\n", b"\n"))
print(digest.hexdigest()[:12])
PY
```

If the deployed value does not match, the new build has not taken over yet — or
the upload never reached the service, which is the failure this endpoint exists
to make visible.
