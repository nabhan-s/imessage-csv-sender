# iMessage CSV Sender for macOS

This Python 3 script previews personalized messages from a CSV, then optionally submits them through your existing macOS Messages account using AppleScript. It works for personal check-ins, friends and family, clubs, event reminders, or other expected one-to-one communication. It addresses one person per call, creating or reusing that person's individual conversation; it never creates a group chat.

No dependencies beyond Python's standard library and macOS's `/usr/bin/osascript`. No Apple credentials, external provider, or Messages database access.

## Setup

1. Open Messages and sign in to your normal iMessage account. Verify that you can manually send an iMessage to your own phone number.
2. Clone or download this repository, open its folder in Terminal, and check `python3 --version`. Python 3.9 or newer is supported.
3. Copy `recipients.example.csv` to `recipients.csv`, then replace the fictional numbers with your intended recipients. Start with **only your own number**. Do not send the example numbers.
4. Copy `message.example.txt` to `message.txt`, then edit it in UTF-8. Multiline text and emoji are supported. A final newline is part of the message and its hash.

```sh
cp recipients.example.csv recipients.csv
cp message.example.txt message.txt
```

The real input files and send log are excluded by `.gitignore`. Do not force-add them to Git: they can contain personal information.

CSV headers must be `phone,name` or just `phone`. With the first header, use a trailing comma for an absent name:

```csv
phone,name
+12025550101,Alex Chen
+12025550102,
```

Phone numbers require a leading `+` and explicit country code. Spaces, parentheses, periods and hyphens are removed; the result must contain 2–15 ASCII digits with a nonzero first digit. No country code is guessed. This validates the international **format**, not whether a number is allocated, reachable or registered for iMessage. Extensions and `00` prefixes are rejected. Normalized duplicate numbers keep the first row's name. Every row, including duplicates, is validated before sending anything; malformed input aborts the whole run.

As an Excel compatibility exception, an 11-digit number beginning with `1` is accepted and normalized to `+1…`: the country code is already explicit and Excel has only removed the plus sign. A 10-digit number is still rejected because adding a country code would require guessing. Trailing unnamed columns created by Excel are accepted only when every cell in them is empty.

For a list known to contain only US/Canada numbers, pass `--default-country-code +1`. This is an explicit instruction to normalize a 10-digit national number such as `4045550101` to `+14045550101`; without the option, 10-digit numbers remain rejected.

`{name}` uses the trimmed full name, and `{first_name}` uses its first whitespace-separated word. Both become `there` when the name is absent. Literal braces use `{{` and `}}`; other placeholders, conversions and formatting specifications are rejected. Names are inserted once, so braces within a name are literal text.

## Commands

```sh
python3 send_imessages.py --check
python3 send_imessages.py
python3 send_imessages.py --send --limit 1
python3 send_imessages.py --send --delay 5
python3 send_imessages.py --split-message
python3 send_imessages.py --split-message --send --delay 5
python3 send_imessages.py --attachment flyer.jpg
```

Use `--split-message` when the template contains two nonempty sections separated by its first blank line. The script previews and sends them as two separate messages to each recipient, in order. Both parts form one logged operation, so an interruption after the first part remains excluded on reruns to prevent an automatic duplicate.

`--check` checks the installed scripting dictionary, Automation access and availability of an enabled iMessage account without sending anything. It can open Messages or trigger a macOS permission prompt. It does not verify recipient registration, connection readiness or delivery.

Use `--attachment PATH` to include one JPEG, PNG, GIF, or HEIC image. The script validates the image path before confirmation, displays its filename and size in the preview, and passes its absolute path to AppleScript as data. Messages receives the personalized text first and the image second in the same individual conversation. The send log identity includes the image contents, so replacing the image creates a new operation even if its filename stays the same.

If Messages accepts the text but the attachment fails, use `--attachment-only` with `--attachment` to recover without duplicating the text. This is logged as a separate operation and still requires preview plus `SEND` confirmation.

`--retry-attempted` is an explicit recovery override that includes combinations already present in the log. It can duplicate content and should only be used after reviewing the relevant conversations. It does not delete or rewrite log history and still requires interactive confirmation.

The default command previews eligible recipients and their exact rendered messages between boundary markers. Preview requires no Messages access and creates no log. `--limit` selects at most that many eligible recipients in CSV order. Previously attempted combinations do not count against the limit.

Sending requires **both** `--send` and typing exactly `SEND` in an interactive terminal after preview. Piped confirmation is refused. Cancelling confirmation sends nothing. The default pause is five seconds between submissions; `--delay 0` removes it. The first submission has no delay. An exclusive log lock prevents concurrent sending processes that use the same log file.

Additional options:

```sh
python3 send_imessages.py --recipients my_people.csv --message my_message.txt --log send_log.csv
python3 send_imessages.py --send --limit 1 --timeout 30
python3 send_imessages.py --send --attachment flyer.jpg
python3 send_imessages.py --service sms --recipients android_followup.csv --send
```

