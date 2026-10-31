# PayeeGuard: Product Demo Video Script

ContextCon 03 · IIT Bombay · 2026-10-31 · Live doc: https://claude.ai/artifact/49KmQwS6PWsqmz5eCeZng5

A user story for the Loom demo: one person, one working morning, showing how they use the PayeeGuard desk and how it helps. No technical detail.

## Run of show

The video follows one person through one working morning in six scenes. It runs 2:00, about 300 spoken words. Every screen named here exists in the desk today.

**Our user:** Riya runs accounts payable at a mid-size company. Riya pays 40 suppliers every month and is the last person between an email and the company's money.

| Time | Scene | What Riya does | Screen | File |
| --- | --- | --- | --- | --- |
| 0:00–0:15 | 1. Monday inbox | Opens the desk | Inbox and "Who are you really paying?" | 00_desk_home.png |
| 0:15–0:30 | 2. The normal invoice | Clicks the Freshworks invoice | CLEARED TO PAY | 01_case.png |
| 0:30–0:50 | 3. The unsure one | Clicks the Gmail invoice | ON HOLD, call and email ready | 03_case.png |
| 0:50–1:05 | 4. The fake one | Clicks the Zoho invoice | BLOCKED, lookalike | 05_case.png |
| 1:05–1:35 | 5. The perfect one | Clicks the second Freshworks invoice, tries to pay | BLOCKED, money trail, payment refused | 06_case.png, 06_hero_full.png |
| 1:35–2:00 | 6. The whole list, and the close | Opens Vendors, runs the audit | ALERTS, then Memory | 12_vendors.png, 11_memory.png |

## Scene 1: Monday inbox (0:00–0:15)

**Screen:** the desk opens. The inbox of supplier emails sits on the left; the big headline reads "Who are you really paying?"

**Action:** none. Let the inbox sit for a beat, then slowly move the cursor down the list.

> This is Riya. Riya pays the company's suppliers. Every Monday, the inbox fills up with invoices, and a few of them ask to send money somewhere new.
>
> One wrong click can cost hundreds of thousands. So Riya doesn't open these emails alone anymore. Riya opens them in PayeeGuard.

## Scene 2: The normal invoice (0:15–0:30)

**Screen:** the October invoice from Freshworks. A green **CLEARED TO PAY** stamp lands. Three boxes are ticked: **WHO**, **CHANNEL**, **MONEY**.

**Action:** click invoice 01. Hover over the three ticked boxes, left to right.

> First, the monthly software bill. PayeeGuard answers three questions in about two seconds. Is this a real company? Did the email really come from them? Is the money going where it always goes?
>
> Three ticks. Cleared to pay. Riya moves on.

## Scene 3: The unsure one (0:30–0:50)

**Screen:** an invoice from someone calling themselves Freshworks' Finance Manager, sent from a Gmail address, asking to pay a new account. An amber **ON HOLD** stamp. The headline reads "Unverified person on a personal mailbox."

**Action:** click invoice 03. Point at the Gmail sender. Scroll to the finished work and show the **Call (official number)** and **Open in my mail app** buttons.

> Next: someone says they're Freshworks' finance manager, writing from Gmail because "our server is down." Maybe true. Maybe not.
>
> PayeeGuard doesn't guess. It puts the payment on hold and gets the work ready: a call script with Freshworks' real phone number, found on its own, not copied from the email, and a verification email to send.
>
> One call, and Riya knows.

## Scene 4: The fake one (0:50–1:05)

**Screen:** a Zoho invoice from `zoh0-billing.com`, with a zero instead of an "o", asking to pay a UK account. A red **BLOCKED** stamp. The headline reads "Lookalike-domain impersonation."

**Action:** click invoice 05. Zoom in on the sender address so the zero is visible.

> Then this one. Look closely: Zoho, spelled with a zero. A fake.
>
> Blocked instantly. And PayeeGuard remembers that UK bank account, because fraudsters reuse their accounts.

## Scene 5: The perfect one (1:05–1:35)

This is the moment the video exists for. Slow down here.

**Screen:** a Freshworks invoice from the real `billing@freshworks.com` address. **WHO** and **CHANNEL** are ticked, **MONEY** is not. A red **BLOCKED** stamp. The headline reads "Account linked to a prior blocked attack." The money-trail picture shows the payment heading to the same UK account from the fake Zoho email.

**Action:**

1. Click invoice 06. Pause on the two ticks.
2. Scroll to the money trail. Trace the red line with the cursor: invoice → UK account → blocked case.
3. Show the **Fraud case file** in the finished work.
4. Click **Release payment**. It is refused.

> Now the hard one. A perfect email, from Freshworks' real address. Real company, real sender. Any normal check would say "pay it."
>
> But PayeeGuard follows the money. Freshworks has been paid fourteen times, always to the same account. This email asks for a different one: the same UK account the fake Zoho email used a minute ago.
>
> *(pause)*
>
> Every identity check passed. The money trail didn't. Blocked.
>
> The case file is already written, ready to report. And if anyone, person or AI, tries to pay it anyway, PayeeGuard refuses.

## Scene 6: The whole list, and the close (1:35–2:00)

**Screen:** the **Vendors** tab. After the audit runs, a red stamp reads "3 alerts, 3 to review": one bank account shared by two different suppliers, and the same supplier listed twice. Then the **Memory** view.

**Action:** click **Vendors**, then **Run on sample list (7 vendors)**. Hover over the shared-account alert. End on **Memory**.

> Before lunch, Riya checks every supplier on file in one click. PayeeGuard finds one bank account shared by two different suppliers, and the same supplier listed twice: the kind of mess fraud hides in.
>
> Every decision is remembered, so next Monday PayeeGuard knows a little more.
>
> Riya didn't need to be a fraud expert. Riya just needed to know who was really being paid.
>
> **PayeeGuard: know your payee before the money moves.**

## Recording notes

Record the screen in one take, in the scene order above, then lay the voice-over on top.

**Before you record:**

- [ ] Click **Reset** in the top right, so the totals strip starts at $0 and climbs as Riya works.
- [ ] Open samples in this order: 01, 03, 05, 06. Sample 06 is only blocked because 05 ran first.
- [ ] Rehearse the **Release payment** click on case 06 once. Check it refuses before you film it.
- [ ] Zoom the browser to 125% so text is readable in the video.
- [ ] Close other tabs and turn off notifications.

**Click path:** desk home → 01 → 03 (scroll to Call button) → 05 (zoom on sender) → 06 (money trail, case file, Release payment) → Vendors → Run on sample list → Memory.

**What to avoid:**

- Don't read out check names, codes or the evidence table. Let the stamps and ticks speak.
- Don't scroll fast. Every scroll should end on something the voice-over is talking about.
- Don't show the browser address bar or any settings page.

**Backup stills:** if a live click misbehaves, cut to the screenshot named in the run of show.
