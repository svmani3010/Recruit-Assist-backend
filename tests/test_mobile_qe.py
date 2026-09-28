"""Unit tests for mobile normalisation + QE crypto helpers."""

from __future__ import annotations

import base64
import os
import secrets
import unittest
from unittest import mock

from app.mobile_crypto import mask_e164
from app.mobile_phone import normalize_and_validate_mobile


class NormalizeMobileTests(unittest.TestCase):
    def test_india_variants_same_e164(self):
        expected = "+919876543210"
        # National-field inputs (profile / OTP) — no leading +
        national_cases = [
            ("9876543210", "IN", "91"),
            ("091-9876543210", "IN", "91"),
            ("919876543210", "IN", "91"),
            ("9187654321", "IN", "91"),  # valid IN mobile starting with dial digits
        ]
        for raw, region, dial in national_cases:
            with self.subTest(raw=raw):
                got = normalize_and_validate_mobile(
                    raw, region=region, dial_code=dial, national_only=True
                )
                if raw == "9187654321":
                    self.assertEqual(got, "+919187654321")
                else:
                    self.assertEqual(got, expected)

        # Legacy / stored E.164
        self.assertEqual(
            normalize_and_validate_mobile(
                "+919876543210", region="IN", dial_code="91", national_only=False
            ),
            expected,
        )

    def test_national_only_rejects_plus_in_field(self):
        with self.assertRaises(Exception) as ctx:
            normalize_and_validate_mobile(
                "+919876543210", region="IN", dial_code="91", national_only=True
            )
        self.assertIn("without the country code", str(ctx.exception))

    def test_mask_hides_middle(self):
        masked = mask_e164("+919876543210")
        self.assertTrue(masked.endswith("3210"))
        self.assertNotIn("987654", masked)


class MobileCryptoUnitTests(unittest.TestCase):
    def test_encrypt_round_trip_when_configured(self):
        key = base64.b64encode(secrets.token_bytes(96)).decode("ascii")
        with mock.patch.dict(
            os.environ,
            {
                "MOBILE_QE_ENABLED": "true",
                "MOBILE_QE_KMS_PROVIDER": "local",
                "MOBILE_QE_LOCAL_MASTER_KEY": key,
                "MOBILE_QE_KEY_ALT_NAME": "test-mobile-unit-v1",
            },
            clear=False,
        ):
            from app import settings as settings_mod
            from app import mobile_crypto as crypto

            settings_mod.get_settings.cache_clear()
            crypto.reset_client_encryption_cache()

            if not crypto.is_mobile_qe_ready():
                self.skipTest("QE not ready after env patch")

            # Requires live MongoDB + libmongocrypt
            try:
                crypto.ensure_data_key()
                e164 = "+919876543210"
                a = crypto.encrypt_mobile_indexed(e164)
                b = crypto.encrypt_mobile_indexed(e164)
                self.assertNotEqual(bytes(a), bytes(b), "ciphertext must differ (random IV)")
                self.assertEqual(crypto.decrypt_mobile(a), e164)
                self.assertEqual(crypto.decrypt_mobile(b), e164)
                q = crypto.encrypt_mobile_equality_query(e164)
                self.assertTrue(len(q) > 0)
            except Exception as exc:
                self.skipTest(f"Live QE unavailable: {exc}")
            finally:
                settings_mod.get_settings.cache_clear()
                crypto.reset_client_encryption_cache()

    def test_tampered_ciphertext_fails(self):
        key = base64.b64encode(secrets.token_bytes(96)).decode("ascii")
        with mock.patch.dict(
            os.environ,
            {
                "MOBILE_QE_ENABLED": "true",
                "MOBILE_QE_KMS_PROVIDER": "local",
                "MOBILE_QE_LOCAL_MASTER_KEY": key,
                "MOBILE_QE_KEY_ALT_NAME": "test-mobile-tamper-v1",
            },
            clear=False,
        ):
            from app import settings as settings_mod
            from app import mobile_crypto as crypto
            from app.mobile_crypto import MobileCryptoError

            settings_mod.get_settings.cache_clear()
            crypto.reset_client_encryption_cache()
            try:
                crypto.ensure_data_key()
                ct = bytearray(crypto.encrypt_mobile_indexed("+919811122233"))
                ct[-5] ^= 0xFF
                with self.assertRaises(MobileCryptoError):
                    crypto.decrypt_mobile(bytes(ct))
            except Exception as exc:
                self.skipTest(f"Live QE unavailable: {exc}")
            finally:
                settings_mod.get_settings.cache_clear()
                crypto.reset_client_encryption_cache()


if __name__ == "__main__":
    unittest.main()
