import base64
import tempfile
import unittest
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import launcher


class LauncherTests(unittest.TestCase):
    def test_signed_manifest_rejects_tampering(self):
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        manifest = {
            'version': 'v1.2.3',
            'asset': 'CVA-EngClassAI-v1.2.3.zip',
            'sha256': 'a' * 64,
        }
        signature = base64.b64encode(
            private_key.sign(launcher.canonical_manifest(manifest))
        ).decode('ascii')
        public_key_b64 = base64.b64encode(public_key).decode('ascii')

        self.assertEqual(
            launcher.verify_manifest(manifest, signature, public_key_b64),
            'v1.2.3',
        )
        manifest['sha256'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'signature'):
            launcher.verify_manifest(manifest, signature, public_key_b64)

    def test_update_zip_rejects_parent_directory_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / 'update.zip'
            staging_path = root / 'staging'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr('../outside.txt', 'unsafe')

            with self.assertRaisesRegex(ValueError, 'unsafe path'):
                launcher.safe_extract(archive_path, staging_path)
            self.assertFalse((root / 'outside.txt').exists())

    def test_update_zip_requires_application_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / 'update.zip'
            staging_path = root / 'staging'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr('EngClassAI/readme.txt', 'missing executable')

            with self.assertRaisesRegex(ValueError, 'does not contain'):
                launcher.safe_extract(archive_path, staging_path)


if __name__ == '__main__':
    unittest.main()
