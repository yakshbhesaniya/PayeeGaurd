# PayeeGuard: presentation script (simple English)

Total: about 6 minutes + questions. Screen: the desk on the left (2/3), Slack #payeeguard-alerts on the right (1/3).
Before you start: `python web.py`, open http://127.0.0.1:8787, click **Reset**, check the lights are green.

---

## Part 1. The problem (30 seconds)

> "Every company pays its vendors. Sometimes an email says: *here is our invoice, and by the way, we changed our bank account.*
> Sometimes that email is real. Often it is a scammer.
> Two in five businesses have been hit by this, and one incident costs about **$367,000** on average.
> Today a person in the finance team has to check every payment by hand. And now AI agents are starting to pay invoices on their own, and nobody checks who they are paying."

## Part 2. What we built (30 seconds)

> "We built **PayeeGuard**. It does the job of that finance checker, automatically.
> An invoice email comes in. PayeeGuard checks **who** sent it, **where** the money is going, and what **our own team** said about this vendor.
> Then it decides: **pay, hold, or block**. It explains why, with a source for every fact.
> And it does the paperwork: a payment, a callback script, a warning email, or a fraud case file.
> A human only steps in for the unclear cases."

## Part 3. How it works, in simple words (45 seconds)

> "It works in five steps.
> **One, read** the email: who sent it, how much, which bank account.
> **Two, check who**: we ask Crustdata's identity graph. Is this a real company? Is this its real website? Does this person really work there?
> **Three, follow the money**: we keep a memory of every payment. Have we ever paid this vendor to this bank account? Was this account used in a scam before?
> **Four, check our own history**: we read our Slack and our emails. Did someone in our team say *we stopped working with this vendor*?
> **Five, decide and finish the job**: the decision is made by simple fixed rules, not by an AI guessing. The same email always gets the same answer."

---

## Part 4. Live demo (about 4 minutes)

### Demo 1: an obvious scam (40 seconds)

**Click:** Inbox → email **05** (Zoho invoice).

> "Here is an email that says it is from Zoho, asking us to pay a UK bank account.
> Look at the sender: **zoh0-billing.com**, with a zero instead of an O. Crustdata knows Zoho's real website, and this is a fake copy.
> PayeeGuard says **BLOCKED**."

**Point at Slack:**
> "And without anyone clicking, an alert just appeared in our Slack.
> PayeeGuard also remembers this bank account as part of a scam."

### Demo 2: the hardest case (90 seconds) ← the most important part

**Click:** email **06** (Freshworks invoice).

> "Now a much harder one. This email comes from **billing@freshworks.com**, the real Freshworks address. It is polite. It looks perfect.
> Crustdata confirms: the company is real, the website is theirs, and they even have an office in London."

**Point at the WHO and CHANNEL ticks:**
> "Every normal check passes. Most tools would pay this."

**Point at the BLOCKED stamp and the money trail:**
> "But PayeeGuard **follows the money**. We have paid Freshworks **14 times**, always to the same US account, never to this UK account.
> And this exact UK account is the one the Zoho scammer used a minute ago.
> So this is a hacked Freshworks mailbox, used by the same scam group. **Blocked.**"

**Point at the crossed-out chips:**
> "It also tells us what it is **not**: it is not a fake company, not a fake website, not a wrong country."

**Click:** Finished work → Fraud case file.
> "It has already written a fraud case file that links both scam attempts, ready to send to the police."

**Click:** **Release payment →**
> "Even if a person or an AI agent tries to pay anyway, the **payment gate refuses**."

### Demo 3: fully automatic, our own Slack (60 seconds)

**Point at Slack**, at the message *"We will not continue with Freshworks…"*
> "Some time ago, someone in our team wrote this in Slack: *we will not continue with Freshworks, don't send more invoices.*"

**Send the invoice email from your phone to drbahulmishra@gmail.com. Don't touch the laptop.**
> "Now Freshworks sends a normal invoice to our real inbox. I am not clicking anything."

