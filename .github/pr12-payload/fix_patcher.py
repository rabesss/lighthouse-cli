from pathlib import Path

# Remove a redundant self-replacement before applying the substantive patch.
path = Path('/tmp/pr12_followups.py')
text = path.read_text()
old = '''replace_once(
    "tests/test_ms_auth_characterization.py",
    ''' + "'''" + '''        """Both paths unusable: the HTTP path's transport error propagates
        after the sanitized Playwright-unavailable warning."""
''' + "'''" + ''',
    ''' + "'''" + '''        """Both paths unusable: the HTTP path's transport error propagates
        after the sanitized Playwright-unavailable warning."""
''' + "'''" + ''',
)
'''
count = text.count(old)
if count != 1:
    raise RuntimeError(f'expected one redundant no-op block, found {count}')
path.write_text(text.replace(old, '', 1))
