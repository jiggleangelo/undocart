import unittest
from remedy.vault import Vault


class VaultTests(unittest.TestCase):
    def test_write_only_and_unique(self):
        vault = Vault()
        for name in ("get", "read", "__getitem__", "items"):
            self.assertFalse(hasattr(vault, name))
        first, second = vault.put("a"), vault.put("a")
        self.assertNotEqual(first, second)
        self.assertTrue(vault.has(first))
        self.assertEqual(2, vault.count())


if __name__ == "__main__": unittest.main()
