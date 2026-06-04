import logging
import uuid

logger = logging.getLogger(__name__)

# TODO: Replace with real JWT decode once authentication is implemented.
_DEV_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def get_current_user_id() -> uuid.UUID:
    # TODO: decode Bearer JWT, validate, and return user_id claim
    return _DEV_USER_ID
