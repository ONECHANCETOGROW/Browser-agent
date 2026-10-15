# Real-Chrome smoke test (your installed Chrome 154, your normal profile)

Takes about five minutes. Nothing in your existing tabs is touched: the script only reads them (titles/URLs, never page text) and does
all clicking, typing and navigating in tabs it opens itself on a local test page, then closes them.

## 1. Load the extension
1. In Chrome open `chrome://extensions` and switch **Developer mode** on (top right).
2. **Load unpacked** → select `D:\Tool\browser agent\extension`.
3. The card must show **ID `kfofbldlpmoobgiafcbohiiebmfggecg`** and no red *Errors* button. (A different ID means the wrong folder was chosen.)
4. Click the puzzle-piece icon and pin **OCTG Browser Agent**.

## 2. Start the agent (terminal 1, leave it open)
```powershell
cd "D:\Tool\browser agent\agent"
python -m pip install aiohttp
python -m octg_agent serve
```
It prints a line `pairing token  <token>`.

## 3. Connect (side panel)
1. Click the pinned OCTG icon → the side panel opens.
2. Open **Connection**, paste the token (`python -m octg_agent token` prints it again), press **Connect**.
3. Check: badge **connected**; *Current website / tab / URL / Page title* match the tab you are looking at; *Browser status* shows your tab count.
4. Switch to a `chrome://extensions` tab: the panel shows *"This Chrome page cannot be controlled by the extension."*

## 4. Automated smoke run (terminal 2)
```powershell
cd "D:\Tool\browser agent\agent"
python -m octg_agent call get_tabs
python -m tests.smoke_real_chrome
```
`get_tabs` must list your real tabs (ids, titles, URLs). `smoke_real_chrome` then runs, in its own tabs: get_page_state, read_page, inspect_page,
new tab, switch_tab, navigate, back, forward, click, type (including a dummy password), scroll, screenshot, the restricted-page check
(`chrome://version`, `about:blank` → `UNSUPPORTED_PAGE`), scans the agent logs for the dummy password, and cleans up. Expect `13/13 steps passed`.
Add `--keep-tabs` to leave its tabs open for inspection and `--debug` for tracebacks.

## 5. Manual spot checks you can do yourself
```powershell
python -m octg_agent call new_tab url=https://example.com
python -m octg_agent call inspect_page
python -m octg_agent call screenshot
python -m octg_agent call navigate url=https://example.org
python -m octg_agent call back
python -m octg_agent call forward
python -m octg_agent call get_page_state
python -m octg_agent call read_page
python -m octg_agent call click element_id=el_001          # id from the latest inspect_page; the CLI fills in snapshot_id
python -m octg_agent call scroll direction=down
python -m octg_agent call get_tabs                          # note the tab_id of the new tab, then:
python -m octg_agent call close_tab tab_id=<that id>
python -m octg_agent call get_page_state                    # while a chrome:// tab is active -> UNSUPPORTED_PAGE
```

## 6. Logs
`agent\data\logs\agent-<date>.jsonl`. Typed text for password / card-number / one-time-code / secret-looking fields is replaced by
`[redacted: credential field]`; page text, element values and verification values are never written (only URLs, titles, counts, timings).
Text typed into ordinary fields and the URLs you visit **are** logged; treat the folder as private.

## If something fails
| Symptom | Cause / fix |
|---|---|
| Card shows an *Errors* button | Click it and send me the text. |
| Badge stays `disconnected` / "Agent not reachable" | Agent not running, or wrong port. Check terminal 1; default is `ws://127.0.0.1:8765/ext`. |
| Badge `auth_failed` | Wrong token → paste the output of `python -m octg_agent token` again. |
| `EXTENSION_NOT_CONNECTED` from the CLI | Agent is up but the panel isn't connected (step 3), or Chrome is closed. |
| After the PC wakes / long idle the first command fails | The MV3 worker was asleep; it reconnects by itself within ~30 s. Retry. |
| `UNSUPPORTED_PAGE` on a normal website | Check `get_tabs` → `restricted_reason`; Web Store, PDFs viewer, `chrome://`, extension pages and error pages are blocked by Chrome. |
