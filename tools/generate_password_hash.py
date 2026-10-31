import base64
import getpass
import hashlib
import secrets


def main():
    password = getpass.getpass('Password (input hidden): ')
    confirmation = getpass.getpass('Confirm password: ')
    if len(password) < 6:
        raise SystemExit('Use a password with at least 6 characters.')
    if password != confirmation:
        raise SystemExit('Passwords do not match.')

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
    encoded = '$'.join((
        'scrypt',
        base64.b64encode(salt).decode('ascii'),
        base64.b64encode(password_hash).decode('ascii'),
    ))
    print()
    print('Copy this hash into the matching account row in Student, under password:')
    print(encoded)


if __name__ == '__main__':
    main()
