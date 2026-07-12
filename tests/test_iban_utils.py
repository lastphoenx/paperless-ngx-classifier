import unittest

from iban_utils import extract_ibans_from_text, is_valid_iban, normalize_iban, validate_iban


class TestIbanUtils(unittest.TestCase):
    def test_valid_ch_iban(self):
        iban = "CH5604835012345678009"
        self.assertTrue(is_valid_iban(iban))
        self.assertEqual(validate_iban("CH56 0483 5012 3456 7800 9"), iban)

    def test_valid_de_iban(self):
        iban = "DE89370400440532013000"
        self.assertTrue(is_valid_iban(iban))
        self.assertEqual(validate_iban("DE89 3704 0044 0532 0130 00"), iban)

    def test_invalid_checksum(self):
        self.assertFalse(is_valid_iban("DE12345678901234567890"))

    def test_wrong_length(self):
        self.assertFalse(is_valid_iban("CH930076201162385295"))

    def test_impfpass_ocr_garbage_rejected(self):
        text = """
        CHRISTODOERVACCINATION IMPFSTOFF TETANUS
        CHEODERKOMBINIERTEIMP CHRI FTOD ERVA CCIN ATION
        Blutgruppe Impfpass Basel-Landschaft
        """
        self.assertEqual(extract_ibans_from_text(text), [])

    def test_extract_from_mixed_text(self):
        text = """
        Bitte überweisen auf CH56 0483 5012 3456 7800 9 oder
        DE89 3704 0044 0532 0130 00 — DE12 3456 7890 ist falsch.
        """
        found = extract_ibans_from_text(text)
        self.assertEqual(len(found), 2)
        self.assertEqual(normalize_iban(found[0]), "CH5604835012345678009")
        self.assertEqual(normalize_iban(found[1]), "DE89370400440532013000")


if __name__ == "__main__":
    unittest.main()
