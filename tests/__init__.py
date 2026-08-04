"""Test package.

Disabling the OS credential vault here, before any test module is imported, is
deliberate. The vault is process-wide operating-system state: on a machine
where the user has configured Gmail in the app, a real stored password would
shadow every ``monkeypatch.setenv`` a test performs, and the suite would pass
or fail depending on whose machine it ran on. Tests must see only the fake
credentials they set for themselves.
"""

import os

os.environ.setdefault("CRM_DISABLE_KEYRING", "1")
