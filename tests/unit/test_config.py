from __future__ import annotations

import pytest

from returns_desk.config import Settings, require_real_secrets


def test_local_environment_may_use_the_development_secrets():
    require_real_secrets(Settings(_env_file=None))


@pytest.mark.parametrize("missing", ["jwt_secret", "internal_token", "payment_webhook_secret"])
def test_a_deployed_environment_refuses_the_development_secrets(missing: str):
    real = {
        "jwt_secret": "a" * 48,
        "internal_token": "b" * 48,
        "payment_webhook_secret": "c" * 48,
    }
    del real[missing]
    settings = Settings(_env_file=None, environment="production", **real)
    with pytest.raises(RuntimeError, match=missing.upper()):
        require_real_secrets(settings)


def test_a_deployed_environment_starts_with_real_secrets():
    settings = Settings(
        _env_file=None,
        environment="production",
        jwt_secret="a" * 48,
        internal_token="b" * 48,
        payment_webhook_secret="c" * 48,
    )
    require_real_secrets(settings)
