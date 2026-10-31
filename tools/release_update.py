import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def generate_key():
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    print('CVA_UPDATE_PRIVATE_KEY_B64=' + base64.b64encode(private_bytes).decode('ascii'))
    print('UPDATE_PUBLIC_KEY_B64=' + base64.b64encode(public_bytes).decode('ascii'))


def configure(args):
    if (
        not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository)
        or not args.public_key
    ):
        raise ValueError('A distribution repository and an Ed25519 public key are required.')
    public_key = base64.b64decode(args.public_key, validate=True)
    if len(public_key) != 32:
        raise ValueError('The updater public key must be a 32-byte Ed25519 key.')
    config = (
        f'DISTRIBUTION_REPOSITORY = {args.repository!r}\n'
        f'UPDATE_PUBLIC_KEY_B64 = {args.public_key!r}\n'
    )
    Path('launcher_config.py').write_text(config, encoding='utf-8')


def sign(args):
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,60}', args.version):
        raise ValueError('The release version must contain only letters, numbers, dots, underscores, and hyphens.')
    private_key_value = os.getenv('CVA_UPDATE_PRIVATE_KEY_B64', '').strip()
    if not private_key_value:
        raise ValueError('CVA_UPDATE_PRIVATE_KEY_B64 is not set.')
    private_bytes = base64.b64decode(private_key_value, validate=True)
    if len(private_bytes) != 32:
        raise ValueError('The updater private key must be a 32-byte Ed25519 key.')
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)

    asset_path = Path(args.asset)
    manifest = {
        'version': args.version,
        'asset': asset_path.name,
        'sha256': hashlib.sha256(asset_path.read_bytes()).hexdigest(),
    }
    manifest_bytes = canonical_json(manifest)
    signature = base64.b64encode(private_key.sign(manifest_bytes)).decode('ascii')
    output_directory = Path(args.output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / 'update-manifest.json').write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + '\n',
        encoding='utf-8',
    )
    (output_directory / 'update-manifest.sig').write_text(signature + '\n', encoding='ascii')


def main():
    parser = argparse.ArgumentParser(description='Create and sign CVA application update metadata.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('generate-key', help='Generate an Ed25519 signing key pair.')
    configure_parser = commands.add_parser('configure', help='Configure the public release repository and verification key.')
    configure_parser.add_argument('--repository', required=True)
    configure_parser.add_argument('--public-key', required=True)
    sign_parser = commands.add_parser('sign', help='Create signed manifest for a release ZIP.')
    sign_parser.add_argument('--version', required=True)
    sign_parser.add_argument('--asset', required=True)
    sign_parser.add_argument('--output-directory', required=True)
    args = parser.parse_args()

    if args.command == 'generate-key':
        generate_key()
    elif args.command == 'configure':
        configure(args)
    else:
        sign(args)


if __name__ == '__main__':
    main()
