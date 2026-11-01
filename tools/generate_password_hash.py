import argparse
import base64
import getpass
import hashlib
import hmac
import secrets


def create_password_hash(password):
    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(
        password.encode('utf-8'),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=64,
        maxmem=64 * 1024 * 1024,
    )
    return '$'.join((
        'scrypt',
        base64.b64encode(salt).decode('ascii'),
        base64.b64encode(password_hash).decode('ascii'),
    ))


def verify_password_hash(password, encoded):
    parts = encoded.strip().split('$')
    if len(parts) != 3 or parts[0] != 'scrypt':
        return False
    try:
        salt = base64.b64decode(parts[1], validate=True)
        expected = base64.b64decode(parts[2], validate=True)
    except (ValueError, base64.binascii.Error):
        return False
    if len(salt) != 16 or len(expected) != 64:
        return False
    actual = hashlib.scrypt(
        password.encode('utf-8'),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=len(expected),
        maxmem=64 * 1024 * 1024,
    )
    return hmac.compare_digest(actual, expected)


def generate():
    password = getpass.getpass('Password (input hidden): ')
    confirmation = getpass.getpass('Confirm password: ')
    if len(password) < 6:
        raise SystemExit('Use a password with at least 6 characters.')
    if password != confirmation:
        raise SystemExit('Passwords do not match.')

    encoded = create_password_hash(password)
    print()
    print('Copy this hash into the matching account row in Student, under password:')
    print(encoded)


def verify():
    encoded = getpass.getpass('Paste the Users sheet scrypt hash (input hidden): ')
    password = getpass.getpass('Enter the password you are trying to use (input hidden): ')
    if verify_password_hash(password, encoded):
        print('MATCH: this password matches the pasted hash.')
    else:
        print('NO MATCH: the password and pasted hash do not match or the hash is invalid.')


def main():
    parser = argparse.ArgumentParser(description='Create or verify a Users sheet scrypt password hash.')
    parser.add_argument(
        'action',
        choices=('generate', 'verify'),
        nargs='?',
        default='generate',
    )
    args = parser.parse_args()
    if args.action == 'verify':
        verify()
    else:
        generate()


if __name__ == '__main__':
    main()
