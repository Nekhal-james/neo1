# Neo — security

Two things on this robot are reachable over a network that is not a private
cable, and both are dangerous if left open:

| Exposed thing | What an attacker gets | Control |
|---|---|---|
| The off-board inference service on the laptop | An open LLM proxy on the campus network, running on your daily-driver machine | Mutual TLS, interface binding, firewall |
| The admin panel on the Pi | Drives the servos and rewrites the campus dataset | HTTPS + argon2 login + login rate limiting |

Neither is a hardening pass to do later. The plan
([§0.3.1](IMPLEMENTATION_PLAN.md)) makes them structural, and there is a
practical reason beyond principle: **browsers only grant camera and microphone
access in a secure context**, so without a certificate the browser accepts, the
webapp camera and mic backends do not work at all from any device other than the
Pi itself. TLS here is a functional requirement, not just a safety one.

---

## 1. One CA, three certificates

```
                    Neo private CA  (certs/model_conn/ca-cert.pem + ca-key.pem)
                    born on the laptop, key never leaves it
                              |
       +----------------------+----------------------+
       |                      |                      |
  server cert            client cert            panel cert
  "neo-model-conn-host"  "neo-model-conn-       "neo-admin-panel"
                          receiver"
  on the LAPTOP           on the PI              on the PI
  SAN: eth IP +           no SAN (never          SAN: neo, neo.local,
       neo-brain.local     matched by name)           neo-pi, neo-pi.local,
                                                      the Pi's IPs
  10 years                10 years               397 days
       |                      |                      |
       +------ mutual TLS ----+                      |
              Pi <-> laptop                    HTTPS to your
                                               phone / laptop browser
```

**Why one CA and not two.** The panel certificate could trivially be
self-signed — `neo --webapp devcert` still does exactly that. But then every
device you browse from shows a warning you have to click past, and a warning you
have always clicked past is a warning you will click past on the day it matters.
One CA means **one install on your phone**, after which both the panel and the
link are trusted silently. Two CAs, in practice, means one of them never gets
installed.

**Why the panel certificate expires in 397 days and the others in ten years.**
The panel's is the only certificate a browser ever sees, and browsers police
server-certificate lifetimes — Apple platforms reject TLS server certificates
valid for more than 398 days. Whether that limit applies to a root you installed
yourself has varied by OS version, so Neo stays inside it rather than depending
on the exemption. The Pi-to-laptop certificates never meet a browser, so they
keep the long lifetime and stay out of your way.

> If Safari or an iPhone refuses the panel while Chrome and Firefox accept it,
> a panel certificate with too long a lifetime is the first thing to suspect.

---

## 2. Setting it up

### 2.1 On the laptop — create the CA and the link certificates

```bash
neo --tls init
```

Idempotent for the CA: re-running never overwrites it. The server and client
certificates *are* reissued each time, which is what you want after adding a new
address to `receiver.endpoints` — the server certificate's SAN has to cover
whichever name or address the Pi will actually dial, or validation fails on the
path you were relying on for failover.

It writes, all under `certs/model_conn/`:

| File | Stays on | Purpose |
|---|---|---|
| `ca-cert.pem` | both machines | The trust anchor. Public; safe to copy anywhere. |
| `ca-key.pem` | **laptop only** | Signs new certificates. Whoever holds this can impersonate every machine here. |
| `server-cert.pem` / `server-key.pem` | **laptop only** | What the inference service presents. |
| `client-cert.pem` / `client-key.pem` | **Pi only** | What the Pi presents back. |

### 2.2 Copy to the Pi

```bash
scp certs/model_conn/{ca-cert.pem,client-cert.pem,client-key.pem} \
    neo@neo-pi.local:~/neo1/certs/model_conn/
```

The server certificate and key stay on the laptop. `ca-key.pem` stays on the
laptop too — *unless* you want to issue the panel certificate on the Pi, which
needs it (see below).

### 2.3 Turn mTLS on — on **both** machines

In `config/model_conn.local.yaml` (gitignored, per machine):

```yaml
tls:
  enabled: true
```

It defaults to `false` so that a first run on one laptop works before any
certificates exist. That path is deliberately loud: `tls.warn_insecure()` prints
a banner on every process start. If you ever see it on the robot, the link is
unauthenticated.

### 2.4 On the Pi — the admin panel certificate

Signing needs the CA key, so pick one:

- **Simplest:** copy `ca-key.pem` to the Pi, run the command below, then delete
  it from the Pi. The key exists on the Pi only for the seconds it takes.
- **Stricter:** run `neo --tls panel` on the laptop, then copy the resulting
  `certs/panel/` to the Pi. The CA key never moves at all. You must make sure
  the SANs match the Pi's names, not the laptop's — check the printed list.

```bash
neo --tls panel
```

Run it **after** the Pi's first reboot following `scripts/pi/setup-system.sh`.
The certificate names the machine's hostname and `<hostname>.local`, plus every
IPv4 address on its interfaces — so the static `192.168.50.2` on the laptop
cable is only included once that address exists. See
[pi-setup.md](pi-setup.md).

Then point the panel at it, in `config/webapp.local.yaml`:

```yaml
server:
  tls:
    enabled: true
    certfile: certs/panel/panel-cert.pem
    keyfile: certs/panel/panel-key.pem
```

The committed `config/webapp.yaml` still points at the self-signed
`certs/dev-cert.pem` so a fresh checkout works with no CA at all. Overriding it
in the `.local.yaml` is the intended path for real use.

### 2.5 Install the CA on the devices you browse from

This is the step that stops the warnings. `ca-cert.pem` is a public certificate —
mailing it to yourself is fine. **Never** send `ca-key.pem` anywhere.