Default files are next to the script; explicit relative paths are relative to the terminal's current directory. Delay must be finite and nonnegative, timeout finite and positive, and limit positive. Timeout applies separately to each AppleScript call, including the account check. Keep using the same log path; a different or deleted log removes the prior-attempt protection.

The script reads the installed Messages `.sdef` dictionary from the app bundle and requires the supported `account`, `participant`, iMessage service and text-send interface. Incompatible dictionaries are rejected. It does not rely on `sdef` or require full Xcode. If multiple enabled iMessage accounts exist, it selects the first one exposed by Messages and keeps that account ID for the run. Recipient and message values are passed as `osascript` arguments, never inserted into AppleScript source.

## Automation permissions and troubleshooting

When macOS asks whether Terminal (or your terminal/editor app) may control Messages, allow it. Review the permission under **System Settings → Privacy & Security → Automation → your terminal/editor → Messages**. An entry may only appear after the first `--check`. Accessibility permission and Full Disk Access are not required by this script.

If the check fails, confirm that Messages is signed in, its iMessage account is enabled, and your terminal has Automation access. Run the script directly in Terminal to isolate editor permissions. The script intentionally suppresses raw AppleScript error text because it can include message bodies. It reports failure and stops. A missing dictionary or unsupported interface is a compatibility error: inspect **Script Editor → File → Open Dictionary → Messages** before adapting the script. Apple's [scripting terminology documentation](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/AboutScriptingTerminology.html) describes dictionaries stored in app bundles.

If a log is locked, wait for the other run to exit. Do not work around the lock using a different log. A malformed/truncated log fails closed; preserve it and investigate before making any manual correction. Do not erase uncertain attempts to retry blindly.

A successful call means **submitted to Messages**, not delivered. Check the conversation in Messages manually for delivery or error indications. The default `--service imessage` does not automatically fall back because AppleScript cannot reliably report a later delivery failure. After the iMessage run, identify failed or Android recipients in Messages, place only those people in a second CSV, preview it, then explicitly use `--service sms`. SMS/MMS requires an enabled SMS account exposed by Messages, normally through iPhone Text Message Forwarding. The service is part of the log identity, so an earlier iMessage attempt does not suppress an intentional SMS follow-up. The script does not automatically retry or use RCS.

## Logging and interruption

`send_log.csv` is an append-only event log with:

- `phone`: normalized recipient number.
- `timestamp`: UTC ISO 8601 time.
- `message_hash`: SHA-256 of the exact UTF-8 rendered text.
- `status`: `attempted`, `submitted`, `error`, `timeout`, or `interrupted`.

An `attempted` row is flushed and synced to disk **before dispatch**. A second row records the observed outcome. All statuses exclude that recipient/hash pair on later runs, including bare attempts left by crashes. This deliberately favors avoiding duplicates: an attempt may be skipped even if nothing actually reached Messages. Changing rendered text (including whitespace, emoji, or personalized name) creates a new message and makes that combination eligible again. Changing a template without changing the rendered text does not.

Control+C stops the run, kills an active `osascript` process, and records interruption when possible. It cannot recall an Apple event already dispatched; a timeout or interruption can occur after submission. The script stops on the first error or timeout without retrying. If logging an outcome fails, the durable attempt still excludes the combination. During the pause, interruption stops before the next attempt.

The log contains phone numbers and hashes but no plaintext bodies. Preview displays bodies in the terminal, and arguments can be visible to local process inspection while `osascript` runs. Treat the CSV, template, log and terminal output as personal information. Hashes are not encryption. Files use your normal filesystem permissions.

## Automated tests versus real integration

Run:

```sh
python3 -m unittest -v test_send_imessages.py
```

Sending and Automation calls are mocked; coverage includes format validation, CSV errors, personalization, deduplication, exact preview, confirmation, limits, delay, attachment validation and hashing, SMS separation, pre-dispatch logging, reruns, changed messages, errors, timeouts, interruption, process cleanup and log locking. One test reads the actual installed dictionary without controlling Messages; another rejects an unsupported dictionary. Automated tests never send real messages.

## Scope and responsible use

This project is intended for small, expected communications—not unsolicited bulk outreach. Apple does not provide a delivery receipt through this AppleScript interface, and macOS or Messages updates may change automation behavior. Always preview first, test with your own number, keep the send log, and inspect Messages for delivery failures.

## Manual test with your own number

1. Replace `recipients.csv` with its header and **one row containing your own international phone number**, optionally your name. Use a recognizable short message in `message.txt`.
2. Run `python3 send_imessages.py --check`, approve the Automation prompt if appropriate, and confirm the account check succeeds. This sends nothing.
3. Run `python3 send_imessages.py` and inspect the exact recipient and text.
4. Run `python3 send_imessages.py --send --limit 1`. Confirm that the displayed number is yours, then type `SEND`.
5. Inspect your individual conversation in Messages and your receiving device. Verify iMessage behavior and delivery manually; the script's `submitted` output alone is insufficient.
6. Run the preview again. The same rendered message to your number should be skipped. Keep the log. Change the text only when you intentionally want a new test message.
