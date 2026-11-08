#!/usr/bin/env python3
"""Preview by default; explicitly confirmed, individually addressed Messages."""
import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import math
import mimetypes
import os
from pathlib import Path
import plistlib
import re
import string
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parent
FIELDS = ['phone', 'timestamp', 'message_hash', 'status']
STATUSES = {'attempted', 'submitted', 'error', 'timeout', 'interrupted'}


def normalize(phone, default_country_code=None):
    phone = re.sub(r'[\s().-]', '', phone)
    # Excel commonly stores +1... as an 11-digit number and drops only the plus.
    # The country code is still explicit, so restoring the international prefix
    # does not require guessing a country.
    if re.fullmatch(r'1[0-9]{10}', phone):
        phone = '+' + phone
    elif default_country_code == '+1' and re.fullmatch(r'[2-9][0-9]{9}', phone):
        phone = default_country_code + phone
    if not re.fullmatch(r'\+[1-9][0-9]{1,14}', phone):
        raise ValueError('phone must have an explicit +country code and 2–15 ASCII digits')
    return phone


def personalize(template, name):
    values = {'name': name or 'there', 'first_name': name.split()[0] if name else 'there'}
    for _, field, spec, conversion in string.Formatter().parse(template):
        if field is not None and (field not in values or spec or conversion):
            raise ValueError('only {name} and {first_name} placeholders are supported; use {{ and }} for literal braces')
    message = template.format_map(values)
    if not message.strip() or '\0' in message:
        raise ValueError('message must be nonempty and contain no NUL characters')
    return message


def split_message(message):
    parts = re.split(r'\r?\n[ \t]*\r?\n', message, maxsplit=1)
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise ValueError('split message must contain two nonempty parts separated by a blank line')
    return tuple(part.strip() for part in parts)


def operation_hash(message, service='imessage', attachment=None, attachment_only=False,
                   split=False):
    if service == 'imessage' and attachment is None and not split:
        # Preserve compatibility with logs made before attachment/service support.
        return hashlib.sha256(message.encode('utf-8')).hexdigest()
    digest = hashlib.sha256()
    digest.update(b'messages-operation-v1\0')
    digest.update(service.encode('ascii') + b'\0')
    digest.update(b'split\0' if split else b'single\0')
    digest.update(b'attachment-only\0' if attachment_only else b'text-and-attachment\0')
    digest.update(message.encode('utf-8') + b'\0')
    if attachment is not None:
        with attachment.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()


def validate_attachment(path):
    if path is None:
        return None
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f'attachment is not a regular file: {path}')
    if not os.access(path, os.R_OK):
        raise ValueError(f'attachment is not readable: {path}')
    mime, _ = mimetypes.guess_type(path.name)
    if mime not in {'image/jpeg', 'image/png', 'image/gif', 'image/heic'}:
        raise ValueError('attachment must be a JPEG, PNG, GIF, or HEIC image')
    if path.stat().st_size == 0:
        raise ValueError('attachment is empty')
    return path


def load_inputs(csv_path, template_path, service='imessage', attachment=None, attachment_only=False,
                split=False, default_country_code=None):
    template = template_path.read_text(encoding='utf-8')
    personalize(template, '')
    recipients, seen = [], set()
    with csv_path.open(encoding='utf-8-sig', newline='') as source:
        reader = csv.DictReader(source, strict=True)
        fields = reader.fieldnames or []
        meaningful_fields = [field for field in fields if field]
        if meaningful_fields not in (['phone'], ['phone', 'name']):
            raise ValueError('CSV header must be phone or phone,name')
        for line, row in enumerate(reader, 2):
            try:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError('wrong number of CSV fields')
                if any(value.strip() for field, value in row.items() if not field):
                    raise ValueError('unnamed CSV columns must be empty')
                phone = normalize(row['phone'], default_country_code)
                name = row.get('name', '').strip()
                message = personalize(template, name)
                if split:
                    split_message(message)
                digest = operation_hash(message, service, attachment, attachment_only, split)
            except ValueError as exc:
                raise ValueError(f'CSV row {line}: {exc}') from exc
            if phone not in seen:
                recipients.append((phone, message, digest))
                seen.add(phone)
    if not recipients:
        raise ValueError('CSV has no recipients')
    return recipients


def read_log(file):
    file.seek(0)
    reader = csv.DictReader(file, strict=True)
    if reader.fieldnames is None:
        return set()
    if reader.fieldnames != FIELDS:
        raise ValueError('invalid log header; refusing to send')
    seen = set()
    for row in reader:
        if (None in row or any(v is None for v in row.values()) or
                row['status'] not in STATUSES or normalize(row['phone']) != row['phone'] or
                not re.fullmatch('[0-9a-f]{64}', row['message_hash'])):
            raise ValueError('invalid log row; refusing to send')
        dt.datetime.fromisoformat(row['timestamp'])
        seen.add((row['phone'], row['message_hash']))
    return seen


