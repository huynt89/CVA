import unittest

from tools.generate_password_hash import create_password_hash, verify_password_hash


class PasswordHashTests(unittest.TestCase):
    def test_generated_hash_matches_password(self):
        encoded = create_password_hash('Correct horse battery staple')
        self.assertTrue(verify_password_hash('Correct horse battery staple', encoded))

    def test_hash_rejects_different_password(self):
        encoded = create_password_hash('Correct horse battery staple')
        self.assertFalse(verify_password_hash('different password', encoded))

    def test_hash_rejects_plaintext_and_malformed_values(self):
        self.assertFalse(verify_password_hash('password', 'password'))
        self.assertFalse(verify_password_hash('password', 'scrypt$invalid$invalid'))


if __name__ == '__main__':
    unittest.main()
