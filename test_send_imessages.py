import contextlib
import csv
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import send_imessages as app


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.csv = self.root / 'recipients.csv'
        self.message = self.root / 'message.txt'
        self.log = self.root / 'send_log.csv'
        self.attachment = self.root / 'flyer.jpeg'
        self.csv.write_text('phone,name\n+1 (202) 555-0101,Alex Chen\n+12025550102,\n+12025550101,Other\n', encoding='utf-8')
        self.message.write_text('Hi {first_name} ({name})!\n👋\n', encoding='utf-8')
        self.attachment.write_bytes(b'fake jpeg bytes')
        self.args = ['--recipients', str(self.csv), '--message', str(self.message), '--log', str(self.log)]

    def invoke(self, extra=(), answer='SEND', effects=None, tty=True):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                patch.object(app, 'dictionary_script', return_value=('SEND_SCRIPT', 'CHECK_SCRIPT')) as dictionary, \
                patch.object(app, 'run_applescript', side_effect=effects or ['account-id', 'SUBMITTED', 'SUBMITTED']) as run, \
                patch('builtins.input', return_value=answer) as confirmation, \
                patch.object(app.sys.stdin, 'isatty', return_value=tty), \
                patch.object(app.time, 'sleep') as sleep:
            result = app.main(self.args + list(extra))
        return result, output.getvalue(), run, confirmation, sleep, dictionary

    def rows(self):
        with self.log.open(newline='') as file:
            return list(csv.DictReader(file))

    def test_validation(self):
        self.assertEqual(app.normalize('+44 (20) 1234-5678'), '+442012345678')
        self.assertEqual(app.normalize('12025550101'), '+12025550101')
        self.assertEqual(app.normalize('2025550101', '+1'), '+12025550101')
        with self.assertRaises(ValueError):
            app.normalize('2025550101')
        for value in ['2025550101', '00442012345678', '+012345', '+1', '+１２３', '+1202ext3', '+1234567890123456']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.normalize(value)
        self.csv.write_text('phone,name\n+12025550101,Alex\nBAD,Jordan\n')
        result, _, run, *_ = self.invoke(['--send'])
        self.assertEqual(result, 1)
        run.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_personalization_and_dedup(self):
        recipients = app.load_inputs(self.csv, self.message)
        self.assertEqual(len(recipients), 2)
        self.assertEqual(recipients[0][1], 'Hi Alex (Alex Chen)!\n👋\n')
        self.assertEqual(recipients[1][1], 'Hi there (there)!\n👋\n')
        self.assertEqual(app.personalize('{{hello}} {name}', 'A {first_name}'), '{hello} A {first_name}')
        for template in ['{unknown}', '{name!r}', '{name:10}', '{name.x}', '{', '', '\0']:
            with self.subTest(template=template), self.assertRaises(ValueError):
                app.personalize(template, '')

    def test_optional_name_and_bad_csv(self):
        self.csv.write_text('phone\n+12025550101\n')
        self.assertEqual(app.load_inputs(self.csv, self.message)[0][1], 'Hi there (there)!\n👋\n')
        for content in ['phone,name\n+12025550101\n', 'phone,name\n+12025550101,A,extra\n', 'phone,phone\n', 'phone,name\n"unclosed']:
            self.csv.write_text(content)
            with self.assertRaises((ValueError, csv.Error)):
                app.load_inputs(self.csv, self.message)

    def test_excel_trailing_empty_columns_are_ignored(self):
        self.csv.write_text('phone,,\n12025550101,,\n', encoding='utf-8')
        recipients = app.load_inputs(self.csv, self.message)
        self.assertEqual(recipients[0][0], '+12025550101')
        self.csv.write_text('phone,,\n12025550101,,unexpected\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unnamed CSV columns'):
            app.load_inputs(self.csv, self.message)

    def test_explicit_default_country_code(self):
        self.csv.write_text('phone\n2025550101\n', encoding='utf-8')
        recipients = app.load_inputs(self.csv, self.message, default_country_code='+1')
        self.assertEqual(recipients[0][0], '+12025550101')
        result, output, run, *_ = self.invoke(['--default-country-code', '+1'])
        self.assertEqual(result, 0)
        self.assertIn('To: +12025550101', output)
        run.assert_not_called()

    def test_preview_exact_and_no_automation(self):
        result, output, run, confirmation, _, dictionary = self.invoke()
        self.assertEqual(result, 0)
        self.assertIn('Hi Alex (Alex Chen)!\n👋\n', output)
        run.assert_not_called()
        dictionary.assert_not_called()
        confirmation.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_attachment_preview_hash_and_send_arguments(self):
        options = ['--attachment', str(self.attachment), '--limit', '1']
        result, output, run, *_ = self.invoke(options)
        self.assertEqual(result, 0)
        self.assertIn('Attachment: flyer.jpeg (15 bytes)', output)
        run.assert_not_called()
        plain = app.load_inputs(self.csv, self.message)[0][2]
        attached = app.load_inputs(self.csv, self.message, attachment=self.attachment)[0][2]
        self.assertNotEqual(plain, attached)
        self.attachment.write_bytes(b'changed')
        changed = app.load_inputs(self.csv, self.message, attachment=self.attachment)[0][2]
        self.assertNotEqual(attached, changed)
        result, _, run, *_ = self.invoke(options + ['--send'])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_args_list[1].args[1][-1], str(self.attachment.resolve()))

    def test_sms_is_separate_operation(self):
        imessage_hash = app.load_inputs(self.csv, self.message)[0][2]
        sms_hash = app.load_inputs(self.csv, self.message, service='sms')[0][2]
        self.assertNotEqual(imessage_hash, sms_hash)
        result, output, run, *_ = self.invoke(['--service', 'sms', '--limit', '1', '--send'])
        self.assertEqual(result, 0)
        self.assertIn('Service: SMS', output)
        self.assertEqual(run.call_args_list[1].args[1][1], '+12025550101')

    def test_attachment_validation(self):
        for path in [self.root / 'missing.jpeg', self.root / 'bad.txt']:
            if path.suffix == '.txt':
                path.write_text('not an image')
            result, _, run, *_ = self.invoke(['--attachment', str(path), '--send'])
            self.assertEqual(result, 1)
            run.assert_not_called()

    def test_split_message_preview_and_send_order(self):
        self.message.write_text('https://example.com/post\n\nHi {first_name}!\nSecond line.\n', encoding='utf-8')
        options = ['--split-message', '--limit', '1']
        result, output, run, *_ = self.invoke(options)
        self.assertEqual(result, 0)
        self.assertIn('message part 1 begins', output)
        self.assertIn('message part 2 begins', output)
        run.assert_not_called()
        result, _, run, *_ = self.invoke(options + ['--send'])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_args_list[1].args[1],
                         ['account-id', '+12025550101', 'https://example.com/post'])
        self.assertEqual(run.call_args_list[2].args[1],
                         ['account-id', '+12025550101', 'Hi Alex!\nSecond line.'])

    def test_split_message_requires_blank_line(self):
        self.message.write_text('one part only', encoding='utf-8')
        result, _, run, *_ = self.invoke(['--split-message', '--send'])
        self.assertEqual(result, 1)
        run.assert_not_called()

    def test_attachment_only_never_sends_text(self):
        options = ['--attachment', str(self.attachment), '--attachment-only', '--limit', '1', '--send']
        result, output, run, *_ = self.invoke(options)
        self.assertEqual(result, 0)
        self.assertIn('attachment only', output)
        send_script, _ = app.dictionary_script('imessage', True, True)
        self.assertNotIn('send (item 3 of argv)', send_script)
        self.assertIn('send attachmentFile', send_script)
        combined = app.load_inputs(self.csv, self.message, attachment=self.attachment)[0][2]
        recovery = app.load_inputs(self.csv, self.message, attachment=self.attachment, attachment_only=True)[0][2]
        self.assertNotEqual(combined, recovery)

    def test_confirmation_and_tty(self):
        for answer, tty in [('send', True), (' SEND', True), ('SEND', False)]:
            result, _, run, *_ = self.invoke(['--send'], answer=answer, tty=tty)
            self.assertEqual(result, 0 if tty else 1)
            run.assert_not_called()
            self.assertEqual(self.rows(), [])

    def test_limit_log_and_argument_data(self):
        result, _, run, _, sleep, _ = self.invoke(['--send', '--limit', '1'])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 2)
        args = run.call_args_list[1].args
        self.assertEqual(args[0], 'SEND_SCRIPT')
        self.assertEqual(args[1], ['account-id', '+12025550101', 'Hi Alex (Alex Chen)!\n👋\n'])
        sleep.assert_not_called()
        rows = self.rows()
        self.assertEqual([r['status'] for r in rows], ['attempted', 'submitted'])
        self.assertEqual(set(rows[0]), set(app.FIELDS))
        self.assertNotIn('Hi Alex', self.log.read_text())

    def test_attempt_persisted_before_dispatch_and_delay(self):
        def dispatch(script, args, timeout):
            if script == 'CHECK_SCRIPT':
                return 'account-id'
            self.assertEqual(self.rows()[-1]['status'], 'attempted')
            self.assertEqual(self.rows()[-1]['phone'], args[1])
            return 'SUBMITTED'
        result, _, _, _, sleep, _ = self.invoke(['--send', '--delay', '5'], effects=dispatch)
        self.assertEqual(result, 0)
        sleep.assert_called_once_with(5)

    def test_rerun_and_changed_message(self):
        self.assertEqual(self.invoke(['--send'])[0], 0)
        result, _, run, confirmation, *_ = self.invoke(['--send'])
        self.assertEqual(result, 0)
        run.assert_not_called()
        confirmation.assert_not_called()
        self.message.write_text('Changed {name}')
        self.assertEqual(self.invoke(['--send'])[2].call_count, 3)

    def test_explicit_retry_override(self):
        self.assertEqual(self.invoke(['--send'])[0], 0)
        result, output, run, *_ = self.invoke(['--send', '--retry-attempted', '--limit', '1'])
        self.assertEqual(result, 0)
        self.assertIn('may be duplicated', output)
        self.assertEqual(run.call_count, 2)

    def test_error_timeout_interrupt_stop_and_skip(self):
        for failure, status, code in [(RuntimeError('failed'), 'error', 1),
                                     (subprocess.TimeoutExpired('osascript', 30), 'timeout', 1),
                                     (KeyboardInterrupt(), 'interrupted', 130)]:
            with self.subTest(status=status):
                self.log.write_text('')
                result, _, run, *_ = self.invoke(['--send'], effects=['account-id', failure])
                self.assertEqual(result, code)
                self.assertEqual(run.call_count, 2)
                self.assertEqual([r['status'] for r in self.rows()], ['attempted', status])
                result, _, run, *_ = self.invoke(['--send'])
                self.assertEqual(result, 0)
                self.assertEqual(run.call_args_list[1].args[1][1], '+12025550102')

    def test_bare_attempt_and_corrupt_log(self):
        recipient = app.load_inputs(self.csv, self.message)[0]
        with self.log.open('a+', newline='') as file:
            app.record(file, recipient, 'attempted')
        self.assertIn('1 previously attempted', self.invoke()[1])
        self.log.write_text('phone,timestamp,message_hash,status\n+12025550101,truncated')
        self.assertEqual(self.invoke(['--send'])[0], 1)
        self.invoke(['--send'])[2].assert_not_called()

    def test_interrupt_during_pause(self):
        with patch.object(app.time, 'sleep', side_effect=KeyboardInterrupt()):
            # invoke has its own sleep mock; set its side effect through a direct run.
            with patch.object(app, 'dictionary_script', return_value=('s', 'c')), \
                    patch.object(app, 'run_applescript', side_effect=['id', 'SUBMITTED']) as run, \
                    patch('builtins.input', return_value='SEND'), patch.object(app.sys.stdin, 'isatty', return_value=True), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(self.args + ['--send']), 130)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(len(self.rows()), 2)

    def test_check_never_sends(self):
        result, _, run, confirmation, *_ = self.invoke(['--check'], effects=['CHECK_OK'])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_args.args[1], [])
        confirmation.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_concurrent_send_refused(self):
        with self.log.open('a+') as file:
            app.fcntl.flock(file, app.fcntl.LOCK_EX | app.fcntl.LOCK_NB)
            result, _, run, confirmation, *_ = self.invoke(['--send'])
            self.assertEqual(result, 1)
            run.assert_not_called()
            confirmation.assert_not_called()

    def test_unsupported_dictionary_refused(self):
        with patch.object(app.sys, 'platform', 'darwin'), patch.object(app.ET, 'parse') as parse:
            parse.return_value.getroot.return_value = app.ET.fromstring('<dictionary/>')
            with self.assertRaisesRegex(ValueError, 'dictionary lacks'):
                app.dictionary_script()

    def test_invalid_cli_values(self):
        for options in [['--limit', '0'], ['--delay', 'nan'], ['--timeout', '0'], ['--check', '--send'], ['--attachment-only']]:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                app.main(self.args + options)
            self.assertEqual(error.exception.code, 2)

    def test_process_argv_and_kill_on_interrupt(self):
        with patch.object(app.subprocess, 'Popen') as popen:
            process = popen.return_value
            process.returncode = 0
            process.communicate.return_value = ('SUBMITTED\n', '')
            message = '" & do shell script "bad"\n👋'
            self.assertEqual(app.run_applescript('fixed source', ['+12025550101', message], 3), 'SUBMITTED')
            self.assertEqual(popen.call_args.args[0], ['/usr/bin/osascript', '-', '+12025550101', message])
            process.communicate.assert_called_with('fixed source', timeout=3)
            process.communicate.side_effect = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                app.run_applescript('fixed source', [], 3)
            process.kill.assert_called_once()
            process.wait.assert_called_once()

    @unittest.skipUnless(app.sys.platform == 'darwin', 'macOS dictionary inspection only')
    def test_installed_dictionary_read_only(self):
        script, check = app.dictionary_script()
        self.assertIn('send (item 3 of argv) to targetParticipant', script)
        self.assertNotIn('send ', check)
        sms_script, _ = app.dictionary_script('sms', True)
        self.assertIn('service type is SMS', sms_script)
        self.assertIn('send attachmentFile to targetParticipant', sms_script)
        self.assertLess(sms_script.index('set attachmentFile'), sms_script.index('tell application "Messages"'))
        self.assertIn('as alias', sms_script)


if __name__ == '__main__':
    unittest.main()
