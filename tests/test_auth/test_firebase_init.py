"""``init_firebase`` -- the lazy singleton that never raises (02 §2, §3.1).

Covers acceptance criterion 14.  ``firebase_tokens`` is section 01's shared
fixture: unconfigured until a test says ``configure()``, and faked at the
``firebase_admin`` SDK boundary so ``init_firebase`` itself runs un-mocked.
"""

from __future__ import annotations

from app.core.firebase import init_firebase

# Does not parse at all -- the `[HARD-WON]` `.env`-quoting failure in §3.1,
# where double quotes turn the `\n` inside `private_key` into real newlines
# and the line stops being JSON.
UNPARSEABLE_SERVICE_ACCOUNT = '{"type": "service_account", "private_key": "broken'


# --- AC 14 -------------------------------------------------------------------


def test_init_firebase_is_a_lazy_singleton(firebase_tokens):
    """AC 14: called twice, ``init_firebase`` returns the identical object.

    The faked ``initialize_app`` builds a *new* app object on every call, so an
    implementation that re-initialises instead of caching fails here on
    identity rather than merely on equality -- and the recorded call list says
    the SDK was entered exactly once.
    """
    firebase_tokens.configure()

    first = init_firebase()
    second = init_firebase()

    assert first is not None
    assert first is second
    assert len(firebase_tokens.initialize_app_calls) == 1


def test_an_unconfigured_result_is_not_memoised(firebase_tokens):
    """§3.1: only a **successful** initialisation is memoised.

    An unset variable returns ``None`` on every call rather than poisoning the
    process, so a deployment that supplies the value late -- or a later test --
    is not permanently stuck with the negative result.
    """
    assert init_firebase() is None
    assert init_firebase() is None

    firebase_tokens.configure()

    assert init_firebase() is not None


def test_init_firebase_returns_none_for_an_unparseable_service_account(
    firebase_tokens,
):
    """§3.1: a value that will not parse returns ``None``, and never raises.

    An ``init_firebase`` that propagates the error takes the whole app down at
    import instead of degrading to guest-only play.
    """
    firebase_tokens.configure(UNPARSEABLE_SERVICE_ACCOUNT)

    assert init_firebase() is None
    assert init_firebase() is None
