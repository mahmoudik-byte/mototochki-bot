# Diagnostics for Mototochki web-login. Reproduces what the bot does on
# /start auth_<code>: create auth user -> generate magic-link token -> write
# login_tokens. Prints the real server response so we can see the actual error.
# Secrets are read locally from secrets.txt and never sent anywhere.
import httpx

url = key = None
for line in open('/root/moto/secrets.txt', encoding='utf-8').read().splitlines():
    line = line.strip()
    if line.startswith('SUPABASE_URL='):
        url = line.split('=', 1)[1].strip().rstrip('/')
    elif line.startswith('SUPABASE_KEY='):
        key = line.split('=', 1)[1].strip()

AUTH = url + '/auth/v1'
REST = url + '/rest/v1'
H = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
uid = 999000001
email = 'tg%d@tg.mototochki.ru' % uid

print('SB_URL =', url)
print('key length =', len(key) if key else None, '(service_role expected)')

print('\n=== 1) admin/users (create test user) ===')
try:
    r = httpx.post(AUTH + '/admin/users', headers=H, timeout=20,
                   json={'email': email, 'email_confirm': True,
                         'user_metadata': {'telegram_id': uid, 'nick': 'diag'}})
    print('HTTP', r.status_code)
    print(r.text[:800])
except Exception as e:
    print('EXC', repr(e))

print('\n=== 2) admin/generate_link (magiclink) ===')
try:
    r = httpx.post(AUTH + '/admin/generate_link', headers=H, timeout=20,
                   json={'type': 'magiclink', 'email': email})
    print('HTTP', r.status_code)
    print(r.text[:1200])
except Exception as e:
    print('EXC', repr(e))

print('\n=== 3) login_tokens upsert ===')
try:
    HR = dict(H); HR['Prefer'] = 'resolution=merge-duplicates'
    r = httpx.post(REST + '/login_tokens', headers=HR, params={'on_conflict': 'code'},
                   timeout=20, json={'code': 'diagtest000', 'token_hash': 'x',
                                     'telegram_id': uid, 'nick': 'diag'})
    print('HTTP', r.status_code)
    print(r.text[:400])
except Exception as e:
    print('EXC', repr(e))

print('\nDONE')
