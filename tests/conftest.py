"""Shared pytest fixtures for the offline test suite.

Stubs external paid-SDK modules (fal_client, elevenlabs, openai) that this
suite never needs installed for real — every test that imports production
code transitively touching these SDKs relies on ``sys.modules`` already
carrying a stub by the time that import runs, so no real package is
required and no live call is ever made (CLAUDE.md §19.1).

Centralized here rather than duplicated as a copy-pasted ``sys.modules``
block at the top of every test file (the prior, per-file convention) because
that duplication had a real, repeated gap: several files stubbed
``"elevenlabs"``/``"elevenlabs.types"`` but not ``"elevenlabs.core"`` — which
``app/agents/agent3_audio/services/tts.py`` imports unconditionally
(``from elevenlabs.core import ApiError``) — so any test file whose import
chain reached that module raised ``ModuleNotFoundError`` the moment it was
actually collected under pytest. This was undetected for a while because the
suite had only ever been verified with ``ast.parse``, never actually run
(code_report/TODO Task 2, 2026-08-05).

pytest imports ``conftest.py`` before any test module in this directory, so
these ``sys.modules`` entries already exist by the time a test file's own
(now redundant, but harmless — ``setdefault`` is idempotent) stub lines run.
Existing per-file stub lines are intentionally left in place rather than
stripped out across dozens of files — this file is the backstop, not a
mandate to remove working code.
"""

import sys
from types import SimpleNamespace


class _VoiceSettingsStub:
    """Accepts and stores arbitrary kwargs.

    Production code always calls ``VoiceSettings(**kwargs)``
    (``app/agents/agent3_audio/services/tts.py``), never uses the class as a
    bare type/identity marker — a callable stub is a strict superset of the
    plain ``VoiceSettings=object`` stub some test files used to define
    locally (calling ``object(**kwargs)`` for non-empty kwargs raises
    ``TypeError``, which is exactly the gap this centralizes a fix for).
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _ElevenLabsApiErrorStub(Exception):
    """A distinct exception type — NOT an alias for the builtin ``Exception``.

    ``tts.is_permanent_tts_provider_error()`` does
    ``isinstance(exc, ElevenLabsApiError)`` then unconditionally reads
    ``exc.body``/``exc.status_code``. An earlier ad-hoc version of this stub
    used ``SimpleNamespace(ApiError=Exception)`` — aliasing the builtin
    ``Exception`` — which made that ``isinstance`` check true for literally
    every exception (a plain ``TimeoutError`` included), then crashed with
    ``AttributeError`` reading ``.body`` off a real ``TimeoutError``. Found by
    actually running the transport-retry test suite, not just import-checking
    it (code_report/TODO Task 2, 2026-08-05). Real ElevenLabs ``ApiError``
    instances carry both attributes; default them so a test can construct one
    bare (``_ElevenLabsApiErrorStub("msg")``) or with either populated.
    """

    def __init__(self, *args, body=None, status_code=None, **kwargs):
        super().__init__(*args)
        self.body = body
        self.status_code = status_code


sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=_VoiceSettingsStub))
sys.modules.setdefault("elevenlabs.core", SimpleNamespace(ApiError=_ElevenLabsApiErrorStub))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))
