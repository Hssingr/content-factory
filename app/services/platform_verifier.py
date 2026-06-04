import logging

logger = logging.getLogger(__name__)

# TODO: Replace each stub with a real API call once platform credentials are confirmed.
#       Each verifier receives the decrypted credentials dict and returns True if valid.


def _verify_youtube(credentials: dict) -> bool:
    # TODO: Call YouTube Data API channels.list with credentials["access_token"]; check scope
    logger.debug("YouTube credential verification stubbed — returning True")
    return True


def _verify_tiktok(credentials: dict) -> bool:
    # TODO: Call TikTok /v2/user/info/ with credentials["access_token"]; check scope
    logger.debug("TikTok credential verification stubbed — returning True")
    return True


def _verify_instagram(credentials: dict) -> bool:
    # TODO: Call Meta Graph API /me with credentials["access_token"]; check instagram_basic scope
    logger.debug("Instagram credential verification stubbed — returning True")
    return True


def _verify_facebook(credentials: dict) -> bool:
    # TODO: Call Meta Graph API /me/accounts with credentials["access_token"]; check pages_manage_posts
    logger.debug("Facebook credential verification stubbed — returning True")
    return True


_VERIFIERS = {
    "youtube": _verify_youtube,
    "tiktok": _verify_tiktok,
    "instagram": _verify_instagram,
    "facebook": _verify_facebook,
}


def verify(platform: str, credentials: dict) -> bool:
    verifier = _VERIFIERS.get(platform.lower())
    if verifier is None:
        raise ValueError(f"Unknown platform: {platform!r} — expected one of {list(_VERIFIERS)}")
    return verifier(credentials)
