# OCTG Browser Agent ("the eye")

A free, local tool that lets Claude work inside **your own Chrome**, cheaply and safely.

Claude is good at deciding *what* to look at on the web. Driving a browser one screenshot at a time is slow and burns a lot of tokens. OCTG gives Claude a small gate in front of your Chrome instead: Claude says what it wants to see, a local agent on your computer does the opening, reading and clicking, and only a **short text digest** comes back (cleaned page text, tables as rows, a few controls). Nothing leaves your computer.

> **Status: early, pre-release.** Built and tested on Windows 11 with Chrome and Python 3.11. It is used on real small-business SEO and ads reporting work, but it has not been run by many people yet. Expect rough edges and please open issues.

## What it is made of
| Part | What it does |
|---|---|
| **Chrome extension** (`extension/`) | Runs in your Chrome, talks only to `127.0.0.1`. Has a side panel (Task, Ask, Inbox). |
| **Local agent** (`agent/octg_agent/`) | A small Python program (aiohttp) on `127.0.0.1:8765`. Holds the rules and the capability registry. |
| **The eye** (`eye.*`) | Opens pages in one dedicated tab, returns compact digests, reads whole tables, clicks exact labels. |
| **MCP connector** (`octg_agent.collab.mcp_server`) | Lets Claude Desktop (or any MCP client) call the eye as tools, and sends every chat a short playbook so you do not have to re-explain it. |
| **Knowledge Hub** (`/hub/`) | Local dashboard with PIN-protected approvals and an inbox. |

## What Claude can do with it
- `eye.open / look / act / table / close`: read any page you are signed in to (Search Console, Analytics, Google Ads, Business Profile, competitor sites), page through tables, click exact labels.
- `eye.wait_human`: when a page shows a CAPTCHA or "verify you are human" check, ask **you** to pass it in the side panel and carry on afterwards. It never tries to pass a check itself.
- `gbp.reviews`: count Google Business Profile reviews with no owner reply across an account.
- `gbp.reply_check` / `gbp.reply_post`: reply to a Google review. Posting waits for **your approval of the exact text** in the Hub.
- `owner.ask`: Claude asks you a question in the side panel and reads your answer.
- `seo.rank_check`, `search.report`: paced checks of where a site ranks for chosen searches.

## Safety, by design (enforced in the agent, not left to the AI)
- **Read-only by default.** The gate refuses anything that looks like it changes something: delete, send, pay, publish, save, approve, log out. Anything that changes the outside world needs your approval.
- **No passwords or personal fields.** It never types into them. Typing is allowed only in search and filter boxes.
- **Blocked sites by default:** banks, payments, password managers, e-mail and chat, account-security pages. You can change the list (`python -m octg_agent eye ...`).
- **No CAPTCHA bypass.** Ever.
- **An owner switch:** `python -m octg_agent eye off` stops all browsing at once.
- **Local only.** The agent listens on `127.0.0.1`; the extension refuses any other address. A pairing token ties the two together. Do not expose the agent to the internet.
- Sessions are limited (80 actions, 3 tabs, closed after 10 idle minutes) so a runaway loop stops.

## Quick start (Windows)
1. **Install:** `cd agent && pip install -e .` (Python 3.10+).
2. **Start the agent:** `python -m octg_agent serve`
3. **Get the pairing token:** `python -m octg_agent token`
4. **Load the extension:** Chrome, `chrome://extensions`, Developer mode, **Load unpacked**, choose the `extension` folder.
5. **Tell the agent your extension's ID.** Chrome gives every unpacked copy its own ID (shown on the extension card). Put it in `agent/config.json` as `"extension_id"` (copy `config.example.json`), or set `OCTG_EXTENSION_ID`. Using `""` accepts any extension origin (the token is still required).
6. **Pair:** open the extension's side panel, paste the token under Connection, press **Connect**. The badge turns green.
7. **Only one Chrome profile at a time** can be connected. Pressing Connect in a second profile takes the connection over.

## Use it from Claude Desktop (MCP)
Add this to Claude Desktop's `claude_desktop_config.json` (fully quit the app first, it rewrites the file while running), then restart it:
```json
{
  "mcpServers": {
    "octg-collab": {
      "command": "python",
      "args": ["-m", "octg_agent.collab.mcp_server"],
      "env": {
        "PYTHONPATH": "<path to the agent folder>",
        "OCTG_COLLAB_CALLER": "collaborator",
        "OCTG_COLLAB_TOKEN_FILE": "<path to agent/data/collab_collaborator.token>"
      }
    }
  }
}
```
The token file is created the first time the agent runs. Every new chat then gets the playbook automatically (`octg_agent/collab/playbook.py`).

## Tests
`cd agent && python -m unittest discover -s tests -t .`
About 1,000 tests: unit tests, a real agent against a fake extension, Node tests for the extension code, and headless-Chromium tests for the panel and the Hub. A few browser tests need a Chrome/Chromium binary.

## Honest limits
- It reads **page text**. Canvas-heavy pages, charts without text, and content only in images are not readable.
- Search Console's sort headers are not real controls, so rows are read and sorted locally.
- Google and Bing show CAPTCHAs if searches are too frequent. Search checks are deliberately paced (20 to 45 seconds apart).
- Local search results depend on the IP address of the computer; it does not fake a location.
- Sites that block your region need a proxy or VPN set up by you outside this tool.
- Facebook and Instagram are heavy and may time out.
- Windows is the tested platform.

## Documents
- `EYE.md`: the eye in plain language, with the owner controls.
- `COLLAB.md`, `PROTOCOL.md`: the collaboration protocol (`octg-collab/1`) and capability model.
- `ARCHITECTURE.md`, `PHASE*.md`: design notes. `README_DEVELOPER.md`: the original developer README.

## License
MIT. See `LICENSE`.
