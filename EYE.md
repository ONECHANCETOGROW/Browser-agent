# The Eye: how the AI (Cloudy) looks at pages for you, cheaply and safely

**Why it exists.** You do not know where to look or what to click to improve a business's visibility; the AI does. Having an AI drive a browser screenshot by screenshot burns tokens. The eye is a small,
local gate in front of your Chrome: the AI says what it wants to see, the agent does the clicking and reading on your computer, and only a **short digest** comes back (cleaned text, tables as rows, a few controls).
A whole Search Console report is a few hundred tokens; reading and ranking 211 queries took 3 calls and 34 seconds.

## What the AI can do (capabilities, category `eye`)
| Call | What it does |
|---|---|
| `eye.open url=...` | Opens the page in ONE dedicated tab (never your own tabs) in your logged-in Chrome. Returns the digest: cleaned text, tables as rows, controls with refs. |
| `eye.look` | Looks again (after a wait or a change), optionally `find=[words]` returns the lines around them. |
| `eye.act do=click/type/select/check/scroll/key/back/reload target="Exact label"` | One careful action, returns only what changed. A partial label is only offered as a candidate, never pressed. |
| `eye.table all_pages=true max_rows=300` | Reads the page's biggest table and presses its own "Next page" until done: rows exactly as shown. |
| `eye.close`, `eye.status` | Close my tab; see whether browsing is on and which sites are blocked. |

Tip: many report pages keep their settings in the address (Search Console: `&metrics=CLICKS%2CIMPRESSIONS%2CCTR%2CPOSITION`), so opening the right URL beats clicking switches. Sorting can also be done by reading all rows and sorting them locally.

## What it refuses (the gate is in the agent, not left to the AI)
- Anything that looks like it changes something: delete, remove, send, post, publish, pay, buy, confirm, approve, save, update, create, log out, request indexing ... It tells the AI to ask you with `owner.ask`.
- Password / personal / card fields. Typing is allowed only into search or filter boxes; Enter only right after typing there.
- Blocked sites: banks, payments, password managers, e-mail/chat, account-security pages (see below). A link that lands on one is undone.
- More than 80 actions per session, more than 3 sessions, and tabs idle for 10 minutes are closed.

## Google Business Profile reviews (read, check, reply)
| Call | What it does | Needs your approval? |
|---|---|---|
| `gbp.reviews` | Pages through every review of every business on the account (newest first, 10 per page) and reports how many have **no owner reply**, per business, with the list. Only reads and turns pages. 2,011 reviews took ~8 minutes. | No |
| `gbp.reply_check business=... reviewer=... review_excerpt=...` | Finds ONE review, opens its Reply box, proves the box is directly under that review, can type a harmless test string, then presses **Cancel**. Nothing is posted. | No |
| `gbp.reply_post business=... reviewer=... review_excerpt=... reply_text=...` | Posts a **public** reply. The job waits in the Knowledge Hub **Approvals** list showing the exact text and the review; it only runs after you approve with your PIN. | **Yes, every time** |

Safety built into `gbp.reply_post`: it refuses if more than one review matches, if the review already has a reply, if the page's Reply buttons cannot be matched one-to-one to the unreplied reviews, or if the Reply box that opens is not under the requested review.
It checks the box holds exactly the approved text before pressing **Post reply** (one press, never retried) and reads the page afterwards to confirm the reply is there. It needs the separate `web:post` permission.
Not reported: star ratings. The page draws filled and empty stars with the same icon (only the colour differs), so the text cannot tell them apart.

## Your controls
```
python -m octg_agent eye status          # on/off and the blocked-site list
python -m octg_agent eye off             # the AI cannot browse at all (takes effect at once)
python -m octg_agent eye on
python -m octg_agent eye deny example.com   # add a site to the blocked list
python -m octg_agent eye allow wise.com     # allow a site that is blocked by default
```
You can watch it work: the side panel's Task tab shows the current action, and its tab sits in your Chrome while it runs (it is closed afterwards).

## Honest limits
- Page tables are read from the page's text: cells must come out separated by tabs (real tables and most app grids do).
- Sortable column headers that are not real controls (Search Console's) cannot be clicked yet; read all rows and sort locally instead. (A future extension change can add click-by-visible-text.)
- Search Console hides rare (anonymous) queries from the query table, so the rows add up to less than the totals at the top.
- Everything runs in your signed-in Chrome profile: what it sees is what you see.