def record(file, recipient, status):
    file.seek(0, os.SEEK_END)
    writer = csv.writer(file)
    if file.tell() == 0:
        writer.writerow(FIELDS)
    writer.writerow([recipient[0], dt.datetime.now(dt.timezone.utc).isoformat(), recipient[2], status])
    file.flush()
    os.fsync(file.fileno())


def dictionary_script(service='imessage', attachment=False, attachment_only=False):
    if sys.platform != 'darwin':
        raise ValueError('Messages automation requires macOS')
    app = Path('/System/Applications/Messages.app/Contents')
    if not app.exists():
        app = Path('/Applications/Messages.app/Contents')
    with (app / 'Info.plist').open('rb') as source:
        info = plistlib.load(source)
    definition = info.get('OSAScriptingDefinition', 'Messages.sdef')
    if Path(definition).name != definition:
        raise ValueError('unexpected scripting dictionary path')
    if not definition.endswith('.sdef'):
        definition += '.sdef'
    root = ET.parse(app / 'Resources' / definition).getroot()
    account = root.find(".//class[@name='account']")
    participant = root.find(".//class[@name='participant']")
    send = root.find(".//command[@name='send']")
    extension = root.find(".//class-extension[@extends='application']")
    service_name = {'imessage': 'iMessage', 'sms': 'SMS'}[service]
    if (account is None or participant is None or send is None or extension is None or
        account.find("property[@name='service type']") is None or
        account.find("property[@name='enabled']") is None or
        account.find("property[@name='id']") is None or
        account.find("element[@type='participant']") is None or
        extension.find("element[@type='account']") is None or
        root.find(f".//enumerator[@name='{service_name}']") is None or
        send.find("direct-parameter/type[@type='text']") is None or
        (attachment and send.find("direct-parameter/type[@type='file']") is None) or
        send.find("parameter[@name='to']/type[@type='participant']") is None):
        raise ValueError('installed Messages dictionary lacks the supported account/participant interface; refusing to send')
    attachment_setup = '''    set attachmentFile to (POSIX file (item 4 of argv)) as alias
''' if attachment else ''
    attachment_send = '''
        send attachmentFile to targetParticipant''' if attachment else ''
    text_send = '' if attachment_only else '        send (item 3 of argv) to targetParticipant\n'
    send_script = '''on run argv
ATTACHMENT_SETUP    tell application "Messages"
        set availableAccounts to every account whose service type is SERVICE_NAME and enabled is true
        if (count of availableAccounts) is 0 then error "No enabled SERVICE_NAME account is available"
        set selectedAccount to account id (item 1 of argv)
        if service type of selectedAccount is not SERVICE_NAME or enabled of selectedAccount is false then error "SERVICE_NAME account is unavailable"
        set targetParticipant to participant (item 2 of argv) of selectedAccount
TEXT_SEND
ATTACHMENT_SEND
        return "SUBMITTED"
    end tell
end run'''.replace('SERVICE_NAME', service_name).replace('ATTACHMENT_SETUP', attachment_setup).replace('TEXT_SEND', text_send).replace('ATTACHMENT_SEND', attachment_send)
    check_script = '''on run argv
    tell application "Messages"
        set availableAccounts to every account whose service type is SERVICE_NAME and enabled is true
        if (count of availableAccounts) is 0 then error "No enabled SERVICE_NAME account is available"
        return id of item 1 of availableAccounts
    end tell
end run'''.replace('SERVICE_NAME', service_name)
    return send_script, check_script