**Wait ~10 seconds. It appears in the Live mailbox. Click it.**
> "PayeeGuard picked it up by itself. It read our Slack, found that message, and stopped the payment:
> *Invoice from a vendor we ended the relationship with*, with the exact Slack message as proof.
> And the alert is already in Slack."

### Demo 4: the human part (40 seconds)

**Click:** email **04** (on hold).
> "Some cases are not clearly fraud. Here the vendor is real but asks for a new bank account. PayeeGuard does not block it. It **holds** it for a human."

**Click:** Callback script → **Call (official number)**
> "It gives us the vendor's official phone number from Crustdata, never the number from the email, because a scammer would put his own number there."

**Click:** Verification email → **Send now**
> "With one click, the verification email is really sent."

**Type your name and a note, click Approve, then Release payment.**
> "After the call, a person approves, and the payment is released. PayeeGuard now remembers this new account as trusted."

### Demo 5: check all vendors at once (25 seconds)

**Click:** Vendors tab → **Run on sample list**.
> "We can also check our whole vendor list. It finds one bank account shared by two different vendors, a duplicate vendor, and a fake look-alike vendor. You can't see these from one invoice."

### Demo 6: memory (15 seconds)

**Click:** **Memory** (top right).
> "This is what PayeeGuard has learned: our vendors, the accounts we pay them, and the accounts used in scams. It remembers everything, even after a restart."

---

## Part 5. Why you can trust it (30 seconds)

> "Four things make it safe.
> **One:** missing information is never treated as fraud. If we don't know a vendor, we hold and ask a person. We never block an honest small business just because it's unknown.
> **Two:** an AI never makes the decision. AI can read messy emails, but fixed rules decide, so nobody can trick it with a clever email.
> **Three:** it never moves money or emails a vendor on its own. In this demo the payment is simulated and the emails come to us.
> **Four:** privacy: we never store full bank account numbers, and we only read the emails and messages about that vendor."

## Part 6. How we know it works (20 seconds)

> "We tested it heavily: **368 automatic tests**, **13 out of 13** real test cases correct with live Crustdata data, and the same cases still correct when we rewrite the emails in different ways.
> While testing, we found and fixed **27 bugs**, and we wrote down every one in our report."

## Part 7. Business and next steps (20 seconds)

> "Who pays for this? Finance teams, audit firms and AI payment platforms. A few cents of Crustdata data per invoice can prevent a loss of hundreds of thousands of dollars.
> Next, we would connect to banks to confirm who owns an account, connect to accounting software like Tally and Zoho Books, and connect the live GST portal."

## Part 8. Closing line (10 seconds)

> "PayeeGuard: every identity check can pass, but the money trail doesn't lie.
> **Know who you pay.** Thank you."

---

## Questions judges may ask (short answers)

| Question | Answer |
|---|---|
| Is an AI making the decision? | "No. AI helps read emails. Fixed rules decide, so it's repeatable and can't be tricked." |
| What if Crustdata doesn't know the vendor? | "We never call that fraud. We hold it and give a person a checklist to verify the vendor." |
| Is the money real? | "No, on purpose. The payment gateway is simulated or in test mode. A human releases real payments." |
| Can you prove a bank account belongs to the vendor? | "Not yet. That needs a bank partnership. We check the request: who sent it, our history, and our own team's messages." |
| What about privacy? | "We store only a coded fingerprint of bank accounts, we don't keep emails, and the inbox is read-only." |
| How is this different from other tools? | "Other tools check bank accounts. We check the whole story: the person, the company, the money trail and our own conversations." |
| Why Crustdata? | "One clean identity for every company and person, with their job history. That's what lets us catch an ex-employee or a fake company." |
| What if the internet fails on stage? | "It runs from saved data in offline mode." |

## If something goes wrong

| Problem | What to do |
|---|---|
| The email doesn't appear | Click **+ Paste an email** and paste the same text: same result instantly |
| Email 06 shows HOLD, not BLOCK | You opened 06 before 05. Click **Reset**, open 05, then 06 |
| Slack alert is slow | Ignore it; the desk shows the same result |
| Internet stops | Restart with `PAYEEGUARD_OFFLINE=1 python web.py` and skip Demo 3 |
