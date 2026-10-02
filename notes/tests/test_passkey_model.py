from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase

User = get_user_model()


class PasskeySettingsTests(SimpleTestCase):
    def test_rp_id_defaults_to_notes_tomd_org(self):
        self.assertEqual(settings.WEBAUTHN_RP_ID, "notes.tomd.org")

    def test_rp_name_set(self):
        self.assertTrue(settings.WEBAUTHN_RP_NAME)

    def test_origin_matches_rp_id(self):
        self.assertEqual(
            settings.WEBAUTHN_ORIGIN,
            f"https://{settings.WEBAUTHN_RP_ID}",
        )

    def test_rp_id_can_be_set_from_environment(self):
        values = _settings_in_subprocess(
            {"WEBAUTHN_RP_ID": "notes.example.org"},
            "WEBAUTHN_RP_ID", "WEBAUTHN_RP_NAME", "WEBAUTHN_ORIGIN",
        )
        self.assertEqual(
            values,
            ["notes.example.org", "notes.example.org", "https://notes.example.org"],
        )

    def test_railway_healthcheck_host_is_always_allowed(self):
        (hosts,) = _settings_in_subprocess(
            {"ALLOWED_HOSTS": "notes.example.org"}, "ALLOWED_HOSTS"
        )
        self.assertEqual(hosts, ["notes.example.org", "healthcheck.railway.app"])


def _settings_in_subprocess(env, *names):
    """Import a fresh copy of the settings module under `env`."""
    import json
    import os
    import subprocess
    import sys

    script = (
        "import json, noteserver.settings as s;"
        f"print(json.dumps([getattr(s, n) for n in {list(names)!r}]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "DEBUG": "1", **env},
        cwd=settings.BASE_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class PasskeyModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="tom", password="pw")

    def test_create_passkey(self):
        from notes.models import Passkey

        p = Passkey.objects.create(
            user=self.user,
            credential_id=b"cred-1",
            public_key=b"pub",
            sign_count=0,
            name="yubikey",
        )
        self.assertEqual(p.user, self.user)
        self.assertEqual(bytes(p.credential_id), b"cred-1")
        self.assertEqual(p.sign_count, 0)

    def test_credential_id_is_unique(self):
        from notes.models import Passkey

        Passkey.objects.create(
            user=self.user, credential_id=b"dup", public_key=b"k", sign_count=0
        )
        with self.assertRaises(IntegrityError):
            Passkey.objects.create(
                user=self.user, credential_id=b"dup", public_key=b"k", sign_count=0
            )

    def test_str(self):
        from notes.models import Passkey

        p = Passkey.objects.create(
            user=self.user,
            credential_id=b"x",
            public_key=b"k",
            sign_count=0,
            name="macbook",
        )
        self.assertIn("macbook", str(p))
