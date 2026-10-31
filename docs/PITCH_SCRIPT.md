# PayeeGuard: 2-Minute Pitch Script

ContextCon 03 · IIT Bombay · 2026-10-31 · Live doc: https://claude.ai/artifact/1B8i5RXWLGnvJtuvdB4CEX

The spoken pitch for the finals: connect with the room, make the problem felt, then explain the solution in plain words. No technical detail.

## Run of show

The pitch is about 320 spoken words, which fits 2:00 at a brisk, confident pace. Every line below is meant to be said out loud. The solution gets the most time, because that is where the new features live.

| Time | Beat | Goal | Speaker |
| --- | --- | --- | --- |
| 0:00–0:20 | Hook | Make the room picture themselves paying a fake invoice | TBD |
| 0:20–0:50 | Problem | Show the cost, and why AI agents make it worse | TBD |
| 0:50–1:40 | Solution | What PayeeGuard does, from inbox to finished job | TBD |
| 1:40–2:00 | Close | Why it beats doing it by hand, one line they remember | TBD |

## 1. Hook (0:00–0:20)

Open with a story the audience can see themselves in. Pause after the question.

> Imagine it's Friday evening. An email lands from a supplier you've paid every month for a year. Same name, same email address, same signature. It says: *"We've moved banks — please send this month's payment to our new account."*
>
> You'd pay it. Most of us would.
>
> *(pause)*
>
> And that is exactly how companies lose hundreds of thousands of dollars in a single click.

## 2. The problem (0:20–0:50)

Three beats: it is common, it is expensive, and it is about to get worse.

> This is invoice fraud. Two in five businesses have been hit by it, losing **$367,000 per incident** on average.
>
> And these emails don't look fake anymore. Often the fraudster is inside the supplier's *real* mailbox. Every normal check says *"looks fine."* The only thing wrong is **where the money is going**.
>
> Today, one tired person in finance is the only defence. And companies are now letting **AI agents pay invoices on their own**. An AI doesn't get suspicious. It just pays.

## 3. Our solution (0:50–1:40)

Keep it human: it watches, it asks, it remembers, it answers, it finishes the job. No architecture, no check names.

> So we built **PayeeGuard: the trust check that runs before any money moves**, whether a person or an AI is paying.
>
> It watches your finance inbox. For every invoice that arrives, it asks three questions: **Is this company real?** **Did this email really come from them?** And the one everyone forgets: **is the money going where it always goes?**
>
> It even remembers what your own team said. If someone wrote in Slack, *"we've ended this vendor,"* PayeeGuard won't pay their next invoice.
>
> Then one clear answer: **Pay**, **Hold**, or **Block**, with the proof behind it. And it finishes the job. Safe payments are released. When it's unsure, it alerts the team and prepares a call to the supplier's *real* number, not the one in the email. When it's fraud, it blocks the payment and writes the case file, ready to report.
>
> That perfect email from the start? Every identity check passed. **The money trail didn't.** Blocked.

## 4. Why it matters, and the close (1:40–2:00)

End on why it beats doing it by hand, and one line to remember. Slow down for the last sentence.

> Today, companies pay people to check this by hand, and they still miss the perfect fakes.
>
> PayeeGuard does it in seconds. It never treats an honest new supplier as a criminal: when it isn't sure, it asks a human. And any AI payment agent can ask PayeeGuard before it pays.
>
> **PayeeGuard: know your payee before the money moves.**

## Video shot list

Each line of the script has one screen behind it. The hook and problem use no product screens, so the story lands before the product appears. Screenshots are 1600 px wide, captured from the PayeeGuard desk (not committed to the repo).

| Time | Script line | On screen | File |
| --- | --- | --- | --- |
| 0:00–0:20 | "Imagine it's Friday evening…" | A normal-looking email in an inbox: show the raw text of sample 06 | 06_case.png, cropped to the inbox list, or a screen recording of the email |
| 0:20–0:50 | "$367,000 per incident… It just pays." | Big text on a plain background: "$367,000", then "2 in 5 businesses", then "AI agents now pay invoices" | None: title cards |
| 0:50–1:00 | "So we built PayeeGuard…" | The desk home screen with the inbox | 00_desk_home.png |
| 1:00–1:10 | "three questions…" | A safe invoice: CLEARED TO PAY, with WHO, CHANNEL and MONEY all ticked | 01_case.png |
| 1:10–1:15 | "Pay, Hold, or Block" | Quick cuts: PAY, then HOLD, then BLOCK | 01_case.png → 03_case.png → 02_case.png |
| 1:15–1:25 | "it finishes the job… writes the case file" | The finished-work documents at the bottom of the case | 05_lookalike_full.png (scroll down) |
| 1:25–1:40 | "Every identity check passed. The money trail didn't." | **Hero:** BLOCKED, WHO and CHANNEL ticked, MONEY not, money trail pointing to the blocked case | 06_case.png, then 06_hero_full.png |
| 1:40–1:50 | "never treats an honest new supplier as a criminal" | An unknown vendor on HOLD, not BLOCK | 07_case.png |
| 1:50–2:00 | "know your payee before the money moves" | Memory view: everything it has learned, then the logo | 11_memory.png |

Not captured, because they need live connections that were switched off for safety:

- The Slack "we've ended this vendor" moment: record it on the team's live desk with Slack connected.
- The payment gate refusing to pay a blocked case: click Release on case 06 during a screen recording.

Screen recordings beat stills for the hero. Record 05 then 06 live, and use the stills as cut-aways.

## Delivery notes

Memorise these three lines word for word. Everything else can be said naturally.

1. "The only thing that's wrong is where the money is going."
2. "Every identity check passed. The money trail didn't."
3. "PayeeGuard: know your payee before the money moves."

How to say it:

- Speak to one person in the room during the hook, as if it happened to them.
- Pause for a full second after "You'd pay it" and after "It just pays."
- Say "Pay, Hold, or Block" slowly, with a beat between each word.
- Rehearse with a timer. If you run long, cut "whether a person or an AI is paying" in section 3 first; the close already says it.

What to avoid:

- No technical words: graph, API, MCP, scoring, Crustdata, regex.
- Don't say "100% accurate" or "production-ready."
- Don't read from the screen. The demo shows the product; the pitch tells the story.

## If a judge asks for more

These features are built but don't fit in two minutes. Keep each answer to one or two sentences.

| If they ask… | Say |
| --- | --- |
| "Can it check all my existing suppliers?" | Yes. Upload your vendor list and it finds shared bank accounts, duplicate suppliers and lookalike names in one pass. |
| "Does it work for Indian invoices?" | Yes. It checks the GST and PAN numbers on the invoice and flags fake or mistyped ones. |
| "How does an AI agent use it?" | The agent asks PayeeGuard before paying, and gets back allow or stop, with the reason. |
| "Does it ever email real companies or move real money?" | No. Emails go to a safe test address and payments only use a test gateway. A human releases every payment. |
| "How do you know it works?" | It passes all 13 test scenarios, and gave the same answer on 54 reworded versions of those emails. |
| "New suppliers get blocked?" | Never just for being new. Missing information means "ask a human", not "fraud". |

## Sources

- Two in five businesses hit, $367,000 average loss, about a quarter with automated defences: [Ivalua press release, Sept 2026](https://www.ivalua.com/press-releases/two-in-five-businesses-fall-victim-to-invoice-fraud-with-losses-averaging-367000/). Taken from our project spec; re-check before you go on stage.