def run_applescript(script, args, timeout):
    # '-' reads source from stdin; subsequent argv values are data, never code.
    process = subprocess.Popen(['/usr/bin/osascript', '-', *args], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(script, timeout=timeout)
    except BaseException:
        process.kill()
        process.wait()
        raise
    if process.returncode:
        # AppleScript errors can echo message bodies: do not persist or print them.
        raise RuntimeError(f'AppleScript failed (exit {process.returncode}); inspect Messages and Automation permissions')
    return stdout.strip()


def preview(recipients, seen, limit, service='imessage', attachment=None, attachment_only=False,
            retry_attempted=False, split=False):
    eligible = recipients if retry_attempted else [r for r in recipients if (r[0], r[2]) not in seen]
    selected = eligible if limit is None else eligible[:limit]
    print(f'{len(recipients)} unique recipients; {len(recipients)-len(eligible)} previously attempted; {len(selected)} selected.')
    if retry_attempted:
        print('WARNING: retry override enabled; previously attempted content may be duplicated.')
    print(f'Service: {service.upper()}')
    if attachment:
        print(f'Attachment: {attachment.name} ({attachment.stat().st_size:,} bytes)')
    if attachment_only:
        print('Content: attachment only (message text will not be resent)')
    for phone, message, _ in selected:
        print(f'\nTo: {phone}')
        if not attachment_only:
            parts = split_message(message) if split else (message,)
            for number, part in enumerate(parts, 1):
                label = f' part {number}' if split else ''
                print(f'----- message{label} begins -----')
                print(part, end='')
                print(f'\n----- message{label} ends -----')
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--delay', type=float, default=5)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--service', choices=('imessage', 'sms'), default='imessage')
    parser.add_argument('--default-country-code', choices=('+1',),
                        help='explicitly treat 10-digit US/Canada numbers as +1 numbers')
    parser.add_argument('--attachment', type=Path)
    parser.add_argument('--attachment-only', action='store_true', help='send only the attachment; requires --attachment')
    parser.add_argument('--split-message', action='store_true',
                        help='send text before and after the first blank line as two messages')
    parser.add_argument('--retry-attempted', action='store_true', help='include logged attempts; may duplicate content')
    parser.add_argument('--recipients', type=Path, default=BASE / 'recipients.csv')
    parser.add_argument('--message', type=Path, default=BASE / 'message.txt')
    parser.add_argument('--log', type=Path, default=BASE / 'send_log.csv')
    args = parser.parse_args(argv)
    if args.check and args.send:
        parser.error('--check cannot be combined with --send')
    if args.attachment_only and not args.attachment:
        parser.error('--attachment-only requires --attachment')
    if args.split_message and args.attachment_only:
        parser.error('--split-message cannot be combined with --attachment-only')
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    if not math.isfinite(args.delay) or args.delay < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--delay must be finite and nonnegative; --timeout must be finite and positive')
    try:
        if args.check:
            _, check_script = dictionary_script(args.service, bool(args.attachment), args.attachment_only)
            if not run_applescript(check_script, [], args.timeout):
                raise RuntimeError('unexpected Messages check response')
            print(f'Automation access works; an enabled {args.service.upper()} account is available. No message sent.')
            return 0
        attachment = validate_attachment(args.attachment)
        paths = [p.resolve() for p in (args.recipients, args.message, args.log)]
        if attachment:
            paths.append(attachment)
        if len(set(paths)) != len(paths):
            raise ValueError('input and log paths must be different')
        recipients = load_inputs(args.recipients, args.message, args.service, attachment,
                                 args.attachment_only, args.split_message,
                                 args.default_country_code)
        if not args.send:
            if args.log.exists():
                with args.log.open(newline='', encoding='utf-8') as file:
                    fcntl.flock(file, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    seen = read_log(file)
            else:
                seen = set()
            preview(recipients, seen, args.limit, args.service, attachment, args.attachment_only,
                    args.retry_attempted, args.split_message)
            print('\nPreview only. Nothing sent. Changing rendered text, attachment contents, or service creates a new message.')
            return 0
        # One exclusive lock covers eligibility, confirmation and every dispatch.
        with args.log.open('a+', newline='', encoding='utf-8') as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            selected = preview(recipients, read_log(file), args.limit, args.service, attachment,
                               args.attachment_only, args.retry_attempted, args.split_message)
            if not selected:
                return 0
            if not sys.stdin.isatty():
                raise ValueError('sending requires an interactive terminal')
            print('\nEach message goes to one participant. Submission is not delivery confirmation.')
            if input('Type SEND to submit these messages: ') != 'SEND':
                print('Cancelled. Nothing sent.')
                return 0
            script, check_script = dictionary_script(args.service, bool(attachment), args.attachment_only)
            account_id = run_applescript(check_script, [], args.timeout)
            if not account_id:
                raise RuntimeError('Messages returned an empty account ID')
            for index, recipient in enumerate(selected):
                if index:
                    time.sleep(args.delay)
                record(file, recipient, 'attempted')
                try:
                    messages = split_message(recipient[1]) if args.split_message else (recipient[1],)
                    for message in messages:
                        script_args = [account_id, recipient[0], message]
                        if attachment:
                            script_args.append(str(attachment))
                        if run_applescript(script, script_args, args.timeout) != 'SUBMITTED':
                            raise RuntimeError('unexpected submission response')
                except KeyboardInterrupt:
                    record(file, recipient, 'interrupted')
                    raise
                except subprocess.TimeoutExpired:
                    record(file, recipient, 'timeout')
                    raise
                except Exception:
                    record(file, recipient, 'error')
                    raise
                record(file, recipient, 'submitted')
                print(f'{recipient[0]}: submitted to Messages')
        return 0
    except KeyboardInterrupt:
        print('\nStopped. Logged attempts remain excluded on reruns.', file=sys.stderr)
        return 130
    except (OSError, ValueError, csv.Error, ET.ParseError, RuntimeError, subprocess.TimeoutExpired, EOFError) as exc:
        detail = 'AppleScript timed out; submission may have occurred' if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
        print(f'Error: {detail}. Stopped; no automatic retry.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