- **iOS/iPadOS:** open the file, Settings → Profile Downloaded → Install, then —
  and this is the part everyone misses — Settings → General → About → Certificate
  Trust Settings and switch it **on**. Installing alone does nothing.
- **Android:** Settings → Security → Encryption & credentials → Install a
  certificate → CA certificate.
- **macOS:** open in Keychain Access (System keychain), then set it to "Always
  Trust".
- **Windows:** import into "Trusted Root Certification Authorities" for the
  local machine.
- **Linux:** copy to `/usr/local/share/ca-certificates/` and run
  `sudo update-ca-certificates`. Firefox keeps its own store — import it there
  separately.

### 2.6 The admin account

```bash
neo --webapp setup
```

One admin account: argon2 password hash and the session secret land in
`config/webapp.local.yaml`, which is gitignored. There is no multi-user or role
support and none is planned — one operator, one account.

`neo --webapp setup` also prints a **recovery code**, once. Keep it somewhere
away from the robot.

- **Changing the password**, signed in: **Password** in the panel's header. It
  asks for the current password (wrong guesses count toward the login lockout)
  and saving rotates the session secret, which signs out every other browser.
  The same dialog makes a new recovery code, replacing the old one, and also
  needs the current password.
- **Forgot the password**: **Forgot password?** on the sign-in page. Enter the
  recovery code and a new password. The code works once; a replacement is
  shown on the spot, every other browser is signed out, and you are signed in.

Why a code and not a reset link: the robot has no email to send one with, and a
reset that needed less than a secret only the owner holds would let anyone who
can reach the panel on the campus network take over a robot that moves and
talks. Codes are about 100 bits of randomness, stored only as argon2 hashes, and
a wrong one counts as a failed login. Lost the code too? `neo --webapp setup`
from a shell on the robot still resets everything.

Defaults in `config/webapp.yaml`: 12-hour sessions, 5 failed attempts, 5-minute
lockout window. The session cookie is `Secure` + `HttpOnly` + `SameSite`, which
is another reason HTTPS is not optional: a `Secure` cookie is simply not set over
plain HTTP.

---

## 3. Where secrets live

```
certs/                        gitignored in full
├── model_conn/               CA + link certificates
├── panel/                    admin panel certificate
└── dev-cert.pem, dev-key.pem self-signed fallback

config/*.local.yaml           gitignored: password hash, session secret,
                              per-machine addresses, tls.enabled
```

`.gitignore` covers `certs/` and `config/*.local.yaml`. Nothing secret belongs in
a committed YAML — the committed files hold defaults and comments only.

Note that the campus dataset is *not* a secret but is the project's most valuable
asset and lives on an SD card. That is a backup problem, not a security one; see
the plan's §7.3 and its risk register.

---

## 4. Verifying it actually works

```bash
# From the Pi: the link, with the client certificate. Should return JSON.
curl --cacert certs/model_conn/ca-cert.pem \
     --cert   certs/model_conn/client-cert.pem \
     --key    certs/model_conn/client-key.pem \
     https://neo-brain.local:11434/api/tags

# The same call WITHOUT the client certificate. This MUST fail.
curl --cacert certs/model_conn/ca-cert.pem \
     https://neo-brain.local:11434/api/tags

# The panel, from a machine with the CA installed. No -k, no warning.
curl https://neo-pi.local:8443/api/health
```

The second command is the one that matters. If it succeeds, the service is
accepting unauthenticated clients and is an open LLM proxy on your network —
stop and fix it before anything else.

`neo --connection:status` and `neo --connection:ping` exercise the same path
with the same certificates and are the quicker day-to-day check.

Two things worth knowing about the network itself, both from the plan's Phase 0:

- **Many campus Wi-Fi networks enable AP isolation**, which blocks
  client-to-client traffic outright. If yours does, the Wi-Fi failover path does
  not exist no matter how correct the code is. One `curl` tells you.
- When `tls.enabled` is true, Ollama itself binds loopback-only
  (`internal_ollama_port`) and only the mTLS proxy is reachable from the network.
  Confirm with `ss -tlnp` that nothing else is listening on an external
  interface.

---

## 5. Rotation and recovery

| Situation | What to do |
|---|---|
| Panel certificate expired (yearly) | `neo --tls panel` on the Pi. No CA change, so no device needs touching again. |
| Laptop changed address or name | `neo --tls init` on the laptop, re-copy the client cert/key to the Pi. |
| CA key exposed | Delete `certs/`, `neo --tls init`, `neo --tls panel`, re-copy everything, reinstall the CA on every device. There is no revocation here — reissuing the CA is the revocation. |
| Pi lost or stolen | Same as above. The client certificate on it is valid until the CA is replaced. |
| Forgot the panel password | **Forgot password?** on the sign-in page, with the recovery code. No code: `neo --webapp setup` again from a shell on the Pi, which overwrites the hash and prints a new code. |

There is deliberately no CRL or OCSP. For three certificates under one person's
control, replacing the CA is simpler and harder to get wrong than running
revocation infrastructure.

---

## 6. The rules that do not bend

1. **Never run the off-board inference service without mTLS** — not "briefly for
   testing", not "only on the home network". An unauthenticated inference
   endpoint is an open proxy for anyone who can route to it.
2. **`ca-key.pem` never leaves the machine that owns it**, except for the
   deliberate, temporary copy in §2.4.
3. **No secrets in committed files.** Certificates and `*.local.yaml` are
   gitignored; keep it that way.
4. **The panel is never exposed without authentication**, on any network, for
   any duration. It can move the servos and rewrite the campus dataset.
5. **If you see the `warn_insecure` banner on the robot, stop.** It means the
   link is running unauthenticated, and it prints precisely so that this can
   never be a silent state.
